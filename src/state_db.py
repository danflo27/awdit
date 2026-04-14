from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from paths import state_root


@dataclass(frozen=True)
class LearnedModelLimit:
    provider: str
    model: str
    learned_tpm_limit: int | None
    headroom_fraction: float
    observed_peak_input_tokens: dict[str, int]
    updated_at: str


def default_state_db_path(cwd: Path) -> Path:
    return state_root(cwd.resolve()) / "awdit.db"


def _ensure_runs_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(runs)")
    }
    desired_columns = {
        "failure_stage": "TEXT",
        "failure_worker_id": "TEXT",
        "failure_message": "TEXT",
        "failure_artifact": "TEXT",
    }
    for column_name, column_type in desired_columns.items():
        if column_name in existing_columns:
            continue
        connection.execute(f"ALTER TABLE runs ADD COLUMN {column_name} {column_type}")


def _ensure_learned_model_limits_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS learned_model_limits (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            learned_tpm_limit INTEGER,
            headroom_fraction REAL NOT NULL,
            observed_peak_input_tokens TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, model)
        )
        """
    )


def ensure_state_db(cwd: Path) -> Path:
    db_path = default_state_db_path(cwd)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                repo_key TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                run_dir TEXT NOT NULL,
                failure_stage TEXT,
                failure_worker_id TEXT,
                failure_message TEXT,
                failure_artifact TEXT
            )
            """
        )
        _ensure_runs_columns(connection)
        _ensure_learned_model_limits_table(connection)
        connection.commit()
    return db_path


def insert_run(
    *,
    cwd: Path,
    run_id: str,
    repo_key: str,
    mode: str,
    status: str,
    run_dir: Path,
) -> Path:
    db_path = ensure_state_db(cwd)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO runs (run_id, repo_key, mode, status, created_at, completed_at, run_dir)
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                run_id,
                repo_key,
                mode,
                status,
                datetime.now().isoformat(timespec="seconds"),
                str(run_dir.resolve()),
            ),
        )
        connection.commit()
    return db_path


def update_run_status(
    *,
    cwd: Path,
    run_id: str,
    status: str,
    completed: bool,
) -> Path:
    db_path = ensure_state_db(cwd)
    completed_at = datetime.now().isoformat(timespec="seconds") if completed else None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE runs
            SET status = ?, completed_at = ?
            WHERE run_id = ?
            """,
            (status, completed_at, run_id),
        )
        connection.commit()
    return db_path


def record_run_failure(
    *,
    cwd: Path,
    run_id: str,
    failure_stage: str | None,
    failure_worker_id: str | None,
    failure_message: str,
    failure_artifact: Path | None,
) -> Path:
    db_path = ensure_state_db(cwd)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE runs
            SET failure_stage = ?, failure_worker_id = ?, failure_message = ?, failure_artifact = ?
            WHERE run_id = ?
            """,
            (
                failure_stage,
                failure_worker_id,
                failure_message,
                str(failure_artifact.resolve()) if failure_artifact is not None else None,
                run_id,
            ),
        )
        connection.commit()
    return db_path


def load_learned_model_limit(
    *,
    cwd: Path,
    provider: str,
    model: str,
) -> LearnedModelLimit | None:
    db_path = ensure_state_db(cwd)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT provider, model, learned_tpm_limit, headroom_fraction, observed_peak_input_tokens, updated_at
            FROM learned_model_limits
            WHERE provider = ? AND model = ?
            """,
            (provider, model),
        ).fetchone()
    if row is None:
        return None
    try:
        observed_peak_input_tokens = json.loads(row[4] or "{}")
    except json.JSONDecodeError:
        observed_peak_input_tokens = {}
    normalized_peaks: dict[str, int] = {}
    if isinstance(observed_peak_input_tokens, dict):
        for worker_type, peak_value in observed_peak_input_tokens.items():
            try:
                normalized = int(peak_value)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                normalized_peaks[str(worker_type)] = normalized
    return LearnedModelLimit(
        provider=str(row[0]),
        model=str(row[1]),
        learned_tpm_limit=int(row[2]) if row[2] is not None else None,
        headroom_fraction=float(row[3]),
        observed_peak_input_tokens=normalized_peaks,
        updated_at=str(row[5]),
    )


def save_learned_model_limit(
    *,
    cwd: Path,
    provider: str,
    model: str,
    learned_tpm_limit: int | None,
    headroom_fraction: float,
    observed_peak_input_tokens: dict[str, int],
) -> Path:
    db_path = ensure_state_db(cwd)
    normalized_peaks: dict[str, int] = {}
    for worker_type, peak_value in observed_peak_input_tokens.items():
        try:
            normalized = int(peak_value)
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            normalized_peaks[str(worker_type)] = normalized
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO learned_model_limits (
                provider,
                model,
                learned_tpm_limit,
                headroom_fraction,
                observed_peak_input_tokens,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model) DO UPDATE SET
                learned_tpm_limit = excluded.learned_tpm_limit,
                headroom_fraction = excluded.headroom_fraction,
                observed_peak_input_tokens = excluded.observed_peak_input_tokens,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                model,
                learned_tpm_limit,
                headroom_fraction,
                json.dumps(normalized_peaks, sort_keys=True),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
    return db_path
