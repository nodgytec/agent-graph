import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from graph_agents import build_graph
from graph_agents.llm import StubLLM
from main import main, run


class InteractiveCLITests(unittest.TestCase):
    def session(self, inputs, arguments=(), side_effect=None):
        app = Mock()
        with (
            patch("sys.argv", ["main.py", *arguments]),
            patch("builtins.input", side_effect=inputs),
            patch("sys.stdout", new_callable=io.StringIO) as output,
            patch("main.build_graph", return_value=app) as build,
            patch("main.run", side_effect=side_effect) as requests,
        ):
            main()
        build.assert_called_once_with()
        return app, requests, output.getvalue()

    def test_no_arguments_opens_prompt_and_handles_multiple_requests(self):
        app, requests, output = self.session(["", "  ", "Implement pagination", "Review this code", "/exit"])
        self.assertEqual(requests.call_args_list, [
            call(app, "Implement pagination", "", None), call(app, "Review this code", "", None),
        ])
        self.assertIn("Development Agent CLI", output)
        self.assertIn("Goodbye.", output)

    def test_context_can_be_loaded_preserved_on_error_and_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source code.py"
            source.write_text("def greeting(): return 'hello'", encoding="utf-8-sig")
            missing = Path(directory) / "missing.py"
            app, requests, output = self.session([
                f'/context "{source}"', "Review the greeting",
                f"/context {missing}", "Write tests for the greeting",
                "/context", "/clear", "/context", "Implement pagination", "quit",
            ])
        self.assertEqual(requests.call_args_list, [
            call(app, "Review the greeting", "def greeting(): return 'hello'", None),
            call(app, "Write tests for the greeting", "def greeting(): return 'hello'", None),
            call(app, "Implement pagination", "", None),
        ])
        self.assertIn("Cannot load context file:", output)
        self.assertIn("Context cleared.", output)
        self.assertIn("Context: (none)", output)

    def test_context_file_argument_preloads_interactive_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes.md"
            source.write_text("Preserve the public API.", encoding="utf-8")
            app, requests, output = self.session(
                ["Refactor the API", "/exit"], ["--context-file", str(source)],
            )
        requests.assert_called_once_with(app, "Refactor the API", "Preserve the public API.", None)
        self.assertIn(str(source), output)

    def test_failed_or_cancelled_request_returns_to_prompt(self):
        for error, message in (
            (RuntimeError("Network unavailable"), "Request failed: Network unavailable"),
            (KeyboardInterrupt(), "Request cancelled."),
        ):
            with self.subTest(error=type(error).__name__):
                _, requests, output = self.session(
                    ["Review first task", "Review second task", "exit"], side_effect=[error, None],
                )
                self.assertEqual(requests.call_count, 2)
                self.assertIn(message, output)
                self.assertIn("Goodbye.", output)

    def test_help_unknown_commands_and_exit_do_not_submit_tasks(self):
        for exit_command in ("/exit", "/quit", "exit", "quit"):
            with self.subTest(exit_command=exit_command):
                _, requests, output = self.session(["/help", "/unknown", exit_command])
                requests.assert_not_called()
                self.assertIn("/context PATH", output)
                self.assertIn("Unknown command.", output)

    def test_eof_or_interrupt_at_prompt_exits_cleanly(self):
        for interrupt in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interrupt=type(interrupt).__name__):
                _, requests, output = self.session([interrupt])
                requests.assert_not_called()
                self.assertIn("Goodbye.", output)

    def test_workspace_argument_preloads_folder_and_relative_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "notes.md").write_text("Project context", encoding="utf-8")
            app, requests, output = self.session(
                ["Review this codebase", "/exit"],
                ["--workspace", str(root), "--context-file", "notes.md"],
            )
        requests.assert_called_once_with(app, "Review this codebase", "Project context", root)
        self.assertIn(f"Workspace: {root}", output)

    def test_workspace_switch_clears_context_and_failed_switch_preserves_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            first, second = base / "first project", base / "second project"
            first.mkdir()
            second.mkdir()
            (first / "notes.md").write_text("First context", encoding="utf-8")
            app, requests, output = self.session([
                f'/workspace "{first}"', "/context notes.md", "Review first",
                "/workspace missing", "Review first again",
                '/workspace "../second project"', "/workspace", "Review second", "/exit",
            ])
        self.assertEqual(requests.call_args_list, [
            call(app, "Review first", "First context", first),
            call(app, "Review first again", "First context", first),
            call(app, "Review second", "", second),
        ])
        self.assertIn("Cannot select workspace:", output)
        self.assertIn(f"Workspace: {second}", output)

    def test_invalid_workspace_is_rejected_before_building_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.py"
            source.touch()
            for path in ("", str(source), str(source.parent / "missing")):
                with (
                    self.subTest(path=path), patch("sys.argv", ["main.py", "--workspace", path]),
                    patch("sys.stderr", new_callable=io.StringIO), patch("main.build_graph") as build,
                ):
                    with self.assertRaises(SystemExit) as error:
                        main()
                    self.assertEqual(error.exception.code, 2)
                    build.assert_not_called()

    def test_single_request_passes_workspace_through_graph_to_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch("sys.argv", ["main.py", "Review this codebase", "--workspace", str(root)]),
                patch("sys.stdout", new_callable=io.StringIO) as output,
                patch("graph_agents.agents.get_llm", return_value=StubLLM("Workspace review")) as factory,
            ):
                main()
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["workspace"], str(root))
        self.assertEqual(factory.call_args.kwargs["profile"], "review")
        self.assertIn("Answer: Workspace review", output.getvalue())

    def test_real_graph_streams_progress_and_starts_each_request_fresh(self):
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            app = build_graph()
            run(app, "Implement pagination")
            run(app, "Review the pagination code")
        text = output.getvalue()
        self.assertEqual(text.count("classify ->"), 2)
        self.assertEqual(text.count("planner ->"), 1)
        self.assertEqual(text.count("review_agent ->"), 2)
        self.assertEqual(text.count("Answer:"), 2)
        self.assertLess(text.index("Working..."), text.index("classify ->"))
        second_request = text.split("Development request: Review the pagination code")[1]
        self.assertNotIn("[Stub implementation agent]", second_request)


if __name__ == "__main__":
    unittest.main()
