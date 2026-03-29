from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from awdit.provider_openai import OpenAIResponsesProvider, ProviderBackgroundHandle


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
        self._stream_manager = None
        self._retrieve_results = []
        self._create_results = []

    def stream(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._stream_manager

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._create_results.pop(0)

    def retrieve(self, response_id):
        self.retrieve_calls.append(response_id)
        return self._retrieve_results.pop(0)


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

        with mock.patch("awdit.provider_openai.OpenAI") as mock_openai:
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
            instructions="system",
            input_text="input",
            previous_response_id=None,
            tools=[],
            tool_executor=lambda name, args: "",
            event_callback=lambda event_type, data: deltas.append((event_type, data)),
        )

        self.assertEqual("resp_1", result.response_id)
        self.assertEqual("hello world", result.final_text)
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
        self.assertEqual("running", poll_one.status)
        self.assertEqual("completed", poll_two.status)
        self.assertEqual("done", poll_two.final_text)

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


if __name__ == "__main__":
    unittest.main()
