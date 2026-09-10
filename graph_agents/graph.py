from langgraph.graph import END, StateGraph

from .agents import (
    classify_node,
    debugging_agent_node,
    implementation_agent_node,
    planner_node,
    refactoring_agent_node,
    review_agent_node,
    testing_agent_node,
)
from .state import GraphState


def route_after_classify(state: GraphState) -> str:
    return "review" if state["route"] == "review" else "plan"


def route_after_plan(state: GraphState) -> str:
    return state["route"]


def build_graph():
    """Compile the development workflow: classify, plan, specialize, review.

    Review-only requests go directly from classification to code review.
    """
    graph = StateGraph(GraphState)

    graph.add_node("classify", classify_node)
    graph.add_node("planner", planner_node)
    graph.add_node("implement_agent", implementation_agent_node)
    graph.add_node("debug_agent", debugging_agent_node)
    graph.add_node("refactor_agent", refactoring_agent_node)
    graph.add_node("test_agent", testing_agent_node)
    graph.add_node("review_agent", review_agent_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {"plan": "planner", "review": "review_agent"},
    )
    graph.add_conditional_edges(
        "planner",
        route_after_plan,
        {
            "implement": "implement_agent",
            "debug": "debug_agent",
            "refactor": "refactor_agent",
            "test": "test_agent",
        },
    )
    for node in ("implement_agent", "debug_agent", "refactor_agent", "test_agent"):
        graph.add_edge(node, "review_agent")
    graph.add_edge("review_agent", END)

    return graph.compile()
