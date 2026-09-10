"""Rich terminal presentation, with plain logs when output is redirected."""

from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, ProgressColumn, TextColumn, TimeElapsedColumn
from rich.spinner import Spinner
from rich.table import Column
from rich.text import Text
from .budget import TaskStopped


SPECIALIST_LABELS = {
    "implement": "Implementation", "debug": "Debugging",
    "refactor": "Refactoring", "test": "Testing",
}
STATUS_STYLES = {
    "Waiting": "dim", "Running": "cyan", "Done": "green",
    "Skipped": "dim", "Failed": "bold red", "Cancelled": "yellow", "Not run": "dim",
    "Awaiting input": "bold yellow",
}


class StatusColumn(ProgressColumn):
    def render(self, task):
        status = task.fields["status"]
        return Text(status, style=STATUS_STYLES[status])


class AgentSpinnerColumn(ProgressColumn):
    def __init__(self):
        super().__init__()
        self.spinner = Spinner("dots", style="cyan")

    def render(self, task):
        if task.fields["status"] == "Running":
            return self.spinner.render(task.get_time())
        return Text("+" if task.fields["status"] == "Done" else " ", style="green")


class TokensColumn(ProgressColumn):
    def render(self, task):
        tokens = task.fields["tokens"]
        return Text(f"{tokens:,} tok" if tokens else "-", style="dim", justify="right")


class AgentBarColumn(BarColumn):
    def render(self, task):
        bar = super().render(task)
        bar.pulse = task.fields["status"] == "Running"
        if not bar.pulse:
            bar.total = 1
        return bar


class ActivityColumn(ProgressColumn):
    def render(self, task):
        return Text(task.fields["detail"], style="dim", overflow="ellipsis", no_wrap=True)


class TerminalUI:
    def __init__(self, console=None):
        self.console = console or Console(highlight=False)
        self.interactive = self.console.is_terminal and not self.console.is_dumb_terminal

    def message(self, message, style=""):
        self.console.print(Text(str(message), style=style), soft_wrap=not self.interactive)

    def welcome(self, workspace, context_file, help_text):
        if not self.interactive:
            self.message("\nDevelopment Agent CLI")
            self.message("Describe a task to edit the selected workspace or review code.")
            self.message(help_text)
        else:
            heading = Text("DEVELOPMENT AGENTS\n", style="bold cyan")
            heading.append("Plan, edit workspace files, and review changes.", style="white")
            self.console.print(Panel(heading, border_style="cyan", padding=(1, 2)))
            self.help(help_text)
        self.message(f"Workspace: {workspace if workspace is not None else '(none)'}", "dim")
        self.message(f"Context: {context_file if context_file is not None else '(none)'}", "dim")

    def help(self, help_text):
        if self.interactive:
            self.console.print(Panel(Text(help_text.rstrip()), title="Commands", border_style="bright_black"))
        else:
            self.message(help_text)

    def prompt(self):
        if not self.interactive:
            return input("\nYou> ")
        self.console.print()
        return self.console.input(Text("You> ", style="bold cyan"))

    def answer(self, result):
        if not self.interactive:
            self.message(f"Answer: {result['answer']}")
            return
        if result.get("plan") and result.get("draft") and result.get("review"):
            sections = (("Plan", "plan", "cyan"), ("Development result", "draft", "blue"),
                        ("Staff review", "review", "green"))
        else:
            sections = (("Staff review" if result.get("route") == "review" else "Answer", "answer", "green"),)
        for title, key, color in sections:
            self.console.print(Panel(Markdown(result[key]), title=title, border_style=color, padding=(1, 2)))


