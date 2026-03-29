from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from openai import OpenAI


ToolExecutor = Callable[[str, dict[str, Any]], str]
ProviderEventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ToolTraceRecord:
    call_id: str
    name: str
    arguments: dict[str, Any]
    output: str
    response_id: str


@dataclass(frozen=True)
class ProviderTurnResult:
    response_id: str
    final_text: str
    tool_traces: tuple[ToolTraceRecord, ...]
    status: str
    model: str


@dataclass(frozen=True)
class ProviderBackgroundHandle:
    response_id: str


@dataclass(frozen=True)
class BackgroundPollResult:
    status: str
    response_id: str
    final_text: str
    tool_traces: tuple[ToolTraceRecord, ...]
    failure_message: str | None = None


class OpenAIResponsesProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: OpenAI | None = None,
    ) -> None:
        self._client = client or OpenAI(base_url=base_url, api_key=api_key)

    @classmethod
    def from_loaded_config(cls, loaded) -> OpenAIResponsesProvider:
        provider_name = loaded.effective.active_provider
        provider_config = loaded.effective.providers[provider_name]
        api_key = loaded.resolved_env.get(provider_config.api_key_env) or os.environ.get(
            provider_config.api_key_env
        )
        if not api_key:
            raise RuntimeError(
                f"Missing provider env var {provider_config.api_key_env!r} for runtime startup."
            )
        return cls(base_url=provider_config.base_url, api_key=api_key)

    def start_foreground_turn(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        previous_response_id: str | None,
        tools: Iterable[dict[str, Any]],
        tool_executor: ToolExecutor,
        event_callback: ProviderEventCallback | None = None,
    ) -> ProviderTurnResult:
        current_input: Any = input_text
        current_instructions: str | None = instructions
        current_previous_response_id = previous_response_id
        tool_traces: list[ToolTraceRecord] = []

        while True:
            stream_text_chunks: list[str] = []
            with self._client.responses.stream(
                model=model,
                instructions=current_instructions,
                input=current_input,
                previous_response_id=current_previous_response_id,
                tools=list(tools),
                store=True,
            ) as stream:
                for event in stream:
                    event_type = getattr(event, "type", "")
                    delta = getattr(event, "delta", None)
                    if event_type == "response.output_text.delta" and isinstance(delta, str):
                        stream_text_chunks.append(delta)
                        if event_callback is not None:
                            event_callback("output_delta", {"delta": delta})
                response = stream.get_final_response()

            response_id = getattr(response, "id", "")
            function_calls = self._extract_function_calls(response)
            if not function_calls:
                final_text = self._response_text(response, "".join(stream_text_chunks))
                return ProviderTurnResult(
                    response_id=response_id,
                    final_text=final_text,
                    tool_traces=tuple(tool_traces),
                    status=str(getattr(response, "status", "completed")),
                    model=model,
                )

            if event_callback is not None:
                event_callback(
                    "tool_calls_requested",
                    {"count": len(function_calls), "response_id": response_id},
                )

            tool_outputs, new_traces = self._execute_tool_calls(
                function_calls=function_calls,
                tool_executor=tool_executor,
                response_id=response_id,
            )
            tool_traces.extend(new_traces)
            current_input = tool_outputs
            current_instructions = None
            current_previous_response_id = response_id

    def list_model_ids(self) -> tuple[str, ...]:
        response = self._client.models.list()
        items = getattr(response, "data", response)
        model_ids = sorted(
            {
                str(getattr(item, "id", "")).strip()
                for item in items
                if str(getattr(item, "id", "")).strip()
            }
        )
        return tuple(model_ids)

    def start_background_turn(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        previous_response_id: str | None,
        tools: Iterable[dict[str, Any]],
    ) -> ProviderBackgroundHandle:
        response = self._client.responses.create(
            model=model,
            instructions=instructions,
            input=input_text,
            previous_response_id=previous_response_id,
            tools=list(tools),
            background=True,
            store=True,
        )
        return ProviderBackgroundHandle(response_id=getattr(response, "id", ""))

    def poll_background_turn(
        self,
        *,
        handle: ProviderBackgroundHandle,
        model: str,
        tools: Iterable[dict[str, Any]],
        tool_executor: ToolExecutor,
        event_callback: ProviderEventCallback | None = None,
    ) -> BackgroundPollResult:
        response = self._client.responses.retrieve(handle.response_id)
        failure_message = self.classify_provider_failure(response)
        if failure_message is not None:
            return BackgroundPollResult(
                status="failed",
                response_id=handle.response_id,
                final_text="",
                tool_traces=(),
                failure_message=failure_message,
            )

        status = str(getattr(response, "status", "unknown"))
        if status in {"queued", "in_progress"}:
            return BackgroundPollResult(
                status="running",
                response_id=handle.response_id,
                final_text="",
                tool_traces=(),
            )

        response_id = getattr(response, "id", handle.response_id)
        function_calls = self._extract_function_calls(response)
        if function_calls:
            if event_callback is not None:
                event_callback(
                    "tool_calls_requested",
                    {"count": len(function_calls), "response_id": response_id},
                )
            tool_outputs, new_traces = self._execute_tool_calls(
                function_calls=function_calls,
                tool_executor=tool_executor,
                response_id=response_id,
            )
            continuation = self._client.responses.create(
                model=model,
                previous_response_id=response_id,
                input=tool_outputs,
                tools=list(tools),
                background=True,
                store=True,
            )
            return BackgroundPollResult(
                status="running",
                response_id=getattr(continuation, "id", response_id),
                final_text="",
                tool_traces=tuple(new_traces),
            )

        final_text = self._response_text(response, "")
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=final_text,
            tool_traces=(),
        )

    def classify_provider_failure(self, value: Any) -> str | None:
        if isinstance(value, BaseException):
            return f"{type(value).__name__}: {value}"

        error = getattr(value, "error", None)
        if error:
            return str(error)

        status = getattr(value, "status", None)
        if status in {"failed", "cancelled", "incomplete"}:
            return f"provider returned status={status}"

        return None

    def _extract_function_calls(self, response: Any) -> list[Any]:
        output = getattr(response, "output", None) or []
        return [item for item in output if getattr(item, "type", "") == "function_call"]

    def _execute_tool_calls(
        self,
        *,
        function_calls: Iterable[Any],
        tool_executor: ToolExecutor,
        response_id: str,
    ) -> tuple[list[dict[str, str]], list[ToolTraceRecord]]:
        tool_outputs: list[dict[str, str]] = []
        tool_traces: list[ToolTraceRecord] = []
        for tool_call in function_calls:
            call_id = str(getattr(tool_call, "call_id", ""))
            name = str(getattr(tool_call, "name", ""))
            raw_arguments = getattr(tool_call, "arguments", "{}")
            arguments = json.loads(raw_arguments or "{}")
            output = tool_executor(name, arguments)
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
            tool_traces.append(
                ToolTraceRecord(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    output=output,
                    response_id=response_id,
                )
            )
        return tool_outputs, tool_traces

    def _response_text(self, response: Any, stream_text: str) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text:
            return output_text
        if stream_text:
            return stream_text
        text_blocks: list[str] = []
        for item in getattr(response, "output", None) or []:
            content = getattr(item, "content", None) or []
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str) and text:
                    text_blocks.append(text)
        return "\n".join(text_blocks).strip()
