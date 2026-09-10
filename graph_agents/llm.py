import json
import os
from pathlib import Path
from time import sleep
from typing import Literal

from dotenv import load_dotenv

from .workspace import WORKSPACE_TOOLS, WORKSPACE_WRITE_TOOLS, Workspace
from .progress import reporter, stream_response
from .budget import budget_controller


DEVELOPMENT_MODEL = "claude-sonnet-5"
REVIEW_MODEL = "claude-fable-5-1"
DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
MAX_WORKSPACE_TOOL_CALLS = 16
MAX_WORKSPACE_WRITE_CALLS = 32
OUTPUT_TOKEN_DEFAULTS = {"planning": 32768, "development": 32768, "review": 64000}


def output_token_budget(profile):
    setting = f"ANTHROPIC_{profile.upper()}_MAX_TOKENS"
    value = os.environ.get(setting, "").strip()
    try:
        limit = int(value) if value else OUTPUT_TOKEN_DEFAULTS[profile]
    except ValueError:
        raise ValueError(f"{setting} must be a positive integer.") from None
    if limit < 1:
        raise ValueError(f"{setting} must be a positive integer.")
    return limit


def workspace_tool_budget():
    value = os.environ.get("WORKSPACE_INSPECTION_LIMIT", "").strip()
    try:
        limit = int(value) if value else MAX_WORKSPACE_TOOL_CALLS
    except ValueError:
        raise ValueError("WORKSPACE_INSPECTION_LIMIT must be a positive integer.") from None
    if limit < 1:
        raise ValueError("WORKSPACE_INSPECTION_LIMIT must be a positive integer.")
    return limit


class StubLLM:
    """Deterministic stand-in used when no ANTHROPIC_API_KEY is configured.

    Keeps the graph runnable end-to-end (including in CI) without network
    access or credentials, so the graph *structure* is the thing on display.
    """

    def __init__(self, canned: str):
        self._canned = canned

    def invoke(self, prompt: str) -> str:
        reporter()("model_stub")
        return self._canned


