from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import APITimeoutError

from provider_openai import OpenAIResponsesProvider, ProviderBackgroundHandle


class FakeStream:
    def __init__(self, events, final_response) -> None:
        self._events = list(events)
        self._final_response = final_response

    def __iter__(self):
        return iter(self._events)

    def get_final_response(self):
        return self._final_response


class FakeStreamManager:
    def __init__(self, stream: FakeStream) -> None:
        self._stream = stream

    def __enter__(self):
        return self._stream

    def __exit__(self, exc_type, exc, exc_tb):
        return None


class FakeResponses:
    def __init__(self) -> None:
        self.create_calls = []
        self.retrieve_calls = []
        self.cancel_calls = []
        self.cancel_call_kwargs = []
        self._stream_manager = None
        self._retrieve_results = []
        self._create_results = []

    def stream(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._stream_manager

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._create_results.pop(0)

    def retrieve(self, response_id, **kwargs):
        self.retrieve_calls.append({"response_id": response_id, **kwargs})
        result = self._retrieve_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def cancel(self, response_id, **kwargs):
        self.cancel_calls.append(response_id)
        self.cancel_call_kwargs.append(kwargs)
        return SimpleNamespace(status="cancelled")


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


class ProviderTests(unittest.TestCase):
    def test_from_loaded_config_uses_resolved_env(self) -> None:
        responses = FakeResponses()
        loaded = SimpleNamespace(
            effective=SimpleNamespace(
                active_provider="openai",
                providers={
                    "openai": SimpleNamespace(
                        api_key_env="OPENAI_API_KEY",
                        base_url="https://api.openai.com/v1",
                    )
                },
            ),
            resolved_env={"OPENAI_API_KEY": "dotenv-token"},
        )

        with mock.patch("provider_openai.OpenAI") as mock_openai:
            OpenAIResponsesProvider.from_loaded_config(loaded)

        mock_openai.assert_called_once_with(
            base_url="https://api.openai.com/v1",
            api_key="dotenv-token",
        )

    def test_start_foreground_turn_collects_streamed_text(self) -> None:
        responses = FakeResponses()
        final_response = SimpleNamespace(
            id="resp_1",
            status="completed",
            output=[],
            output_text="hello world",
        )
        responses._stream_manager = FakeStreamManager(
            FakeStream(
                events=[
                    SimpleNamespace(type="response.output_text.delta", delta="hello "),
                    SimpleNamespace(type="response.output_text.delta", delta="world"),
                ],
                final_response=final_response,
            )
        )
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        deltas = []
        result = provider.start_foreground_turn(
            model="gpt-5.4",
            reasoning_effort="high",
            instructions="system",
            input_text="input",
            previous_response_id=None,
            tools=[],
            tool_executor=lambda name, args: "",
            event_callback=lambda event_type, data: deltas.append((event_type, data)),
        )

        self.assertEqual("resp_1", result.response_id)
        self.assertEqual("hello world", result.final_text)
        self.assertEqual({"effort": "high"}, responses.create_calls[0]["reasoning"])
        self.assertEqual(
            ["hello ", "world"],
            [payload["delta"] for event_type, payload in deltas if event_type == "output_delta"],
        )

    def test_background_start_and_poll_to_completion(self) -> None:
        responses = FakeResponses()
        responses._create_results = [SimpleNamespace(id="bg_1")]
        responses._retrieve_results = [
            SimpleNamespace(id="bg_1", status="in_progress", error=None, output=[]),
            SimpleNamespace(id="bg_1", status="completed", error=None, output=[], output_text="done"),
        ]
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        handle = provider.start_background_turn(
            model="gpt-5.4",
            reasoning_effort="low",
            instructions="system",
            input_text="input",
            previous_response_id=None,
            tools=[],
        )
        poll_one = provider.poll_background_turn(
            handle=handle,
            model="gpt-5.4",
            tools=[],
            tool_executor=lambda name, args: "",
        )
        poll_two = provider.poll_background_turn(
            handle=ProviderBackgroundHandle(response_id=poll_one.response_id),
            model="gpt-5.4",
            tools=[],
            tool_executor=lambda name, args: "",
        )

        self.assertEqual("bg_1", handle.response_id)
        self.assertEqual({"effort": "low"}, responses.create_calls[0]["reasoning"])
        self.assertEqual(30.0, responses.create_calls[0]["timeout"].read)
        self.assertEqual(30.0, responses.retrieve_calls[0]["timeout"].read)
        self.assertEqual("running", poll_one.status)
        self.assertEqual("completed", poll_two.status)
        self.assertEqual("done", poll_two.final_text)

    def test_background_poll_treats_transient_timeout_as_running_for_existing_handle(self) -> None:
        responses = FakeResponses()
        request = httpx.Request("GET", "https://api.openai.com/v1/responses/bg_timeout")
        responses._retrieve_results = [
            APITimeoutError(request=request),
            SimpleNamespace(id="bg_timeout", status="completed", error=None, output=[], output_text="done"),
        ]
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        poll_one = provider.poll_background_turn(
            handle=ProviderBackgroundHandle(response_id="bg_timeout"),
            model="gpt-5.4-mini",
            tools=[],
            tool_executor=lambda name, args: "",
        )
        poll_two = provider.poll_background_turn(
            handle=ProviderBackgroundHandle(response_id=poll_one.response_id),
            model="gpt-5.4-mini",
            tools=[],
            tool_executor=lambda name, args: "",
        )

        self.assertEqual("running", poll_one.status)
        self.assertIn("APITimeoutError", poll_one.failure_message or "")
        self.assertEqual("completed", poll_two.status)
        self.assertEqual("done", poll_two.final_text)
        self.assertEqual(30.0, responses.retrieve_calls[0]["timeout"].read)

    def test_background_poll_raises_after_repeated_transient_timeouts(self) -> None:
        responses = FakeResponses()
        request = httpx.Request("GET", "https://api.openai.com/v1/responses/bg_timeout")
        responses._retrieve_results = [
            APITimeoutError(request=request),
            APITimeoutError(request=request),
            APITimeoutError(request=request),
            APITimeoutError(request=request),
        ]
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        for _ in range(3):
            poll_result = provider.poll_background_turn(
                handle=ProviderBackgroundHandle(response_id="bg_timeout"),
                model="gpt-5.4-mini",
                tools=[],
                tool_executor=lambda name, args: "",
            )
            self.assertEqual("running", poll_result.status)

        with self.assertRaises(APITimeoutError):
            provider.poll_background_turn(
                handle=ProviderBackgroundHandle(response_id="bg_timeout"),
                model="gpt-5.4-mini",
                tools=[],
                tool_executor=lambda name, args: "",
            )

    def test_background_start_passes_prompt_cache_key_and_text_format(self) -> None:
        responses = FakeResponses()
        responses._create_results = [SimpleNamespace(id="bg_schema")]
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        provider.start_background_turn(
            model="gpt-5.4-mini",
            reasoning_effort="low",
            instructions="system",
            input_text="input",
            previous_response_id=None,
            tools=[],
            text_format={"type": "json_schema", "name": "x", "schema": {"type": "object"}},
            prompt_cache_key="cache-key",
        )

        self.assertEqual("cache-key", responses.create_calls[0]["prompt_cache_key"])
        self.assertEqual(
            {"format": {"type": "json_schema", "name": "x", "schema": {"type": "object"}}},
            responses.create_calls[0]["text"],
        )

    def test_cancel_background_turn_calls_responses_cancel(self) -> None:
        responses = FakeResponses()
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        status = provider.cancel_background_turn(ProviderBackgroundHandle(response_id="bg_123"))

        self.assertEqual("cancelled", status)
        self.assertEqual(["bg_123"], responses.cancel_calls)
        self.assertEqual(30.0, responses.cancel_call_kwargs[0]["timeout"].read)

    def test_background_usage_event_waits_for_terminal_poll(self) -> None:
        responses = FakeResponses()
        responses._create_results = [SimpleNamespace(id="bg_usage")]
        responses._retrieve_results = [
            SimpleNamespace(
                id="bg_usage",
                status="in_progress",
                error=None,
                output=[],
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=10,
                    total_tokens=110,
                    input_tokens_details=SimpleNamespace(cached_tokens=5),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=2),
                ),
            ),
            SimpleNamespace(
                id="bg_usage",
                status="completed",
                error=None,
                output=[],
                output_text="done",
                usage=SimpleNamespace(
                    input_tokens=600,
                    output_tokens=90,
                    total_tokens=690,
                    input_tokens_details=SimpleNamespace(cached_tokens=50),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=30),
                ),
            ),
        ]
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        events = []
        handle = provider.start_background_turn(
            model="gpt-5.4-mini",
            reasoning_effort="low",
            instructions="system",
            input_text="input",
            previous_response_id=None,
            tools=[],
        )
        poll_one = provider.poll_background_turn(
            handle=handle,
            model="gpt-5.4-mini",
            tools=[],
            tool_executor=lambda name, args: "",
            event_callback=lambda event_type, data: events.append((event_type, data)),
        )
        poll_two = provider.poll_background_turn(
            handle=ProviderBackgroundHandle(response_id=poll_one.response_id),
            model="gpt-5.4-mini",
            tools=[],
            tool_executor=lambda name, args: "",
            event_callback=lambda event_type, data: events.append((event_type, data)),
        )

        usage_events = [payload for event_type, payload in events if event_type == "provider_usage"]
        self.assertEqual("running", poll_one.status)
        self.assertEqual("completed", poll_two.status)
        self.assertEqual(1, len(usage_events))
        self.assertEqual(600, usage_events[0]["input_tokens"])
        self.assertEqual(90, usage_events[0]["output_tokens"])
        self.assertEqual(690, usage_events[0]["total_tokens"])

    def test_provider_usage_events_include_token_fields(self) -> None:
        responses = FakeResponses()
        final_response = SimpleNamespace(
            id="resp_usage_1",
            status="completed",
            model="gpt-5.4-mini",
            output=[],
            output_text="done",
            usage=SimpleNamespace(
                input_tokens=1200,
                output_tokens=200,
                total_tokens=1400,
                input_tokens_details=SimpleNamespace(cached_tokens=300),
                output_tokens_details=SimpleNamespace(reasoning_tokens=90),
            ),
        )
        responses._stream_manager = FakeStreamManager(
            FakeStream(events=[], final_response=final_response)
        )
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(responses),
        )

        events = []
        provider.start_foreground_turn(
            model="gpt-5.4-mini",
            reasoning_effort=None,
            instructions="system",
            input_text="input",
            previous_response_id=None,
            tools=[],
            tool_executor=lambda name, args: "",
            event_callback=lambda event_type, data: events.append((event_type, data)),
        )

        usage_events = [payload for event_type, payload in events if event_type == "provider_usage"]
        self.assertEqual(1, len(usage_events))
        usage = usage_events[0]
        self.assertEqual("resp_usage_1", usage["response_id"])
        self.assertEqual("gpt-5.4-mini", usage["model"])
        self.assertEqual(1200, usage["input_tokens"])
        self.assertEqual(200, usage["output_tokens"])
        self.assertEqual(1400, usage["total_tokens"])
        self.assertEqual(300, usage["cached_input_tokens"])
        self.assertEqual(90, usage["reasoning_output_tokens"])

    def test_list_model_ids_returns_sorted_model_ids(self) -> None:
        responses = FakeResponses()
        responses_models = SimpleNamespace(
            list=lambda: SimpleNamespace(
                data=[
                    SimpleNamespace(id="gpt-5.4-mini"),
                    SimpleNamespace(id="gpt-5.4"),
                    SimpleNamespace(id="gpt-5.4-mini"),
                ]
            )
        )
        client = FakeClient(responses)
        client.models = responses_models
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=client,
        )

        model_ids = provider.list_model_ids()

        self.assertEqual(("gpt-5.4", "gpt-5.4-mini"), model_ids)

    def test_classify_provider_failure_normalizes_exception_and_failed_response(self) -> None:
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(FakeResponses()),
        )

        self.assertIn("RuntimeError", provider.classify_provider_failure(RuntimeError("boom")))
        self.assertEqual(
            "provider returned status=failed",
            provider.classify_provider_failure(SimpleNamespace(status="failed", error=None)),
        )

    def test_tool_execution_errors_are_returned_to_the_model(self) -> None:
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(FakeResponses()),
        )
        outputs, traces = provider._execute_tool_calls(
            function_calls=[
                SimpleNamespace(
                    call_id="call_1",
                    name="read_file",
                    arguments='{"path":"missing.txt"}',
                )
            ],
            tool_executor=lambda name, args: (_ for _ in ()).throw(RuntimeError("out of scope")),
            response_id="resp_1",
        )

        self.assertEqual("call_1", outputs[0]["call_id"])
        self.assertIn('"ok": false', outputs[0]["output"])
        self.assertIn('"tool_name": "read_file"', outputs[0]["output"])
        self.assertIn("out of scope", outputs[0]["output"])
        self.assertEqual("read_file", traces[0].name)

    def test_invalid_tool_arguments_are_returned_to_the_model(self) -> None:
        provider = OpenAIResponsesProvider(
            base_url="https://api.openai.com/v1",
            api_key="token",
            client=FakeClient(FakeResponses()),
        )
        outputs, traces = provider._execute_tool_calls(
            function_calls=[
                SimpleNamespace(
                    call_id="call_1",
                    name="read_file",
                    arguments="{broken",
                )
            ],
            tool_executor=lambda name, args: "should not run",
            response_id="resp_1",
        )

        self.assertEqual("call_1", outputs[0]["call_id"])
        self.assertIn('"ok": false', outputs[0]["output"])
        self.assertIn("JSONDecodeError", outputs[0]["output"])
        self.assertEqual({}, traces[0].arguments)


if __name__ == "__main__":
    unittest.main()
