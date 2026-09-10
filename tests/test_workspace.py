import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from graph_agents import build_graph
from graph_agents.llm import get_llm
from graph_agents.workspace import MAX_FILE_BYTES, MAX_READ_CHARS, Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "project"
        self.root.mkdir()
        self.workspace = Workspace(self.root)
        (self.root / "main.py").write_text("first line\nsecond line\nthird line\n", encoding="utf-8-sig")

    def test_directory_listing_and_numbered_source_ranges(self):
        (self.root / "src").mkdir()
        listing = self.workspace.list_files()
        self.assertEqual(listing["entries"], [
            {"path": "main.py", "type": "file"}, {"path": "src", "type": "directory"},
        ])
        result = self.workspace.read_file("main.py", start_line=2, end_line=3)
        self.assertEqual(result["content"], "2: second line\n3: third line")
        self.assertEqual(result["total_lines"], 3)

    def test_secrets_and_generated_directories_are_excluded(self):
        for name in (".env", ".env.local", "private.pem", "credentials.json"):
            (self.root / name).write_text("sensitive test fixture", encoding="utf-8")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "excluded"):
                self.workspace.read_file(name)
        for directory in (".git", ".venv", "node_modules"):
            (self.root / directory).mkdir()
            (self.root / directory / "file.py").write_text("excluded fixture", encoding="utf-8")
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, "excluded"):
                self.workspace.read_file(f"{directory}/file.py")
        self.assertEqual([entry["path"] for entry in self.workspace.list_files()["entries"]], ["main.py"])

    def test_parent_traversal_and_absolute_paths_cannot_read_outside_workspace(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside test fixture", encoding="utf-8")
        for path in ("../outside.txt", str(outside)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.read_file(path)
        with self.assertRaises(ValueError):
            self.workspace.list_files("..")

    def test_symlink_cannot_escape_root_or_expose_excluded_files(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside test fixture", encoding="utf-8")
        (self.root / ".env").write_text("secret test fixture", encoding="utf-8")
        try:
            (self.root / "outside.py").symlink_to(outside)
            (self.root / "alias.py").symlink_to(self.root / ".env")
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        for name in ("outside.py", "alias.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.workspace.read_file(name)
        self.assertEqual([entry["path"] for entry in self.workspace.list_files()["entries"]], ["main.py"])

    def test_binary_oversized_and_invalid_line_requests_are_rejected(self):
        (self.root / "binary.bin").write_bytes(b"hello\x00world")
        (self.root / "large.txt").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
        for path in ("binary.bin", "large.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.read_file(path)
        for start, end in ((0, 2), (2, 1), (1, 301), (True, 2)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.workspace.read_file("main.py", start, end)

    def test_listing_pagination_and_long_line_output_are_bounded(self):
        for index in range(201):
            (self.root / f"file{index:03}.py").touch()
        first = self.workspace.list_files()
        second = self.workspace.list_files(offset=first["next_offset"])
        self.assertEqual(len(first["entries"]), 200)
        self.assertEqual(len(second["entries"]), 2)
        self.assertIsNone(second["next_offset"])
        (self.root / "main.py").write_text("x" * (MAX_READ_CHARS + 1), encoding="utf-8")
        result = self.workspace.read_file("main.py")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), MAX_READ_CHARS)

    def test_invalid_workspace_is_rejected(self):
        for path in ("", self.root / "missing", self.root / "main.py"):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                Workspace(path)


class WorkspaceToolLoopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "main.py").write_text("def answer(): return 42\n", encoding="utf-8")
        environment = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_REVIEW_MODEL": ""})
        environment.start()
        self.addCleanup(environment.stop)
        factory = patch("langchain_anthropic.ChatAnthropic")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.model = self.factory.return_value.bind_tools.return_value

    def tool_call(self, name="read_workspace_file", arguments=None):
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": arguments if arguments is not None else {"path": "main.py"}, "id": "tool_1",
        }])

    def test_tool_results_reach_model_before_final_answer(self):
        messages_seen = []
        responses = iter((self.tool_call(), AIMessage(content="The function returns 42.")))

        def respond(messages):
            messages_seen.append(list(messages))
            return next(responses)

        self.model.invoke.side_effect = respond
        answer = get_llm("System", "Stub", workspace=self.root).invoke("Review main.py")
        self.assertEqual(answer, "The function returns 42.")
        result = messages_seen[1][-1]
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.tool_call_id, "tool_1")
        self.assertIn("1: def answer(): return 42", json.loads(result.content)["content"])

    def test_tool_failure_is_returned_to_model_for_recovery(self):
        self.model.invoke.side_effect = [
            self.tool_call(arguments={"path": "../missing.py"}), AIMessage(content="Please supply the file inside the workspace."),
        ]
        get_llm("System", "Stub", workspace=self.root).invoke("Review a file")
        result = self.model.invoke.call_args.args[0][-1]
        self.assertEqual(result.status, "error")
        self.assertIn("Workspace tool error:", result.content)

    def test_tool_budget_stops_repeated_requests(self):
        self.model.invoke.return_value = self.tool_call()
        with patch("graph_agents.llm.MAX_WORKSPACE_TOOL_CALLS", 2):
            with self.assertRaisesRegex(RuntimeError, "inspection limit"):
                get_llm("System", "Stub", workspace=self.root).invoke("Review this codebase")
        self.assertEqual(self.model.invoke.call_count, 3)

    def test_no_workspace_does_not_bind_tools(self):
        self.factory.return_value.invoke.return_value = AIMessage(content="Proposal")
        self.assertEqual(get_llm("System", "Stub").invoke("Implement a feature"), "Proposal")
        self.factory.return_value.bind_tools.assert_not_called()

    def test_workspace_reaches_planner_specialist_and_reviewer(self):
        self.model.invoke.return_value = AIMessage(content="Proposal based on workspace")
        result = build_graph().invoke({"query": "Implement a feature", "workspace": str(self.root)})
        self.assertEqual(result["workspace"], str(self.root))
        self.assertEqual(self.factory.return_value.bind_tools.call_count, 3)
        for invocation in self.model.invoke.call_args_list:
            self.assertIn(str(self.root), invocation.args[0][1][1])


if __name__ == "__main__":
    unittest.main()
