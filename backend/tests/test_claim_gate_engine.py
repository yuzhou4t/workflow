from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hypoweaver.engine import WorkflowEngine
from hypoweaver.models import (
    AnalysisPlan,
    ClaimLedger,
    CreateRunRequest,
    DesignArena,
    GateDecisionRequest,
)
from hypoweaver.repository import RunRepository


class ClaimGateEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = RunRepository(Path(self.tempdir.name) / "runs.db")
        self.engine = WorkflowEngine(self.repository)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _to_h2(self):
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        return await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="panel-h1"),
        )

    async def _to_h3(self):
        run = await self._to_h2()
        return await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="panel-h2"),
        )

    async def test_panel_flow_preserves_candidate_and_h3_reads_gated_ledger(self) -> None:
        h2 = await self._to_h2()
        plan = AnalysisPlan.model_validate(h2.artifacts["analysis_plan"]["payload"])
        self.assertEqual(plan.check_registry_version, "enterprise-panel-v1")
        self.assertTrue(plan.baseline_models[0].required_for_admission)

        h3 = await self.engine.decide_gate(
            h2.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="panel-h2-live"),
        )

        self.assertEqual((h3.status, h3.current_gate), ("waiting_human", "H3"))
        self.assertIn("candidate_claim_ledger", h3.artifacts)
        self.assertIn("evidence_registry", h3.artifacts)
        self.assertIn("claim_gate_report", h3.artifacts)
        candidate = ClaimLedger.model_validate(
            h3.artifacts["candidate_claim_ledger"]["payload"]
        )
        gated = ClaimLedger.model_validate(h3.artifacts["claim_ledger"]["payload"])
        self.assertNotEqual(candidate.ledger_id, gated.ledger_id)
        self.assertTrue(
            all(claim.admission_status == "prohibited" for claim in gated.claims)
        )
        claim_step = next(
            step for step in h3.steps if step.node_id == "claim_ledger"
        )
        self.assertEqual(
            claim_step.input["allowed_claim_specs"],
            [
                {
                    "claim_id": "claim-H1",
                    "hypothesis_id": "H1",
                    "claim_type": "associational",
                }
            ],
        )

    async def test_old_h2_artifact_is_refreshed_and_waits_for_second_confirmation(self) -> None:
        run = await self._to_h2()
        arena = DesignArena.model_validate(run.artifacts["design_arena"]["payload"])
        downgraded_candidates = [
            candidate.model_copy(
                update={
                    "plan": candidate.plan.model_copy(
                        update={"check_registry_version": None}
                    )
                }
            )
            for candidate in arena.candidates
        ]
        arena = arena.model_copy(update={"candidates": downgraded_candidates})
        selected = next(
            item
            for item in downgraded_candidates
            if item.candidate_id == arena.provisional_candidate_id
        )
        self.engine._put_artifact(run, "design_arena", arena)
        self.engine._put_artifact(run, "analysis_plan", selected.plan)
        run = self.repository.save(run, expected_version=run.version)

        refreshed = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="legacy-h2-first"),
        )

        self.assertEqual(
            (refreshed.status, refreshed.current_gate),
            ("waiting_human", "H2"),
        )
        self.assertNotIn("formal_research_contract", refreshed.artifacts)
        upgraded = AnalysisPlan.model_validate(
            refreshed.artifacts["analysis_plan"]["payload"]
        )
        self.assertEqual(upgraded.check_registry_version, "enterprise-panel-v1")

        advanced = await self.engine.decide_gate(
            refreshed.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="legacy-h2-second"),
        )
        self.assertEqual((advanced.status, advanced.current_gate), ("waiting_human", "H3"))

    async def test_old_h3_candidate_is_gated_and_waits_for_second_confirmation(self) -> None:
        run = await self._to_h3()
        candidate = ClaimLedger.model_validate(
            run.artifacts["candidate_claim_ledger"]["payload"]
        )
        for key in (
            "candidate_claim_ledger",
            "evidence_registry",
            "claim_gate_report",
        ):
            run.artifacts.pop(key, None)
        self.engine._put_artifact(run, "claim_ledger", candidate)
        run.claims = candidate.claims
        run = self.repository.save(run, expected_version=run.version)

        refreshed = await self.engine.decide_gate(
            run.id,
            "H3",
            GateDecisionRequest(
                action="generate_plan_only",
                idempotency_key="legacy-h3-first",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "hold"}
                    for claim in candidate.claims
                ],
            ),
        )

        self.assertEqual(
            (refreshed.status, refreshed.current_gate),
            ("waiting_human", "H3"),
        )
        self.assertIn("candidate_claim_ledger", refreshed.artifacts)
        self.assertIn("evidence_registry", refreshed.artifacts)
        self.assertIn("claim_gate_report", refreshed.artifacts)
        gated = ClaimLedger.model_validate(refreshed.artifacts["claim_ledger"]["payload"])
        self.assertTrue(all(claim.admission_status != "unassessed" for claim in gated.claims))

        advanced = await self.engine.decide_gate(
            refreshed.id,
            "H3",
            GateDecisionRequest(
                action="generate_plan_only",
                idempotency_key="legacy-h3-second",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "hold"}
                    for claim in gated.claims
                ],
            ),
        )
        self.assertEqual((advanced.status, advanced.current_gate), ("waiting_human", "H4"))


if __name__ == "__main__":
    unittest.main()
