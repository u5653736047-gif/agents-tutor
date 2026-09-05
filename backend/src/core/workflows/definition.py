"""固定工作流的声明式定义与注册表（lesson-workflow-design §三）。

设计边界：
- 工作流是**代码化静态定义**（步骤顺序、预算、失败策略都是我方代码
  写死的），模型只能经 start_workflow 以注册表 id 触发并填参数，不能
  自造步骤——这是「确定性调度」的根基，也是与 create_task_plan（模型
  自由拆解）的本质区别。
- 本包只依赖 core.state，不依赖 graph_builder / nodes（依赖方向：
  graph_builder → workflows → state），保证调度器实现不会反向耦合。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from core.state import (
    WORKFLOW_ITERATION_HARD_CAP,
    AgentRole,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepState,
)

StepFailurePolicy = Literal["abort", "continue", "retry"]

# 文件名中的非法/危险字符（Windows 保留集 + 路径分隔符），统一替换。
_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
# 产物文件名长度上限（含扩展名），超长截断防止文件系统拒绝。
_FILENAME_MAX_CHARS = 80


@dataclass(frozen=True, slots=True)
class WorkflowStepDefinition:
    """一个固定步骤的静态定义。

    instruction_template 面向 Worker（分派时作为 HumanMessage 注入），
    可用占位符：{topic}/{grade_hint}/{artifact_dir}/{artifact_path}
    （由 WorkflowDefinition.format_instruction 统一格式化）。
    iteration_budget 覆盖该步 ReAct 迭代上限（截断在
    WORKFLOW_ITERATION_HARD_CAP）；on_failure 语义与 TaskPlanStep
    一致：abort / continue / retry（重试预算固定 1 次）。
    artifact_filename_template 非 None 表示该步产出登记文件，
    文件名经 sanitize_artifact_filename 规整。
    """

    step_id: str
    # AgentRole 而非 WorkerAgentRole Literal：裸 dataclass 字段不做
    # pydantic 式字面量收敛，枚举成员与字符串在 StrEnum 语义下等价；
    # worker 限定在 __post_init__ 运行时校验（与 TaskPlanStep 的
    # pydantic Literal 同一约束，两套机制不混用）。
    worker_role: AgentRole
    instruction_template: str
    iteration_budget: int = 5
    on_failure: StepFailurePolicy = "abort"
    artifact_filename_template: str | None = None
    # 步骤输出结构门禁（ppt-workflow-design §五-1）：声明后，
    # _workflow_worker_updates 在暂存前以终端输出调用；返回 False 该步
    # 按 AGENT_OUTPUT_INVALID 判 FAILED（不进暂存、触发 on_failure）。
    # 自由正文步骤（如教案 draft）不声明即行为不变。
    output_validator: Callable[[str], bool] | None = None
    # 产物落盘闸（ppt-workflow-design §五-2）：声明 True 的步骤落终态前
    # 做**磁盘存在性**判定——artifact_root/期望文件名（模板按 params 格式
    # 化 + sanitize）存在且非空才允许 COMPLETED，否则 FAILED
    # （AGENT_OUTPUT_INVALID）触发 on_failure retry。防「模型谎报完成」
    # 的机械闸：不信任模型输出，也不只信任回执登记。
    requires_artifact: bool = False

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("workflow step_id must not be blank")
        if self.worker_role is AgentRole.SUPERVISOR:
            raise ValueError("workflow worker_role must be a worker agent")
        if not 1 <= self.iteration_budget <= WORKFLOW_ITERATION_HARD_CAP:
            raise ValueError(
                "workflow step iteration budget must be within "
                f"[1, {WORKFLOW_ITERATION_HARD_CAP}]: {self.iteration_budget}"
            )


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """一个可注册工作流的静态定义（步骤序列即执行顺序）。

    revise_policy / max_revise_rounds 支持有界的「校验不合格回退重做」：
    步骤 COMPLETED 后调度器调用 revise_policy(step_index, summary)，返回
    回退起始步骤索引则整段 [fallback, step_index] 重置 PENDING 重跑
    （workflow.attempts 计轮，超过 max_revise_rounds 后回退不再生效）。
    教案工作流用它实现「review 判 revise → 回退 draft」一次。
    """

    workflow_id: str
    title: str
    steps: tuple[WorkflowStepDefinition, ...]
    max_revise_rounds: int = 0
    revise_policy: (
        Callable[[int, str | None], int | None] | None
    ) = None
    # 模型可经 start_workflow 提供的额外参数键白名单
    # （ppt-workflow-design §五-3）：未声明的键在启动时结构化拒绝。
    # 模板占位符即可引用这些键（如 {page_count}）。
    extra_params: frozenset[str] = frozenset()
    # 参数规整钩子（确定性，代码侧）：start_workflow 在校验后、写状态前
    # 调用，返回规整后的参数（如 page_count 非数字→"12"、越界截断）。
    # 模型无法把非法值注入指令模板。
    param_normalizer: Callable[[dict[str, str]], dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if not self.workflow_id.strip():
            raise ValueError("workflow_id must not be blank")
        if not self.steps:
            raise ValueError("workflow must define at least one step")
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("workflow step_ids must be unique")

    def build_state(
        self,
        params: dict[str, str],
        *,
        artifact_root: str | None = None,
    ) -> WorkflowState:
        """按定义生成一份全新的运行进度（PENDING 全表）。

        参数管道：未声明键拒绝（required ∪ extra_params 之外）→
        param_normalizer 确定性规整 → 必填键校验。模型只能提供声明过的
        键，且写进状态前已过规整钩子。
        """
        declared = self.required_params() | set(self.extra_params)
        unknown = sorted(set(params.keys()) - declared)
        if unknown:
            raise ValueError(f"undeclared workflow params: {unknown}")
        normalized = (
            self.param_normalizer(dict(params)) if self.param_normalizer else dict(params)
        )
        if not self.params_valid(normalized):
            missing = sorted(set(self.required_params()) - normalized.keys())
            raise ValueError(f"missing workflow params: {missing}")
        return WorkflowState(
            workflow_id=self.workflow_id,
            status=WorkflowStatus.RUNNING,
            steps=[
                WorkflowStepState(
                    step_id=step.step_id,
                    worker_role=step.worker_role,
                )
                for step in self.steps
            ],
            artifact_root=artifact_root,
            params=normalized,
        )

    def required_params(self) -> set[str]:
        """指令模板与产物名模板联合要求的参数键（artifact_* 由调度器提供）。"""
        keys: set[str] = set()
        for step in self.steps:
            keys.update(_template_keys(step.instruction_template))
            if step.artifact_filename_template is not None:
                keys.update(_template_keys(step.artifact_filename_template))
        return {key for key in keys if key not in _SCHEDULER_PARAMS}

    def params_valid(self, params: dict[str, str]) -> bool:
        return self.required_params() <= params.keys()

    def artifact_relative_path(
        self,
        step: WorkflowStepDefinition,
        params: dict[str, str],
    ) -> str:
        """步骤产物在 artifact_root 内的相对 POSIX 路径。"""
        if step.artifact_filename_template is None:
            raise ValueError(f"step {step.step_id!r} has no artifact template")
        filename = sanitize_artifact_filename(
            step.artifact_filename_template.format(**params)
        )
        return filename

    def format_instruction(
        self,
        step: WorkflowStepDefinition,
        params: dict[str, str],
        *,
        artifact_dir: str | None,
    ) -> str:
        """把步骤指令模板格式化为分派 HumanMessage 正文。"""
        context = {
            **params,
            "artifact_dir": artifact_dir or "",
            "artifact_path": (
                f"{artifact_dir}/{self.artifact_relative_path(step, params)}"
                if artifact_dir is not None
                and step.artifact_filename_template is not None
                else ""
            ),
        }
        return step.instruction_template.format(**context)


_SCHEDULER_PARAMS = frozenset({"artifact_dir", "artifact_path"})
_TEMPLATE_KEY = re.compile(r"\{([a-z_]+)\}")


def _template_keys(template: str) -> set[str]:
    return set(_TEMPLATE_KEY.findall(template))


def sanitize_artifact_filename(filename: str) -> str:
    """把模型提供的主题等参数规整为安全文件名。

    替换路径分隔符与 Windows 保留字符、折叠空白、截断到
    _FILENAME_MAX_CHARS；不改变扩展名语义（调用方模板自带后缀）。
    """
    cleaned = _FILENAME_UNSAFE.sub("-", filename)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".。")
    if not cleaned:
        raise ValueError("artifact filename collapses to empty")
    if len(cleaned) > _FILENAME_MAX_CHARS:
        cleaned = cleaned[:_FILENAME_MAX_CHARS]
    return cleaned
