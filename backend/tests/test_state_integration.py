"""验证 AgentState 与 LangGraph StateGraph 的集成."""

from langgraph.graph import END, StateGraph

from core.state import AgentState


def supervisor_node(state: AgentState) -> dict:
    """模拟 Supervisor 节点：设置当前 Agent 并结束."""
    return {"current_agent": "supervisor", "next_agent": None}


# 构建最小图
graph = StateGraph(AgentState)
graph.add_node("supervisor", supervisor_node)
graph.set_entry_point("supervisor")
graph.add_edge("supervisor", END)

app = graph.compile()

# 执行
result = app.invoke({"messages": [], "tool_results": []})
agent = result["current_agent"]
print(f"LangGraph StateGraph integration OK, current_agent={agent}")
assert agent == "supervisor"
print("All assertions passed!")
