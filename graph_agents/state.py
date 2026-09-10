from typing import List, Literal, Optional, TypedDict


class GraphState(TypedDict):
    """Shared state threaded through every node in the graph.

    Each node reads the state, does its work, and returns a partial update
    that LangGraph merges back in before handing state to the next node.
    """

    query: str
    route: Optional[Literal["math", "research", "general"]]
    steps: List[str]
    answer: Optional[str]
