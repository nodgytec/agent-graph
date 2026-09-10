from graph_agents import build_graph

EXAMPLE_QUERIES = [
    "What is the average of 4, 8, and 15?",
    "What is the history of the Eiffel Tower?",
    "Tell me a fun fact.",
]


def run(app, query: str) -> None:
    result = app.invoke({"query": query, "route": None, "steps": [], "answer": None})

    print(f"\nQuery: {query}")
    print("Graph trace:")
    for step in result["steps"]:
        print(f"  - {step}")
    print(f"Answer: {result['answer']}")


if __name__ == "__main__":
    app = build_graph()
    for q in EXAMPLE_QUERIES:
        run(app, q)
