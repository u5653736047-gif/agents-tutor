"""四个角色的极简 Prompt，含 S2-T2 学生水平分层讲解策略与 S2-T3 评价规则。"""

from ..state import AgentRole, StudentLevel

# 所有角色共用的 ReAct 总则：先观察工具结果，再完成回答。
_REACT_RULE = "按需调用工具，观察结果后继续；完成后直接回答。"

ROLE_PROMPTS: dict[AgentRole, str] = {
    # 协调者：意图识别 + 任务分派，复杂请求拆子任务、简单请求直接交接。
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
    # S4-T2 检索约定：search_knowledge 已注入备课角色（授权见
    # api/app.py 模块注释）。备课/教案/例题是教材内容的再加工，
    # 必须先调用工具检索教材、基于检索结果生成——工具在列表里不等于
    # 模型知道何时该用，若不写明约定，模型会跳过检索直接凭空编写。
    AgentRole.TEACHING_ASSISTANT: (
        f"{_REACT_RULE}\n你是助教，负责知识讲解与备课支持。"
        "备课/教案/例题生成必须先调用 search_knowledge 检索教材，"
        "基于检索结果生成，禁止脱离教材凭空编写。"
    ),
    # S2-T2 分层讲解：静态部分只约定「分层策略」，不绑定具体水平——
    # 具体水平由 learning_assistant_system_prompt() 在运行时按
    # state["level"] 追加（见下方 _LEVEL_GUIDANCE），这样 ROLE_PROMPTS
    # 保持稳定（get_node_info 与既有测试依赖它），水平指令随状态变化。
    # S4-T2 检索约定：search_knowledge 已注入答疑角色（授权见
    # api/app.py 模块注释）。答疑涉及教材/知识内容时，模型必须先调用
    # 工具检索知识库再作答——工具在列表里不等于模型知道何时该用，
    # 若不写明约定，模型会跳过检索直接编造教材内容（线上冒烟实测
    # 出现过「已完成知识库检索」的幻觉回答）；检索无命中（found=False）
    # 时如实说明「知识库未覆盖」，与 S4-T3 检索阈值语义一致——低于
    # 阈值即视为未覆盖，不强行作答。引用编号由系统按命中顺序自动
    # 生成并挂到回答消息元数据（见 graph_builder._citations_from_
    # tool_results），模型只需基于检索片段作答，无需自行编写编号。
    AgentRole.LEARNING_ASSISTANT: (
        f"{_REACT_RULE}\n你是助学助手，负责答疑与学习规划。"
        "讲解须按学生水平分层：基础水平重直觉类比与例子、"
        "进阶水平重推导过程与边界条件；"
        "尚不清楚学生水平时默认中等深度，并主动说明可按需调整讲解深度。"
        "面向教材或知识性提问，必须先调用 search_knowledge 检索知识库"
        "再作答，禁止凭空编造教材内容；"
        "检索无命中时如实说明「知识库未覆盖」而非强行作答；"
        "回答基于检索到的知识片段组织，引用编号由系统自动生成，无需自行编写。"
    ),
    # 评价者：必须基于检索证据做结构化评价，禁止凭空打分。
    AgentRole.EVALUATOR: (
        f"{_REACT_RULE}\n你是评价助手。必须基于本轮最终回答与检索证据"
        "（工具观察结果）评价，禁止凭空评价；先调用 submit_evaluation "
        "提交结构化结论：verdict 总结论、fact_accuracy 事实准确性、"
        "citation_completeness 引用完整性（无检索证据引用时不得判通过）、"
        "reason 理由；再给出简要评价文本。"
    ),
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
        f"[当前学生水平:{effective}]\n"  # 机器可读水平标记：测试断言用的稳定锚点
        f"{_LEVEL_GUIDANCE[effective]}"
    )


__all__ = ["ROLE_PROMPTS", "learning_assistant_system_prompt"]
