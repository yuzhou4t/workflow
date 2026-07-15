from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .models import RunState, utc_now


class RunNotFoundError(KeyError):
    pass


class VersionConflictError(RuntimeError):
    pass


class TransitionInProgressError(RuntimeError):
    pass


class RunRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.getenv("HYPOWEAVER_DB_PATH")
        project_root = Path(__file__).resolve().parents[3]
        self.path = Path(configured) if configured else project_root / "backend" / "var" / "hypoweaver.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS transition_claims (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL
                )
                """
            )
            connection.execute("PRAGMA user_version = 1")

    def create(self, state: RunState) -> RunState:
        state.version = 1
        state.updated_at = utc_now()
        payload = state.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (id, version, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (state.id, state.version, payload, state.created_at, state.updated_at),
            )
        return state

    def get(self, run_id: str) -> RunState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return RunState.model_validate_json(row["payload"])

    def list(self) -> list[RunState]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM runs ORDER BY updated_at DESC"
            ).fetchall()
        return [RunState.model_validate_json(row["payload"]) for row in rows]

    def delete(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM transition_claims WHERE run_id = ?", (run_id,))
            cursor = connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        if cursor.rowcount != 1:
            raise RunNotFoundError(run_id)

    def save(self, state: RunState, *, expected_version: int) -> RunState:
        next_version = expected_version + 1
        next_updated_at = utc_now()
        state.version = next_version
        state.updated_at = next_updated_at
        payload = state.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE runs
                   SET version = ?, payload = ?, updated_at = ?
                 WHERE id = ? AND version = ?
                """,
                (next_version, payload, next_updated_at, state.id, expected_version),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                state.version = expected_version
                raise VersionConflictError(
                    f"run {state.id} changed; expected version {expected_version}"
                )
        return state

    def claim_transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RunNotFoundError(run_id)
            if row["version"] != expected_version:
                connection.rollback()
                raise VersionConflictError(
                    f"run {run_id} changed; expected version {expected_version}"
                )
            try:
                connection.execute(
                    "INSERT INTO transition_claims (run_id, idempotency_key, expected_version, claimed_at) VALUES (?, ?, ?, ?)",
                    (run_id, idempotency_key, expected_version, utc_now()),
                )
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise TransitionInProgressError(
                    f"run {run_id} already has a transition in progress"
                ) from error

    def release_transition(self, run_id: str, idempotency_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM transition_claims WHERE run_id = ? AND idempotency_key = ?",
                (run_id, idempotency_key),
            )

    def delete_all_for_tests(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM transition_claims")
            connection.execute("DELETE FROM runs")
