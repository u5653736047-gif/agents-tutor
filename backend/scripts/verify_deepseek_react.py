"""使用 .env 中的 DeepSeek 配置验证一次真实 ReAct 工具循环。"""

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from core.models import DeepSeekSettings, create_deepseek_model
from core.nodes import create_agent_nodes
from core.state import AgentRole, create_initial_state


@tool
def double(value: int) -> int:
    """返回输入整数的两倍。"""
    return value * 2


def main() -> None:
    settings = DeepSeekSettings.from_env()
    model = create_deepseek_model(settings)
    agent = create_agent_nodes(model=model, tools=[double])[AgentRole.TEACHING_ASSISTANT]

    state = create_initial_state(session_id="deepseek-react-check")
    state["messages"] = [
        HumanMessage(content="必须调用 double 工具计算 21 的两倍；观察结果后只回答数字。")
    ]
    result = agent.run(state)

    if result.error:
        raise RuntimeError(result.error)
    successful_tools = [item for item in result.updates["tool_results"] if item.success]
    final_messages = [
        message
        for message in result.messages
        if isinstance(message, AIMessage) and not message.tool_calls and message.content
    ]
    if not any(item.tool_name == "double" for item in successful_tools):
        raise RuntimeError("DeepSeek 未调用 double 工具")
    if not final_messages:
        raise RuntimeError("DeepSeek 未生成最终回答")

    print(f"model={settings.model}")
    print(f"iterations={result.metadata['iterations']}")
    print(f"tools={[item.tool_name for item in successful_tools]}")
    print(f"answer={final_messages[-1].content}")


if __name__ == "__main__":
    main()
