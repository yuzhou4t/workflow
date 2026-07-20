from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .benchmark_models import PairedEvaluationRequest, PairedEvaluationView
from .models import utc_now


class PairedEvaluationNotFoundError(KeyError):
    pass


class PairedBlindRepository:
    """Separate App B v2 storage; it cannot mutate App A or legacy App B."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.getenv("HYPOWEAVER_PAIRED_BLIND_DB_PATH")
        project_root = Path(__file__).resolve().parents[3]
        self.path = (
            Path(configured)
            if configured
            else project_root / "backend" / "var" / "blind" / "hypoweaver_paired_blind.db"
        )
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
                CREATE TABLE IF NOT EXISTS paired_evaluations (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    packet_a_id TEXT NOT NULL,
                    packet_b_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    view_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("PRAGMA user_version = 2")

    def create(
        self, request: PairedEvaluationRequest, view: PairedEvaluationView
    ) -> PairedEvaluationView:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paired_evaluations
                    (id, case_id, packet_a_id, packet_b_id, request_json,
                     view_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    view.id,
                    view.case_id,
                    view.packet_a_id,
                    view.packet_b_id,
                    request.model_dump_json(),
                    view.model_dump_json(),
                    view.created_at,
                    view.updated_at,
                ),
            )
        return view

    def update(self, view: PairedEvaluationView) -> PairedEvaluationView:
        view.updated_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE paired_evaluations SET view_json = ?, updated_at = ? WHERE id = ?",
                (view.model_dump_json(), view.updated_at, view.id),
            )
        return view

    def get(self, evaluation_id: str) -> PairedEvaluationView:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT view_json FROM paired_evaluations WHERE id = ?",
                (evaluation_id,),
            ).fetchone()
        if row is None:
            raise PairedEvaluationNotFoundError(evaluation_id)
        return PairedEvaluationView.model_validate_json(row["view_json"])

    def list(self) -> list[PairedEvaluationView]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT view_json FROM paired_evaluations ORDER BY updated_at DESC"
            ).fetchall()
        return [
            PairedEvaluationView.model_validate_json(row["view_json"])
            for row in rows
        ]
