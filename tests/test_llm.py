import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anthropic
import httpx

from graph_agents import build_graph
from graph_agents.llm import get_llm


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_WORKSPACE_ID": "",
            "ANTHROPIC_REVIEW_MODEL": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        self.workspace_headers = []
        self.answer = "Review findings."
        self.stop_reason = "end_turn"
        self.api_error = False
        self.api_error_message = "Model unavailable"
        self.next_tool_call = None
        # Intercept HTTP, exercising the real LangChain/Anthropic request and
        # streaming response adapters without network access or paid inference.
        transport = patch("httpx.Client.send", side_effect=self.respond)
        transport.start()
        self.addCleanup(transport.stop)

    def respond(self, request, **kwargs):
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.workspace_headers.append(request.headers.get("anthropic-workspace-id"))
        if self.api_error:
            return httpx.Response(400, request=request, json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": self.api_error_message},
            })
        message = {
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": payload["model"], "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
        answer_blocks = [{"type": "text", "text": self.answer}] if self.answer else []
        stop_reason = self.stop_reason
        if self.next_tool_call is not None:
            answer_blocks = [self.next_tool_call]
            self.next_tool_call = None
            stop_reason = "tool_use"
        if not payload.get("stream"):
            message.update(content=answer_blocks, stop_reason=stop_reason)
            return httpx.Response(200, request=request, json=message)

        events = [{"type": "message_start", "message": message}]
        blocks = [{"type": "thinking", "thinking": "Internal reasoning", "signature": "test-signature"}]
        blocks.extend(answer_blocks)
        for index, block in enumerate(blocks):
            field = block["type"]
            if field == "tool_use":
                initial = {**block, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                initial = {**block, field: ""}
                delta = {"type": f"{field}_delta", field: block[field]}
            events.extend([
                {"type": "content_block_start", "index": index, "content_block": initial},
                {"type": "content_block_delta", "index": index,
                 "delta": delta},
                {"type": "content_block_stop", "index": index},
            ])
        events.extend([
            {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
             "usage": {"output_tokens": 100}},
            {"type": "message_stop"},
        ])
        stream = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, text=stream)

    def test_graph_uses_strongest_profile_only_for_staff_review(self):
        result = build_graph().invoke({"query": "Implement pagination"})
        self.assertEqual([request["model"] for request in self.requests], [
            "claude-sonnet-5", "claude-sonnet-5", "claude-fable-5-1",
        ])
        review = self.requests[-1]
        self.assertEqual(review["thinking"], {"type": "adaptive"})
        self.assertEqual(review["output_config"], {"effort": "max"})
        self.assertEqual(review["max_tokens"], 64000)
        self.assertTrue(review["stream"])
        self.assertIn("staff-level software engineer", review["system"])
        self.assertEqual(result["review"], self.answer)
        self.assertNotIn("Internal reasoning", result["answer"])

    def test_review_model_can_be_overridden_without_changing_development(self):
        with patch.dict(os.environ, {"ANTHROPIC_REVIEW_MODEL": " claude-opus-5 "}):
            get_llm("Developer", "Stub").invoke("Request")
            get_llm("Reviewer", "Stub", profile="review").invoke("Request")
        self.assertEqual([request["model"] for request in self.requests], [
            "claude-sonnet-5", "claude-opus-5",
        ])

    def test_missing_api_key_keeps_both_profiles_offline(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            for profile in ("development", "review"):
                self.assertEqual(get_llm("System", "Offline stub", profile=profile).invoke("Request"), "Offline stub")
        self.assertEqual(self.requests, [])

    def test_incomplete_review_is_not_returned_as_success(self):
        for answer, stop_reason, error in (
            ("Partial review", "max_tokens", "truncated"),
            ("", "end_turn", "no answer text"),
        ):
            with self.subTest(stop_reason=stop_reason):
                self.answer, self.stop_reason = answer, stop_reason
                with self.assertRaisesRegex(RuntimeError, error):
                    get_llm("Reviewer", "Stub", profile="review").invoke("Request")

    def test_api_error_does_not_fall_back_to_a_different_model(self):
        self.api_error = True
        with self.assertRaises(anthropic.BadRequestError):
            get_llm("Reviewer", "Stub", profile="review").invoke("Request")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["model"], "claude-fable-5-1")

    def test_account_workspace_header_reaches_all_agents_with_a_local_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": " wrkspc_account_test "}):
                build_graph().invoke({"query": "Implement pagination", "workspace": directory})
        self.assertEqual(self.workspace_headers, ["wrkspc_account_test"] * 3)

    def test_blank_account_workspace_omits_header_for_scoped_keys(self):
        for value in ("", "  "):
            with patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": value}):
                for profile in ("development", "review"):
                    get_llm("System", "Stub", profile=profile).invoke("Request")
        self.assertEqual(self.workspace_headers, [None] * 4)

    def test_missing_workspace_header_error_explains_the_required_setting(self):
        self.api_error = True
        self.api_error_message = "This API key is not scoped to a workspace; include the anthropic-workspace-id header."
        with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_WORKSPACE_ID") as error:
            get_llm("System", "Stub").invoke("Request")
        self.assertIn("local /workspace folder is a separate setting", str(error.exception))

    def test_workspace_tools_round_trip_through_both_provider_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "main.py").write_text("def answer(): return 42\n", encoding="utf-8")
            for profile in ("development", "review"):
                with self.subTest(profile=profile):
                    self.requests.clear()
                    self.workspace_headers.clear()
                    self.next_tool_call = {
                        "type": "tool_use", "id": "read_call", "name": "read_workspace_file",
                        "input": {"path": "main.py"},
                    }
                    with patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": "wrkspc_tool_test"}):
                        answer = get_llm("Inspect the code", "Stub", profile=profile, workspace=directory).invoke("Review main.py")
                    self.assertEqual(answer, self.answer)
                    self.assertEqual(len(self.requests), 2)
                    self.assertEqual(self.workspace_headers, ["wrkspc_tool_test", "wrkspc_tool_test"])
                    self.assertEqual([tool["name"] for tool in self.requests[0]["tools"]], [
                        "list_workspace_files", "read_workspace_file",
                    ])
                    result = self.requests[1]["messages"][-1]["content"][0]
                    self.assertEqual(result["type"], "tool_result")
                    self.assertEqual(result["tool_use_id"], "read_call")
                    self.assertIn("1: def answer(): return 42", json.dumps(result["content"]))


if __name__ == "__main__":
    unittest.main()
