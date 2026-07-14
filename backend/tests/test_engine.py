from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from hypoweaver.definition import build_app_a_definition
from hypoweaver.engine import WorkflowEngine, WorkflowTransitionError
from hypoweaver.models import (
    CaseSubmission,
    CreateRunRequest,
    CriticIssue,
    CriticReport,
    GateDecisionRequest,
    FormalResearchContract,
    ResearchRun,
    RevisionRequest,
)
from hypoweaver.repository import (
    RunRepository,
    TransitionInProgressError,
    VersionConflictError,
)


class WorkflowEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "runs.db"
        self.repository = RunRepository(self.db_path)
        self.engine = WorkflowEngine(self.repository)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _to_h2(self):
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )
        return await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="approve-h1"),
        )

    async def _to_h3(self):
        run = await self._to_h2()
        return await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="approve-h2"),
        )

    async def test_fixture_flow_stops_at_each_gate_and_completes_plan_only(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H1"))

        run = await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="fixture-h1"),
        )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H2"))

        run = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="fixture-h2"),
        )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H3"))
        self.assertEqual(run.execution_status, "fixture_only")
        self.assertEqual(run.scientific_status, "not_evaluated")
        self.assertTrue(run.plan_only)
        self.assertTrue(run.claims)
        self.assertTrue(
            all(
                claim.evidence_status == "not_tested"
                and claim.allowed_strength == "prohibited"
                for claim in run.claims
            )
        )

        run = await self.engine.decide_gate(
            run.id,
            "H3",
            GateDecisionRequest(
                action="generate_plan_only",
                idempotency_key="fixture-h3",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "hold"}
                    for claim in run.claims
                ],
            ),
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.current_node_id, "complete")
        manuscript = run.artifacts["manuscript_package"]["payload"]
        self.assertEqual(manuscript["mode"], "research_plan_only")
        self.assertEqual(manuscript["empirical_findings_status"], "prohibited_fixture")
        self.assertIn("sealed_output", run.artifacts)

    async def test_fixture_contains_no_estimates_or_diagnostics(self) -> None:
        run = await self._to_h3()
        research_run = run.artifacts["research_run"]["payload"]
        self.assertTrue(research_run["fixture_only"])
        for execution in research_run["executions"]:
            self.assertEqual(execution["estimates"], [])
            self.assertEqual(execution["diagnostic_results"], {})

    async def test_h2_refuses_unresolved_critical_issue(self) -> None:
        run = await self._to_h2()
        report = CriticReport.model_validate(run.artifacts["critic_report"]["payload"])
        report.verdict = "blocked"
        report.issues.append(
            CriticIssue(
                issue_id="critical-1",
                dimension="causal",
                severity="critical",
                evidence="处理分配无法定义",
                why_it_matters="核心因果参数不可识别",
                required_fix="补充处理组与实施时间",
                return_stage="human",
                repair_type="human_required",
            )
        )
        run.artifacts["critic_report"]["payload"] = report.model_dump(mode="json")
        self.repository.save(run, expected_version=run.version)

        with self.assertRaises(WorkflowTransitionError):
            await self.engine.decide_gate(
                run.id,
                "H2",
                GateDecisionRequest(action="approve", idempotency_key="critical-h2"),
            )
        persisted = self.engine.get_run(run.id)
        self.assertEqual((persisted.status, persisted.current_gate), ("waiting_human", "H2"))
        self.assertNotIn("formal_research_contract", persisted.artifacts)

    async def test_gate_decision_is_idempotent(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        decision = GateDecisionRequest(
            action="approve",
            idempotency_key="same-request",
            expected_run_version=run.version,
        )
        first = await self.engine.decide_gate(run.id, "H1", decision)
        step_count = len(first.steps)
        second = await self.engine.decide_gate(run.id, "H1", decision)
        self.assertEqual(second.version, first.version)
        self.assertEqual(len(second.steps), step_count)
        self.assertEqual(second.current_gate, "H2")

    async def test_optimistic_version_conflict_is_rejected(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        with self.assertRaises(VersionConflictError):
            await self.engine.decide_gate(
                run.id,
                "H1",
                GateDecisionRequest(
                    action="approve",
                    expected_run_version=run.version + 1,
                    idempotency_key="wrong-version",
                ),
            )

    async def test_gate_rejects_stale_reviewed_artifact_hashes(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        with self.assertRaisesRegex(WorkflowTransitionError, "artifact hashes"):
            await self.engine.decide_gate(
                run.id,
                "H1",
                GateDecisionRequest(
                    action="approve",
                    idempotency_key="stale-artifact",
                    reviewed_artifact_hashes={"research_package": "stale"},
                ),
            )
        persisted = self.engine.get_run(run.id)
        self.assertEqual((persisted.status, persisted.current_gate), ("waiting_human", "H1"))

    async def test_unknown_data_structure_does_not_silently_fall_back(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(
                mode="fixture",
                case={
                    "case_id": "unknown-structure",
                    "title": "尚未明确结构的研究",
                    "research_question": "变量 X 与结果 Y 有什么关系？",
                    "hypotheses": [{"hypothesis_id": "H1", "statement": "X 与 Y 相关。"}],
                    "data_structure_hint": "unknown",
                    "variables": [
                        {"name": "y", "role": "outcome"},
                        {"name": "x", "role": "exposure"},
                    ],
                },
            )
        )
        run = await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="unknown-h1"),
        )
        self.assertEqual(run.status, "blocked")
        self.assertEqual(run.current_node_id, "method_route")
        route = run.artifacts["method_route"]["payload"]
        self.assertEqual(route["route_status"], "needs_human_review")
        self.assertIsNone(route["primary_route"])

    async def test_sqlite_snapshot_survives_repository_reopen(self) -> None:
        run = await self._to_h2()
        reopened = WorkflowEngine(RunRepository(self.db_path)).get_run(run.id)
        self.assertEqual(reopened.current_gate, "H2")
        self.assertEqual(reopened.version, run.version)
        self.assertEqual(len(reopened.events), len(run.events))

    async def test_returned_h1_can_accept_a_revision_and_reopen_gate(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        run = await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="revise", idempotency_key="return-h1"),
        )
        revised_case = run.case_submission.model_copy(update={"title": "ESG 与融资成本（修订）"})
        run = await self.engine.submit_revision(
            run.id,
            RevisionRequest(
                gate="H1",
                expected_run_version=run.version,
                idempotency_key="revision-h1",
                case=revised_case,
            ),
        )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H1"))
        self.assertEqual(run.case_name, "ESG 与融资成本（修订）")

    async def test_returned_h2_can_accept_a_new_plan_version(self) -> None:
        run = await self._to_h2()
        run = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="revise", idempotency_key="return-h2"),
        )
        plan = run.artifacts["analysis_plan"]["payload"]
        plan["plan_version"] += 1
        run = await self.engine.submit_revision(
            run.id,
            RevisionRequest(
                gate="H2",
                expected_run_version=run.version,
                idempotency_key="revision-h2",
                analysis_plan=plan,
            ),
        )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H2"))
        self.assertEqual(run.artifacts["analysis_plan"]["payload"]["plan_version"], 2)

    async def test_transition_claim_blocks_concurrent_side_effects(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        self.repository.claim_transition(
            run.id,
            expected_version=run.version,
            idempotency_key="first",
        )
        with self.assertRaises(TransitionInProgressError):
            self.repository.claim_transition(
                run.id,
                expected_version=run.version,
                idempotency_key="second",
            )
        self.repository.release_transition(run.id, "first")

    async def test_research_run_must_match_frozen_contract(self) -> None:
        run = await self._to_h3()
        contract = FormalResearchContract.model_validate(
            run.artifacts["formal_research_contract"]["payload"]
        )
        research_run = ResearchRun.model_validate(
            run.artifacts["research_run"]["payload"]
        ).model_copy(update={"case_id": "another-case"})
        with self.assertRaisesRegex(ValueError, "case_id"):
            self.engine._validate_research_run_binding(research_run, contract)

    async def test_plan_only_writer_is_deterministic_even_if_provider_changes(self) -> None:
        run = await self._to_h3()
        run.model_provider = "qwen"
        self.repository.save(run, expected_version=run.version)
        run = self.engine.get_run(run.id)
        run = await self.engine.decide_gate(
            run.id,
            "H3",
            GateDecisionRequest(
                action="generate_plan_only",
                idempotency_key="deterministic-plan-only",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "hold"}
                    for claim in run.claims
                ],
            ),
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            run.artifacts["manuscript_package"]["payload"]["mode"],
            "research_plan_only",
        )

    def test_hidden_reference_fields_are_rejected_before_persistence(self) -> None:
        with self.assertRaises(ValidationError):
            CaseSubmission.model_validate(
                {
                    "case_id": "leak",
                    "title": "泄漏案例",
                    "research_question": "问题",
                    "hypotheses": [{"hypothesis_id": "H1", "statement": "假设"}],
                    "variables": [{"name": "y", "role": "outcome"}],
                    "reference_paper": "hidden.pdf",
                    "published_result": "显著正向",
                }
            )

    def test_run_mode_rejects_incompatible_provider_and_executor(self) -> None:
        invalid_combinations = (
            (
                {"mode": "fixture", "model_provider": "qwen"},
                "fixture mode requires model_provider=fixture",
            ),
            (
                {"mode": "fixture", "execution_mode": "external"},
                "fixture mode requires execution_mode=fixture",
            ),
            (
                {"mode": "research", "model_provider": "fixture"},
                "research mode requires model_provider=qwen",
            ),
            (
                {"mode": "research", "execution_mode": "fixture"},
                "research mode requires execution_mode=external",
            ),
        )

        for values, expected_message in invalid_combinations:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValidationError, expected_message):
                    CreateRunRequest(preset_case_id="esg-panel", **values)

    def test_definition_is_code_owned_and_edges_are_valid(self) -> None:
        definition = build_app_a_definition()
        node_ids = {node["id"] for node in definition["nodes"]}
        self.assertEqual(definition["id"], "app-a")
        self.assertGreater(len(node_ids), 20)
        self.assertTrue(
            all(edge["source"] in node_ids and edge["target"] in node_ids for edge in definition["edges"])
        )
        self.assertIn("Dify YAML", definition["description"])


if __name__ == "__main__":
    unittest.main()
