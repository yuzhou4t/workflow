from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from hypoweaver.case_import import DatasetRegistry, LocalCaseImporter
from hypoweaver.definition import build_app_a_definition
from hypoweaver.engine import WorkflowEngine, WorkflowTransitionError
from hypoweaver.models import (
    AnalysisPlan,
    CaseSubmission,
    ClaimLedger,
    CreateRunRequest,
    CriticIssue,
    CriticReport,
    DataProfile,
    GateDecisionRequest,
    FormalResearchContract,
    MethodRoute,
    ManuscriptPackage,
    ManuscriptSection,
    ResearchPackage,
    ResearchRun,
    RevisionRequest,
)
from hypoweaver.prompts import get_prompt
from hypoweaver.repository import (
    RunRepository,
    TransitionInProgressError,
    VersionConflictError,
)


class FullManuscriptGateway:
    async def generate(self, prompt_key, payload, output_model):
        spec = payload["section_spec"]
        return ManuscriptSection(
            section_id=spec["section_id"],
            title=spec["title"],
            content_markdown=(
                "本节依据研究问题、冻结设计与已经执行的证据展开论述。"
                "所有结论均保持授权强度，未执行的分析明确列为后续研究计划。"
            )
            * 12,
            status="generated",
            claim_ids=[],
            run_ids=[],
        )


class FailingWriterGateway:
    async def generate(self, prompt_key, payload, output_model):
        raise RuntimeError("writer timeout")


class RepairingManuscriptGateway(FullManuscriptGateway):
    async def generate(self, prompt_key, payload, output_model):
        if (
            payload["section_spec"]["section_id"] == "introduction"
            and "revision_feedback" not in payload
        ):
            return ManuscriptSection(
                section_id="introduction",
                title="引言",
                content_markdown="现有研究多聚焦其他问题。" * 30,
                status="generated",
            )
        return await super().generate(prompt_key, payload, output_model)


