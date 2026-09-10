import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_agents import build_graph
from graph_agents.agents import classify_node
from graph_agents.llm import StubLLM
from main import EXAMPLE_QUERIES, main


class DevelopmentGraphTests(unittest.TestCase):
    def setUp(self):
        # Tests must remain offline even when the developer has an API key.
        environment = patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.app = build_graph()

    def test_request_intent_selects_development_specialist(self):
        cases = {
            "Implement pagination for /api/items": "implement",
            "Build a code review dashboard": "implement",
            "Add error handling to the API": "implement",
            "Fix failing unit tests": "debug",
            "Can you please debug this traceback?": "debug",
            "The application crashes on startup": "debug",
            "How do I fix this function?": "debug",
            "Refactor tests without changing their behavior": "refactor",
            "Please simplify this function": "refactor",
            "Write regression tests for this bug": "test",
            "Write a unit test for error handling": "test",
            "Add unit tests for error handling": "test",
            "We need better test coverage": "test",
            "Review this refactor for bugs": "review",
            "Could you audit this authentication code?": "review",
            "Explain how this function works": "implement",
            "Design a contest results endpoint": "implement",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(classify_node({"query": query})["route"], expected)

    def test_all_examples_reach_review_and_produce_an_answer(self):
        for query, route in zip(EXAMPLE_QUERIES, ("implement", "debug", "refactor", "test", "review")):
            with self.subTest(route=route):
                result = self.app.invoke({"query": query})
                self.assertEqual(result["route"], route)
                nodes = [step.split(" -> ")[0] for step in result["steps"]]
                expected = ["classify", "review_agent"] if route == "review" else [
                    "classify", "planner", f"{route}_agent", "review_agent",
                ]
                self.assertEqual(nodes, expected)
                self.assertIn("[Stub staff engineer]", result["answer"])
                if route != "review":
                    self.assertIn(result["draft"], result["answer"])
                    self.assertIn(result["plan"], result["answer"])

    def test_context_and_artifacts_reach_the_next_agent(self):
        prompts = []
        responses = iter(("Acceptance criteria and plan", "Proposed implementation", "Review findings"))

        def fake_llm(**kwargs):
            response = next(responses)

            class RecordingLLM:
                def invoke(self, prompt):
                    prompts.append(prompt)
                    return response

            return RecordingLLM()

        context = "def total(items): return sum(items)"
        with patch("graph_agents.agents.get_llm", side_effect=fake_llm):
            result = self.app.invoke({"query": "Refactor total", "context": context})

        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIn(context, prompt)
            self.assertIn("Refactor total", prompt)
        self.assertIn("Acceptance criteria and plan", prompts[1])
        self.assertIn("Acceptance criteria and plan", prompts[2])
        self.assertIn("Proposed implementation", prompts[2])
        for artifact in ("Acceptance criteria and plan", "Proposed implementation", "Review findings"):
            self.assertIn(artifact, result["answer"])

    def test_review_only_uses_supplied_code_and_discards_old_artifacts(self):
        prompts = []

        class Reviewer:
            def invoke(self, prompt):
                prompts.append(prompt)
                return "Guard against an empty list."

        with patch("graph_agents.agents.get_llm", return_value=Reviewer()) as factory:
            result = self.app.invoke({
                "query": "Review this function",
                "context": "def average(values): return sum(values) / len(values)",
                "plan": "STALE PLAN",
                "draft": "STALE DRAFT",
                "answer": "STALE ANSWER",
            })

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.kwargs["profile"], "review")
        self.assertIn("def average(values)", prompts[0])
        self.assertNotIn("STALE", prompts[0])
        self.assertEqual(result["plan"], "")
        self.assertEqual(result["draft"], "")
        self.assertEqual(result["answer"], "Guard against an empty list.")

    def test_empty_request_is_rejected_before_model_use(self):
        with patch("graph_agents.agents.get_llm") as factory:
            with self.assertRaisesRegex(ValueError, "non-empty development request"):
                self.app.invoke({"query": "  "})
        factory.assert_not_called()

    def test_cli_passes_context_file_to_the_review_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "example.py"
            source.write_text("def greeting(): return 'hello'", encoding="utf-8")
            with (
                patch("sys.argv", ["main.py", "Review this code", "--context-file", str(source)]),
                patch("sys.stdout", new_callable=io.StringIO) as output,
                patch("graph_agents.agents.get_llm", return_value=StubLLM("Review result")) as factory,
                patch.object(StubLLM, "invoke", return_value="Review result") as invoke,
            ):
                main()
            self.assertEqual(factory.call_count, 1)
            self.assertIn("def greeting()", invoke.call_args.args[0])
            self.assertIn("review_agent -> produced staff engineer review", output.getvalue())
            self.assertIn("Answer: Review result", output.getvalue())

    def test_cli_without_arguments_runs_all_examples(self):
        with (
            patch("sys.argv", ["main.py"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            main()
        for query in EXAMPLE_QUERIES:
            self.assertIn(query, output.getvalue())
        self.assertEqual(output.getvalue().count("Graph trace:"), 5)
        self.assertEqual(output.getvalue().count("review_agent -> produced staff engineer review"), 5)

    def test_cli_rejects_invalid_input_before_building_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.py")
            for arguments in ([" "], ["--context-file", missing], ["Review this", "--context-file", missing]):
                with (
                    self.subTest(arguments=arguments),
                    patch("sys.argv", ["main.py", *arguments]),
                    patch("sys.stderr", new_callable=io.StringIO),
                    patch("main.build_graph") as build,
                ):
                    with self.assertRaises(SystemExit) as error:
                        main()
                    self.assertEqual(error.exception.code, 2)
                    build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
