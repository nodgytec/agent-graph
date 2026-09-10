from langgraph.graph import END, StateGraph

from .agents import classify_node, general_agent_node, math_agent_node, research_agent_node
from .state import GraphState


def route_after_classify(state: GraphState) -> str:
    return state["route"]


def build_graph():
    """Wires nodes (agents) and edges (control flow) into a compiled,
    runnable graph.

        classify --(math)------> math_agent -----> END
                 --(research)--> research_agent --> END
                 --(general)---> general_agent ----> END
    """
    graph = StateGraph(GraphState)

    graph.add_node("classify", classify_node)
    graph.add_node("math_agent", math_agent_node)
    graph.add_node("research_agent", research_agent_node)
    graph.add_node("general_agent", general_agent_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {
            "math": "math_agent",
            "research": "research_agent",
            "general": "general_agent",
        },
    )
    graph.add_edge("math_agent", END)
    graph.add_edge("research_agent", END)
    graph.add_edge("general_agent", END)

    return graph.compile()
