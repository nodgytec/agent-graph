from typing import List, Literal, Optional, TypedDict


DevelopmentRoute = Literal["implement", "debug", "refactor", "test", "review"]


class _RequiredInput(TypedDict):
    query: str


class GraphState(_RequiredInput, total=False):
    """Shared state threaded through every node in the graph.

    Each node reads the state, does its work, and returns a partial update
    that LangGraph merges back in before handing state to the next node.
    """

    context: str  # Source code, logs, constraints, or repository notes.
    route: Optional[DevelopmentRoute]
    plan: str
    draft: str
    review: str
    steps: List[str]
    answer: Optional[str]
