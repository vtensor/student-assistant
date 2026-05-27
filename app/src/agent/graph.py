# LangGraph wiring: pii_in -> load_context -> agent <-> tools -> persist
# -> END. No checkpointer — chat history is owned by
# `Memory.session_recent` (Redis cache + Mongo fallback for cold chats).
# Output PII redaction was removed; `persist` extracts the final AI text
# directly from state["messages"] now.
from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from app.src.agent.nodes import (
    agent_node,
    has_tool_calls,
    load_context,
    persist,
    pii_in,
    tool_node,
)
from app.src.agent.state import AgentState


@lru_cache
def build_graph():
    g = StateGraph(AgentState)
    g.add_node("pii_in", pii_in)
    g.add_node("load_context", load_context)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node)
    g.add_node("persist", persist)

    g.add_edge(START, "pii_in")
    g.add_edge("pii_in", "load_context")
    g.add_edge("load_context", "agent")
    g.add_conditional_edges(
        "agent", has_tool_calls, {"tools": "tools", "end": "persist"}
    )
    g.add_edge("tools", "agent")
    g.add_edge("persist", END)
    return g.compile()
