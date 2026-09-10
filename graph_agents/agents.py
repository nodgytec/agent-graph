from .llm import get_llm
from .state import GraphState


def classify_node(state: GraphState) -> dict:
    """Router node: inspects the query and decides which specialist agent
    node should handle it next."""
    query = state["query"].lower()
    if any(w in query for w in ["calculate", "sum", "average", "how many", "+", "-", "*", "/"]):
        route = "math"
    elif any(w in query for w in ["who", "what is", "when did", "explain", "history of"]):
        route = "research"
    else:
        route = "general"

    return {
        "route": route,
        "steps": state["steps"] + [f"classify -> routed to '{route}' agent"],
    }


def math_agent_node(state: GraphState) -> dict:
    llm = get_llm(
        system_prompt="You are a precise math assistant. Show your work briefly, then give the final number.",
        canned_fallback="42 (stub math agent: set ANTHROPIC_API_KEY for a real answer)",
    )
    answer = llm.invoke(state["query"])
    return {"answer": answer, "steps": state["steps"] + ["math_agent -> produced answer"]}


def research_agent_node(state: GraphState) -> dict:
    llm = get_llm(
        system_prompt="You are a research assistant. Answer factually and note when you're unsure.",
        canned_fallback=(
            "(stub research agent: set ANTHROPIC_API_KEY for a real answer) "
            "A real research agent would call a search tool here."
        ),
    )
    answer = llm.invoke(state["query"])
    return {"answer": answer, "steps": state["steps"] + ["research_agent -> produced answer"]}


def general_agent_node(state: GraphState) -> dict:
    llm = get_llm(
        system_prompt="You are a friendly general-purpose assistant.",
        canned_fallback="(stub general agent: set ANTHROPIC_API_KEY for a real answer) Happy to help!",
    )
    answer = llm.invoke(state["query"])
    return {"answer": answer, "steps": state["steps"] + ["general_agent -> produced answer"]}
