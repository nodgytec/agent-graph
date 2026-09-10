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
