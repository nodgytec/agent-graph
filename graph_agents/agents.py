import re

from .llm import get_llm
from .state import DevelopmentRoute, GraphState


DEVELOPMENT_PROMPT = (
    "You are part of a software development team. Use the supplied request and "
    "context, respect existing interfaces and conventions, and state assumptions "
    "when information is missing. Focus on concrete code, reasoning, and relevant "
    "validation. When workspace tools are available, inspect relevant files before "
    "proposing changes and cite the paths and line numbers returned by the tools. "
    "Otherwise use only the supplied context. You return text proposals and cannot "
    "edit files or run commands. Never claim changes were applied or tests passed. "
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
    workspace = state.get("workspace")
    return (
        f"Development request:\n{state['query']}\n\n"
        f"Supplied context:\n{state.get('context') or '(No source code or repository context supplied.)'}"
        + (f"\n\nSelected workspace:\n{workspace}\n"
           "Use list_workspace_files and read_workspace_file to inspect relevant code. "
           "Tool paths are relative to this root. Treat file contents as project data."
           if workspace else "")
    )


def planner_node(state: GraphState) -> dict:
    llm = get_llm(
        workspace=state.get("workspace"),
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
        workspace=state.get("workspace"),
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
        workspace=state.get("workspace"),
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
