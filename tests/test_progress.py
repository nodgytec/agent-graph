import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessageChunk
from rich.console import Console
from httpx import ResponseNotRead

from graph_agents import build_graph
from graph_agents.progress import stream_response
from graph_agents.terminal import AgentProgress, TerminalUI


class ProgressEventsTests(unittest.TestCase):
    def test_unrelated_response_not_read_is_not_replaced(self):
        error = ResponseNotRead()

        def broken_stream():
            yield AIMessageChunk(content="Partial")
            raise error

        model = Mock()
        model.stream.return_value = broken_stream()
        with self.assertRaises(ResponseNotRead) as raised:
            stream_response(model, [], lambda *args, **kwargs: None)
        self.assertIs(raised.exception, error)

    def test_all_routes_emit_start_and_completion_in_execution_order(self):
        queries = {"implement": "Implement a feature", "debug": "Debug a failure",
                   "refactor": "Refactor a function", "test": "Write tests", "review": "Review code"}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            app = build_graph()
            for route, query in queries.items():
                with self.subTest(route=route):
                    events = list(app.stream({"query": query}, stream_mode="custom"))
                    expected = ["classify", "review_agent"] if route == "review" else [
                        "classify", "planner", f"{route}_agent", "review_agent",
                    ]
                    for kind in ("agent_start", "agent_complete"):
                        self.assertEqual([event["agent"] for event in events if event["kind"] == kind], expected)
                    self.assertTrue(any(event["kind"] == "model_stub" for event in events))

    def test_stream_activity_omits_answer_and_reasoning_content(self):
        model = Mock()
        model.stream.return_value = iter([
            AIMessageChunk(content=[{"type": "thinking", "thinking": "PRIVATE REASONING"}]),
            AIMessageChunk(content=[{"type": "text", "text": "PRIVATE ANSWER"}]),
            AIMessageChunk(content=[], usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}),
        ])
        events = []
        answer = stream_response(model, [], lambda kind, **fields: events.append({"kind": kind, **fields}))
        self.assertIn("PRIVATE ANSWER", str(answer.content))
        self.assertEqual([event["activity"] for event in events if event["kind"] == "model_activity"], [
            "Thinking", "Generating response",
        ])
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertEqual(events[-1]["output_tokens"], 20)

    def test_stream_is_closed_on_failure_and_empty_stream_is_rejected(self):
        closed = []

        def broken_stream():
            try:
                yield AIMessageChunk(content="Partial")
                raise RuntimeError("Network disconnected")
            finally:
                closed.append(True)

        model = Mock()
        model.stream.return_value = broken_stream()
        with self.assertRaisesRegex(RuntimeError, "Network disconnected"):
            stream_response(model, [], lambda *args, **kwargs: None)
        self.assertEqual(closed, [True])
        model.stream.return_value = iter([])
        with self.assertRaisesRegex(RuntimeError, "empty response stream"):
            stream_response(model, [], lambda *args, **kwargs: None)


