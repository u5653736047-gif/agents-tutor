"""四个角色的极简 Prompt，含 S2-T2 学生水平分层讲解策略。"""

from ..state import AgentRole, StudentLevel

_REACT_RULE = "按需调用工具，观察结果后继续；完成后直接回答。"

ROLE_PROMPTS: dict[AgentRole, str] = {
    AgentRole.SUPERVISOR: (
        f"{_REACT_RULE}\n你是协调者：先调用 detect_intent 识别用户意图"
        "——答疑 answer_question：直接回答或转 learning_assistant 深入辅导；"
        "备课/讲解 lesson_prep：转 teaching_assistant 生成教案/讲解材料；"
        "评价/批改 evaluation：转 evaluator；其他 other：直接回答；"
        "无法确定 unclear：只追问澄清，禁止 handoff 与 create_task_plan。"
        "复杂请求先且仅调用 create_task_plan 创建至少两个有序子任务，"
        "由系统依次分派；简单请求直接调用 handoff；收到系统命名的"
        "[TASK_RESULTS] 时只据其汇总，失败项必须明确说明缺失，不得补造。"
        "若学生自述基础水平（如“我基础差”“我学得比较深”），"
        "调用 detect_level 记录水平画像后再分派。"
    ),
    AgentRole.TEACHING_ASSISTANT: f"{_REACT_RULE}\n你是助教，负责知识讲解与备课支持。",
    # S2-T2 分层讲解：静态部分只约定「分层策略」，不绑定具体水平——
    # 具体水平由 learning_assistant_system_prompt() 在运行时按
    # state["level"] 追加（见下方 _LEVEL_GUIDANCE），这样 ROLE_PROMPTS
    # 保持稳定（get_node_info 与既有测试依赖它），水平指令随状态变化。
    AgentRole.LEARNING_ASSISTANT: (
        f"{_REACT_RULE}\n你是助学助手，负责答疑与学习规划。"
        "讲解须按学生水平分层：基础水平重直觉类比与例子、"
        "进阶水平重推导过程与边界条件；"
        "尚不清楚学生水平时默认中等深度，并主动说明可按需调整讲解深度。"
    ),
    AgentRole.EVALUATOR: f"{_REACT_RULE}\n你是评价助手，负责检查回答质量。",
}

# ─────────────────────────────────────────────
# S2-T2 学生水平分层讲解策略（动态部分）
# ─────────────────────────────────────────────
# 键是 StudentLevel 枚举值字符串，值是对应水平的讲解策略指令。
# 为什么拆成动态部分而不是全写进 ROLE_PROMPTS：
# - ROLE_PROMPTS 是四个角色的静态角色卡（测试断言 system_prompt ==
#   ROLE_PROMPTS[role]，get_node_info 也直接读它），保持静态不动；
# - 具体水平是跨轮变化的画像，必须在每个 ReAct 轮次按 state["level"]
#   现取现拼，因此 learning_assistant 通过 prompt_builder 钩子（见
#   react_agent.py / factory.py）在运行时调用 learning_assistant_system_prompt()。
#
# 指令措辞刻意使用彼此不同的「锚点词」以便测试断言：
# - basic 用「类比」，advanced 用「边界条件」，unknown 用「中等深度」，
#   测试只需检查这些独有词是否出现在发给模型的 system prompt 中，
#   即可确定「哪一档水平生效」，不依赖真实模型的输出玄学。
_LEVEL_GUIDANCE: dict[str, str] = {
    StudentLevel.BASIC.value: (
        "学生基础较弱：优先用生活化类比与直观例子建立直觉，"
        "逐步引入术语，少用公式推导，避免一次灌输过多概念。"
    ),
    StudentLevel.ADVANCED.value: (
        "学生基础扎实：直接给出严谨推导与边界条件，"
        "可以引用公式与符号，不必铺垫基础概念。"
    ),
    StudentLevel.UNKNOWN.value: (
        "尚不清楚学生水平：默认按中等深度讲解，先给出概述再展开，"
        "并主动说明讲解深度可按学生反馈调整。"
    ),
}


def learning_assistant_system_prompt(level: str | None) -> str:
    """按学生水平生成助学 Agent 的系统提示词（分层讲解的核心入口）。

    为什么这样设计：
    - 在基础角色提示词（ROLE_PROMPTS[LEARNING_ASSISTANT]）后追加一行
      「[当前学生水平:<value>]」机器可读标记与对应的策略指令，使发给
      模型的 system prompt 随状态中的水平画像变化——基础水平看到类比
      指令、进阶水平看到推导/边界条件指令、未知水平看到默认中等深度
      指令；
    - level 为 None（从未识别）或不在枚举内（历史脏数据）时归一为
      unknown，保证「首次提问无水平信息」走默认中等深度路径，不抛错；
    - 返回文本中的水平标记是稳定锚点：替身模型测试用它断言「哪一档
      水平生效」（验收标准：不同水平产出深度可区分，不依赖真实模型）。

    Args:
        level: AgentState["level"] 的值（StudentLevel 枚举值字符串或 None）。
    """
    # 归一化：None / 非法字符串 → unknown（默认中等深度），
    # 合法枚举值字符串 → 原样使用（basic / advanced）。
    effective = level if level in _LEVEL_GUIDANCE else StudentLevel.UNKNOWN.value
    return (
        f"{ROLE_PROMPTS[AgentRole.LEARNING_ASSISTANT]}\n"
        f"[当前学生水平:{effective}]\n"
        f"{_LEVEL_GUIDANCE[effective]}"
    )


__all__ = ["ROLE_PROMPTS", "learning_assistant_system_prompt"]
