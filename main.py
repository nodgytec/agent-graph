import argparse
from pathlib import Path

from graph_agents import build_graph
from graph_agents.workspace import Workspace

EXAMPLE_QUERIES = [
    "Implement pagination for a Python API endpoint.",
    "Debug a KeyError when loading a missing configuration value.",
    "Refactor a large request handler into smaller functions without changing behavior.",
    "Write unit tests for a function that validates email addresses.",
    "Review this function for bugs: def average(values): return sum(values) / len(values)",
]

SESSION_HELP = """Type a development request and press Enter.
  /workspace PATH  Select the codebase folder agents can inspect
  /workspace       Show the selected workspace
  /context PATH  Load a UTF-8 file of source code, logs, or notes
  /context       Show the selected context file
  /clear         Clear the selected context
  /help          Show these commands
  /exit          Exit (quit and exit also work)
Each request starts fresh and uses the selected workspace and context.
"""


def resolve_input_path(value: str | Path, workspace: Path | None = None) -> Path:
    filename = str(value).strip()
    # Preserve Windows backslashes and allow quoted paths with spaces.
    if len(filename) >= 2 and filename[0] == filename[-1] and filename[0] in "\"'":
        filename = filename[1:-1]
    if not filename.strip():
        raise ValueError("Provide a non-empty path.")
    path = Path(filename).expanduser()
    if workspace is not None and not path.is_absolute():
        path = workspace / path
    return path.resolve()


def run(app, query: str, context: str = "", workspace: Path | None = None) -> None:
    request = {"query": query, "context": context}
    if workspace is not None:
        request["workspace"] = str(workspace)
        print(f"\nWorkspace: {workspace}", flush=True)
    print(f"\nDevelopment request: {query}", flush=True)
    print("Working... Press Ctrl+C to cancel.", flush=True)
    print("Graph trace:", flush=True)
    result = {}
    seen_steps = 0
    for result in app.stream(request, stream_mode="values"):
        steps = result.get("steps", [])
        for step in steps[seen_steps:]:
            print(f"  - {step}", flush=True)
        seen_steps = len(steps)
    if not result.get("answer"):
        raise RuntimeError("The workflow finished without an answer.")
    print(f"Answer: {result['answer']}")


def interactive(app, context: str = "", context_file: Path | None = None, workspace: Path | None = None) -> None:
    print("\nDevelopment Agent CLI")
    print("Describe a task to get a code proposal or review.")
    print(SESSION_HELP)
    print(f"Workspace: {workspace if workspace is not None else '(none)'}")
    print(f"Context: {context_file if context_file is not None else '(none)'}")
    while True:
        try:
            query = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if not query:
            continue
        command, *arguments = query.split(maxsplit=1)
        command = command.lower()
        if query.lower() in ("/exit", "/quit", "exit", "quit"):
            print("Goodbye.")
            return
        if command == "/help":
            print(SESSION_HELP)
            continue
        if command == "/clear":
            context, context_file = "", None
            print("Context cleared.")
            continue
        if command == "/workspace":
            if not arguments:
                print(f"Workspace: {workspace if workspace is not None else '(none)'}")
                continue
            try:
                selected_workspace = Workspace(resolve_input_path(arguments[0], workspace)).root
            except (OSError, ValueError, RuntimeError) as error:
                print(f"Cannot select workspace: {error}")
                continue
            if selected_workspace != workspace:
                context, context_file = "", None
                print("Context cleared.")
            workspace = selected_workspace
            print(f"Workspace selected: {workspace}")
            continue
        if command == "/context":
            if not arguments:
                print(f"Context: {context_file if context_file is not None else '(none)'}")
                continue
            try:
                selected_file = resolve_input_path(arguments[0], workspace)
                selected_context = selected_file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError, ValueError, RuntimeError) as error:
                print(f"Cannot load context file: {error}")
                continue
            context, context_file = selected_context, selected_file
            print(f"Context loaded: {context_file} ({len(context):,} characters)")
            continue
        if query.startswith("/"):
            print("Unknown command. Type /help to see available commands.")
            continue
        try:
            run(app, query, context, workspace)
        except KeyboardInterrupt:
            print("\nRequest cancelled. You can enter another task.")
        except Exception as error:
            # An API or workflow failure should not end the interactive session.
            print(f"Request failed: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Work with development agents from an interactive prompt.")
    parser.add_argument("query", nargs="?", help="Run one development request; omit to open the interactive prompt.")
    parser.add_argument("--context-file", type=Path, help="UTF-8 file containing source code, logs, or notes.")
    parser.add_argument("--workspace", help="Codebase folder agents can inspect; relative context paths use this root.")
    parser.add_argument("--examples", action="store_true", help="Run the five built-in examples and exit.")
    args = parser.parse_args()
    if args.query is not None and not args.query.strip():
        parser.error("query must be a non-empty development request")
    if args.examples and (args.query is not None or args.context_file is not None or args.workspace is not None):
        parser.error("--examples cannot be combined with a request, --context-file, or --workspace")
    workspace = None
    if args.workspace is not None:
        try:
            workspace = Workspace(resolve_input_path(args.workspace)).root
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(f"cannot select workspace: {error}")
    context = ""
    if args.context_file:
        try:
            args.context_file = resolve_input_path(args.context_file, workspace)
            context = args.context_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, ValueError, RuntimeError) as error:
            parser.error(f"cannot read context file: {error}")
    app = build_graph()
    if args.query is not None:
        run(app, args.query, context, workspace)
    elif args.examples:
        for query in EXAMPLE_QUERIES:
            run(app, query)
    else:
        interactive(app, context, args.context_file, workspace)


if __name__ == "__main__":
    main()
