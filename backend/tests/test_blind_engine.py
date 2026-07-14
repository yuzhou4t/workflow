from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hypoweaver.blind_engine import (
    BlindEngine,
    FixtureBlindGateway,
    SealValidationError,
)
from hypoweaver.blind_models import (
    DIMENSION_WEIGHTS,
    BlindEvaluationRequest,
    BlindPromptPayload,
    BlindSuggestionSet,
    DimensionSuggestion,
)
from hypoweaver.blind_prompts import build_app_b_definition
from hypoweaver.blind_repository import BlindRepository
from hypoweaver.engine import WorkflowEngine
from hypoweaver.models import CreateRunRequest, GateDecisionRequest
from hypoweaver.repository import RunRepository
from hypoweaver.seal import canonical_sha256


class NonZeroFixtureGateway(FixtureBlindGateway):
    async def suggest(self, payload: BlindPromptPayload) -> BlindSuggestionSet:
        dimensions = []
        for name, weight in DIMENSION_WEIGHTS.items():
            not_applicable = payload.fixture_only and name == "main_result_match"
            dimensions.append(
                DimensionSuggestion(
                    dimension=name,
                    applicable=not not_applicable,
                    suggested_points=None if not_applicable else weight / 2,
                    diagnosis="测试建议",
                    difference_vs_reference="测试差异",
                )
            )
        return BlindSuggestionSet(dimensions=dimensions, notes="测试")


class BlindEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.app_a_db = root / "research.db"
        self.app_b_db = root / "blind.db"
        self.app_a = WorkflowEngine(RunRepository(self.app_a_db))
        self.repository = BlindRepository(self.app_b_db)
        self.engine = BlindEngine(self.repository)
        self.request = await self._completed_fixture_request()

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _completed_fixture_request(self) -> BlindEvaluationRequest:
        run = await self.app_a.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )
        run = await self.app_a.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="blind-h1"),
        )
        run = await self.app_a.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="blind-h2"),
        )
        run = await self.app_a.decide_gate(
            run.id,
            "H3",
            GateDecisionRequest(
                action="generate_plan_only",
                idempotency_key="blind-h3",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "hold"}
                    for claim in run.claims
                ],
            ),
        )
        return BlindEvaluationRequest(
            case_id=run.case_id,
            sealed_output=run.artifacts["sealed_output"]["payload"],
            analysis_plan=run.artifacts["analysis_plan"]["payload"],
            research_run=run.artifacts["research_run"]["payload"],
            claim_ledger=run.artifacts["approved_claim_ledger"]["payload"],
            reference_summary="隐藏参考摘要：仅用于验证独立盲测链路。",
            reference_paper_text="隐藏原论文文本：测试占位，不提供给 App A。",
            model_provider="fixture",
        )

    async def test_fixture_evaluation_is_isolated_and_main_result_is_na(self) -> None:
        view = await self.engine.evaluate(self.request)
        self.assertEqual(view.status, "completed")
        self.assertIsNotNone(view.result)
        assert view.result is not None
        self.assertTrue(view.result.fixture_only)
        self.assertEqual(view.result.overall_score, 0)
        self.assertEqual(len(view.result.dimension_scores), 6)
        main = next(
            item
            for item in view.result.dimension_scores
            if item.dimension == "main_result_match"
        )
        self.assertEqual(main.status, "not_applicable")
        self.assertIsNone(main.points)

        persisted = BlindEngine(BlindRepository(self.app_b_db)).get(view.id)
        self.assertEqual(persisted.result, view.result)
        self.assertNotEqual(self.app_a_db, self.app_b_db)
        with sqlite3.connect(self.app_b_db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(tables, {"blind_evaluations"})

    async def test_seal_manifest_tampering_is_rejected_before_persistence(self) -> None:
        payload = self.request.model_dump(mode="json")
        payload["sealed_output"]["run_id"] = "tampered-run"
        tampered = BlindEvaluationRequest.model_validate(payload)
        with self.assertRaises(SealValidationError):
            await self.engine.evaluate(tampered)
        self.assertEqual(self.repository.list(), [])

    async def test_sealed_artifact_tampering_is_rejected(self) -> None:
        payload = self.request.model_dump(mode="json")
        payload["research_run"]["warnings"].append("tampered")
        tampered = BlindEvaluationRequest.model_validate(payload)
        with self.assertRaisesRegex(SealValidationError, "research_run_sha256"):
            await self.engine.evaluate(tampered)

    async def test_plain_hash_recomputation_cannot_forge_hmac_seal(self) -> None:
        payload = self.request.model_dump(mode="json")
        payload["sealed_output"]["run_id"] = "attacker-run"
        manifest = {
            key: value
            for key, value in payload["sealed_output"].items()
            if key != "seal_sha256"
        }
        payload["sealed_output"]["seal_sha256"] = canonical_sha256(manifest)
        forged = BlindEvaluationRequest.model_validate(payload)
        with self.assertRaisesRegex(SealValidationError, "seal_sha256"):
            await self.engine.evaluate(forged)

    async def test_overall_score_is_computed_from_applicable_dimensions(self) -> None:
        with patch(
            "hypoweaver.blind_engine.FixtureBlindGateway",
            NonZeroFixtureGateway,
        ):
            view = await self.engine.evaluate(self.request)
        assert view.result is not None
        self.assertEqual(view.result.overall_score, 50.0)
        self.assertNotIn(
            "overall_score",
            BlindSuggestionSet.model_json_schema().get("properties", {}),
        )

    def test_definition_exposes_prompt_and_isolation_contract(self) -> None:
        definition = build_app_b_definition()
        self.assertEqual(definition["id"], "app-b")
        self.assertEqual(
            [step["id"] for step in definition["steps"]],
            ["verify_seal", "blind_evaluate", "validate_score"],
        )
        self.assertFalse(definition["isolation"]["can_mutate_app_a"])
        llm_step = definition["steps"][1]
        self.assertIn("system_prompt", llm_step)
        self.assertIn("output_schema", llm_step)


if __name__ == "__main__":
    unittest.main()
