import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_agents.llm import StubLLM, get_llm


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"
        env_path = patch("graph_agents.llm.DOTENV_PATH", self.env_file)
        env_path.start()
        self.addCleanup(env_path.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_REVIEW_MODEL", "PYTHON_DOTENV_DISABLED"):
            os.environ.pop(key, None)
        # Never construct real API clients or send requests with test credentials.
        client = patch("langchain_anthropic.ChatAnthropic")
        self.client = client.start()
        self.addCleanup(client.stop)

    def test_file_supplies_one_key_to_both_model_profiles(self):
        # Windows editors may save UTF-8 with a byte-order mark.
        self.env_file.write_text('ANTHROPIC_API_KEY="file-test-key"\n', encoding="utf-8-sig")
        for profile in ("development", "review"):
            get_llm("System", "Stub", profile=profile)
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "file-test-key")
        self.assertEqual(self.client.call_count, 2)
        for call in self.client.call_args_list:
            self.assertEqual(call.kwargs["api_key"], "file-test-key")

    def test_existing_environment_takes_precedence(self):
        self.env_file.write_text(
            "ANTHROPIC_API_KEY=file-test-key\nANTHROPIC_WORKSPACE_ID=wrkspc_file\n", encoding="utf-8",
        )
        os.environ["ANTHROPIC_API_KEY"] = "session-test-key"
        os.environ["ANTHROPIC_WORKSPACE_ID"] = "wrkspc_session"
        get_llm("System", "Stub")
        self.assertEqual(self.client.call_args.kwargs["api_key"], "session-test-key")
        self.assertEqual(self.client.call_args.kwargs["default_headers"], {"anthropic-workspace-id": "wrkspc_session"})

    def test_file_supplies_account_workspace_header_to_both_profiles(self):
        self.env_file.write_text(
            'ANTHROPIC_API_KEY=file-test-key\nANTHROPIC_WORKSPACE_ID=" wrkspc_from_file "\n', encoding="utf-8",
        )
        for profile in ("development", "review"):
            get_llm("System", "Stub", profile=profile)
        self.assertEqual(self.client.call_count, 2)
        for call in self.client.call_args_list:
            self.assertEqual(call.kwargs["default_headers"], {"anthropic-workspace-id": "wrkspc_from_file"})

    def test_explicit_empty_environment_keeps_both_profiles_offline(self):
        self.env_file.write_text("ANTHROPIC_API_KEY=file-test-key\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = ""
        for profile in ("development", "review"):
            self.assertIsInstance(get_llm("System", "Stub", profile=profile), StubLLM)
        self.client.assert_not_called()

    def test_missing_or_empty_file_key_uses_stubs(self):
        for contents in (None, "# No key configured\n", "ANTHROPIC_API_KEY=\n"):
            with self.subTest(contents=contents):
                if contents is not None:
                    self.env_file.write_text(contents, encoding="utf-8")
                self.assertIsInstance(get_llm("System", "Stub"), StubLLM)
        self.client.assert_not_called()

    def test_launch_directory_does_not_select_an_unrelated_env_file(self):
        self.env_file.write_text("ANTHROPIC_API_KEY=project-test-key\n", encoding="utf-8")
        other_directory = self.env_file.parent / "other"
        other_directory.mkdir()
        (other_directory / ".env").write_text("ANTHROPIC_API_KEY=unrelated-test-key\n", encoding="utf-8")
        previous_directory = Path.cwd()
        try:
            os.chdir(other_directory)
            get_llm("System", "Stub")
        finally:
            os.chdir(previous_directory)
        self.assertEqual(self.client.call_args.kwargs["api_key"], "project-test-key")


if __name__ == "__main__":
    unittest.main()
