# Development Agent Graph

A runnable LangGraph example focused on software development. A request is routed
to an implementation, debugging, refactoring, testing, or staff engineer review agent.
Proposed changes pass through planning and an independent staff review, with each
agent sharing its work through graph state.

The agents produce **text proposals**: plans, code suggestions, tests, and review
findings. They do not read a repository automatically, modify files, or execute
tests. Supply relevant code, logs, and constraints as context for concrete results.

## The graph

```mermaid
graph LR
    C[classify] -- development task --> P[planner]
    C -- review only --> R[review_agent: Staff Engineer]
    P -- implement --> I[implement_agent]
    P -- debug --> D[debug_agent]
    P -- refactor --> F[refactor_agent]
    P -- test --> T[test_agent]
    I --> R
    D --> R
    F --> R
    T --> R
    R --> E((END))
```

| Node | Responsibility |
| --- | --- |
| `classify` | Select a development task using deterministic intent and keyword rules. |
| `planner` | Define acceptance criteria, an approach, affected areas, and validation steps. |
| `implement_agent` | Propose feature code and explain integration and edge cases. |
| `debug_agent` | Identify reproduction steps, likely causes, a targeted fix, and regression checks. |
| `refactor_agent` | Improve code structure while preserving behavior and public interfaces. |
| `test_agent` | Propose tests covering expected behavior, boundaries, and failures. |
| `review_agent` | Act as the team's staff engineer: assess architecture, correctness, reliability, security, maintainability, and release risks; prioritize findings and validation. |

Leading intent takes precedence: `Write tests for this bug` routes to testing,
`Fix failing tests` to debugging, and `Review this refactor` directly to review.
If no leading intent matches, keywords are checked in review, refactor, debug,
then test order. Requests without a match default to implementation. The router
is deliberately lightweight; ambiguous requests may need clearer wording or a
more capable classifier.

For development tasks, the final answer includes the plan, specialist proposal,
and review findings. Review-only requests return the review. Review is advisory:
findings are reported without an automatic revision loop or an approval gate.
The staff engineer gives an evidence-based advisory verdict, separates blocking
findings from suggestions, and explains tradeoffs and remaining validation.

## Models

The planner and development specialists use `claude-sonnet-5`. The staff engineer
uses **`claude-fable-5-1` with maximum reasoning effort** and adaptive thinking.
This default was selected on September 9, 2026, based on Anthropic's description
of [Claude Fable 5.1](https://www.anthropic.com/claude/fable) as its most capable
model for coding. It is a pinned model choice, not an automatic latest-model lookup.

The reviewer has a 64,000-token output limit covering thinking and answer text,
with streaming handled internally. Only answer text enters graph state. Empty
or truncated responses raise an error instead of being presented as a completed
review. Maximum effort favors review depth and can take longer and cost more;
see Anthropic's [effort guidance](https://platform.claude.com/docs/en/build-with-claude/effort).

Override the staff engineer's model independently through `ANTHROPIC_REVIEW_MODEL`:

```powershell
$env:ANTHROPIC_REVIEW_MODEL = "claude-fable-5-1"
```

Overrides must support adaptive thinking, `max` effort, and the configured output
limit. A missing or blank override uses the default. API errors surface directly;
the reviewer does not silently fall back to a less capable model. Without
`ANTHROPIC_API_KEY`, both model profiles use the offline stubs.

## Running it

From PowerShell on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

On macOS or Linux, use `.venv/bin/python` in place of
`.\.venv\Scripts\python.exe`.

Without `ANTHROPIC_API_KEY`, every agent uses a deterministic, clearly marked
stub. The five built-in examples demonstrate every route and print the graph
trace. Stubs demonstrate traversal; they do not generate or validate real code.

Run a specific request, optionally supplying a UTF-8 context file:

```powershell
.\.venv\Scripts\python.exe main.py "Implement pagination for a Python API endpoint"
.\.venv\Scripts\python.exe main.py "Refactor this module while preserving behavior" --context-file graph_agents/agents.py
.\.venv\Scripts\python.exe main.py "Review this code for correctness" --context-file graph_agents/graph.py
```

To use Claude, set your key once in the project's `.env` file. If the file is
missing, copy `.env.example` to `.env`, then edit this line:

```dotenv
ANTHROPIC_API_KEY=your-api-key
```

The shared model client uses [python-dotenv](https://bbc2.github.io/python-dotenv/)
to load this file into the Python process environment for every agent, whether
you run the CLI or call the graph from Python. The path is relative to the
project, so it also works when launched from another directory. `.env` is
excluded from Git; `.env.example` is the shareable template.

Restart the script (or Python session) after editing `.env`. Existing environment
variables take precedence, including an explicitly empty value. This loads
settings for the application; it does not change Windows user or system variables.
You can still set a key directly in PowerShell for a session:

```powershell
$env:ANTHROPIC_API_KEY = "your-api-key"
.\.venv\Scripts\python.exe main.py "Write unit tests for this module" --context-file graph_agents/agents.py
```

With a key set, the request and supplied context are sent to the configured model
in `graph_agents/llm.py`. A development task makes three model calls (planner,
specialist, reviewer); a review-only task makes one. To return to offline stubs,
clear the value in `.env` and remove any session override before restarting:

```powershell
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
```

To return to the `.env` value after using a PowerShell override, remove the
session variable with the same command and restart the script.

## Calling the graph from Python

```python
from graph_agents import build_graph

app = build_graph()
result = app.invoke({
    "query": "Debug division by zero for an empty list",
    "context": "def average(values): return sum(values) / len(values)",
})
print(result["answer"])
print(result["steps"])
```

Only a non-empty `query` is required. Optional `context` supplies source code,
logs, or repository notes. Nodes return partial state updates that LangGraph
merges before the next node runs. The state also holds `route`, `plan`, `draft`,
`review`, `answer`, and the traversal trace in `steps`. Classification clears
previous output artifacts so a new request does not reuse an old proposal.

## Validation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests run offline even if an API key is configured. They cover routing precedence,
all five graph paths, context and artifact handoffs, review-only behavior, empty
requests, CLI context-file handling, and staff reviewer model configuration and output.

## Layout

```text
graph_agents/
  state.py    # Development routes and shared GraphState
  agents.py   # Router, planner, development specialists, and reviewer
  graph.py    # Compiled graph and conditional edges
  llm.py      # Development/staff review model profiles and offline stubs
main.py       # Development examples and CLI with optional context file
tests/        # Offline routing, graph, and CLI checks
```
