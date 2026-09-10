import argparse
from pathlib import Path

from graph_agents import build_graph

EXAMPLE_QUERIES = [
    "Implement pagination for a Python API endpoint.",
    "Debug a KeyError when loading a missing configuration value.",
    "Refactor a large request handler into smaller functions without changing behavior.",
    "Write unit tests for a function that validates email addresses.",
    "Review this function for bugs: def average(values): return sum(values) / len(values)",
]


def run(app, query: str, context: str = "") -> None:
    result = app.invoke({"query": query, "context": context})

    print(f"\nDevelopment request: {query}")
    print("Graph trace:")
    for step in result["steps"]:
        print(f"  - {step}")
    print(f"Answer: {result['answer']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a development request through an agent graph.")
    parser.add_argument("query", nargs="?", help="Development request; omit to run the examples.")
    parser.add_argument("--context-file", type=Path, help="UTF-8 file containing source code, logs, or notes.")
    args = parser.parse_args()
    if args.query is not None and not args.query.strip():
        parser.error("query must be a non-empty development request")
    if args.context_file and args.query is None:
        parser.error("--context-file requires a development request")
    context = ""
    if args.context_file:
        try:
            context = args.context_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            parser.error(f"cannot read context file: {error}")
    app = build_graph()
    for query in [args.query] if args.query is not None else EXAMPLE_QUERIES:
        run(app, query, context)


if __name__ == "__main__":
    main()