class AgentProgress:
    def __init__(self, query, workspace=None, console=None):
        self.ui = TerminalUI(console)
        self.console = self.ui.console
        self.query, self.workspace = query, workspace
        columns = [
            AgentSpinnerColumn(),
            TextColumn("{task.description}", table_column=Column(no_wrap=True)),
            AgentBarColumn(bar_width=None, complete_style="green", finished_style="green", pulse_style="cyan"),
            StatusColumn(), TimeElapsedColumn(), TokensColumn(),
        ]
        if self.console.width >= 110:
            columns.append(ActivityColumn(table_column=Column(ratio=1, max_width=42)))
        self.progress = Progress(*columns, console=self.console, auto_refresh=False, expand=True)
        self.tasks = {}
        for name, label in (("classify", "01 Router"), ("planner", "02 Planner"),
                            ("specialist", "03 Specialist"), ("review_agent", "04 Staff review")):
            self.tasks[name] = self.progress.add_task(
                label, total=None, start=False, status="Waiting", detail="Queued",
                calls=0, files=0, tokens=0, characters=0, phase="", inspection_limit=None,
                inspections_remaining=None, inspection_exhausted=False,
            )
        self.route = None
        self.active = None
        self.activity = Text("Preparing the workflow...", style="cyan")
        self.history = deque(maxlen=3)
        self.live = None
        self.seen_steps = 0
        self.outcome = None
        self.changed_files = {}

    def task(self, name):
        task_id = self.tasks.get(name)
        return next((task for task in self.progress.tasks if task.id == task_id), None)

    def set_route(self, route):
        if not route or route == self.route:
            return
        self.route = route
        if route == "review":
            for name in ("planner", "specialist"):
                self.progress.update(self.tasks[name], status="Skipped", detail="Direct review route")
        elif route in SPECIALIST_LABELS:
            self.tasks[f"{route}_agent"] = self.tasks["specialist"]
            self.progress.update(self.tasks["specialist"], description=f"03 {SPECIALIST_LABELS[route]}")

    def log(self, label, message, style="cyan"):
        self.activity = Text(f"{label}  /  {message}", style=style)
        self.history.append(Text(f"{label}: {message}", style="dim" if style == "cyan" else style))
        if not self.ui.interactive:
            self.ui.message(f"  {label}: {message}")

    def handle(self, event):
        if not isinstance(event, dict):
            return
        name, kind = event.get("agent"), event.get("kind")
        if kind == "agent_complete" and name == "classify":
            self.set_route(event.get("route"))
        task = self.task(name)
        if task is None:
            return
        label = task.description.split(" ", 1)[-1]
        if kind == "agent_start":
            self.active = name
            self.progress.start_task(task.id)
            self.progress.update(task.id, status="Running", detail="Preparing")
            self.log(label, "Choosing the route" if name == "classify" else "Preparing")
        elif kind == "inspection_budget":
            self.progress.update(task.id, inspection_limit=event["limit"],
                                 inspections_remaining=event["remaining"])
            if not self.ui.interactive:
                self.ui.message(f"  {label}: {event['remaining']}/{event['limit']} inspections left")
        elif kind == "inspection_exhausted":
            self.progress.update(task.id, inspection_exhausted=True, detail="Finishing with available evidence")
            self.log(label, "Inspection limit reached; finishing with available evidence", "yellow")
        elif kind == "model_start":
            calls = task.fields["calls"] + 1
            self.progress.update(task.id, calls=calls, detail=f"Waiting for model | call {calls}", phase="", characters=0)
            self.log(label, f"Waiting for model | call {calls}")
        elif kind == "model_retry":
            delay = f" in {event['delay_seconds']}s" if event.get("delay_seconds") else ""
            self.log(label, f"{event.get('reason', 'Output limit reached')}; "
                           f"retrying{delay} at {event['max_tokens']:,} tokens", "yellow")
        elif kind == "token_budget_approved":
            self.progress.update(task.id, status="Running", detail=f"Budget: {event['max_tokens']:,} tokens")
            self.log(label, f"Resuming with {event['max_tokens']:,} tokens for this task", "green")
        elif kind == "model_activity":
            phase, characters = event["activity"], event.get("characters", 0)
            detail = f"{phase} | {characters:,} characters received" if characters else phase
            if phase != task.fields["phase"]:
                self.log(label, phase)
            self.activity = Text(f"{label}  /  {detail}", style="cyan")
            self.progress.update(task.id, detail=detail, phase=phase, characters=characters)
        elif kind == "model_end":
            self.progress.update(task.id, tokens=task.fields["tokens"] + event.get("output_tokens", 0),
                                 detail="Checking response")
        elif kind == "model_stub":
            self.progress.update(task.id, detail="Offline demonstration")
            self.log(label, "Offline demonstration")
        elif kind == "tool_start":
            action = {"read_workspace_file": "Reading", "list_workspace_files": "Listing",
                      "create_workspace_file": "Creating", "edit_workspace_file": "Editing"}.get(event.get("tool"), "Using tool on")
            detail = f"{action} {event.get('path', '.')}"
            self.progress.update(task.id, detail=detail)
            self.log(label, detail)
        elif kind == "tool_end":
            if event.get("success"):
                if event.get("tool") == "read_workspace_file":
                    self.progress.update(task.id, files=task.fields["files"] + 1)
            else:
                self.log(label, f"Tool failed: {event.get('path', '.')}", "yellow")
        elif kind == "file_changed":
            self.changed_files.setdefault(event["path"], event["action"])
            self.log(label, f"Saved {event['path']} ({event['action']})", "green")
        elif kind == "agent_complete":
            self.progress.update(task.id, total=1, completed=1, status="Done",
                                 detail=f"{task.fields['calls']} calls | {task.fields['files']} files read")
            self.progress.stop_task(task.id)
            self.active = None
            self.log(label, "Completed", "green")
        elif kind in ("agent_failed", "agent_cancelled"):
            self.finish_failure("Cancelled" if kind == "agent_cancelled" else "Failed")
        if self.live is not None:
            self.live.update(self.render())

    def record_state(self, result):
        self.set_route(result.get("route"))
        steps = result.get("steps", [])
        if not self.ui.interactive:
            for step in steps[self.seen_steps:]:
                self.ui.message(f"  - {step}")
        self.seen_steps = len(steps)

    def ask_token_limit(self, event):
        task = self.task(event.get("agent"))
        label = task.description.split(" ", 1)[-1] if task else event["profile"].title()
        if task is not None:
            self.progress.update(task.id, status="Awaiting input", detail="Output limit reached")
        if self.live is not None:
            self.live.update(self.render(), refresh=True)
            self.live.stop()
        prompt_console = self.console if self.console.is_terminal else Console(stderr=True, highlight=False)
        try:
            if event.get("reason"):
                prompt_console.print(Text(event["reason"], style="yellow"))
            prompt_console.print(Text(
                f"{label} reached its {event['current_limit']:,}-token output limit.\n"
                "Progress and saved edits are retained. Increase the limit for this agent's current task?\n"
                f"Enter y for {event['suggested_limit']:,}, a larger token count, or n to stop.\n"
                "Higher limits may increase usage and time and must be supported by the model. "
                "This does not change .env or future tasks.", style="yellow",
            ))
            while True:
                choice = prompt_console.input("Token limit> ").strip().lower()
                if choice in ("n", "no", "stop"):
                    return None
                if choice in ("y", "yes"):
                    return event["suggested_limit"]
                try:
                    limit = int(choice.replace(",", ""))
                except ValueError:
                    limit = 0
                if limit > event["current_limit"]:
                    return limit
                prompt_console.print("Enter y, a larger positive integer, or n to stop.", markup=False)
        finally:
            if task is not None:
                self.progress.update(task.id, status="Running")
            if self.live is not None:
                self.live.update(self.render())
                self.live.start()

    def finish_failure(self, status):
        self.outcome = status
        for task in self.progress.tasks:
            if task.fields["status"] == "Running":
                self.progress.stop_task(task.id)
                self.progress.update(task.id, total=1, completed=0, status=status, detail="Request stopped")
            elif task.fields["status"] == "Waiting":
                self.progress.update(task.id, status="Not run", detail="Request stopped")
        self.activity = Text(f"Request {status.lower()}.", style=STATUS_STYLES[status])

    def render(self):
        tasks = self.progress.tasks
        total = sum(task.fields["status"] != "Skipped" for task in tasks)
        done = sum(task.fields["status"] == "Done" for task in tasks)
        calls = sum(task.fields["calls"] for task in tasks)
        files = sum(task.fields["files"] for task in tasks)
        summary = Text(f"\n{done}/{total} stages complete  |  {calls} model calls  |  {files} files read", style="dim")
        summary.append(f"  |  {len(self.changed_files)} files changed")
        if self.outcome:
            summary.append(f"  |  {self.outcome}", style=STATUS_STYLES[self.outcome])
        for task in tasks:
            if task.fields["inspection_limit"] is not None:
                label = task.description.split(" ", 1)[-1]
                summary.append(f"\n{label}: {task.fields['inspections_remaining']}/"
                               f"{task.fields['inspection_limit']} inspections left")
                if task.fields["inspection_exhausted"]:
                    summary.append(" | limit reached; using available evidence", style="yellow")
        activity = Group(self.activity, *list(self.history)[:-1])
        return Group(
            Panel(Group(self.progress, summary), title="Agent progress", border_style="cyan", padding=(1, 1)),
            Panel(activity, title="Activity", subtitle="Bars animate while working; full bars mean complete.",
                  border_style="bright_black"),
        )

    def __enter__(self):
        if self.ui.interactive:
            self.console.print(Panel(Text(self.query), title="Request", border_style="blue"))
            if self.workspace is not None:
                self.ui.message(f"Workspace: {self.workspace}", "dim")
            self.ui.message("Ctrl+C cancels the current request. Token counts are reported output usage.", "dim")
            self.live = Live(self.render(), console=self.console, refresh_per_second=8)
            self.live.start()
        else:
            if self.workspace is not None:
                self.ui.message(f"\nWorkspace: {self.workspace}")
            self.ui.message(f"\nDevelopment request: {self.query}")
            self.ui.message("Working... Press Ctrl+C to cancel.")
            self.ui.message("Graph trace:")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.finish_failure("Cancelled" if issubclass(exc_type, (KeyboardInterrupt, EOFError, TaskStopped)) else "Failed")
        if self.live is not None:
            self.live.update(self.render())
            self.live.stop()
        if self.workspace is not None:
            if self.changed_files:
                self.ui.message(f"Saved files in {self.workspace}:", "bold green")
                for path, action in self.changed_files.items():
                    self.ui.message(f"  {action}: {path}")
                if exc_type is not None:
                    self.ui.message("These changes remain saved despite the interrupted request.", "yellow")
            else:
                self.ui.message("No workspace files were changed.", "dim")
        return False
