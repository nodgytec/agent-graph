"""Exchange token-budget decisions between the graph worker and CLI thread."""

from contextvars import ContextVar
from threading import Event, Lock
from uuid import uuid4


_controller = ContextVar("token_budget_controller", default=None)


class TaskStopped(RuntimeError):
    """The user chose to stop the current task at a budget prompt."""


def budget_controller():
    return _controller.get()


class TokenBudgetController:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self._pending = {}
        self._lock = Lock()
        self._closed = False

    def __enter__(self):
        self._token = _controller.set(self if self.enabled else None)
        return self

    def request(self, report, *, profile, current_limit, suggested_limit=None, reason=None):
        request_id = uuid4().hex
        pending = {"ready": Event(), "limit": None}
        with self._lock:
            if self._closed:
                raise TaskStopped("Task stopped. Previously saved files remain on disk.")
            self._pending[request_id] = pending
        try:
            report("token_budget_request", request_id=request_id, profile=profile,
                   current_limit=current_limit, suggested_limit=suggested_limit or current_limit * 2,
                   reason=reason)
            pending["ready"].wait()
            if pending["limit"] is None:
                raise TaskStopped("Task stopped at your request. Previously saved files remain on disk.")
            if type(pending["limit"]) is not int or pending["limit"] <= current_limit:
                raise ValueError("The approved token limit must be an integer larger than the current limit.")
            return pending["limit"]
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def respond(self, request_id, limit):
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None:
                pending["limit"] = limit
                pending["ready"].set()

    def cancel(self):
        with self._lock:
            self._closed = True
            for pending in self._pending.values():
                pending["limit"] = None
                pending["ready"].set()

    def __exit__(self, *exc):
        self.cancel()
        _controller.reset(self._token)
