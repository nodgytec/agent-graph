import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .workspace import WORKSPACE_TOOLS, Workspace


DEVELOPMENT_MODEL = "claude-sonnet-5"
REVIEW_MODEL = "claude-fable-5-1"
DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
MAX_WORKSPACE_TOOL_CALLS = 16


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
    workspace: str | Path | None = None,
):
    """Returns a real Claude-backed callable when ANTHROPIC_API_KEY is set,
    otherwise a StubLLM. The review profile uses the staff engineer's model.
    """
    if profile not in ("development", "review"):
        raise ValueError(f"Unknown model profile: {profile}")
    selected_workspace = Workspace(workspace) if workspace is not None else None
    # Use this project's file regardless of the caller's working directory.
    # Existing variables, including an empty key for offline mode, take priority.
    load_dotenv(DOTENV_PATH, override=False, encoding="utf-8-sig")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return StubLLM(canned_fallback)

    from anthropic import BadRequestError
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import ToolMessage

    account_workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": account_workspace_id} if account_workspace_id else None

    if profile == "review":
        model = ChatAnthropic(
            model=os.environ.get("ANTHROPIC_REVIEW_MODEL", "").strip() or REVIEW_MODEL,
            api_key=api_key,
            default_headers=headers,
            thinking={"type": "adaptive"},
            max_tokens=64000,
            streaming=True,
            # extra_body supports the current effort API with langchain-anthropic 0.3.
            model_kwargs={"extra_body": {"output_config": {"effort": "max"}}},
        )
    else:
        model = ChatAnthropic(
            model=DEVELOPMENT_MODEL, api_key=api_key, default_headers=headers, max_tokens=8192,
        )

    if selected_workspace is not None:
        model = model.bind_tools(WORKSPACE_TOOLS)

    class _Wrapped:
        def invoke(self, prompt: str) -> str:
            messages = [("system", system_prompt), ("human", prompt)]
            tool_calls_used = 0
            while True:
                try:
                    response = model.invoke(messages)
                except BadRequestError as error:
                    if "anthropic-workspace-id" in str(error):
                        raise RuntimeError(
                            "Anthropic requires a valid account workspace ID. Set "
                            "ANTHROPIC_WORKSPACE_ID in this app's .env to the wrkspc_... ID "
                            "from Claude Console > Settings > Workspaces, then restart. "
                            "Alternatively, use an API key scoped to a workspace. "
                            "The local /workspace folder is a separate setting."
                        ) from error
                    raise
                if response.response_metadata.get("stop_reason") == "max_tokens":
                    raise RuntimeError("Model output was truncated before completion; narrow the request or context.")
                if response.invalid_tool_calls:
                    raise RuntimeError("Model returned malformed workspace tool arguments; retry the request.")
                if not response.tool_calls:
                    break
                if selected_workspace is None:
                    raise RuntimeError("Model requested workspace tools without a selected folder.")
                tool_calls_used += len(response.tool_calls)
                if tool_calls_used > MAX_WORKSPACE_TOOL_CALLS:
                    raise RuntimeError("Workspace inspection limit reached; narrow the task or select a smaller folder.")
                messages.append(response)
                for tool_call in response.tool_calls:
                    try:
                        result = selected_workspace.execute(tool_call["name"], tool_call["args"])
                        status = "success"
                    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as error:
                        result, status = f"Workspace tool error: {error}", "error"
                    messages.append(ToolMessage(content=result, tool_call_id=tool_call["id"], status=status))
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
