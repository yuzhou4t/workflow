from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .blind_models import BlindEvaluationRequest, BlindEvaluationView
from .models import utc_now


class BlindEvaluationNotFoundError(KeyError):
    pass


class BlindRepository:
    """Storage owned by App B; it has no methods that can mutate App A."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.getenv("HYPOWEAVER_BLIND_DB_PATH")
        project_root = Path(__file__).resolve().parents[3]
        self.path = Path(configured) if configured else project_root / "backend" / "var" / "blind" / "hypoweaver_blind.db"
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
                CREATE TABLE IF NOT EXISTS blind_evaluations (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    seal_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    view_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("PRAGMA user_version = 1")

    def create(self, request: BlindEvaluationRequest, view: BlindEvaluationView) -> BlindEvaluationView:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO blind_evaluations
                    (id, case_id, source_run_id, seal_sha256, request_json, view_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    view.id,
                    view.case_id,
                    view.source_run_id,
                    view.seal_sha256,
                    request.model_dump_json(),
                    view.model_dump_json(),
                    view.created_at,
                    view.updated_at,
                ),
            )
        return view

    def get(self, evaluation_id: str) -> BlindEvaluationView:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT view_json FROM blind_evaluations WHERE id = ?", (evaluation_id,)
            ).fetchone()
        if row is None:
            raise BlindEvaluationNotFoundError(evaluation_id)
        return BlindEvaluationView.model_validate_json(row["view_json"])

    def list(self) -> list[BlindEvaluationView]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT view_json FROM blind_evaluations ORDER BY updated_at DESC"
            ).fetchall()
        return [BlindEvaluationView.model_validate_json(row["view_json"]) for row in rows]

    def update(self, view: BlindEvaluationView) -> BlindEvaluationView:
        view.updated_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE blind_evaluations SET view_json = ?, updated_at = ? WHERE id = ?",
                (view.model_dump_json(), view.updated_at, view.id),
            )
        return view