class SecondRoundRepairingGateway(FullManuscriptGateway):
    def __init__(self) -> None:
        self.introduction_calls = 0

    async def generate(self, prompt_key, payload, output_model):
        if payload["section_spec"]["section_id"] == "introduction":
            self.introduction_calls += 1
            if self.introduction_calls < 3:
                return ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown="现有研究多聚焦其他问题。" * 30,
                    status="generated",
                )
        return await super().generate(prompt_key, payload, output_model)


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

    async def test_research_mode_reaches_h1_without_calling_qwen(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did", mode="research")
        )

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H1"))
        self.assertEqual(run.model_provider, "qwen")
        intake = next(step for step in run.steps if step.node_id == "intake_agent")
        self.assertIn("H1 前未调用千问", intake.logs[0])
        self.assertEqual(intake.prompts[0].role, "code")

    async def test_data_profile_reads_registered_csv_before_h2(self) -> None:
        case_root = Path(self.tempdir.name) / "profile-case"
        case_root.mkdir()
        (case_root / "panel-data.csv").write_text(
            "YEAR,证券代码,SDLA,ESG,SIZE\n"
            "2019,000001.SZ,0.1,72,21.0\n"
            "2020,000001.SZ,0.2,74,21.2\n"
            "2020,000001.SZ,,75,21.3\n",
            encoding="utf-8",
        )
        registry = DatasetRegistry(Path(self.tempdir.name) / "profile-datasets.json")
        imported = LocalCaseImporter(registry).import_folder(case_root)
        engine = WorkflowEngine(self.repository, dataset_registry=registry)
        package = ResearchPackage(
            **imported.case_submission.model_dump(),
            input_conflicts=[],
            missing_required_information=[],
        )

        profile = engine._profile(package)

        self.assertEqual(profile.profile_execution_status, "succeeded")
        self.assertEqual(profile.row_count, 3)
        self.assertEqual(profile.column_count, 5)
        self.assertEqual(profile.duplicate_key_count, 2)
        missingness = {item.variable: item for item in profile.missingness}
        self.assertEqual(missingness["SDLA"].missing_count, 1)
        self.assertAlmostEqual(missingness["SDLA"].missing_rate or 0, 1 / 3)

    async def test_method_route_is_deterministic_and_schema_safe(self) -> None:
        run = await self._to_h2()
        route_step = next(step for step in run.steps if step.node_id == "method_route")

        self.assertEqual(route_step.status, "succeeded")
        self.assertEqual(route_step.prompts[0].role, "code")
        self.assertEqual(run.artifacts["method_route"]["payload"]["route_status"], "routed")

    async def test_run_history_can_delete_one_run(self) -> None:
        first = await self.engine.create_run(CreateRunRequest(preset_case_id="esg-panel"))
        second = await self.engine.create_run(CreateRunRequest(preset_case_id="green-finance-did"))

        self.engine.delete_run(first.id)

        self.assertEqual([run.id for run in self.engine.list_runs()], [second.id])
        with self.assertRaises(KeyError):
            self.engine.get_run(first.id)

    async def test_four_critic_dimensions_run_concurrently(self) -> None:
        run = await self._to_h2()
        package = ResearchPackage.model_validate(run.artifacts["research_package"]["payload"])
        profile = DataProfile.model_validate(run.artifacts["data_profile"]["payload"])
        route = MethodRoute.model_validate(run.artifacts["method_route"]["payload"])
        plan = AnalysisPlan.model_validate(run.artifacts["analysis_plan"]["payload"])
        active = 0
        max_active = 0

        async def review(_state, node_id, prompt_key, _payload, _output_model, gateway=None):
            nonlocal active, max_active
            self.assertEqual(prompt_key, "method_critic")
            self.assertIsNotNone(gateway)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return CriticReport(
                report_id=f"report-{node_id}",
                review_round=1,
                verdict="pass",
                issues=[],
                approved_elements=[node_id],
                remaining_risks=[],
            )

        with patch.object(self.engine, "_llm_step", side_effect=review):
            await self.engine._review_plan(run, package, profile, route, plan)

        self.assertEqual(max_active, 4)

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

    async def test_critic_block_can_accept_a_human_plan_revision(self) -> None:
        run = await self._to_h2()
        run.status = "blocked"
        run.current_gate = None
        run.current_node_id = "critic_merge"
        run.last_error = "Critic 发现必须由人工处理的 critical 问题，H2 未开放。"
        run = self.repository.save(run, expected_version=run.version)
        plan = run.artifacts["analysis_plan"]["payload"]
        plan["plan_version"] += 1

        run = await self.engine.submit_revision(
            run.id,
            RevisionRequest(
                gate="H2",
                expected_run_version=run.version,
                idempotency_key="critic-revision-h2",
                analysis_plan=plan,
            ),
        )

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H2"))

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

    async def _retryable_research_writer_run(self):
        run = await self._to_h3()
        research_run = ResearchRun.model_validate(
            run.artifacts["research_run"]["payload"]
        )
        research_run.fixture_only = False
        research_run.execution_status = "succeeded"
        research_run.scientific_status = "limited"
        research_run.not_executed_reason = None
        execution_id = research_run.executions[0].execution_id
        research_run.executions[0].execution_status = "succeeded"
        self.engine._put_artifact(run, "research_run", research_run)

        ledger = ClaimLedger.model_validate(run.artifacts["claim_ledger"]["payload"])
        ledger.research_run_id = research_run.research_run_id
        for claim in ledger.claims:
            claim.evidence_status = "supported"
            claim.allowed_strength = "associational"
            claim.supporting_runs = [execution_id]
            claim.approval_status = "downgraded"
            claim.final_text = "基准模型提供初步关联证据，不支持因果解释。"
        run.claims = ledger.claims
        self.engine._put_artifact(run, "approved_claim_ledger", ledger)
        run.mode = "research"
        run.model_provider = "qwen"
        run.execution_mode = "external"
        run.plan_only = False
        run.status = "failed"
        run.current_gate = None
        run.current_node_id = "scientific_writer"
        run.last_error = "writer timeout"
        return self.repository.save(run, expected_version=run.version)

    async def test_failed_scientific_writer_retries_without_template_fallback(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            run = await self.engine.advance(run.id)

        self.assertEqual(run.status, "completed")
        self.assertIn("sealed_output", run.artifacts)
        self.assertFalse(any(step.node_id == "scientific_writer_fallback" for step in run.steps))
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        self.assertEqual(len(manuscript.manuscript_sections), 8)
        self.assertEqual(manuscript.audit_result, "pass_with_no_critical_issues")

        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            regenerated = await self.engine.retry_writing(run.id)
        self.assertEqual(regenerated.status, "completed")
        self.assertEqual(
            regenerated.artifacts["manuscript_package"]["payload"]["version"],
            2,
        )

        previous_manuscript_hash = regenerated.artifacts["manuscript_package"]["sha256"]
        with patch.object(self.engine, "_gateway", return_value=FailingWriterGateway()):
            failed_regeneration = await self.engine.retry_writing(regenerated.id)
        self.assertEqual(failed_regeneration.status, "failed")
        self.assertEqual(
            failed_regeneration.artifacts["manuscript_package"]["sha256"],
            previous_manuscript_hash,
        )
        self.assertIn("sealed_output", failed_regeneration.artifacts)

    async def test_content_failure_is_rewritten_once_before_sealing(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(
            self.engine,
            "_gateway",
            return_value=RepairingManuscriptGateway(),
        ):
            run = await self.engine.advance(run.id)

        self.assertEqual(run.status, "completed")
        manuscript = run.artifacts["manuscript_package"]["payload"]
        introduction = next(
            section
            for section in manuscript["manuscript_sections"]
            if section["section_id"] == "introduction"
        )
        self.assertNotIn("现有研究", introduction["content_markdown"])
        writer_steps = [
            step for step in run.steps if step.node_id == "scientific_writer"
        ]
        self.assertEqual(len(writer_steps), 9)

    async def test_content_failure_can_use_second_bounded_repair_round(self) -> None:
        run = await self._retryable_research_writer_run()
        gateway = SecondRoundRepairingGateway()
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual(run.status, "completed")
        self.assertEqual(gateway.introduction_calls, 3)
        writer_steps = [
            step for step in run.steps if step.node_id == "scientific_writer"
        ]
        self.assertEqual(len(writer_steps), 10)

    async def test_retry_refines_only_sections_that_fail_new_quality_rules(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            run = await self.engine.advance(run.id)

        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        introduction = next(
            section
            for section in manuscript.manuscript_sections
            if section.section_id == "introduction"
        )
        introduction.content_markdown = "现有研究多聚焦其他问题。" * 30
        self.engine._put_artifact(run, "manuscript_package", manuscript)
        run = self.repository.save(run, expected_version=run.version)
        writer_steps_before = len(
            [step for step in run.steps if step.node_id == "scientific_writer"]
        )

        with patch.object(
            self.engine,
            "_gateway",
            return_value=RepairingManuscriptGateway(),
        ):
            run = await self.engine.retry_writing(run.id)

        writer_steps_after = len(
            [step for step in run.steps if step.node_id == "scientific_writer"]
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(writer_steps_after - writer_steps_before, 1)

    async def test_failed_retry_reuses_valid_latest_sections_without_llm_call(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            run = await self.engine.advance(run.id)

        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        for section in manuscript.manuscript_sections:
            output = section
            if section.section_id == "conclusion":
                output = section.model_copy(
                    update={
                        "content_markdown": (
                            "SDL A 回归元 \x08eta 去除个体均值和时间均值后，"
                            "后续执行残分布检查。" * 20
                        )
                    }
                )
            self.engine._record_step(
                run,
                "scientific_writer",
                "succeeded",
                output_value=output,
            )
        self.engine._record_step(
            run,
            "scientific_writer",
            "failed",
            error="conclusion 存在残差分布术语缺字",
        )
        run.status = "failed"
        run.current_node_id = "scientific_writer"
        run.last_error = "conclusion 存在残差分布术语缺字"
        run = self.repository.save(run, expected_version=run.version)
        writer_steps_before = len(
            [step for step in run.steps if step.node_id == "scientific_writer"]
        )

        with patch.object(self.engine, "_gateway", return_value=FailingWriterGateway()):
            run = await self.engine.retry_writing(run.id)

        writer_steps_after = len(
            [step for step in run.steps if step.node_id == "scientific_writer"]
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(writer_steps_after, writer_steps_before)
        conclusion = next(
            section
            for section in run.artifacts["manuscript_package"]["payload"]["manuscript_sections"]
            if section["section_id"] == "conclusion"
        )
        self.assertNotIn("残分布", conclusion["content_markdown"])
        self.assertNotIn("SDL A", conclusion["content_markdown"])
        self.assertNotIn("回归元", conclusion["content_markdown"])
        self.assertNotIn("\x08", conclusion["content_markdown"])
        self.assertNotIn("个体均值和时间均值", conclusion["content_markdown"])
        self.assertIn("β", conclusion["content_markdown"])

    async def test_failed_scientific_writer_remains_failed_when_retry_fails(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(self.engine, "_gateway", return_value=FailingWriterGateway()):
            run = await self.engine.advance(run.id)

        self.assertEqual(run.status, "failed")
        self.assertEqual(run.current_node_id, "scientific_writer")
        self.assertNotIn("manuscript_package", run.artifacts)
        self.assertNotIn("sealed_output", run.artifacts)
        self.assertFalse(any(step.node_id == "scientific_writer_fallback" for step in run.steps))

    def test_full_manuscript_schema_rejects_short_result_card(self) -> None:
        with self.assertRaisesRegex(ValidationError, "missing required sections"):
            ManuscriptPackage(
                package_id="short",
                case_id="case-short",
                mode="full_manuscript",
                status="ready_for_human_review",
                research_plan_markdown="plan",
                manuscript_sections=[
                    ManuscriptSection(
                        section_id="abstract",
                        title="摘要",
                        content_markdown="只有一张结果卡。" * 30,
                        status="generated",
                    )
                ],
                empirical_findings_status="included",
                disclosures=[],
                unresolved_issues=[],
            )

    def test_scientific_writer_prompt_is_generic(self) -> None:
        prompt = get_prompt("scientific_writer_section")
        self.assertEqual(prompt.version, "2.9.2")
        content = prompt.system + prompt.user_template
        for directed_term in ("案例1", "ESG", "SDLA", "短债长用"):
            self.assertNotIn(directed_term, content)

    def test_manuscript_content_audit_rejects_unavailable_tables_and_literature_claims(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown="现有研究多聚焦其他领域。随着监管要求强化，该问题更加重要。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown="表1报告了结果，组内R平方处于合理范围，样本是平衡面板。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unbalanced"},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                }
            },
        )

        self.assertEqual(len(problems), 5)

    def test_manuscript_content_audit_rejects_unexecuted_work_and_unfrozen_plan(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown="本稿实际完成的工作包括清理并匹配多个数据库。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown="组内 R 平方反映了控制变量与固定效应的解释能力。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="conclusion",
                    title="结论",
                    content_markdown="后续工作将进一步执行机制分析。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unbalanced"},
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                        }
                    ]
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 3)

    def test_manuscript_content_audit_allows_explicitly_empty_mechanism_plan(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown=(
                        "由于冻结计划中的机制分析类别为空，"
                        "本研究不预设也不执行具体的机制检验。"
                        "当前冻结研究计划中未包含中介效应检验，"
                        "因此后续不执行也不报告中介模型，但保留条件性理论讨论。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_allows_conditional_theory_without_mechanism_plan(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论分析",
                    content_markdown=(
                        "两种竞争性理论路径都只构成待检验解释。"
                        "在未执行实证机制检验的前提下，任何关于传导机制的讨论"
                        "均保持推测性质，需后续独立验证。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_allows_theory_but_not_empirical_mechanism_claim(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown=(
                        "冻结计划中未包含实证机制检验步骤，因此本稿不对传导路径"
                        "进行实证验证，仅在理论层面讨论可能的条件性解释。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_allows_negated_certainty_phrase(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论分析",
                    content_markdown=(
                        "该检验只能提供间接证据，不能彻底排除其他内生性来源。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_allows_explicitly_absent_methods(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="discussion_limitations",
                    title="讨论与局限",
                    content_markdown=(
                        "冻结研究计划中没有单列内生性处理步骤，"
                        "例如工具变量法或双重差分设计。"
                        "由于机制检验步骤未在冻结计划中列示，"
                        "后续不会执行实证机制分析。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_allows_declining_to_infer_measurement_drift(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="data_variables",
                    title="数据与变量",
                    content_markdown=(
                        "本稿仅使用输入变量的可观察变异，"
                        "不涉及对评级方法或数据提供方口径变迁的推断。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_rejects_additional_draft_quality_errors(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown="由于缺乏针对该问题的直接经验证据，本文展开分析。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="data_variables",
                    title="数据",
                    content_markdown="评分体系在不同年份可能发生结构性调整。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown="残差分布检查能够验证模型设定合理性。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown=(
                        "组内 R²反映去除个体均值和时间均值后的剩余变异。"
                        "本系统验证了原始值与处理值的逐行对应关系。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="conclusion",
                    title="结论",
                    content_markdown="机制计划为空，因此本研究暂不讨论具体传导路径。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                        }
                    ],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 6)

    def test_manuscript_content_audit_rejects_overstated_inference_language(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论",
                    content_markdown=(
                        "第三方评分可以用作抵押或信用增级。"
                        "稳健性检验与证伪测试能够剥离部分内生性干扰。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown="聚类标准误处理确保假设检验可靠性。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="conclusion",
                    title="结论",
                    content_markdown=(
                        "当核心解释变量提升一个单位时，结果变量平均下降一个单位。"
                    ),
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 4)

    def test_manuscript_content_audit_allows_explicit_limits_on_diagnostics(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown=(
                        "冻结计划中未单列内生性处理步骤。"
                        "残差分布检查不能据此验证整体模型设定的合理性。"
                        "证伪检验不能直接剥离或解决内生性问题。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论",
                    content_markdown=(
                        "观察到的关联而非单一理论机制的必然结果。"
                        "未冻结的机制检验不在本研究计划之内。"
                    ),
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_rejects_invented_scoring_weight_change(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论",
                    content_markdown=(
                        "综合得分在不同年份可能受维度权重调整影响。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 1)

    def test_manuscript_content_audit_rejects_conflating_empty_mechanism_plan_with_no_theory(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown=(
                        "机制分析因未被纳入冻结计划，不在本稿讨论范围之内。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                    "planned_falsification": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 1)

    def test_manuscript_content_audit_rejects_unsupported_trend_and_contradiction(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown="随着信息披露制度不断完善，该指标逐渐受到重视。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown="存在不随时间变化但随时间演变的不可观测因素。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {"planned_mechanisms": ["planned"]},
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 2)

    def test_manuscript_content_audit_allows_preprocessed_input_wording(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown=(
                        "本稿实际完成的工作包括：基于预处理后的分析数据，"
                        "执行了冻结的基准回归。输入案例包已提供处理后的数据。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                        }
                    ],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_keeps_input_and_system_subjects_separate(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="data_variables",
                    title="数据与变量",
                    content_markdown=(
                        "本研究的分析单位为省份—年份观测对。"
                        "输入案例包已提供预处理后的分析数据，"
                        "本系统未执行数据清洗、跨库匹配、合并或变量构造操作。"
                        "本系统实际完成的工作仅限于冻结基准模型运行；"
                        "数据清洗、跨库匹配、合并及变量构造由输入案例包预先完成，"
                        "本系统未执行相关操作。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                        }
                    ],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_rejects_unplanned_method_and_causal_coefficient_wording(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown=(
                        "核心解释变量每提高一个单位，结果变量平均下降 0.2 个单位。"
                        "后续将使用工具变量进一步识别。"
                    ),
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                    "planned_robustness": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 2)

    def test_manuscript_content_audit_rejects_fixed_effect_and_temporal_plan_drift(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown=(
                        "在剔除企业和年份固定效应后得到结果。"
                        "引言部分的核心任务在于介绍研究流程。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown="组内 R² 描述去除个体均值和时间趋势后的拟合。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="discussion_limitations",
                    title="讨论",
                    content_markdown="后续将同时检验滞后项或领先项。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                    "planned_falsification": ["Lead Exposure Test"],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 4)

    def test_manuscript_content_audit_rejects_internal_fields_and_unsupported_certainty(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="abstract",
                    title="摘要",
                    content_markdown=(
                        "frozen_design 已记录计划，并吸收企业和年份层面的"
                        "不随时间变化的异质性。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="introduction",
                    title="引言",
                    content_markdown=(
                        "该风险极易引发危机。本文不对潜在传导路径进行讨论。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="data_variables",
                    title="数据与变量",
                    content_markdown=(
                        "缩尾处理有效避免了极端值影响。"
                        "评级体系在不同年份可能发生变化。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown="估计结果具有较高精度。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                    "planned_falsification": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 7)

    def test_manuscript_content_audit_rejects_mechanism_and_fixed_effect_overinterpretation(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论",
                    content_markdown="若系数显著为负，则支持信息渠道机制。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="data_variables",
                    title="数据",
                    content_markdown=(
                        "样本筛选遵循常规实证研究做法。"
                        "评级方法在不同年份可能发生调整。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="research_design",
                    title="设计",
                    content_markdown="后续执行残分布诊断。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown="冻结研究计划中的内生性处理步骤尚未执行。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="conclusion",
                    title="结论",
                    content_markdown="两家企业其他条件相同时，结果平均相差一个单位。",
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {
                    "research_goal": "associational",
                    "planned_mechanisms": [],
                    "planned_falsification": ["Lead Exposure Test"],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                            "diagnostic_results": {
                                "entity_fixed_effects": True,
                            },
                        }
                    ],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(len(problems), 6)

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
        self.assertNotIn("scientific_writer_fallback", node_ids)
        self.assertEqual(definition["id"], "app-a")
        self.assertGreater(len(node_ids), 20)
        self.assertTrue(
            all(edge["source"] in node_ids and edge["target"] in node_ids for edge in definition["edges"])
        )
        self.assertIn("Dify YAML", definition["description"])


if __name__ == "__main__":
    unittest.main()
