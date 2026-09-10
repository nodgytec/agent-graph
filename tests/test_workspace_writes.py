import io
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from graph_agents.terminal import AgentProgress
from graph_agents.workspace import MAX_FILE_BYTES, Workspace


class WorkspaceWritesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = Workspace(self.root, allow_writes=True)

    def test_create_and_edit_save_content_and_record_each_path_once(self):
        self.workspace.create_file("src/app.py", "answer = 41\n")
        self.workspace.edit_file("src/app.py", "41", "42")
        self.assertEqual((self.root / "src/app.py").read_text(), "answer = 42\n")
        self.assertEqual(self.workspace.changed_files, [{"path": "src/app.py", "action": "created"}])
        with self.assertRaises(FileExistsError):
            self.workspace.create_file("src/app.py", "overwrite")
        self.assertEqual((self.root / "src/app.py").read_text(), "answer = 42\n")

    def test_existing_files_require_a_read_and_reject_stale_or_ambiguous_edits(self):
        source = self.root / "app.py"
        source.write_text("same\nsame\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Read this file"):
            self.workspace.edit_file("app.py", "same", "new")
        self.workspace.read_file("app.py")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.workspace.edit_file("app.py", "same", "new")
        source.write_text("external change\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.workspace.edit_file("app.py", "external", "agent")
        self.assertEqual(source.read_text(), "external change\n")
        self.assertEqual(self.workspace.changed_files, [])

    def test_edits_preserve_bom_crlf_and_unrelated_text(self):
        source = self.root / "app.py"
        source.write_bytes(b"\xef\xbb\xbfhead\r\nold\r\ntail\r\n")
        self.workspace.read_file("app.py")
        self.workspace.edit_file("app.py", "head\nold", "head\nnew")
        self.assertEqual(source.read_bytes(), b"\xef\xbb\xbfhead\r\nnew\r\ntail\r\n")
        source.write_bytes(b"a\r\nb\nc\r\n")
        self.workspace.read_file("app.py")
        self.workspace.edit_file("app.py", "b", "changed")
        self.assertEqual(source.read_bytes(), b"a\r\nchanged\nc\r\n")

    def test_noop_edit_does_not_report_a_change(self):
        (self.root / "app.py").write_text("content", encoding="utf-8")
        self.workspace.read_file("app.py")
        self.assertFalse(self.workspace.edit_file("app.py", "content", "content")["changed"])
        self.assertEqual(self.workspace.changed_files, [])

    def test_readonly_access_and_protected_or_outside_paths_cannot_write(self):
        readonly = Workspace(self.root)
        with self.assertRaisesRegex(ValueError, "read-only"):
            readonly.create_file("app.py", "code")
        with self.assertRaisesRegex(ValueError, "Unknown workspace tool"):
            readonly.execute("create_workspace_file", {"path": "app.py", "content": "code"})
        for path in ("../outside.py", str(self.root.parent / "outside.py"), ".env", ".git/config",
                     "node_modules/app.js", "nested/../../outside.py", "app.py:stream"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.create_file(path, "code")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_binary_and_oversized_writes_are_rejected(self):
        for content in ("bad\x00text", "x" * (MAX_FILE_BYTES + 1)):
            with self.assertRaises(ValueError):
                self.workspace.create_file("app.py", content)
        self.assertFalse((self.root / "app.py").exists())

    def test_symlink_cannot_be_used_to_write_outside_workspace(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "alias").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"Symlink creation is unavailable: {error}")
            with self.assertRaises(ValueError):
                self.workspace.create_file("alias/outside.py", "code")
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_saved_files_are_reported_even_when_later_work_fails(self):
        output = io.StringIO()
        with self.assertRaisesRegex(RuntimeError, "Review failed"):
            with AgentProgress("Implement a feature", self.root,
                               console=Console(file=output, force_terminal=False)) as display:
                display.set_route("implement")
                display.handle({"kind": "file_changed", "agent": "implement_agent",
                                "path": "src/app.py", "action": "created"})
                raise RuntimeError("Review failed")
        self.assertIn("created: src/app.py", output.getvalue())
        self.assertIn("remain saved", output.getvalue())


if __name__ == "__main__":
    unittest.main()
