import json
import os
import unittest
from unittest.mock import patch

import anthropic
import httpx

from graph_agents import build_graph
from graph_agents.llm import get_llm


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_REVIEW_MODEL": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        self.answer = "Review findings."
        self.stop_reason = "end_turn"
        self.api_error = False
        # Intercept HTTP, exercising the real LangChain/Anthropic request and
        # streaming response adapters without network access or paid inference.
        transport = patch("httpx.Client.send", side_effect=self.respond)
        transport.start()
        self.addCleanup(transport.stop)

    def respond(self, request, **kwargs):
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.api_error:
            return httpx.Response(400, request=request, json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Model unavailable"},
            })
        message = {
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": payload["model"], "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
        if not payload.get("stream"):
            message.update(content=[{"type": "text", "text": self.answer}], stop_reason=self.stop_reason)
            return httpx.Response(200, request=request, json=message)

        events = [{"type": "message_start", "message": message}]
        blocks = [{"type": "thinking", "thinking": "Internal reasoning", "signature": "test-signature"}]
        if self.answer:
            blocks.append({"type": "text", "text": self.answer})
        for index, block in enumerate(blocks):
            field = block["type"]
            initial = {**block, field: ""}
            events.extend([
                {"type": "content_block_start", "index": index, "content_block": initial},
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": f"{field}_delta", field: block[field]}},
                {"type": "content_block_stop", "index": index},
            ])
        events.extend([
            {"type": "message_delta", "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
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


if __name__ == "__main__":
    unittest.main()
