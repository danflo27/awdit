from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx
from openai import APIConnectionError, APITimeoutError, NOT_GIVEN, OpenAI


ToolExecutor = Callable[[str, dict[str, Any]], str]
ProviderEventCallback = Callable[[str, dict[str, Any]], None]


DEFAULT_BACKGROUND_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_BACKGROUND_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_BACKGROUND_POLL_TRANSIENT_ERROR_LIMIT = 3


@dataclass(frozen=True)
class ToolTraceRecord:
    call_id: str
    name: str
    arguments: dict[str, Any]
    output: str
    output_meta: dict[str, Any]
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
class ProviderToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class BackgroundPollResult:
    status: str
    response_id: str
    final_text: str
    tool_traces: tuple[ToolTraceRecord, ...]
    continuation_input: tuple[dict[str, str], ...] = ()
    tool_calls: tuple[ProviderToolCall, ...] = ()
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
        self._background_request_timeout = httpx.Timeout(
            timeout=DEFAULT_BACKGROUND_REQUEST_TIMEOUT_SECONDS,
            connect=DEFAULT_BACKGROUND_CONNECT_TIMEOUT_SECONDS,
        )
        self._background_poll_transient_error_limit = DEFAULT_BACKGROUND_POLL_TRANSIENT_ERROR_LIMIT
        self._background_poll_transient_errors: dict[str, int] = {}

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
        reasoning_effort: str | None,
        instructions: str,
        input_text: str,
        previous_response_id: str | None,
        tools: Iterable[dict[str, Any]],
        tool_executor: ToolExecutor,
        text_format: dict[str, Any] | None = None,
        prompt_cache_key: str | None = None,
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
                reasoning={"effort": reasoning_effort} if reasoning_effort is not None else NOT_GIVEN,
                instructions=current_instructions,
                input=current_input,
                previous_response_id=current_previous_response_id,
                text={"format": text_format} if text_format is not None else NOT_GIVEN,
                prompt_cache_key=prompt_cache_key if prompt_cache_key is not None else NOT_GIVEN,
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
            self._emit_usage_event(
                response=response,
                fallback_response_id=response_id,
                fallback_model=model,
                event_callback=event_callback,
            )
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
                    {
                        "count": len(function_calls),
                        "response_id": response_id,
                        "tool_names": [str(getattr(call, "name", "")) for call in function_calls],
                        "tool_calls": [
                            {
                                "call_id": str(getattr(call, "call_id", "")),
                                "name": str(getattr(call, "name", "")),
                                "arguments": self._parse_tool_arguments(
                                    getattr(call, "arguments", "{}")
                                ),
                            }
                            for call in function_calls
                        ],
                    },
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
        reasoning_effort: str | None,
        instructions: str,
        input_text: str,
        previous_response_id: str | None,
        tools: Iterable[dict[str, Any]],
        text_format: dict[str, Any] | None = None,
        prompt_cache_key: str | None = None,
    ) -> ProviderBackgroundHandle:
        response = self._client.responses.create(
            model=model,
            reasoning={"effort": reasoning_effort} if reasoning_effort is not None else NOT_GIVEN,
            instructions=instructions,
            input=input_text,
            previous_response_id=previous_response_id,
            text={"format": text_format} if text_format is not None else NOT_GIVEN,
            prompt_cache_key=prompt_cache_key if prompt_cache_key is not None else NOT_GIVEN,
            tools=list(tools),
            background=True,
            store=True,
            timeout=self._background_request_timeout,
        )
        response_id = getattr(response, "id", "")
        self._background_poll_transient_errors.pop(response_id, None)
        return ProviderBackgroundHandle(response_id=response_id)

    def continue_background_turn(
        self,
        *,
        previous_response_id: str,
        model: str,
        input_items: Iterable[dict[str, str]],
        tools: Iterable[dict[str, Any]],
        text_format: dict[str, Any] | None = None,
    ) -> ProviderBackgroundHandle:
        response = self._client.responses.create(
            model=model,
            previous_response_id=previous_response_id,
            input=list(input_items),
            text={"format": text_format} if text_format is not None else NOT_GIVEN,
            tools=list(tools),
            background=True,
            store=True,
            timeout=self._background_request_timeout,
        )
        self._background_poll_transient_errors.pop(previous_response_id, None)
        response_id = getattr(response, "id", previous_response_id)
        self._background_poll_transient_errors.pop(response_id, None)
        return ProviderBackgroundHandle(response_id=response_id)

    def poll_background_turn(
        self,
        *,
        handle: ProviderBackgroundHandle,
        model: str,
        tools: Iterable[dict[str, Any]],
        tool_executor: ToolExecutor,
        text_format: dict[str, Any] | None = None,
        event_callback: ProviderEventCallback | None = None,
    ) -> BackgroundPollResult:
        try:
            response = self._client.responses.retrieve(
                handle.response_id,
                timeout=self._background_request_timeout,
            )
        except Exception as exc:
            if not self._is_retryable_background_poll_error(exc):
                raise
            transient_errors = self._background_poll_transient_errors.get(handle.response_id, 0) + 1
            self._background_poll_transient_errors[handle.response_id] = transient_errors
            if transient_errors <= self._background_poll_transient_error_limit:
                return BackgroundPollResult(
                    status="running",
                    response_id=handle.response_id,
                    final_text="",
                    tool_traces=(),
                    failure_message=self.classify_provider_failure(exc),
                )
            raise

        self._background_poll_transient_errors.pop(handle.response_id, None)
        failure_message = self.classify_provider_failure(response)
        if failure_message is not None:
            self._emit_usage_event(
                response=response,
                fallback_response_id=handle.response_id,
                fallback_model=model,
                event_callback=event_callback,
            )
            return BackgroundPollResult(
                status="failed",
                response_id=handle.response_id,
                final_text=self._safe_response_text(response),
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
        self._emit_usage_event(
            response=response,
            fallback_response_id=handle.response_id,
            fallback_model=model,
            event_callback=event_callback,
        )
        function_calls = self._extract_function_calls(response)
        if function_calls:
            if event_callback is not None:
                event_callback(
                    "tool_calls_requested",
                    {
                        "count": len(function_calls),
                        "response_id": response_id,
                        "tool_names": [str(getattr(call, "name", "")) for call in function_calls],
                        "tool_calls": [
                            {
                                "call_id": str(getattr(call, "call_id", "")),
                                "name": str(getattr(call, "name", "")),
                                "arguments": self._parse_tool_arguments(
                                    getattr(call, "arguments", "{}")
                                ),
                            }
                            for call in function_calls
                        ],
                    },
                )
            tool_outputs, new_traces = self._execute_tool_calls(
                function_calls=function_calls,
                tool_executor=tool_executor,
                response_id=response_id,
            )
            return BackgroundPollResult(
                status="awaiting_continuation",
                response_id=response_id,
                final_text="",
                tool_traces=tuple(new_traces),
                continuation_input=tuple(tool_outputs),
                tool_calls=tuple(
                    ProviderToolCall(
                        call_id=str(getattr(call, "call_id", "")),
                        name=str(getattr(call, "name", "")),
                        arguments=self._parse_tool_arguments(getattr(call, "arguments", "{}")),
                    )
                    for call in function_calls
                ),
            )

        final_text = self._response_text(response, "")
        return BackgroundPollResult(
            status="completed",
            response_id=response_id,
            final_text=final_text,
            tool_traces=(),
        )

    def cancel_background_turn(self, handle: ProviderBackgroundHandle) -> str:
        response = self._client.responses.cancel(
            handle.response_id,
            timeout=self._background_request_timeout,
        )
        self._background_poll_transient_errors.pop(handle.response_id, None)
        return str(getattr(response, "status", "unknown"))

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
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as exc:
                arguments = {}
                output = json.dumps(
                    {
                        "ok": False,
                        "tool_name": name,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    indent=2,
                )
            else:
                try:
                    output = tool_executor(name, arguments)
                except Exception as exc:
                    output = json.dumps(
                        {
                            "ok": False,
                            "tool_name": name,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        },
                        indent=2,
                    )
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
                    output_meta=self._tool_output_meta(output),
                    response_id=response_id,
                )
            )
        return tool_outputs, tool_traces

    def _parse_tool_arguments(self, raw_arguments: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _tool_output_meta(self, output: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"chars": len(output)}
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return meta
        if not isinstance(payload, dict):
            return meta
        for key in (
            "start_line",
            "end_line",
            "truncated",
            "truncated_before",
            "truncated_after",
            "raw_char_count",
            "raw_line_count",
        ):
            if key in payload:
                meta[key] = payload[key]
        if "truncated" in payload and "truncated_after" not in meta:
            meta["truncated_after"] = payload["truncated"]
        return meta

    def _response_text(self, response: Any, stream_text: str) -> str:
        refusal = self._extract_refusal_text(response)
        if refusal:
            raise RuntimeError(f"provider refusal: {refusal}")
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

    def _safe_response_text(self, response: Any) -> str:
        try:
            return self._response_text(response, "")
        except Exception:
            return ""

    def _extract_refusal_text(self, response: Any) -> str | None:
        output = getattr(response, "output", None) or []
        for item in output:
            refusal = getattr(item, "refusal", None)
            if isinstance(refusal, str) and refusal.strip():
                return refusal.strip()
            content = getattr(item, "content", None) or []
            for block in content:
                refusal = getattr(block, "refusal", None)
                if isinstance(refusal, str) and refusal.strip():
                    return refusal.strip()
                if getattr(block, "type", "") != "refusal":
                    continue
                text = getattr(block, "text", None)
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return None

    def _emit_usage_event(
        self,
        *,
        response: Any,
        fallback_response_id: str,
        fallback_model: str,
        event_callback: ProviderEventCallback | None,
    ) -> None:
        if event_callback is None:
            return

        usage = getattr(response, "usage", None)
        usage_payload = {
            "response_id": str(getattr(response, "id", "") or fallback_response_id),
            "model": str(getattr(response, "model", "") or fallback_model),
            "status": str(getattr(response, "status", "unknown")),
            "input_tokens": self._coerce_usage_int(self._usage_field(usage, "input_tokens")),
            "output_tokens": self._coerce_usage_int(self._usage_field(usage, "output_tokens")),
            "total_tokens": self._coerce_usage_int(self._usage_field(usage, "total_tokens")),
            "cached_input_tokens": self._coerce_usage_int(
                self._usage_field(self._usage_field(usage, "input_tokens_details"), "cached_tokens")
            ),
            "reasoning_output_tokens": self._coerce_usage_int(
                self._usage_field(self._usage_field(usage, "output_tokens_details"), "reasoning_tokens")
            ),
        }
        event_callback("provider_usage", usage_payload)

    def _usage_field(self, value: Any, field_name: str) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return value.get(field_name)
        return getattr(value, field_name, None)

    def _coerce_usage_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_retryable_background_poll_error(self, exc: Exception) -> bool:
        return isinstance(
            exc,
            (
                APITimeoutError,
                APIConnectionError,
                httpx.TimeoutException,
                httpx.TransportError,
            ),
        )
