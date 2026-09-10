"""Small, content-free progress events for the CLI's live display."""

from contextvars import ContextVar
from functools import wraps
from time import monotonic

from langgraph.config import get_stream_writer
from .budget import TaskStopped


_agent = ContextVar("progress_agent", default=None)


def reporter():
    """Capture the writer and agent so model streaming can report safely."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return lambda kind, **fields: None
    agent = _agent.get()

    def report(kind: str, **fields):
        writer({"kind": kind, "agent": agent, **fields})

    return report


def track_agent(name, node):
    @wraps(node)
    def tracked(state):
        token = _agent.set(name)
        report = reporter()
        report("agent_start")
        try:
            result = node(state)
        except BaseException as error:
            report("agent_cancelled" if isinstance(error, (KeyboardInterrupt, TaskStopped)) else "agent_failed")
            raise
        else:
            report("agent_complete", route=result.get("route"))
            return result
        finally:
            _agent.reset(token)

    return tracked


def stream_response(model, messages, report):
    """Assemble the full response while reporting activity, never reasoning text."""
    from langchain_core.messages import message_chunk_to_message
    from anthropic import APIStatusError
    from httpx import ResponseNotRead

    response = None
    previous_phase = None
    last_report = 0.0
    characters = 0
    chunks = model.stream(messages)
    try:
        for chunk in chunks:
            response = chunk if response is None else response + chunk
            content = chunk.content
            phase = None
            if isinstance(content, str):
                if content:
                    characters += len(content)
                    phase = "Generating response"
            else:
                for block in content:
                    if isinstance(block, str):
                        characters += len(block)
                        phase = "Generating response"
                    elif block.get("type") == "thinking":
                        phase = "Thinking"
                    elif block.get("type") == "text" and block.get("text"):
                        characters += len(block["text"])
                        phase = "Generating response"
            if chunk.tool_call_chunks:
                phase = "Preparing workspace action"
            now = monotonic()
            if phase and (phase != previous_phase or now - last_report >= 0.15):
                report("model_activity", activity=phase, characters=characters)
                previous_phase, last_report = phase, now
    except ResponseNotRead as error:
        # langchain-core 0.3 can mask an SSE API error while trying to read
        # response.json()/text for callback metadata. The stream is already
        # consumed/closed; recover the original exception, never read it again.
        original = error.__cause__ or error.__context__
        seen = {id(error)}
        while original is not None and id(original) not in seen:
            if isinstance(original, APIStatusError):
                raise original from None
            seen.add(id(original))
            original = original.__cause__ or original.__context__
        raise
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
    if response is None:
        raise RuntimeError("Model returned an empty response stream.")
    result = message_chunk_to_message(response)
    usage = result.usage_metadata or {}
    report(
        "model_end", input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0), characters=characters,
    )
    return result
