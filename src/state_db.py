from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from paths import state_root


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
