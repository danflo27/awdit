from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from awdit.paths import MANAGED_ROOT_NAME
from awdit.provider_openai import (
    BackgroundPollResult,
    OpenAIResponsesProvider,
    ProviderBackgroundHandle,
    ProviderTurnResult,
    ToolTraceRecord,
)


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def _safe_write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except FileNotFoundError:
        return


@dataclass
class RuntimeEvent:
    event_id: str
    timestamp: str
    slot_name: str
    epoch_id: str
    dispatch_id: str | None
    event_type: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionEpochRecord:
    epoch_id: str
    slot_name: str
    status: str
    created_at: str
    created_reason: str
    previous_epoch_id: str | None
    seed_checkpoint_ref: str | None
    seed_checkpoint_body: str | None
    activated_at: str | None = None
    closed_at: str | None = None
    lease_started_at: str | None = None
    last_heartbeat_at: str | None = None
    provider_handle_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DispatchRecord:
    dispatch_id: str
    slot_name: str
    work_label: str
    work_key: str
    status: str
    mode: str
    instructions_ref: str
    epoch_id: str
    artifact_refs: list[str]
    checkpoint_ref: str | None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    superseded_at: str | None = None
    supersedes_dispatch_id: str | None = None
    provider_handle_id: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckpointRecord:
    checkpoint_id: str
    slot_name: str
    epoch_id: str
    dispatch_id: str
    work_key: str
    model_name: str
    mode: str
    created_at: str
    artifact_refs: list[str]
    tool_trace_refs: list[str]
    checkpoint_body: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SlotRuntimeState:
    slot_name: str
    current_epoch_id: str
    current_epoch_live: bool = False
    active_dispatch_id: str | None = None
    pending_dispatch_id: str | None = None
    compaction_requested: bool = False
    latest_checkpoint_ref: str | None = None
    latest_checkpoint_body: str | None = None
    provider_handle_id: str | None = None
    lease_started_at: str | None = None
    last_heartbeat_at: str | None = None
    worker_started_at: str | None = None
    worker_thread_alive: bool = False
    stop_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OneSlotRuntime:
    SLOT_NAME = "hunter_1"
    DEFAULT_POLL_INTERVAL_SECONDS = 0.2
    DISPATCH_PLACEHOLDER = ""

    def __init__(
        self,
        *,
        cwd: Path,
        loaded,
        run_dir: Path,
        default_mode: str,
        provider: OpenAIResponsesProvider | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.cwd = cwd.resolve()
        self.loaded = loaded
        self.run_dir = run_dir.resolve()
        self.default_mode = default_mode
        self.model_name = loaded.effective.slots[self.SLOT_NAME].default_model
        self.reasoning_effort = loaded.effective.slots[self.SLOT_NAME].reasoning_effort
        self.prompt_file = loaded.effective.slots[self.SLOT_NAME].prompt_file
        self.scope_include = loaded.effective.scope.include
        self.scope_exclude = loaded.effective.scope.exclude
        self.provider = provider or OpenAIResponsesProvider.from_loaded_config(loaded)
        self.poll_interval_seconds = poll_interval_seconds

        self.session_state_dir = self.run_dir / "session_state"
        self.events_path = self.session_state_dir / "events.jsonl"
        self.epochs_dir = self.session_state_dir / "epochs" / self.SLOT_NAME
        self.dispatches_dir = self.session_state_dir / "dispatches"
        self.checkpoints_dir = self.session_state_dir / "checkpoints" / self.SLOT_NAME
        self.snapshots_dir = self.session_state_dir / "snapshots"
        self.artifacts_root = self.session_state_dir / "artifacts" / self.SLOT_NAME

        for path in (
            self.session_state_dir,
            self.epochs_dir,
            self.dispatches_dir,
            self.checkpoints_dir,
            self.snapshots_dir,
            self.artifacts_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

        initial_epoch = self._new_epoch_id()
        self._epochs: dict[str, SessionEpochRecord] = {}
        self._dispatches: dict[str, DispatchRecord] = {}
        self._checkpoints: dict[str, CheckpointRecord] = {}
        self._condition = threading.Condition()
        self._worker_thread: threading.Thread | None = None
        self._foreground_dispatch_id: str | None = None
        self._foreground_stream_open = False

        self.state = SlotRuntimeState(slot_name=self.SLOT_NAME, current_epoch_id=initial_epoch)
        self._create_epoch_record(initial_epoch, created_reason="runtime_entry")
        self._emit_event(
            event_type="reserved_epoch_created",
            message="Created reserved epoch record for prototype runtime entry.",
            dispatch_id=None,
            data={"epoch_id": initial_epoch},
        )
        self._write_status_snapshot()

    def interactive_loop(self) -> int:
        print("")
        print("One-slot runtime prototype mode")
        print("- Slot: Hunter 1")
        print("- Dispatch commands: dispatch-fg, dispatch-bg")
        print("Type 'help' for commands.")
        while True:
            raw = input("runtime> ").strip().lower()
            if raw == "dispatch-fg":
                self._interactive_dispatch(mode="foreground")
                continue
            if raw == "dispatch-bg":
                self._interactive_dispatch(mode="background")
                continue
            if raw == "status":
                self._print_status()
                continue
            if raw == "events":
                self._print_events()
                continue
            if raw == "artifacts":
                self._print_artifacts()
                continue
            if raw == "compact":
                print(self.request_compaction())
                continue
            if raw == "help":
                self._print_help()
                continue
            if raw == "quit":
                allowed, message = self.request_shutdown()
                print(message)
                if allowed:
                    return 0
                continue
            if not raw:
                continue
            print("Unknown command. Type 'help' for commands.")

    def submit_dispatch(
        self,
        *,
        work_label: str,
        work_key: str,
        instructions_text: str | None = None,
        mode: str | None = None,
    ) -> tuple[bool, str, str | None]:
        dispatch_mode = mode or self.default_mode
        created_at = _timestamp()
        dispatch_id = self._new_dispatch_id()
        artifact_dir = self.artifacts_root / dispatch_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        instructions_path = artifact_dir / "instructions.txt"
        _safe_write_text(
            instructions_path,
            instructions_text
            if instructions_text is not None
            else self._generated_dispatch_payload_record(mode=dispatch_mode),
        )

        dispatch_record = DispatchRecord(
            dispatch_id=dispatch_id,
            slot_name=self.SLOT_NAME,
            work_label=work_label,
            work_key=work_key,
            status="queued",
            mode=dispatch_mode,
            instructions_ref=str(instructions_path),
            epoch_id=self.state.current_epoch_id,
            artifact_refs=[str(artifact_dir)],
            checkpoint_ref=None,
            created_at=created_at,
        )

        with self._condition:
            active_dispatch = self._dispatches.get(self.state.active_dispatch_id or "")
            pending_dispatch = self._dispatches.get(self.state.pending_dispatch_id or "")
            if self.state.active_dispatch_id is None:
                self._dispatches[dispatch_id] = dispatch_record
                self._persist_dispatch(dispatch_record)
                self.state.active_dispatch_id = dispatch_id
                self._activate_current_epoch_locked()
                self._start_worker_if_needed_locked()
                self._emit_event(
                    event_type="dispatch_queued",
                    message=f"Queued dispatch {dispatch_id} as active work.",
                    dispatch_id=dispatch_id,
                    data={"work_key": work_key, "mode": dispatch_mode},
                )
                self._condition.notify_all()
                return True, f"Dispatch accepted: {dispatch_id}", dispatch_id

            if active_dispatch is None:
                return False, "Runtime state error: missing active dispatch record.", None

            if work_key != active_dispatch.work_key:
                return (
                    False,
                    "Rejected: while a dispatch is active, only same-work_key supersession may use the pending slot.",
                    None,
                )

            if pending_dispatch is not None and pending_dispatch.work_key != work_key:
                return (
                    False,
                    "Rejected: pending slot is occupied by unrelated work.",
                    None,
                )

            if pending_dispatch is not None:
                pending_dispatch.status = "superseded"
                pending_dispatch.superseded_at = created_at
                pending_dispatch.error_message = "Superseded by newer pending dispatch."
                self._persist_dispatch(pending_dispatch)
                self._emit_event(
                    event_type="pending_dispatch_superseded",
                    message=f"Replaced pending dispatch {pending_dispatch.dispatch_id}.",
                    dispatch_id=pending_dispatch.dispatch_id,
                    data={"replacement_dispatch_id": dispatch_id, "work_key": work_key},
                )
                dispatch_record.supersedes_dispatch_id = pending_dispatch.dispatch_id

            self._dispatches[dispatch_id] = dispatch_record
            self._persist_dispatch(dispatch_record)
            self.state.pending_dispatch_id = dispatch_id
            self._emit_event(
                event_type="dispatch_queued_pending",
                message=f"Queued dispatch {dispatch_id} in the pending slot.",
                dispatch_id=dispatch_id,
                data={"work_key": work_key, "mode": dispatch_mode},
            )
            return True, f"Dispatch queued as pending: {dispatch_id}", dispatch_id

    def request_compaction(self) -> str:
        with self._condition:
            if not self.state.current_epoch_live:
                return "Compaction unavailable: the reserved epoch is not live yet."
            if self.state.active_dispatch_id is not None:
                self.state.compaction_requested = True
                self._emit_event(
                    event_type="compaction_requested",
                    message="Compaction requested and will run after the active dispatch completes.",
                    dispatch_id=self.state.active_dispatch_id,
                )
                return "Compaction requested. It will run after the active dispatch completes."
            if not self.state.latest_checkpoint_ref:
                return "Compaction unavailable: no completed dispatch checkpoint exists yet."
            self._perform_compaction_locked(reason="manual_idle_compaction")
            return f"Compaction complete. Current epoch is now {self.state.current_epoch_id}."

    def request_shutdown(self) -> tuple[bool, str]:
        with self._condition:
            if self.state.active_dispatch_id is not None:
                return (
                    False,
                    "Quit refused: an active dispatch is still running, and same-process runtime state would be lost.",
                )
            self.state.stop_requested = True
            self._condition.notify_all()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)
        return True, "Prototype runtime exited cleanly."

    def wait_for_dispatch(self, dispatch_id: str, *, timeout_seconds: float = 5.0) -> DispatchRecord:
        deadline = time.time() + timeout_seconds
        with self._condition:
            while time.time() < deadline:
                record = self._dispatches[dispatch_id]
                if record.status in {"completed", "failed", "superseded"}:
                    return record
                remaining = max(0.0, deadline - time.time())
                self._condition.wait(timeout=min(0.1, remaining))
        return self._dispatches[dispatch_id]

    def wait_for_idle(self, *, timeout_seconds: float = 5.0) -> bool:
        deadline = time.time() + timeout_seconds
        with self._condition:
            while time.time() < deadline:
                if self.state.active_dispatch_id is None and self.state.pending_dispatch_id is None:
                    return True
                remaining = max(0.0, deadline - time.time())
                self._condition.wait(timeout=min(0.1, remaining))
        return self.state.active_dispatch_id is None and self.state.pending_dispatch_id is None

    def latest_status(self) -> dict[str, Any]:
        return {
            "slot_name": self.SLOT_NAME,
            "current_epoch_id": self.state.current_epoch_id,
            "current_epoch_live": self.state.current_epoch_live,
            "active_dispatch_id": self.state.active_dispatch_id,
            "pending_dispatch_id": self.state.pending_dispatch_id,
            "compaction_requested": self.state.compaction_requested,
            "latest_checkpoint_ref": self.state.latest_checkpoint_ref,
            "provider_handle_id": self.state.provider_handle_id,
            "worker_thread_alive": bool(self._worker_thread and self._worker_thread.is_alive()),
            "lease_started_at": self.state.lease_started_at,
            "last_heartbeat_at": self.state.last_heartbeat_at,
        }

    def recent_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]]

    def list_artifact_paths(self) -> list[str]:
        if not self.artifacts_root.exists():
            return []
        return [str(path) for path in sorted(self.artifacts_root.iterdir())]

    def _interactive_dispatch(self, *, mode: str) -> None:
        print("")
        work_label = self._generated_work_label(mode=mode)
        work_key = self._generated_work_key(mode=mode)
        print("Dispatch summary")
        print(f"- mode: {mode}")
        print(f"- label: {work_label}")
        print(f"- key: {work_key}")

        accepted, message, dispatch_id = self.submit_dispatch(
            work_label=work_label,
            work_key=work_key,
            mode=mode,
        )
        print(message)
        if not accepted or dispatch_id is None:
            return
        if mode == "foreground":
            self._foreground_dispatch_id = dispatch_id
            self._foreground_stream_open = False
            record = self.wait_for_dispatch(dispatch_id, timeout_seconds=60.0)
            if self._foreground_stream_open:
                print("")
            self._foreground_dispatch_id = None
            self._foreground_stream_open = False
            print(f"Foreground dispatch {dispatch_id} finished with status={record.status}.")

    def _print_status(self) -> None:
        snapshot_path = self._write_status_snapshot()
        print("")
        print("Runtime status")
        for key, value in self.latest_status().items():
            print(f"- {key}: {value}")
        print(f"- snapshot: {snapshot_path}")

    def _print_events(self) -> None:
        print("")
        print("Recent events")
        events = self.recent_events(limit=20)
        if not events:
            print("- (none)")
            return
        for event in events:
            print(
                f"- {event['timestamp']} {event['event_type']} "
                f"(epoch={event['epoch_id']} dispatch={event['dispatch_id']}): {event['message']}"
            )

    def _print_artifacts(self) -> None:
        print("")
        print("Artifacts")
        artifact_paths = self.list_artifact_paths()
        if not artifact_paths:
            print("- (none)")
            return
        for path in artifact_paths:
            print(f"- {path}")

    def _print_help(self) -> None:
        print("")
        print("Commands")
        print("- dispatch-fg: launch Hunter 1 in foreground mode")
        print("- dispatch-bg: launch Hunter 1 in background mode")
        print("- status: show current runtime state and write a status snapshot")
        print("- events: show recent lifecycle events")
        print("- artifacts: list runtime artifact directories")
        print("- compact: compact immediately if idle, or defer until current work completes")
        print("- help: show this help")
        print("- quit: exit only when the runtime is idle")

    def _start_worker_if_needed_locked(self) -> None:
        if self._worker_thread is not None:
            return
        self.state.worker_started_at = _timestamp()
        self.state.worker_thread_alive = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="awdit-hunter-1-runtime",
            daemon=True,
        )
        self._worker_thread.start()
        self._emit_event(
            event_type="worker_started",
            message="Started Hunter 1 runtime worker thread.",
            dispatch_id=self.state.active_dispatch_id,
        )

    def _activate_current_epoch_locked(self) -> None:
        if self.state.current_epoch_live:
            return
        now = _timestamp()
        self.state.current_epoch_live = True
        self.state.lease_started_at = now
        self.state.last_heartbeat_at = now
        record = self._epochs[self.state.current_epoch_id]
        record.status = "live"
        record.activated_at = now
        record.lease_started_at = now
        record.last_heartbeat_at = now
        self._persist_epoch(record)
        self._emit_event(
            event_type="epoch_live",
            message=f"Activated epoch {record.epoch_id} on first dispatch.",
            dispatch_id=self.state.active_dispatch_id,
        )

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self.state.stop_requested and self.state.active_dispatch_id is None:
                    self._condition.wait(timeout=0.1)
                if self.state.stop_requested and self.state.active_dispatch_id is None:
                    self.state.worker_thread_alive = False
                    self._write_status_snapshot()
                    return
                dispatch_id = self.state.active_dispatch_id
            if dispatch_id is None:
                continue
            self._execute_dispatch(dispatch_id)

    def _execute_dispatch(self, dispatch_id: str) -> None:
        recovery_attempts = 0
        while True:
            with self._condition:
                record = self._dispatches[dispatch_id]
                record.status = "running"
                record.started_at = record.started_at or _timestamp()
                record.epoch_id = self.state.current_epoch_id
                self._persist_dispatch(record)
                self._touch_heartbeat_locked()
                self._emit_event(
                    event_type="dispatch_started",
                    message=f"Started dispatch {dispatch_id}.",
                    dispatch_id=dispatch_id,
                    data={"mode": record.mode, "work_key": record.work_key},
                )
                previous_response_id = self.state.provider_handle_id
                checkpoint_body = self.state.latest_checkpoint_body

            try:
                result = self._run_dispatch_with_provider(
                    record=record,
                    previous_response_id=previous_response_id,
                    checkpoint_body=checkpoint_body,
                )
            except Exception as exc:
                failure_reason = self.provider.classify_provider_failure(exc)
                if (
                    self._should_recover_from_provider_failure(
                        failure_reason=failure_reason,
                        previous_response_id=previous_response_id,
                    )
                    and recovery_attempts < 1
                ):
                    recovery_attempts += 1
                    self._handle_provider_failure(dispatch_id=dispatch_id, reason=failure_reason)
                    continue
                self._mark_dispatch_failed(
                    dispatch_id=dispatch_id,
                    reason=failure_reason or str(exc),
                )
                return

            with self._condition:
                self.state.provider_handle_id = result.response_id
                epoch_record = self._epochs[self.state.current_epoch_id]
                epoch_record.provider_handle_id = result.response_id
                self._persist_epoch(epoch_record)
            self._complete_dispatch(dispatch_id=dispatch_id, result=result)
            return

    def _run_dispatch_with_provider(
        self,
        *,
        record: DispatchRecord,
        previous_response_id: str | None,
        checkpoint_body: str | None,
    ) -> ProviderTurnResult:
        system_instructions = self._compose_system_instructions(
            work_label=record.work_label,
            work_key=record.work_key,
            checkpoint_body=checkpoint_body,
        )
        tools = self._build_tool_schemas()
        if record.mode == "foreground":
            return self.provider.start_foreground_turn(
                model=self.model_name,
                reasoning_effort=self.reasoning_effort,
                instructions=system_instructions,
                input_text=self.DISPATCH_PLACEHOLDER,
                previous_response_id=previous_response_id,
                tools=tools,
                tool_executor=self._run_tool,
                event_callback=lambda event_type, data: self._handle_provider_event(
                    dispatch_id=record.dispatch_id,
                    event_type=event_type,
                    data=data,
                ),
            )

        handle = self.provider.start_background_turn(
            model=self.model_name,
            reasoning_effort=self.reasoning_effort,
            instructions=system_instructions,
            input_text=self.DISPATCH_PLACEHOLDER,
            previous_response_id=previous_response_id,
            tools=tools,
        )
        tool_traces: list[ToolTraceRecord] = []
        while True:
            self._update_dispatch_handle(record.dispatch_id, handle.response_id)
            poll_result = self.provider.poll_background_turn(
                handle=handle,
                model=self.model_name,
                tools=tools,
                tool_executor=self._run_tool,
                event_callback=lambda event_type, data: self._handle_provider_event(
                    dispatch_id=record.dispatch_id,
                    event_type=event_type,
                    data=data,
                ),
            )
            tool_traces.extend(poll_result.tool_traces)
            if poll_result.status == "running":
                handle = ProviderBackgroundHandle(response_id=poll_result.response_id)
                self._handle_provider_event(
                    dispatch_id=record.dispatch_id,
                    event_type="background_poll",
                    data={"response_id": poll_result.response_id},
                )
                time.sleep(self.poll_interval_seconds)
                continue
            if poll_result.status == "failed":
                raise RuntimeError(poll_result.failure_message or "background turn failed")
            return ProviderTurnResult(
                response_id=poll_result.response_id,
                final_text=poll_result.final_text,
                tool_traces=tuple(tool_traces),
                status=poll_result.status,
                model=self.model_name,
            )

    def _compose_system_instructions(
        self,
        *,
        work_label: str,
        work_key: str,
        checkpoint_body: str | None,
    ) -> str:
        prompt_text = self.prompt_file.read_text(encoding="utf-8").strip()
        parts = [
            prompt_text,
            "You are operating inside the awdit one-slot runtime prototype.",
            "Use only the provided read-only tools when you need repository or staged resource context.",
            f"Current work label: {work_label}",
            f"Current work key: {work_key}",
        ]
        if checkpoint_body:
            parts.extend(["Latest persisted checkpoint context:", checkpoint_body])
        return "\n\n".join(parts)

    def _handle_provider_event(
        self,
        *,
        dispatch_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if event_type == "output_delta" and dispatch_id == self._foreground_dispatch_id:
            delta = data.get("delta", "")
            print(delta, end="", flush=True)
            self._foreground_stream_open = True
            return
        if event_type == "background_poll":
            return
        self._emit_event(
            event_type=event_type,
            message=f"Provider event: {event_type}.",
            dispatch_id=dispatch_id,
            data=data,
        )

    def _should_recover_from_provider_failure(
        self,
        *,
        failure_reason: str | None,
        previous_response_id: str | None,
    ) -> bool:
        if not failure_reason or not previous_response_id or not self.state.latest_checkpoint_ref:
            return False
        lowered = failure_reason.lower()
        # Recovery is only for losing an attached warm session, not general request failure.
        return any(
            marker in lowered
            for marker in (
                "provider handle lost",
                "previous response",
                "response id",
                "previous_response_id",
            )
        )

    def _update_dispatch_handle(self, dispatch_id: str, response_id: str) -> None:
        with self._condition:
            record = self._dispatches[dispatch_id]
            record.provider_handle_id = response_id
            self._persist_dispatch(record)
            self.state.provider_handle_id = response_id
            epoch_record = self._epochs[self.state.current_epoch_id]
            epoch_record.provider_handle_id = response_id
            self._persist_epoch(epoch_record)

    def _complete_dispatch(self, *, dispatch_id: str, result: ProviderTurnResult) -> None:
        artifact_dir = self.artifacts_root / dispatch_id
        response_path = artifact_dir / "response.txt"
        _safe_write_text(response_path, result.final_text + ("\n" if result.final_text else ""))

        tool_trace_paths: list[str] = []
        if result.tool_traces:
            tool_trace_path = artifact_dir / "tool_traces.json"
            _safe_write_text(
                tool_trace_path,
                json.dumps([asdict(trace) for trace in result.tool_traces], indent=2) + "\n",
            )
            tool_trace_paths.append(str(tool_trace_path))

        checkpoint_body = self._build_checkpoint_body(dispatch_id=dispatch_id, result=result)
        checkpoint_record = CheckpointRecord(
            checkpoint_id=self._new_checkpoint_id(),
            slot_name=self.SLOT_NAME,
            epoch_id=self.state.current_epoch_id,
            dispatch_id=dispatch_id,
            work_key=self._dispatches[dispatch_id].work_key,
            model_name=self.model_name,
            mode=self._dispatches[dispatch_id].mode,
            created_at=_timestamp(),
            artifact_refs=[str(response_path)],
            tool_trace_refs=tool_trace_paths,
            checkpoint_body=checkpoint_body,
        )
        checkpoint_path = self.checkpoints_dir / f"{checkpoint_record.epoch_id}-{dispatch_id}.json"
        _safe_write_text(
            checkpoint_path,
            json.dumps(checkpoint_record.to_dict(), indent=2, default=_json_default) + "\n",
        )
        self._checkpoints[checkpoint_record.checkpoint_id] = checkpoint_record

        with self._condition:
            record = self._dispatches[dispatch_id]
            record.status = "completed"
            record.completed_at = _timestamp()
            record.checkpoint_ref = str(checkpoint_path)
            if str(response_path) not in record.artifact_refs:
                record.artifact_refs.append(str(response_path))
            self.state.latest_checkpoint_ref = str(checkpoint_path)
            self.state.latest_checkpoint_body = checkpoint_body
            self._persist_dispatch(record)
            self._emit_event(
                event_type="dispatch_completed",
                message=f"Completed dispatch {dispatch_id}.",
                dispatch_id=dispatch_id,
                data={"checkpoint_ref": str(checkpoint_path), "artifact_ref": str(response_path)},
            )
            self._advance_runtime_after_dispatch_locked(dispatch_id)

    def _handle_provider_failure(self, *, dispatch_id: str, reason: str) -> None:
        with self._condition:
            failed_epoch_id = self.state.current_epoch_id
            next_epoch_id = self._perform_compaction_locked(
                reason="provider_failure_recovery",
                recovery_reason=reason,
            )
            artifact_dir = self.artifacts_root / dispatch_id
            recovery_path = artifact_dir / "recovery.json"
            _safe_write_text(
                recovery_path,
                json.dumps(
                    {
                        "failed_epoch_id": failed_epoch_id,
                        "recovered_epoch_id": next_epoch_id,
                        "reason": reason,
                        "recovered_from_checkpoint_ref": self.state.latest_checkpoint_ref,
                    },
                    indent=2,
                )
                + "\n",
            )
            record = self._dispatches[dispatch_id]
            record.artifact_refs.append(str(recovery_path))
            record.epoch_id = next_epoch_id
            self._persist_dispatch(record)
            self._emit_event(
                event_type="provider_failure_recovered",
                message=f"Recovered dispatch {dispatch_id} into epoch {next_epoch_id}.",
                dispatch_id=dispatch_id,
                data={"failed_epoch_id": failed_epoch_id, "reason": reason},
            )

    def _mark_dispatch_failed(self, *, dispatch_id: str, reason: str) -> None:
        with self._condition:
            record = self._dispatches[dispatch_id]
            record.status = "failed"
            record.failed_at = _timestamp()
            record.error_message = reason
            self._persist_dispatch(record)
            self._emit_event(
                event_type="dispatch_failed",
                message=f"Dispatch {dispatch_id} failed.",
                dispatch_id=dispatch_id,
                data={"reason": reason},
            )
            self._advance_runtime_after_dispatch_locked(dispatch_id)

    def _advance_runtime_after_dispatch_locked(self, dispatch_id: str) -> None:
        if self.state.active_dispatch_id != dispatch_id:
            return
        self.state.active_dispatch_id = None
        if self.state.compaction_requested:
            if self.state.latest_checkpoint_ref:
                self._perform_compaction_locked(reason="deferred_manual_compaction")
            else:
                self._emit_event(
                    event_type="compaction_skipped",
                    message="Skipped deferred compaction because no checkpoint exists yet.",
                    dispatch_id=dispatch_id,
                )
            self.state.compaction_requested = False
        if self.state.pending_dispatch_id is not None:
            next_dispatch_id = self.state.pending_dispatch_id
            self.state.pending_dispatch_id = None
            next_record = self._dispatches[next_dispatch_id]
            next_record.epoch_id = self.state.current_epoch_id
            self._persist_dispatch(next_record)
            self.state.active_dispatch_id = next_dispatch_id
            self._emit_event(
                event_type="pending_dispatch_promoted",
                message=f"Promoted pending dispatch {next_dispatch_id} to active.",
                dispatch_id=next_dispatch_id,
            )
        self._write_status_snapshot()
        self._condition.notify_all()

    def _perform_compaction_locked(
        self,
        *,
        reason: str,
        recovery_reason: str | None = None,
    ) -> str:
        if not self.state.latest_checkpoint_ref:
            raise RuntimeError("Compaction requires a completed dispatch checkpoint.")
        current_epoch_id = self.state.current_epoch_id
        current_epoch = self._epochs[current_epoch_id]
        current_epoch.status = "closed"
        current_epoch.closed_at = _timestamp()
        current_epoch.provider_handle_id = self.state.provider_handle_id
        self._persist_epoch(current_epoch)

        next_epoch_id = self._new_epoch_id()
        next_epoch = self._create_epoch_record(
            next_epoch_id,
            created_reason=reason,
            previous_epoch_id=current_epoch_id,
            seed_checkpoint_ref=self.state.latest_checkpoint_ref,
            seed_checkpoint_body=self.state.latest_checkpoint_body,
        )
        now = _timestamp()
        next_epoch.status = "live"
        next_epoch.activated_at = now
        next_epoch.lease_started_at = now
        next_epoch.last_heartbeat_at = now
        self._persist_epoch(next_epoch)

        self.state.current_epoch_id = next_epoch_id
        self.state.current_epoch_live = True
        self.state.provider_handle_id = None
        self.state.lease_started_at = now
        self.state.last_heartbeat_at = now
        self._emit_event(
            event_type="epoch_compacted",
            message=f"Rolled epoch {current_epoch_id} to {next_epoch_id}.",
            dispatch_id=self.state.active_dispatch_id,
            data={
                "reason": reason,
                "recovery_reason": recovery_reason,
                "seed_checkpoint_ref": self.state.latest_checkpoint_ref,
            },
        )
        self._write_status_snapshot()
        return next_epoch_id

    def _create_epoch_record(
        self,
        epoch_id: str,
        *,
        created_reason: str,
        previous_epoch_id: str | None = None,
        seed_checkpoint_ref: str | None = None,
        seed_checkpoint_body: str | None = None,
    ) -> SessionEpochRecord:
        record = SessionEpochRecord(
            epoch_id=epoch_id,
            slot_name=self.SLOT_NAME,
            status="reserved",
            created_at=_timestamp(),
            created_reason=created_reason,
            previous_epoch_id=previous_epoch_id,
            seed_checkpoint_ref=seed_checkpoint_ref,
            seed_checkpoint_body=seed_checkpoint_body,
        )
        self._epochs[epoch_id] = record
        self._persist_epoch(record)
        return record

    def _touch_heartbeat_locked(self) -> None:
        now = _timestamp()
        self.state.last_heartbeat_at = now
        record = self._epochs[self.state.current_epoch_id]
        record.last_heartbeat_at = now
        self._persist_epoch(record)

    def _persist_epoch(self, record: SessionEpochRecord) -> None:
        path = self.epochs_dir / f"{record.epoch_id}.json"
        _safe_write_text(path, json.dumps(record.to_dict(), indent=2, default=_json_default) + "\n")

    def _persist_dispatch(self, record: DispatchRecord) -> None:
        path = self.dispatches_dir / f"{record.dispatch_id}.json"
        _safe_write_text(path, json.dumps(record.to_dict(), indent=2, default=_json_default) + "\n")

    def _emit_event(
        self,
        *,
        event_type: str,
        message: str,
        dispatch_id: str | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = RuntimeEvent(
            event_id=uuid.uuid4().hex[:10],
            timestamp=_timestamp(),
            slot_name=self.SLOT_NAME,
            epoch_id=self.state.current_epoch_id,
            dispatch_id=dispatch_id,
            event_type=event_type,
            message=message,
            data=data or {},
        )
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), default=_json_default) + "\n")
        except FileNotFoundError:
            return

    def _write_status_snapshot(self) -> Path:
        snapshot_path = self.snapshots_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        _safe_write_text(snapshot_path, json.dumps(self.latest_status(), indent=2, default=_json_default) + "\n")
        return snapshot_path

    def _build_checkpoint_body(self, *, dispatch_id: str, result: ProviderTurnResult) -> str:
        record = self._dispatches[dispatch_id]
        body_parts = [
            f"Checkpoint for {self.SLOT_NAME}",
            f"Dispatch: {dispatch_id}",
            f"Work label: {record.work_label}",
            f"Work key: {record.work_key}",
            f"Mode: {record.mode}",
            "",
            result.final_text.strip(),
        ]
        return "\n".join(part for part in body_parts if part is not None).strip()

    def _generated_work_label(self, *, mode: str) -> str:
        return f"Hunter 1 {mode} run"

    def _generated_work_key(self, *, mode: str) -> str:
        return f"{self.SLOT_NAME}/{mode}"

    def _generated_dispatch_payload_record(self, *, mode: str) -> str:
        return "\n".join(
            [
                "Dispatch payload record",
                f"slot: {self.SLOT_NAME}",
                f"mode: {mode}",
                "source: configured slot prompt",
                "user_payload: none",
            ]
        )

    def _build_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "list_scope_files",
                "description": "List repo files in scope plus staged run resources.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path_glob": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                },
            },
            {
                "type": "function",
                "name": "read_file",
                "description": "Read an allowed scoped file or staged resource file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000},
                    },
                    "required": ["path"],
                },
            },
            {
                "type": "function",
                "name": "search_text",
                "description": "Search allowed scoped files and staged resources for plain text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path_glob": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["query"],
                },
            },
        ]

    def _run_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_scope_files":
            return self._tool_list_scope_files(arguments)
        if tool_name == "read_file":
            return self._tool_read_file(arguments)
        if tool_name == "search_text":
            return self._tool_search_text(arguments)
        raise RuntimeError(f"Unknown tool: {tool_name}")

    def _tool_list_scope_files(self, arguments: dict[str, Any]) -> str:
        path_glob = str(arguments.get("path_glob", "") or "").strip()
        limit = int(arguments.get("limit", 50) or 50)
        items = self._allowed_paths(path_glob=path_glob)
        display_paths = [self._display_path(path) for path in items[:limit]]
        return json.dumps({"paths": display_paths, "count": len(display_paths)}, indent=2)

    def _tool_read_file(self, arguments: dict[str, Any]) -> str:
        raw_path = str(arguments.get("path", "") or "").strip()
        if not raw_path:
            raise RuntimeError("read_file requires a path.")
        max_chars = int(arguments.get("max_chars", 12000) or 12000)
        path = self._resolve_allowed_path(raw_path)
        text = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps(
            {
                "path": self._display_path(path),
                "content": text[:max_chars],
                "truncated": len(text) > max_chars,
            },
            indent=2,
        )

    def _tool_search_text(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query", "") or "")
        if not query:
            raise RuntimeError("search_text requires a query.")
        path_glob = str(arguments.get("path_glob", "") or "").strip()
        limit = int(arguments.get("limit", 50) or 50)
        matches: list[dict[str, Any]] = []
        for path in self._allowed_paths(path_glob=path_glob):
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {
                            "path": self._display_path(path),
                            "line_number": line_number,
                            "line": line,
                        }
                    )
                    if len(matches) >= limit:
                        return json.dumps({"matches": matches, "count": len(matches)}, indent=2)
        return json.dumps({"matches": matches, "count": len(matches)}, indent=2)

    def _allowed_paths(self, *, path_glob: str) -> list[Path]:
        allowed: list[Path] = []
        for path in sorted(self.cwd.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.cwd).as_posix()
            if relative.startswith(f"{MANAGED_ROOT_NAME}/"):
                continue
            if self.scope_include and not self._matches_any(relative, self.scope_include):
                continue
            if self._matches_any(relative, self.scope_exclude):
                continue
            if path_glob and not PurePosixPath(relative).match(path_glob):
                continue
            allowed.append(path.resolve())

        resources_root = self.run_dir / "resources"
        if resources_root.exists():
            for staged_dir in sorted(resources_root.rglob("staged")):
                for path in sorted(staged_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    display = self._display_path(path)
                    if path_glob and not PurePosixPath(display).match(path_glob):
                        continue
                    allowed.append(path.resolve())
        return allowed

    def _resolve_allowed_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.cwd / path).resolve()
        else:
            path = path.resolve()
        allowed_prefixes = (self.cwd, self.run_dir / "resources")
        if not any(self._is_relative_to(path, root) for root in allowed_prefixes):
            raise RuntimeError("Path is outside the live repo and staged run resources.")
        if self._is_relative_to(path, self.run_dir / "resources"):
            if not path.exists() or not path.is_file():
                raise RuntimeError("Staged resource path does not exist.")
            return path
        relative = path.relative_to(self.cwd).as_posix()
        if relative.startswith(f"{MANAGED_ROOT_NAME}/"):
            raise RuntimeError("Only staged run resources may be read under awdit.")
        if self.scope_include and not self._matches_any(relative, self.scope_include):
            raise RuntimeError("Path is outside the configured scope include globs.")
        if self._matches_any(relative, self.scope_exclude):
            raise RuntimeError("Path is excluded by scope rules.")
        if not path.exists() or not path.is_file():
            raise RuntimeError("File does not exist.")
        return path

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.cwd))
        except ValueError:
            return str(path.resolve())

    def _matches_any(self, relative_path: str, globs: tuple[str, ...]) -> bool:
        posix = PurePosixPath(relative_path)
        return any(posix.match(pattern) for pattern in globs)

    def _is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return False
        return True

    def _new_epoch_id(self) -> str:
        return f"epoch_{uuid.uuid4().hex[:8]}"

    def _new_dispatch_id(self) -> str:
        return f"dispatch_{uuid.uuid4().hex[:8]}"

    def _new_checkpoint_id(self) -> str:
        return f"checkpoint_{uuid.uuid4().hex[:8]}"