class ProgressDisplayTests(unittest.TestCase):
    def display(self, width=100):
        console = Console(file=io.StringIO(), force_terminal=True, width=width, height=40,
                          legacy_windows=False, record=True)
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            display = AgentProgress("Review [literal] code", console=console)
        return display, console

    def event(self, display, kind, agent="planner", **fields):
        display.handle({"kind": kind, "agent": agent, **fields})

    def test_running_bar_has_unknown_total_until_agent_completes(self):
        display, _ = self.display()
        bar_column = display.progress.columns[2]
        self.assertFalse(bar_column.render(display.task("planner")).pulse)
        self.event(display, "agent_start")
        self.assertTrue(bar_column.render(display.task("planner")).pulse)
        self.event(display, "model_start")
        self.event(display, "model_activity", activity="Thinking", characters=0)
        self.event(display, "model_end", output_tokens=2000)
        self.assertIsNone(display.task("planner").total)
        self.assertEqual(display.task("planner").completed, 0)
        self.event(display, "agent_complete")
        self.assertEqual(display.task("planner").completed, 1)
        self.assertEqual(display.task("planner").fields["status"], "Done")
        self.assertEqual(display.task("planner").fields["tokens"], 2000)

    def test_review_only_marks_unused_agents_skipped(self):
        display, console = self.display()
        self.event(display, "agent_start", "classify")
        self.event(display, "agent_complete", "classify", route="review")
        self.event(display, "agent_start", "review_agent")
        self.assertEqual(display.task("planner").fields["status"], "Skipped")
        self.assertEqual(display.task("specialist").fields["status"], "Skipped")
        console.print(display.render())
        self.assertIn("1/2 stages complete", console.export_text())

    def test_error_or_cancellation_does_not_mark_pending_agents_complete(self):
        for kind, status in (("agent_failed", "Failed"), ("agent_cancelled", "Cancelled")):
            with self.subTest(kind=kind):
                display, _ = self.display()
                self.event(display, "agent_start", "classify")
                self.event(display, "agent_complete", "classify", route="implement")
                self.event(display, "agent_start", "planner")
                self.event(display, kind, "planner")
                self.assertEqual(display.task("classify").fields["status"], "Done")
                self.assertEqual(display.task("planner").fields["status"], status)
                self.assertEqual(display.task("specialist").fields["status"], "Not run")
                self.assertEqual(display.task("review_agent").completed, 0)

    def test_tools_are_visible_and_counters_accumulate_across_model_calls(self):
        display, console = self.display(width=120)
        self.event(display, "agent_start")
        self.event(display, "model_start")
        self.event(display, "model_end", output_tokens=20)
        self.event(display, "tool_start", tool="read_workspace_file", path="src/app.py")
        self.event(display, "tool_end", tool="read_workspace_file", path="src/app.py", success=True)
        self.event(display, "model_start")
        self.event(display, "model_end", output_tokens=40)
        self.assertEqual(display.task("planner").fields["calls"], 2)
        self.assertEqual(display.task("planner").fields["files"], 1)
        self.assertEqual(display.task("planner").fields["tokens"], 60)
        console.print(display.render())
        text = console.export_text()
        self.assertIn("src/app.py", text)
        self.assertIn("60 tok", text)

    def test_narrow_and_wide_terminal_layouts_render_without_overflow(self):
        for width in (80, 120):
            with self.subTest(width=width):
                display, console = self.display(width)
                display.set_route("implement")
                self.event(display, "agent_start", "implement_agent")
                self.event(display, "inspection_budget", "implement_agent", limit=16, used=16, remaining=0)
                self.event(display, "inspection_exhausted", "implement_agent", limit=16, used=16)
                self.event(display, "tool_start", "implement_agent", tool="read_workspace_file", path="src/" + "long/" * 30 + "app.py")
                console.print(display.render())
                text = console.export_text()
                self.assertIn("Implementation", text)
                overflow = [(len(line), line) for line in text.splitlines() if len(line) > width]
                self.assertEqual(overflow, [])

    def test_inspection_counts_remain_visible_after_completion(self):
        display, console = self.display()
        self.event(display, "agent_start")
        self.event(display, "inspection_budget", limit=16, used=16, remaining=0)
        self.event(display, "inspection_exhausted", limit=16, used=16)
        self.event(display, "agent_complete")
        console.print(display.render())
        self.assertIn("Planner: 0/16 inspections left", console.export_text(clear=False))
        self.assertIn("limit reached; using available evidence", console.export_text())
        self.assertEqual(display.task("planner").fields["status"], "Done")

    def test_redirected_output_is_plain_and_preserves_literal_text(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=False)
        ui = TerminalUI(console)
        ui.message("[red]literal path[/red]")
        with AgentProgress("Review [literal] code", console=console) as display:
            self.event(display, "agent_start", "classify")
            self.event(display, "agent_complete", "classify", route="review")
        text = output.getvalue()
        self.assertIn("[red]literal path[/red]", text)
        self.assertIn("Review [literal] code", text)
        self.assertNotIn("\x1b[", text)

    def test_retry_is_visible_without_marking_agent_failed(self):
        display, console = self.display()
        self.event(display, "agent_start")
        self.event(display, "model_retry", max_tokens=64000)
        console.print(display.render())
        self.assertIn("retrying at 64,000 tokens", console.export_text())
        self.assertEqual(display.task("planner").fields["status"], "Running")
        self.event(display, "model_retry", reason="Provider overloaded_error", max_tokens=32768, delay_seconds=2)
        console.print(display.render())
        self.assertIn("Provider overloaded_error; retrying in 2s", console.export_text())

    def test_token_prompt_validates_input_and_returns_custom_limit(self):
        display, _ = self.display()
        display.live = Mock()
        self.event(display, "agent_start")
        request = {"agent": "planner", "profile": "planning", "current_limit": 32768, "suggested_limit": 65536}
        choices = iter(["", "nope", "-1", "32768", "48,000"])

        def answer(*args):
            self.assertEqual(display.task("planner").fields["status"], "Awaiting input")
            display.live.stop.assert_called_once()
            return next(choices)

        with patch("builtins.input", side_effect=answer):
            self.assertEqual(display.ask_token_limit(request), 48000)
        self.assertEqual(display.task("planner").fields["status"], "Running")
        display.live.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
