# Runtime optimization context

## Task

Review this Python development-agent graph and propose concrete changes that
improve responsiveness, output completeness, and predictable API usage.
Assume interactive use from Windows PowerShell with one request at a time.
No target latency or spending budget has been specified. Explain tradeoffs and
identify what needs measurement rather than claiming an unmeasured speedup.

## Current behavior

- The CLI accepts one request and an optional UTF-8 context file.
- Classification is deterministic and makes no model call.
- Development requests run planner -> specialist -> staff reviewer sequentially.
  Each later stage consumes artifacts from the previous stage.
- Review-only requests go directly to the staff reviewer.
- Running without a request executes five examples, totaling 13 model calls
  when an API key is configured.
- The same request and context are included in each stage's prompt.
- Each stage creates a new model client through get_llm.
- The development model is claude-sonnet-5 with no explicit output-token limit.
  The inspected langchain-anthropic 0.3.22 installation defaults to 1,024 tokens.
- The review model defaults to claude-fable-5-1 with adaptive thinking, maximum
  effort, a 64,000-token output limit, and internal streaming. Its model ID can
  be overridden through ANTHROPIC_REVIEW_MODEL.
- The CLI prints only after app.invoke finishes; internal streaming currently
  does not display live progress or answer text to the terminal.
- The shared model client loads the project's .env into the process environment
  with python-dotenv. Existing environment variables take precedence, including
  an empty API key. Both model profiles use the same ANTHROPIC_API_KEY.
- Without ANTHROPIC_API_KEY, the graph uses deterministic offline stubs.
- Agents produce text proposals. They cannot inspect files, apply edits, or
  execute tests themselves.

## Environment and evidence

- The existing virtual environment was created with Python 3.11.9 on Windows.
- Inspected installed packages: langgraph 0.2.76, langchain-anthropic 0.3.22,
  langchain-core 0.3.86, anthropic 0.125.0, and python-dotenv 1.2.3.
- Offline graph and CLI tests passed during inspection. This does not establish
  live API compatibility, output quality, latency, or cost.
- No live API benchmark, usage measurement, or failure traceback is supplied.
- Dependency ranges and source snapshots appear below. These snapshots are
  copied from the working tree and do not automatically update after edits.

## Questions to resolve

1. What explicit output budgets, timeouts, and retry settings are appropriate
   for the planner, specialist, and reviewer? Treat 8,192 development tokens as
   a possible starting point to evaluate, not a proven optimum.
2. How can the CLI show useful progress while preserving the final answer?
3. Would client reuse help repeated requests, and how should configuration
   changes and offline stubs remain isolated?
4. How can elapsed time and token usage per stage be recorded without exposing
   credentials or unnecessarily logging source code?
5. Which optional reductions in review effort or output allowance are worthwhile
   for routine tasks, and what quality tradeoffs require evaluation?
6. How should callers detect and recover from truncation, empty output, network
   failures, or unsupported model settings?

## Constraints and acceptance criteria

- Preserve all five routes, required context/artifact handoffs, and review-only
  behavior. Dependent stages cannot simply run in parallel.
- Preserve the current model choices and staff review by default. Present any
  change in review depth or model choice as an explicit configurable option.
- Preserve deterministic offline behavior without credentials.
- Keep credentials in environment variables; do not embed them in source files.
- Keep the existing CLI invocation compatible. Propose small, relevant changes
  and explain any dependency upgrade that is necessary.
- Continue rejecting empty or truncated answers as incomplete work.
- Keep tests offline by mocking model calls, including error cases and any new
  configuration. The existing validation command is:
  .\.venv\Scripts\python.exe -m unittest discover -s tests -v
- The test source is not included here. State what tests should be added or
  inspected; do not invent existing test implementations or execution results.

## Requested response

Prioritize findings by impact and cite the supplied filenames and functions.
Provide concrete code changes or a patch, explain the speed/quality/cost
tradeoffs, and describe focused validation. Separate verified observations from
hypotheses. Do not claim to have applied changes, executed tests, measured
performance, or verified current provider compatibility.

## Source snapshots

### requirements.txt

```text

langgraph>=0.2,<0.3
langchain-anthropic>=0.3,<0.4
python-dotenv>=1,<2

```

### main.py

```python

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

```

### graph_agents/llm.py

