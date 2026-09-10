import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anthropic
import httpx

from graph_agents import build_graph
from graph_agents.llm import get_llm
from main import run


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_WORKSPACE_ID": "",
            "ANTHROPIC_REVIEW_MODEL": "",
            "WORKSPACE_INSPECTION_LIMIT": "",
            "ANTHROPIC_PLANNING_MAX_TOKENS": "",
            "ANTHROPIC_DEVELOPMENT_MAX_TOKENS": "",
            "ANTHROPIC_REVIEW_MAX_TOKENS": "",
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
        self.forced_stop_reason = None
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
        if self.forced_stop_reason is not None:
            stop_reason = self.forced_stop_reason
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
        self.assertEqual([request["max_tokens"] for request in self.requests], [32768, 32768, 64000])
        self.assertEqual(review["thinking"], {"type": "adaptive"})
        self.assertEqual(review["output_config"], {"effort": "max"})
        self.assertEqual(review["max_tokens"], 64000)
        self.assertTrue(review["stream"])
        self.assertIn("staff-level software engineer", review["system"])
        self.assertEqual(result["review"], self.answer)
        self.assertNotIn("Internal reasoning", result["answer"])

    def test_graph_stream_reports_model_and_file_activity_without_content(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "main.py").write_text("PRIVATE SOURCE", encoding="utf-8")
            self.next_tool_call = {
                "type": "tool_use", "id": "read_call", "name": "read_workspace_file",
                "input": {"path": "main.py"},
            }
            events = list(build_graph().stream(
                {"query": "Implement pagination", "workspace": directory}, stream_mode="custom",
            ))
        self.assertEqual([event["agent"] for event in events if event["kind"] == "model_start"],
                         ["planner", "planner", "implement_agent", "review_agent"])
        self.assertEqual([request["max_tokens"] for request in self.requests], [32768, 32768, 32768, 64000])
        reads = [event for event in events if event["kind"] == "tool_end"]
        self.assertEqual(len(reads), 1)
        self.assertEqual((reads[0]["agent"], reads[0]["path"], reads[0]["success"]),
                         ("planner", "main.py", True))
        self.assertEqual(len([event for event in events if event["kind"] == "model_end"]), 4)
        self.assertIn("Thinking", json.dumps(events))
        for private_text in ("Internal reasoning", "PRIVATE SOURCE", self.answer):
            self.assertNotIn(private_text, json.dumps(events))

    def test_review_model_can_be_overridden_without_changing_development(self):
        with patch.dict(os.environ, {"ANTHROPIC_REVIEW_MODEL": " claude-opus-5 "}):
            get_llm("Developer", "Stub").invoke("Request")
            get_llm("Reviewer", "Stub", profile="review").invoke("Request")
        self.assertEqual([request["model"] for request in self.requests], [
            "claude-sonnet-5", "claude-opus-5",
        ])

    def test_specialists_save_files_and_reviewer_reads_the_result(self):
        for query in ("Implement an answer", "Fix the answer", "Refactor the answer", "Write tests"):
            with self.subTest(query=query), tempfile.TemporaryDirectory() as directory:
                self.requests.clear()
                Path(directory, "app.py").write_text("answer = 41\n", encoding="utf-8")
                tool_steps = {
                    1: ("read_workspace_file", {"path": "app.py"}),
                    2: ("edit_workspace_file", {"path": "app.py", "old_text": "41", "new_text": "42"}),
                    3: ("create_workspace_file", {"path": "tests/test_app.py", "content": "assert 42 == 42\n"}),
                    5: ("read_workspace_file", {"path": "app.py"}),
                }

                def respond(request, **kwargs):
                    index = len(self.requests)
                    if index in tool_steps:
                        name, arguments = tool_steps[index]
                        self.next_tool_call = {"type": "tool_use", "id": f"call_{index}", "name": name, "input": arguments}
                    return self.respond(request, **kwargs)

                with patch("httpx.Client.send", side_effect=respond), patch.dict(os.environ, {"WORKSPACE_INSPECTION_LIMIT": "1"}):
                    events = list(build_graph().stream({"query": query, "workspace": directory},
                                                      stream_mode=["custom", "values"]))
                result = [event for mode, event in events if mode == "values"][-1]
                self.assertEqual(Path(directory, "app.py").read_text(), "answer = 42\n")
                self.assertTrue(Path(directory, "tests/test_app.py").is_file())
                self.assertEqual(result["changed_files"], [
                    {"path": "app.py", "action": "modified"}, {"path": "tests/test_app.py", "action": "created"},
                ])
                changes = [event for mode, event in events if mode == "custom" and event["kind"] == "file_changed"]
                self.assertEqual([event["path"] for event in changes], ["app.py", "tests/test_app.py"])
                for index in (0, 5):
                    self.assertNotIn("edit_workspace_file", [tool["name"] for tool in self.requests[index]["tools"]])
                self.assertIn("edit_workspace_file", [tool["name"] for tool in self.requests[1]["tools"]])
                self.assertNotIn("read_workspace_file", [tool["name"] for tool in self.requests[2]["tools"]])
                self.assertIn("answer = 42", json.dumps(self.requests[6]["messages"]))

    def test_write_capabilities_cannot_be_enabled_for_planning_or_review(self):
        for profile in ("planning", "review"):
            with self.assertRaisesRegex(ValueError, "Only development specialists"):
                get_llm("System", "Stub", profile=profile, allow_writes=True)

    def test_exhaustion_uses_a_final_request_with_tools_disabled_for_all_profiles(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"WORKSPACE_INSPECTION_LIMIT": "1"}):
            Path(directory, "main.py").write_text("source fixture", encoding="utf-8")
            for profile in ("planning", "development", "review"):
                with self.subTest(profile=profile):
                    self.requests.clear()
                    self.next_tool_call = {
                        "type": "tool_use", "id": "read_call", "name": "read_workspace_file",
                        "input": {"path": "main.py"},
                    }
                    answer = get_llm("System", "Stub", workspace=directory, profile=profile).invoke("Review main.py")
                    self.assertEqual(len(self.requests), 2)
                    self.assertEqual(self.requests[-1]["tool_choice"], {"type": "none"})
                    content = self.requests[-1]["messages"][-1]["content"]
                    self.assertEqual(content[0]["type"], "tool_result")
                    self.assertIn("No inspections remain", content[-1]["text"])
                    self.assertIn(self.answer, answer)
                    self.assertIn("Inspection limit reached (1 calls)", answer)

    def test_missing_api_key_keeps_both_profiles_offline(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            for profile in ("planning", "development", "review"):
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

    def test_truncated_generation_retries_without_replaying_or_executing_partial_writes(self):
        def respond(request, **kwargs):
            index = len(self.requests)
            self.forced_stop_reason = "max_tokens" if index == 1 else None
            if index < 3:
                self.next_tool_call = {
                    "type": "tool_use", "id": f"write_{index}", "name": "create_workspace_file",
                    "input": {"path": ["kept.py", "discarded.py", "finished.py"][index], "content": "saved code\n"},
                }
            return self.respond(request, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch("httpx.Client.send", side_effect=respond):
            llm = get_llm("Implement code", "Stub", workspace=directory, allow_writes=True)
            answer = llm.invoke("Implement the feature")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["finished.py", "kept.py"])
            self.assertEqual([change["path"] for change in llm.changed_files], ["kept.py", "finished.py"])
        self.assertEqual(answer, self.answer)
        self.assertEqual([request["max_tokens"] for request in self.requests], [32768, 32768, 64000, 32768])
        retry_messages = json.dumps(self.requests[2]["messages"])
        self.assertIn("kept.py", retry_messages)
        self.assertNotIn("discarded.py", retry_messages)
        self.assertIn("create_workspace_file", [tool["name"] for tool in self.requests[2]["tools"]])

    def test_truncation_recovery_is_bounded_and_identifies_the_profile(self):
        self.stop_reason = "max_tokens"
        with self.assertRaisesRegex(RuntimeError, "development step.*truncated again.*one retry") as error:
            get_llm("System", "Stub").invoke("Implement code")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("ANTHROPIC_DEVELOPMENT_MAX_TOKENS", str(error.exception))

    def test_cli_approval_resumes_saved_work_and_applies_only_to_current_agent_task(self):
        def respond(request, **kwargs):
            index = len(self.requests)
            self.forced_stop_reason = "max_tokens" if index == 2 else None
            if index in (1, 2, 3):
                self.next_tool_call = {
                    "type": "tool_use", "id": f"write_{index}", "name": "create_workspace_file",
                    "input": {"path": {1: "kept.py", 2: "discarded.py", 3: "finished.py"}[index], "content": "code\n"},
                }
            return self.respond(request, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch("httpx.Client.send", side_effect=respond):
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="65536") as prompt:
                with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                    app = build_graph()
                    run(app, "Implement the feature", workspace=Path(directory))
                    run(app, "Implement another feature", workspace=Path(directory))
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["finished.py", "kept.py"])
        prompt.assert_called_once()
        self.assertEqual([request["max_tokens"] for request in self.requests],
                         [32768, 32768, 32768, 65536, 65536, 64000, 32768, 32768, 64000])
        self.assertNotIn("discarded.py", json.dumps(self.requests[3]["messages"]))
        self.assertIn("kept.py", json.dumps(self.requests[3]["messages"]))
        self.assertEqual(os.environ["ANTHROPIC_DEVELOPMENT_MAX_TOKENS"], "")

    def test_cli_can_ask_again_after_an_approved_limit_is_reached(self):
        def respond(request, **kwargs):
            self.forced_stop_reason = "max_tokens" if len(self.requests) in (1, 2) else None
            return self.respond(request, **kwargs)

        with patch("httpx.Client.send", side_effect=respond), patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", side_effect=["y", "96000"]) as prompt:
                with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                    run(build_graph(), "Implement code")
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual([request["max_tokens"] for request in self.requests], [32768, 32768, 65536, 96000, 64000])

    def test_cli_reprompts_when_provider_rejects_the_approved_budget(self):
        def respond(request, **kwargs):
            index = len(self.requests)
            self.forced_stop_reason = "max_tokens" if index == 1 else None
            self.api_error = index == 2
            self.api_error_message = "max_tokens must not exceed 64000"
            return self.respond(request, **kwargs)

        with patch("httpx.Client.send", side_effect=respond), patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", side_effect=["y", "64000"]) as prompt:
                with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as output:
                    run(build_graph(), "Implement code")
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("provider rejected", output.getvalue())
        self.assertEqual([request["max_tokens"] for request in self.requests], [32768, 32768, 65536, 64000, 64000])

    def test_cli_decline_or_eof_stops_without_starting_later_agents(self):
        self.stop_reason = "max_tokens"
        for choice in ("n", EOFError()):
            with self.subTest(choice=type(choice).__name__):
                self.requests.clear()
                with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=[choice]):
                    with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO):
                        run(build_graph(), "Implement code")
                self.assertEqual(len(self.requests), 1)
                self.assertIn("Task stopped", output.getvalue())
                self.assertNotIn("Request failed", output.getvalue())

    def test_ctrl_c_at_budget_prompt_releases_graph_worker(self):
        self.stop_reason = "max_tokens"
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=KeyboardInterrupt):
            with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(KeyboardInterrupt):
                    run(build_graph(), "Implement code")
        self.assertEqual(len(self.requests), 1)

    def test_retry_preserves_disabled_tools_after_inspection_exhaustion(self):
        def respond(request, **kwargs):
            index = len(self.requests)
            if index == 0:
                self.next_tool_call = {"type": "tool_use", "id": "read_1", "name": "list_workspace_files", "input": {}}
            self.forced_stop_reason = "max_tokens" if index == 1 else None
            return self.respond(request, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch("httpx.Client.send", side_effect=respond):
            with patch.dict(os.environ, {"WORKSPACE_INSPECTION_LIMIT": "1"}):
                answer = get_llm("System", "Stub", workspace=directory).invoke("Review code")
        self.assertIn(self.answer, answer)
        self.assertEqual(self.requests[2]["tool_choice"], {"type": "none"})

    def test_api_error_does_not_fall_back_to_a_different_model(self):
        self.api_error = True
        with self.assertRaises(anthropic.BadRequestError):
            get_llm("Reviewer", "Stub", profile="review").invoke("Request")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["model"], "claude-fable-5-1")

    def test_streamed_api_error_preserves_original_message_and_closes_response(self):
        responses = []

        def respond(request, **kwargs):
            payload = {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid streamed request"}}
            response = httpx.Response(
                200, request=request, headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(f"event: error\ndata: {json.dumps(payload)}\n\n".encode()),
            )
            responses.append(response)
            return response

        with patch("httpx.Client.send", side_effect=respond):
            with self.assertRaisesRegex(anthropic.APIStatusError, "Invalid streamed request"):
                get_llm("System", "Stub").invoke("Implement code")
        self.assertEqual(len(responses), 1)
        self.assertTrue(responses[0].is_closed)

    def test_stream_overload_retries_without_executing_partial_or_replaying_saved_writes(self):
        responses, events = [], []

        def respond(request, **kwargs):
            index = len(self.requests)
            if index < 3:
                self.next_tool_call = {
                    "type": "tool_use", "id": f"write_{index}", "name": "create_workspace_file",
                    "input": {"path": ["kept.py", "discarded.py", "finished.py"][index], "content": "saved code\n"},
                }
            fixture = self.respond(request, **kwargs)
            payload = fixture.content
            if index == 1:
                error = {"type": "error", "error": {"type": "overloaded_error", "message": "Temporary overload"}}
                payload = payload.split(b"event: message_delta")[0] + f"event: error\ndata: {json.dumps(error)}\n\n".encode()
            response = httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                      stream=httpx.ByteStream(payload))
            responses.append(response)
            return response

        with tempfile.TemporaryDirectory() as directory, patch("httpx.Client.send", side_effect=respond):
            with patch("graph_agents.llm.sleep") as delay, patch("graph_agents.llm.reporter", return_value=(
                lambda kind, **fields: events.append({"kind": kind, **fields})
            )):
                llm = get_llm("Implement code", "Stub", workspace=directory, allow_writes=True)
                answer = llm.invoke("Implement the feature")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["finished.py", "kept.py"])
            self.assertEqual([change["path"] for change in llm.changed_files], ["kept.py", "finished.py"])
        self.assertEqual(answer, self.answer)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.requests[1]["messages"], self.requests[2]["messages"])
        self.assertTrue(all(response.is_closed for response in responses))
        delay.assert_called_once_with(2)
        retries = [event for event in events if event["kind"] == "model_retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["reason"], "Provider overloaded_error")

    def test_repeated_stream_failures_stop_after_one_retry_and_keep_real_error(self):
        for error_type in ("overloaded_error", "api_error", "rate_limit_error", "timeout_error"):
            with self.subTest(error_type=error_type):
                responses = []

                def respond(request, **kwargs):
                    error = {"type": "error", "error": {"type": error_type, "message": "Provider unavailable"}}
                    response = httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                              stream=httpx.ByteStream(f"event: error\ndata: {json.dumps(error)}\n\n".encode()))
                    responses.append(response)
                    return response

                with patch("httpx.Client.send", side_effect=respond), patch("graph_agents.llm.sleep"):
                    with self.assertRaisesRegex(anthropic.APIStatusError, "Provider unavailable"):
                        get_llm("System", "Stub").invoke("Implement code")
                self.assertEqual(len(responses), 2)
                self.assertTrue(all(response.is_closed for response in responses))

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
