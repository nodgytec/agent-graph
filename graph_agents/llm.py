import os


class StubLLM:
    """Deterministic stand-in used when no ANTHROPIC_API_KEY is configured.

    Keeps the graph runnable end-to-end (including in CI) without network
    access or credentials, so the graph *structure* is the thing on display.
    """

    def __init__(self, canned: str):
        self._canned = canned

    def invoke(self, prompt: str) -> str:
        return self._canned


def get_llm(system_prompt: str, canned_fallback: str):
    """Returns a real Claude-backed callable when ANTHROPIC_API_KEY is set,
    otherwise a StubLLM so the example still runs without one."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return StubLLM(canned_fallback)

    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(model="claude-sonnet-5", api_key=api_key)

    class _Wrapped:
        def invoke(self, prompt: str) -> str:
            response = model.invoke([("system", system_prompt), ("human", prompt)])
            return response.content

    return _Wrapped()