```python

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv


DEVELOPMENT_MODEL = "claude-sonnet-5"
REVIEW_MODEL = "claude-fable-5-1"
DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class StubLLM:
    """Deterministic stand-in used when no ANTHROPIC_API_KEY is configured.

    Keeps the graph runnable end-to-end (including in CI) without network
    access or credentials, so the graph *structure* is the thing on display.
    """

    def __init__(self, canned: str):
        self._canned = canned

    def invoke(self, prompt: str) -> str:
        return self._canned


def get_llm(
    system_prompt: str,
    canned_fallback: str,
    *,
    profile: Literal["development", "review"] = "development",
):
    """Returns a real Claude-backed callable when ANTHROPIC_API_KEY is set,
    otherwise a StubLLM. The review profile uses the staff engineer's model.
    """
    if profile not in ("development", "review"):
        raise ValueError(f"Unknown model profile: {profile}")
    # Use this project's file regardless of the caller's working directory.
    # Existing variables, including an empty key for offline mode, take priority.
    load_dotenv(DOTENV_PATH, override=False, encoding="utf-8-sig")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return StubLLM(canned_fallback)

    from langchain_anthropic import ChatAnthropic

    if profile == "review":
        model = ChatAnthropic(
            model=os.environ.get("ANTHROPIC_REVIEW_MODEL", "").strip() or REVIEW_MODEL,
            api_key=api_key,
            thinking={"type": "adaptive"},
            max_tokens=64000,
            streaming=True,
            # extra_body supports the current effort API with langchain-anthropic 0.3.
            model_kwargs={"extra_body": {"output_config": {"effort": "max"}}},
        )
    else:
        model = ChatAnthropic(model=DEVELOPMENT_MODEL, api_key=api_key)

    class _Wrapped:
        def invoke(self, prompt: str) -> str:
            response = model.invoke([("system", system_prompt), ("human", prompt)])
            if response.response_metadata.get("stop_reason") == "max_tokens":
                raise RuntimeError("Model output was truncated before completion; narrow the request or context.")
            content = response.content
            if isinstance(content, str):
                text = content
            else:
                # Thinking models return mixed blocks; only answer text belongs in state.
                text = "".join(
                    block if isinstance(block, str) else block.get("text", "")
                    for block in content
                    if isinstance(block, str) or block.get("type") == "text"
                )
            if not text.strip():
                raise RuntimeError("Model returned no answer text.")
            return text

    return _Wrapped()

```

### graph_agents/graph.py

```python

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

```

### graph_agents/state.py

```python

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

```

### graph_agents/agents.py

