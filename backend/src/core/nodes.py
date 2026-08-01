"""Agent 节点（Node）抽象（任务 1.1.2 / 1.1.3 / 1.1.4）.

每个 Agent 角色（协调/助教/助学/评价）作为 StateGraph 的一个节点，
节点内部遵循「思考 → 决策 → 执行 → 观察」循环，节点间通过 State 传递上下文，
由 Edge 条件路由决定下一节点。

- 1.1.3：Supervisor 注入 ``IntentRouter`` 替代硬编码路由，多子任务生成
  纯数据 fan-out 分派计划（写入 ``extra["fan_out"]``，由条件边重建 ``Send``
  并行分派），子 Agent 结果经 ``subtask_results`` 通道按 task_id 聚合 fan-in。
- 1.1.4：开启 ``require_confirmation`` 时 Supervisor 在分派前 ``interrupt``
  等待用户确认/修正/取消（HITL 确认闸门）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph import END
from langgraph.types import interrupt

from core.intent_router import INTENT_ALIASES, IntentRouter, RuleBasedIntentRouter
from core.state import (
    AgentRole,
    AgentState,
    SubtaskResult,
    TaskContext,
    TaskStatus,
)

# 节点内部动作哨兵（仅 decide → execute → observe 之间传递，不进入图路由）
_ACTION_WAIT = "__wait__"
_ACTION_CANCEL = "__cancel__"
# override 修正动作前缀：`override:<节点名>` 表示用户指定单一分派目标，
# 此时取消 fan-out 计划，整个任务（含全部子任务）单路径交给该目标。
_OVERRIDE_PREFIX = "override:"


class BaseAgentNode(ABC):
    """LangGraph 节点基类：将一次节点调用编排为 思考→决策→执行→观察 循环.

    Usage::

        graph.add_node(agent.name, agent)

    Args:
        role: 节点对应的 Agent 角色，``role.value`` 即图节点名称。
    """

    def __init__(self, role: AgentRole) -> None:
        self.role = role

    @property
    def name(self) -> str:
        """图节点名称，与 ``AgentRole`` 值保持一致."""
        return self.role.value

    def __call__(self, state: AgentState) -> dict[str, Any]:
        """LangGraph 节点入口，编排四阶段循环并返回 State 的部分更新."""
        plan = self.think(state)
        action = self.decide(state, plan)
        result = self.execute(state, action)
        return self.observe(state, action, result)

    @abstractmethod
    def think(self, state: AgentState) -> Any:
        """思考：基于当前 State 生成处理计划（后续接入 LLM 推理）. """

    @abstractmethod
    def decide(self, state: AgentState, plan: Any) -> str:
        """决策：选择下一步动作（回复 / 调用工具 / 转交其他 Agent）. """

    @abstractmethod
    def execute(self, state: AgentState, action: str) -> Any:
        """执行：执行所选动作，生成回复内容或触发工具调用. """

    @abstractmethod
    def observe(self, state: AgentState, action: str, result: Any) -> dict[str, Any]:
        """观察：将执行结果写回 State，返回图节点需要更新的字段. """


class SupervisorNode(BaseAgentNode):
    """协调 Agent 节点：注入 IntentRouter 按意图分派任务，任务完成后结束.

    职责：
    - 通过 ``router.classify(task)`` 输出目标意图，替代硬编码路由表；
    - ``task_context.subtasks`` 长度 > 1 时构造纯数据 fan-out 分派计划
      （写进 ``extra["fan_out"]`` 由条件边重建 Send 并行分派），每项载荷
      携带独立子任务上下文并透传 session_id/user_id；
    - 并行子任务结果经 ``subtask_results`` 通道聚合：再次被调用时按当前
      ``task_id`` 过滤计数，与 ``extra["total_subtasks"]`` 对比，齐了输出
      汇总消息并 END，否则返回最小更新静默等待其余分支；
    - ``require_confirmation=True`` 时在分派前 ``interrupt`` 暂停等待用户
      确认（HITL），支持 confirm / override / cancel 三种恢复值。

    Args:
        router: 意图分类路由，默认 ``RuleBasedIntentRouter()``。
        require_confirmation: 是否开启确认闸门（依赖 checkpointer 生效）。
    """

    def __init__(
        self,
        *,
        router: IntentRouter | None = None,
        require_confirmation: bool = False,
    ) -> None:
        super().__init__(AgentRole.SUPERVISOR)
        self._router = router or RuleBasedIntentRouter()
        self._require_confirmation = require_confirmation

    @property
    def _worker_names(self) -> frozenset[str]:
        """可作为分派目标（合法节点名）的角色集合，排除 Supervisor 自身."""
        return frozenset(
            role.value for role in AgentRole if role is not AgentRole.SUPERVISOR
        )

    def think(self, state: AgentState) -> str:
        task = state.get("task_context")
        if task and task.subtasks:
            return f"分析意图：{task.intent}，计划分解为 {len(task.subtasks)} 个子任务"
        return f"分析意图：{task.intent if task else 'unknown'}"

    def decide(self, state: AgentState, plan: str) -> str:
        """返回下一节点名；任务全部完成时返回 END."""
        task = state.get("task_context")
        extra = state.get("extra") or {}

        # 1. 聚合轮：fan-out 后子任务结果回交，未齐时静默等待其余并行分支。
        #    计数非法（如外部注入非数字字符串）时忽略聚合标记，按正常分派处理。
        #    计数按当前 task_id 过滤：subtask_results 在整个会话内只增不减，
        #    若按全局长度计数，历史轮次的残留结果会污染新一轮 fan-in 判断。
        raw_total = extra.get("total_subtasks")
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                total = -1
            if total >= 0:
                done = self._count_current_results(state, task)
                if done >= total:
                    return END
                return _ACTION_WAIT

        # 2. 已完成任务直接结束，不再次分派。
        #    「任务终结后又收到新用户消息」的多轮场景由入口 ingest 节点
        #    重建 TaskContext（见 graph.ingest_request），到此处时
        #    task 要么在进行中、要么确实没有新请求。
        if task and task.status == TaskStatus.COMPLETED:
            return END

        # 3. 正常分派：意图分类 + HITL 确认闸门
        target = self._classify_target(task)
        if self._require_confirmation:
            target = self._apply_confirmation(task, target)
        return target

    @staticmethod
    def _count_current_results(
        state: AgentState, task: TaskContext | None
    ) -> int:
        """统计当前任务的已回交子任务结果数（按 task_id 过滤）.

        历史轮次（task_id 不同）的结果不计入，避免跨轮污染 fan-in 判断；
        task 缺失时退化为全局长度（防御性兜底，正常流程不会走到）。
        """
        results = state.get("subtask_results") or []
        if task is None:
            return len(results)
        return sum(1 for r in results if r.task_id == task.task_id)

    def _classify_target(self, task: TaskContext | None) -> str:
        """经注入的路由器分类目标；非法返回值兜底交给助教 Agent.

        路由器输出统一经 ``INTENT_ALIASES`` 转换：短意图标签（如 ``learn``）
        映射为节点名，节点名原样保留；不在合法节点名内的结果兜底助教。
        """
        raw = self._router.classify(task)
        target = INTENT_ALIASES.get(raw, raw)
        if target not in self._worker_names:
            return AgentRole.TEACHING_ASSISTANT.value
        return target

    def _apply_confirmation(self, task: TaskContext | None, target: str) -> str:
        """HITL 确认闸门：分派前中断等待用户确认，返回修正后的分派目标.

        恢复值语义：
        - ``{"confirm": true}``：按原计划分派；
        - ``{"override": "<合法节点名>"}``：改用指定目标（单路径整体分派，
          取消 fan-out 计划）；目标不合法时回退原计划；
        - ``{"cancel": true}``：取消任务（返回取消哨兵，由 observe 收尾）。

        中断恢复后节点会从 ``__call__`` 整体重跑，``interrupt`` 在此处直接
        返回 resume 值而不会二次暂停。
        """
        plan: dict[str, Any] = {
            "intent": task.intent if task else "",
            "target": target,
            "subtasks": task.subtasks if task else [],
        }
        resume = interrupt({"ask": "是否按计划分派？", "plan": plan})
        if isinstance(resume, dict):
            if resume.get("cancel") is True:
                return _ACTION_CANCEL
            override = resume.get("override")
            if isinstance(override, str) and override in self._worker_names:
                return f"{_OVERRIDE_PREFIX}{override}"
        return target

    def execute(self, state: AgentState, action: str) -> str:
        if action == _ACTION_CANCEL:
            return "任务已被用户取消，本次协作结束。"
        if action == _ACTION_WAIT:
            return ""  # 静默等待：不产生任何消息
        if action.startswith(_OVERRIDE_PREFIX):
            return f"协调者将任务分派给 {action[len(_OVERRIDE_PREFIX):]}。"
        if action == END:
            # 只汇总当前任务（task_id 匹配）的结果：subtask_results 跨轮累积，
            # 不过滤会把历史轮次的子任务算进本轮汇总文本与计数。
            results = state.get("subtask_results") or []
            task = state.get("task_context")
            if task is not None:
                results = [r for r in results if r.task_id == task.task_id]
            if results:
                summary = "；".join(
                    f"{r.worker.value} 完成「{r.subtask}」" for r in results
                )
                return f"全部 {len(results)} 个子任务已完成，本次协作结束。{summary}"
            return "所有子任务已完成，本次协作结束。"
        return f"协调者将任务分派给 {action}。"

    def observe(self, state: AgentState, action: str, result: str) -> dict[str, Any]:
        update: dict[str, Any] = {"current_agent": self.name}
        if action == _ACTION_WAIT:
            # 静默等待：不写消息，仅清空 fan_out 计划并保留聚合计数
            # （total_subtasks 需保留到聚合完成，由 END 分支统一清理）
            extra = dict(state.get("extra") or {})
            extra.pop("fan_out", None)
            update.update({"next_agent": END, "extra": extra})
            return update
        if action == _ACTION_CANCEL:
            update["messages"] = [AIMessage(content=result, name=self.name)]
            update["next_agent"] = END
            task = state.get("task_context")
            if task is not None:
                update["task_context"] = task.model_copy(
                    update={"status": TaskStatus.CANCELLED}
                )
            return update
        if action == END:
            update["messages"] = [AIMessage(content=result, name=self.name)]
            update["next_agent"] = END
            # 聚合收尾：消费并清理 fan-out 分派计划与聚合计数。
            # 若不清理，条件边 route_by_next_agent 会读到残留的 fan_out，
            # 无限重放同一批 Send，导致图永不终止（内存/CPU 失控）。
            extra = dict(state.get("extra") or {})
            extra.pop("fan_out", None)
            extra.pop("total_subtasks", None)
            update["extra"] = extra
            return update

        # 正常分派：多子任务走并行 fan-out，否则保持单路径
        task = state.get("task_context")
        if task is not None and len(task.subtasks) > 1 and not action.startswith(
            _OVERRIDE_PREFIX
        ):
            # 状态中只存可序列化的纯数据分派计划（dict），Send 对象由
            # 条件边 route_by_next_agent 读出计划后重建——Send 是
            # LangGraph 运行时类型，直接写入状态会导致 checkpointer
            # 序列化（SQLite/PostgreSQL）失败。
            plan = self._build_fan_out_plan(state, task)
            extra = dict(state.get("extra") or {})
            extra["total_subtasks"] = len(plan)
            extra["fan_out"] = plan
            update["extra"] = extra
            update["next_agent"] = END  # fan-out 由条件边接管，本分支到此为止
        else:
            update["next_agent"] = action.removeprefix(_OVERRIDE_PREFIX)
        update["messages"] = [AIMessage(content=result, name=self.name)]
        return update

    def _build_fan_out_plan(
        self, state: AgentState, task: TaskContext
    ) -> list[dict[str, Any]]:
        """构造并行分派计划（纯数据，可 JSON 序列化）.

        每个子任务独立构造子任务版 TaskContext（description 为该子任务文本，
        意图优先取 ``metadata["subtask_intents"]`` 对应项，否则继承父意图），
        并独立经注入的路由器分类目标；载荷透传 session_id/user_id。
        返回项形如 ``{"node": <节点名>, "payload": {...}}``，其中
        ``payload["task_context"]`` 为 ``model_dump(mode="json")`` 后的字典，
        由条件边 ``model_validate`` 还原为 TaskContext 后构造 Send。
        """
        subtask_intents = task.metadata.get("subtask_intents")
        plan: list[dict[str, Any]] = []
        for index, subtask in enumerate(task.subtasks):
            sub_intent = task.intent
            if isinstance(subtask_intents, list) and index < len(subtask_intents):
                item = subtask_intents[index]
                if isinstance(item, str):
                    sub_intent = item
            subtask_ctx = task.model_copy(
                update={
                    "description": subtask,
                    "intent": sub_intent,
                    "status": TaskStatus.PENDING,
                    "metadata": {
                        **task.metadata,
                        "subtask_index": index,
                        "subtask": subtask,
                    },
                }
            )
            worker = self._classify_target(subtask_ctx)
            plan.append(
                {
                    "node": worker,
                    "payload": {
                        "task_context": subtask_ctx.model_dump(mode="json"),
                        "session_id": state.get("session_id"),
                        "user_id": state.get("user_id"),
                    },
                }
            )
        return plan


class _WorkerNode(BaseAgentNode):
    """子 Agent 节点基类：处理任务后交还 Supervisor 并标记完成."""

    def think(self, state: AgentState) -> str:
        task = state.get("task_context")
        return f"解析任务：{task.description if task else ''}"

    def decide(self, state: AgentState, plan: str) -> str:
        return AgentRole.SUPERVISOR.value

    def execute(self, state: AgentState, action: str) -> str:
        return f"{self.name} 的回复（占位）：任务已处理完毕。"

    def observe(self, state: AgentState, action: str, result: str) -> dict[str, Any]:
        task = state.get("task_context")
        completed_task = (
            task.model_copy(update={"status": TaskStatus.COMPLETED})
            if task is not None
            else TaskContext(status=TaskStatus.COMPLETED)
        )
        subtask = completed_task.metadata.get("subtask", completed_task.description)
        return {
            "messages": [AIMessage(content=result, name=self.name)],
            "current_agent": self.name,
            "next_agent": action,
            "task_context": completed_task,
            "subtask_results": [
                SubtaskResult(
                    task_id=completed_task.task_id,
                    subtask=subtask if isinstance(subtask, str) else "",
                    worker=self.role,
                    output=result,
                    success=True,
                )
            ],
        }


class TeachingAssistantNode(_WorkerNode):
    """助教 Agent 节点（角色行为在阶段二 2.1 实现）."""

    def __init__(self) -> None:
        super().__init__(AgentRole.TEACHING_ASSISTANT)


class LearningAssistantNode(_WorkerNode):
    """助学 Agent 节点（角色行为在阶段二 2.1 实现）."""

    def __init__(self) -> None:
        super().__init__(AgentRole.LEARNING_ASSISTANT)


class EvaluatorNode(_WorkerNode):
    """评价 Agent 节点（角色行为在阶段二 2.1 实现）."""

    def __init__(self) -> None:
        super().__init__(AgentRole.EVALUATOR)