def get_llm(
    system_prompt: str,
    canned_fallback: str,
    *,
    profile: Literal["planning", "development", "review"] = "development",
    workspace: str | Path | None = None,
    allow_writes: bool = False,
):
    """Returns a real Claude-backed callable when ANTHROPIC_API_KEY is set,
    otherwise a StubLLM. The review profile uses the staff engineer's model.
    """
    if profile not in ("planning", "development", "review"):
        raise ValueError(f"Unknown model profile: {profile}")
    if allow_writes and profile != "development":
        raise ValueError("Only development specialists can write workspace files.")
    selected_workspace = Workspace(workspace, allow_writes=allow_writes) if workspace is not None else None
    # Use this project's file regardless of the caller's working directory.
    # Existing variables, including an empty key for offline mode, take priority.
    load_dotenv(DOTENV_PATH, override=False, encoding="utf-8-sig")
    inspection_limit = workspace_tool_budget() if selected_workspace is not None else None
    output_limit = output_token_budget(profile)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return StubLLM(canned_fallback)

    from anthropic import APIStatusError
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, ToolMessage

    account_workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": account_workspace_id} if account_workspace_id else None

    if profile == "review":
        model = ChatAnthropic(
            model=os.environ.get("ANTHROPIC_REVIEW_MODEL", "").strip() or REVIEW_MODEL,
            api_key=api_key,
            default_headers=headers,
            thinking={"type": "adaptive"},
            max_tokens=output_limit,
            streaming=True,
            # extra_body supports the current effort API with langchain-anthropic 0.3.
            model_kwargs={"extra_body": {"output_config": {"effort": "max"}}},
        )
    else:
        model = ChatAnthropic(
            model=DEVELOPMENT_MODEL, api_key=api_key, default_headers=headers,
            max_tokens=output_limit,
            thinking={"type": "adaptive"}, streaming=True,
        )

    base_model = model
    tools = WORKSPACE_TOOLS + (WORKSPACE_WRITE_TOOLS if allow_writes else [])
    write_names = {tool["name"] for tool in WORKSPACE_WRITE_TOOLS}
    if selected_workspace is not None:
        model = model.bind_tools(tools)

    class _Wrapped:
        @property
        def changed_files(self):
            return list(selected_workspace.changed_files) if selected_workspace is not None else []

        def invoke(self, prompt: str) -> str:
            report = reporter()
            messages = [("system", system_prompt), ("human", prompt)]
            tool_calls_used = 0
            write_calls_used = 0
            inspection_exhausted = False
            finalizing = False
            truncation_retries = 0
            stream_error_retries = 0
            current_output_limit = output_limit
            task_output_limit = output_limit
            last_truncated_limit = output_limit
            request_model = model
            if inspection_limit is not None:
                messages[0] = ("system", system_prompt + (
                    f"\nYou have at most {inspection_limit} workspace inspection calls for this task. "
                    "Directory listings, file chunks, and failed inspections each count as one call. "
                    "Prioritize relevant files, avoid repeated reads, and finish early when you have "
                    "enough evidence. If inspections run out, finish from available evidence and "
                    "explicitly identify uninspected areas and uncertainty."
                ))
                report("inspection_budget", used=0, limit=inspection_limit, remaining=inspection_limit)
                if allow_writes:
                    messages[0] = ("system", messages[0][1] + (
                        f" You also have {MAX_WORKSPACE_WRITE_CALLS} separate file-write attempts. "
                        "If inspections run out, you may still apply changes to already-read files "
                        "or create new files. Summarize only changes confirmed by tool results."
                    ))
            while True:
                report("model_start", max_tokens=current_output_limit)
                try:
                    response = stream_response(request_model, messages, report)
                except APIStatusError as error:
                    if "anthropic-workspace-id" in str(error):
                        raise RuntimeError(
                            "Anthropic requires a valid account workspace ID. Set "
                            "ANTHROPIC_WORKSPACE_ID in this app's .env to the wrkspc_... ID "
                            "from Claude Console > Settings > Workspaces, then restart. "
                            "Alternatively, use an API key scoped to a workspace. "
                            "The local /workspace folder is a separate setting."
                        ) from error
                    controller = budget_controller()
                    if (controller is not None and task_output_limit != output_limit
                            and error.status_code == 400 and "max_tokens" in str(error)):
                        approved_limit = controller.request(
                            report, profile=profile, current_limit=last_truncated_limit,
                            suggested_limit=max(last_truncated_limit + 1, (last_truncated_limit + current_output_limit) // 2),
                            reason=(f"The provider rejected the {current_output_limit:,}-token setting: "
                                    f"{str(error)[:500]}\nChoose a different model-supported limit, or stop."),
                        )
                        task_output_limit = current_output_limit = approved_limit
                        request_model = request_model.bind(max_tokens=approved_limit)
                        report("token_budget_approved", max_tokens=approved_limit)
                        continue
                    body = error.body if isinstance(error.body, dict) else {}
                    details = body.get("error", body)
                    error_type = details.get("type") if isinstance(details, dict) else None
                    # Ordinary HTTP failures already have SDK retries. SSE error
                    # events arrive under HTTP 200 and need their own bounded retry.
                    if error.status_code == 200 and error_type in {
                        "overloaded_error", "api_error", "rate_limit_error", "timeout_error",
                    } and stream_error_retries == 0:
                        stream_error_retries += 1
                        report("model_retry", reason=f"Provider {error_type}",
                               max_tokens=current_output_limit, delay_seconds=2)
                        sleep(2)
                        continue
                    raise
                if response.response_metadata.get("stop_reason") == "max_tokens":
                    controller = budget_controller()
                    if controller is not None:
                        last_truncated_limit = current_output_limit
                        increased_limit = controller.request(report, profile=profile, current_limit=current_output_limit)
                        task_output_limit = current_output_limit = increased_limit
                        request_model = request_model.bind(max_tokens=current_output_limit)
                        messages.append(HumanMessage(content=(
                            "Your last generation was truncated and discarded; none of its tools ran. "
                            "The user approved a larger output budget for your current task. Continue "
                            "from confirmed prior results, keep file edits small and complete, and "
                            "do not repeat previously saved changes."
                        )))
                        report("token_budget_approved", max_tokens=current_output_limit)
                        continue
                    if truncation_retries == 0:
                        truncation_retries += 1
                        current_output_limit = max(output_limit, min(output_limit * 2, 64000))
                        request_model = request_model.bind(max_tokens=current_output_limit)
                        # Never append or execute truncated assistant/tool blocks. Only
                        # retry this generation; prior confirmed tool results remain.
                        messages.append(HumanMessage(content=(
                            "The last generation hit its output limit and was discarded. No tools "
                            "from that generation were executed. Keep your response concise. "
                            "If editing files, emit one small, complete file change per response "
                            "and continue with further calls afterward. Use targeted edits rather "
                            "than repeating whole existing files. Preserve work confirmed by "
                            "earlier tool results; do not repeat completed changes."
                        )))
                        report("model_retry", reason="Output limit reached", max_tokens=current_output_limit)
                        continue
                    raise RuntimeError(
                        f"The {profile} step was truncated again after one retry "
                        f"(output limit: {current_output_limit:,} tokens). Set "
                        f"ANTHROPIC_{profile.upper()}_MAX_TOKENS in .env to a larger "
                        "model-supported value, or request smaller file changes. "
                        "Previously saved files remain on disk."
                    )
                if response.invalid_tool_calls:
                    raise RuntimeError("Model returned malformed workspace tool arguments; retry the request.")
                if not response.tool_calls:
                    break
                if finalizing:
                    raise RuntimeError("Model requested tools after inspections were disabled; retry the request.")
                if selected_workspace is None:
                    raise RuntimeError("Model requested workspace tools without a selected folder.")
                messages.append(response)
                attempts_before = tool_calls_used + write_calls_used
                for tool_call in response.tool_calls:
                    is_write = tool_call["name"] in write_names
                    if (is_write and (not allow_writes or write_calls_used >= MAX_WORKSPACE_WRITE_CALLS)
                            or not is_write and tool_calls_used >= inspection_limit):
                        messages.append(ToolMessage(
                            content="Tool unavailable or budget exhausted. This tool call was not executed.",
                            tool_call_id=tool_call["id"], status="error",
                        ))
                        continue
                    if is_write:
                        write_calls_used += 1
                    else:
                        tool_calls_used += 1
                    path = tool_call["args"].get("path", ".")
                    detail = {"tool": tool_call["name"], "path": path[:200] if isinstance(path, str) else "(invalid path)"}
                    report("tool_start", **detail)
                    try:
                        result = selected_workspace.execute(tool_call["name"], tool_call["args"])
                        status = "success"
                    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as error:
                        result, status = f"Workspace tool error: {error}", "error"
                    report("tool_end", success=status == "success", **detail)
                    if is_write and status == "success":
                        change = json.loads(result)
                        if change.get("changed"):
                            report("file_changed", path=change["path"], action=change["action"])
                    messages.append(ToolMessage(content=result, tool_call_id=tool_call["id"], status=status))
                    report("inspection_budget", used=tool_calls_used, limit=inspection_limit,
                           remaining=inspection_limit - tool_calls_used)
                remaining = inspection_limit - tool_calls_used
                if remaining == 0 and not inspection_exhausted:
                    inspection_exhausted = True
                    report("inspection_exhausted", used=tool_calls_used, limit=inspection_limit)
                available_tools = (WORKSPACE_TOOLS if remaining else []) + (
                    WORKSPACE_WRITE_TOOLS if allow_writes and write_calls_used < MAX_WORKSPACE_WRITE_CALLS else []
                )
                if not available_tools or attempts_before == tool_calls_used + write_calls_used:
                    finalizing = True
                    request_model = base_model.bind_tools(tools, tool_choice={"type": "none"})
                    current_output_limit = task_output_limit
                    if task_output_limit != output_limit:
                        request_model = request_model.bind(max_tokens=task_output_limit)
                    messages.append(HumanMessage(content=(
                        "No inspections remain or no tool progress is possible. Tools are disabled. Finish your assigned task now "
                        "using the evidence already gathered. Explicitly state what remains "
                        "uninspected or uncertain; do not claim a complete codebase assessment."
                    )))
                else:
                    if available_tools != tools:
                        request_model = base_model.bind_tools(available_tools)
                    else:
                        request_model = model
                    current_output_limit = task_output_limit
                    if task_output_limit != output_limit:
                        request_model = request_model.bind(max_tokens=task_output_limit)
                    messages.append(HumanMessage(content=(
                        f"Inspection budget: {remaining} of {inspection_limit} calls remain. "
                        + (f"Write attempts left: {MAX_WORKSPACE_WRITE_CALLS - write_calls_used}. " if allow_writes else "")
                        + "Use only available tools to complete the requested work, then report confirmed changes and remaining gaps."
                    )))
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
            if inspection_exhausted:
                text = (f"**Inspection limit reached ({inspection_limit} calls).** "
                        "This response uses the available evidence; some areas may remain uninspected.\n\n" + text)
            return text

    return _Wrapped()