```python

import re

from .llm import get_llm
from .state import DevelopmentRoute, GraphState


DEVELOPMENT_PROMPT = (
    "You are part of a software development team. Use the supplied request and "
    "context, respect existing interfaces and conventions, and state assumptions "
    "when information is missing. Focus on concrete code, reasoning, and relevant "
    "validation. You can only return text: you cannot inspect a repository, edit "
    "files, or run commands. Never claim changes were applied or tests passed. "
)

# Leading intent takes precedence: 'write tests for a bug' is testing, while
# 'fix failing tests' is debugging. Word boundaries avoid substring matches.
INTENT_RULES = (
    ("review", r"^(?:review|audit|critique)\b"),
    ("debug", r"^(?:fix|debug|diagnose|troubleshoot|resolve)\b"),
    ("refactor", r"^(?:refactor|restructure|simplify|clean up|optimize)\b"),
    ("test", r"^(?:test\b|(?:write|add|create|generate|expand)\s+"
             r"(?:(?:a|an|some|more)\s+)?"
             r"(?:(?:unit|integration|regression|end-to-end|automated)\s+)*tests?\b)"),
    ("implement", r"^(?:implement|build|create|add|write|develop)\b"),
)
KEYWORD_RULES = (
    ("review", r"\b(?:review|audit|critique)\b"),
    ("refactor", r"\b(?:refactor(?:ing)?|restructure|clean up|simplify|optimize)\b"),
    ("debug", r"\b(?:bug|bugs|error|errors|exception|traceback|crash|crashes|crashing|"
              r"failing|broken|debug|fix|diagnose|troubleshoot)\b"),
    ("test", r"\b(?:test|tests|testing|coverage|pytest|unittest)\b"),
)


def classify_node(state: GraphState) -> dict:
    """Select a development specialist with deterministic intent rules."""
    query = state["query"].strip().lower()
    if not query:
        raise ValueError("A non-empty development request is required.")
    query = re.sub(
        r"^(?:(?:please|can you|could you|would you|help me(?: to)?|"
        r"i (?:want|need) (?:you )?to)\s+)+", "", query,
    )
    route = next(
        (route for route, pattern in (*INTENT_RULES, *KEYWORD_RULES)
         if re.search(pattern, query)),
        "implement",
    )
    return {
        "route": route,
        "plan": "",
        "draft": "",
        "review": "",
        "answer": None,
        "steps": state.get("steps", []) + [f"classify -> routed to '{route}' agent"],
    }


def _request_context(state: GraphState) -> str:
    return (
        f"Development request:\n{state['query']}\n\n"
        f"Supplied context:\n{state.get('context') or '(No source code or repository context supplied.)'}"
    )


def planner_node(state: GraphState) -> dict:
    llm = get_llm(
        system_prompt=DEVELOPMENT_PROMPT + (
            "You are the development planner. Produce a short plan with acceptance "
            "criteria, the proposed approach, likely affected areas, and validation "
            "steps appropriate to the selected task. Do not invent repository files."
        ),
        canned_fallback=(
            "[Stub planner] Clarify expected behavior, inspect the supplied context, "
            "prepare the smallest appropriate change, and identify focused validation."
        ),
    )
    plan = llm.invoke(f"{_request_context(state)}\n\nTask type: {state['route']}")
    return {"plan": plan, "steps": state["steps"] + ["planner -> produced development plan"]}


SPECIALISTS = {
    "implement": (
        "You are the implementation agent. Follow the plan and propose concrete "
        "code for the requested behavior, including integration details and edge cases.",
        "[Stub implementation agent] Propose the feature code and explain how it "
        "fits the existing interfaces. No files have been changed.",
    ),
    "debug": (
        "You are the debugging agent. Separate observed evidence from hypotheses, "
        "identify a reproduction and likely root cause, and propose a targeted fix "
        "with a regression check. Do not invent errors or execution results.",
        "[Stub debugging agent] Reproduce the failure, isolate its cause, propose "
        "a targeted fix, and describe a regression check. No commands have been run.",
    ),
    "refactor": (
        "You are the refactoring agent. Preserve observable behavior and public "
        "interfaces unless the request explicitly changes them. Propose clearer code, "
        "explain the tradeoffs, and identify checks that protect existing behavior.",
        "[Stub refactoring agent] Simplify the code while preserving behavior and "
        "public interfaces, then identify checks for equivalence. No files have been changed.",
    ),
    "test": (
        "You are the testing agent. Propose meaningful tests for the supplied code "
        "and acceptance criteria using the existing test framework when known. Cover "
        "normal behavior, boundaries, and failure cases. Explain how to run them.",
        "[Stub testing agent] Propose tests for expected behavior, boundary values, "
        "and failures, with execution instructions. No tests have been run.",
    ),
}


def _specialist_node(state: GraphState, route: DevelopmentRoute) -> dict:
    instructions, fallback = SPECIALISTS[route]
    llm = get_llm(
        system_prompt=DEVELOPMENT_PROMPT + instructions,
        canned_fallback=fallback,
    )
    draft = llm.invoke(f"{_request_context(state)}\n\nDevelopment plan:\n{state['plan']}")
    return {
        "draft": draft,
        "steps": state["steps"] + [f"{route}_agent -> produced development proposal"],
    }


def implementation_agent_node(state: GraphState) -> dict:
    return _specialist_node(state, "implement")


def debugging_agent_node(state: GraphState) -> dict:
    return _specialist_node(state, "debug")


def refactoring_agent_node(state: GraphState) -> dict:
    return _specialist_node(state, "refactor")


def testing_agent_node(state: GraphState) -> dict:
    return _specialist_node(state, "test")


def review_agent_node(state: GraphState) -> dict:
    """Have the team's staff engineer review a proposal or supplied code."""
    llm = get_llm(
        profile="review",
        system_prompt=DEVELOPMENT_PROMPT + (
            "You are the team's staff-level software engineer and final technical "
            "reviewer. Own the system-wide perspective: evaluate whether the proposal "
            "solves the right problem and fits the architecture, interfaces, and "
            "long-term maintenance needs. Review against the request, acceptance "
            "criteria, plan, and supplied context. For a review-only task, review "
            "the supplied code directly. Challenge assumptions and unnecessary "
            "complexity; assess correctness, regressions, security, performance, "
            "reliability, and test coverage. Consider compatibility, migrations, "
            "observability, rollout, and rollback when relevant to the change. "
            "Prioritize concrete risks over style preferences. For each actionable "
            "finding give its severity, evidence, impact, and a practical correction "
            "or validation step. Give file and line locations only when supplied. "
            "Explain architectural tradeoffs and coach the team toward a proportionate "
            "solution. Return an advisory verdict (ready for implementation, changes "
            "requested, or insufficient context), blocking findings, non-blocking "
            "suggestions, and remaining validation. A verdict is based only on the "
            "provided evidence, never proof that code was executed or is safe to deploy. "
            "Do not invent findings; if no supported issues are found, say so and "
            "identify any validation gaps. If code is missing, explain what is needed "
            "for a concrete review."
        ),
        canned_fallback=(
            "[Stub staff engineer] Verdict: insufficient context (offline stub). "
            "A staff review would assess architecture, correctness, security, "
            "reliability, maintainability, and release risks, then prioritize findings "
            "and remaining validation. This stub has not validated code or executed tests."
        ),
    )
    review = llm.invoke(
        f"{_request_context(state)}\n\n"
        f"Development plan:\n{state.get('plan') or '(Review-only request.)'}\n\n"
        f"Development proposal:\n{state.get('draft') or '(Review the supplied context directly.)'}"
    )
    if state["route"] == "review":
        answer = review
    else:
        answer = (
            f"Development plan:\n{state['plan']}\n\n"
            f"Proposed solution:\n{state['draft']}\n\n"
            f"Staff engineer review:\n{review}"
        )
    return {
        "review": review,
        "answer": answer,
        "steps": state["steps"] + ["review_agent -> produced staff engineer review"],
    }

```
