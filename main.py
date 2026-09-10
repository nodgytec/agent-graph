import argparse
import sys
from pathlib import Path

from graph_agents import build_graph
from graph_agents.workspace import Workspace
from graph_agents.terminal import AgentProgress, TerminalUI
from graph_agents.budget import TaskStopped, TokenBudgetController

EXAMPLE_QUERIES = [
    "Implement pagination for a Python API endpoint.",
    "Debug a KeyError when loading a missing configuration value.",
    "Refactor a large request handler into smaller functions without changing behavior.",
    "Write unit tests for a function that validates email addresses.",
    "Review this function for bugs: def average(values): return sum(values) / len(values)",
]

SESSION_HELP = """Type a development request and press Enter.
  /workspace PATH  Select the codebase folder specialists can edit
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
    result = {}
    try:
        with TokenBudgetController(enabled=sys.stdin.isatty()) as budget:
            with AgentProgress(query, workspace) as display:
                stream = app.stream(request, stream_mode=["custom", "values"])
                try:
                    for mode, event in stream:
                        if mode == "custom":
                            if event.get("kind") == "token_budget_request":
                                budget.respond(event["request_id"], display.ask_token_limit(event))
                            else:
                                display.handle(event)
                        else:
                            result = event
                            display.record_state(result)
                    if not result.get("answer"):
                        raise RuntimeError("The workflow finished without an answer.")
                finally:
                    # Release a waiting worker before closing LangGraph's iterator.
                    # This also handles Ctrl+C and EOF at the approval prompt.
                    budget.cancel()
                    close = getattr(stream, "close", None)
                    if close is not None:
                        close()
    except (TaskStopped, EOFError) as error:
        display.ui.message(str(error) or "Task stopped at end of input. Saved files remain on disk.", "yellow")
        return
    display.ui.answer(result)


def interactive(app, context: str = "", context_file: Path | None = None, workspace: Path | None = None) -> None:
    ui = TerminalUI()
    ui.welcome(workspace, context_file, SESSION_HELP)
    while True:
        try:
            query = ui.prompt().strip()
        except (EOFError, KeyboardInterrupt):
            ui.message("\nGoodbye.", "cyan")
            return
        if not query:
            continue
        command, *arguments = query.split(maxsplit=1)
        command = command.lower()
        if query.lower() in ("/exit", "/quit", "exit", "quit"):
            ui.message("Goodbye.", "cyan")
            return
        if command == "/help":
            ui.help(SESSION_HELP)
            continue
        if command == "/clear":
            context, context_file = "", None
            ui.message("Context cleared.", "green")
            continue
        if command == "/workspace":
            if not arguments:
                ui.message(f"Workspace: {workspace if workspace is not None else '(none)'}", "cyan")
                continue
            try:
                selected_workspace = Workspace(resolve_input_path(arguments[0], workspace)).root
            except (OSError, ValueError, RuntimeError) as error:
                ui.message(f"Cannot select workspace: {error}", "red")
                continue
            if selected_workspace != workspace:
                context, context_file = "", None
                ui.message("Context cleared.", "dim")
            workspace = selected_workspace
            ui.message(f"Workspace selected: {workspace}", "green")
            continue
        if command == "/context":
            if not arguments:
                ui.message(f"Context: {context_file if context_file is not None else '(none)'}", "cyan")
                continue
            try:
                selected_file = resolve_input_path(arguments[0], workspace)
                selected_context = selected_file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError, ValueError, RuntimeError) as error:
                ui.message(f"Cannot load context file: {error}", "red")
                continue
            context, context_file = selected_context, selected_file
            ui.message(f"Context loaded: {context_file} ({len(context):,} characters)", "green")
            continue
        if query.startswith("/"):
            ui.message("Unknown command. Type /help to see available commands.", "yellow")
            continue
        try:
            run(app, query, context, workspace)
        except KeyboardInterrupt:
            ui.message("\nRequest cancelled. You can enter another task.", "yellow")
        except Exception as error:
            # An API or workflow failure should not end the interactive session.
            ui.message(f"Request failed: {error}", "red")


def main() -> None:
    parser = argparse.ArgumentParser(description="Work with development agents from an interactive prompt.")
    parser.add_argument("query", nargs="?", help="Run one development request; omit to open the interactive prompt.")
    parser.add_argument("--context-file", type=Path, help="UTF-8 file containing source code, logs, or notes.")
    parser.add_argument("--workspace", help="Codebase folder specialists can edit; relative context paths use this root.")
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
