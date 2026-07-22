from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from hypoweaver.adapters import FixtureModelGateway
from hypoweaver.case_import import DatasetRegistry, LocalCaseImporter
from hypoweaver.definition import build_app_a_definition
from hypoweaver.engine import (
    DETERMINISTIC_SAFE_SECTION_TEXTS,
    MANUSCRIPT_SECTION_SPECS,
    WorkflowEngine,
    WorkflowTransitionError,
    _deterministic_safe_fallback_quality_problems,
    _is_reviewer_issue_blocking_design,
    _normalize_external_enterprise_candidate_plan,
    _normalize_external_policy_candidate_plan,
    _neutralize_limited_event_study_language,
    _plan_executable_fingerprint,
)
from hypoweaver.models import (
    AnalysisPlan,
    CaseSubmission,
    CandidateDesignSet,
    DesignCandidate,
    ClaimLedger,
    CreateRunRequest,
    CriticIssue,
    CriticReport,
    DataProfile,
    DatasetRef,
    DecisionRecord,
    DesignEnvelope,
    DesignArena,
    FULL_MANUSCRIPT_SECTION_IDS,
    GateDecisionRequest,
    FormalResearchContract,
    MethodRoute,
    ManuscriptPackage,
    ManuscriptSection,
    ManuscriptSectionDraft,
    ManuscriptSectionDraftBatch,
    ModelSpec,
    PlannedStep,
    ProbeCheck,
    ProbeReport,
    ExecutionRecord,
    ResearchPackage,
    ResearchRun,
    ReproductionAudit,
    RevisionRequest,
    RunState,
    ScientificAudit,
)
from hypoweaver.prompts import get_prompt
from hypoweaver.test_dag import (
    THREAT_FE_CLUSTER_FEASIBILITY,
    THREAT_MECHANISM_INTERACTION_BOUNDARY,
)
from hypoweaver.repository import (
    RunRepository,
    TransitionInProgressError,
    VersionConflictError,
)


class FullManuscriptGateway:
    provider_name = "fixture"

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.assert_writer_prompt(prompt_key)
        return ManuscriptSectionDraftBatch(
            sections=[self.section_draft(spec) for spec in payload["section_specs"]]
        )

    @staticmethod
    def assert_writer_prompt(prompt_key: str) -> None:
        if prompt_key != "manuscript_section_draft_batch":
            raise AssertionError(f"unexpected writer prompt: {prompt_key}")

    @staticmethod
    def section_draft(spec: dict) -> ManuscriptSectionDraft:
        return ManuscriptSectionDraft(
            section_id=spec["section_id"],
            content_template=(
                "本节依据研究问题、冻结设计与已经执行的证据展开论述。"
                "所有结论均保持授权强度，未执行的分析明确列为后续研究计划。"
            )
            * 12,
        )


class FailingWriterGateway:
    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        raise RuntimeError("writer timeout")


class FeedbackTrackingGateway(FullManuscriptGateway):
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.call_contexts = []

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.calls.append(payload)
        self.call_contexts.append(call_context)
        return await super().generate(
            prompt_key,
            payload,
            output_model,
            call_context=call_context,
        )


class ScientificAuditIsolationGateway(FeedbackTrackingGateway):
    @staticmethod
    def section_draft(spec: dict) -> ManuscriptSectionDraft:
        draft = FullManuscriptGateway.section_draft(spec)
        if spec["section_id"] == "empirical_results":
            draft.content_template = (
                "事件研究显示各期动态效应如下。" + draft.content_template
            )
        return draft


class WorkflowCallRecordingGateway(FullManuscriptGateway):
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fixture = FixtureModelGateway()

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.calls.append(
            {
                "prompt_key": prompt_key,
                "payload": payload,
                "call_context": call_context,
            }
        )
        if prompt_key == "manuscript_section_draft_batch":
            return await super().generate(
                prompt_key,
                payload,
                output_model,
                call_context=call_context,
            )
        return await self.fixture.generate(
            prompt_key,
            payload,
            output_model,
            call_context=call_context,
        )


class FailOnceBatchGateway:
    provider_name = "fixture"

    def __init__(
        self,
        prompt_key: str,
        payload_key: str,
        target_values: list[str],
    ) -> None:
        self.prompt_key = prompt_key
        self.payload_key = payload_key
        self.target_values = target_values
        self.failed = False
        self.calls: list[dict] = []
        self.fixture = FixtureModelGateway()

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.calls.append(
            {
                "prompt_key": prompt_key,
                "payload": payload,
                "call_context": call_context,
            }
        )
        if (
            not self.failed
            and prompt_key == self.prompt_key
            and payload.get(self.payload_key) == self.target_values
        ):
            self.failed = True
            raise RuntimeError("fixture batch failure")
        return await self.fixture.generate(
            prompt_key,
            payload,
            output_model,
            call_context=call_context,
        )


class ActiveCallTrackingGateway:
    provider_name = "fixture"

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[dict] = []
        self.fixture = FixtureModelGateway()

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.calls.append(payload)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await self.fixture.generate(
                prompt_key,
                payload,
                output_model,
                call_context=call_context,
            )
        finally:
            self.active -= 1


class CriticalUnknownReviewerGateway:
    provider_name = "fixture"

    def __init__(self) -> None:
        self.fixture = FixtureModelGateway()

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        result = await self.fixture.generate(
            prompt_key,
            payload,
            output_model,
            call_context=call_context,
        )
        if prompt_key != "reviewer_report_batch":
            return result
        for report in result.reports:
            if report.dimension != "measurement":
                continue
            for review in report.candidate_reviews:
                if review.candidate_id == "candidate-identification_first":
                    review.verdict = "revise"
                    review.issues.append(
                        CriticIssue(
                            issue_id="critical-unknown-technical",
                            dimension="measurement",
                            severity="critical",
                            evidence="unregistered technical concern",
                            why_it_matters="preserve it without guessing a repair",
                            required_fix="do not parse this prose",
                            return_stage="analysis_plan",
                            repair_type="technical",
                            threat_id="panel.future_technical_threat",
                        )
                    )
                else:
                    review.verdict = "reject"
                    review.issues.append(
                        CriticIssue(
                            issue_id=f"human-block-{review.candidate_id}",
                            dimension="measurement",
                            severity="critical",
                            evidence="a human-owned input is missing",
                            why_it_matters="code cannot repair the candidate",
                            required_fix="human decision required",
                            return_stage="human",
                            repair_type="human_required",
                        )
                    )
        return result


class PolicyCriticalReviewerGateway:
    """Reproduce the repairable critical cluster review seen in Case002 r2."""

    provider_name = "fixture"

    def __init__(self) -> None:
        self.fixture = FixtureModelGateway()

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        result = await self.fixture.generate(
            prompt_key,
            payload,
            output_model,
            call_context=call_context,
        )
        if prompt_key != "reviewer_report_batch":
            return result
        for report in result.reports:
            if report.dimension != "statistical":
                continue
            # Deliberately flag only one of three otherwise identical frozen
            # cluster specifications. The code-owned merge must propagate the
            # shared invariant instead of permitting candidate shopping.
            for review in report.candidate_reviews[:1]:
                review.verdict = "revise"
                review.issues.append(
                    CriticIssue(
                        issue_id=f"sparse-cluster-{review.candidate_id}",
                        dimension="statistical",
                        severity="critical",
                        evidence="the frozen interaction cluster contains many singletons",
                        why_it_matters="inference requires the frozen entity-cluster sensitivity",
                        required_fix="retain the code-owned entity cluster check",
                        return_stage="analysis_plan",
                        repair_type="technical",
                        threat_id="policy.entity_cluster_sensitivity",
                    )
                )
        return result


class RepairingManuscriptGateway(FullManuscriptGateway):
    def __init__(self) -> None:
        self.call_contexts = []

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.assert_writer_prompt(prompt_key)
        self.call_contexts.append(call_context)
        sections = []
        for spec in payload["section_specs"]:
            if spec["section_id"] == "introduction" and "revision_feedback" not in spec:
                sections.append(
                    ManuscriptSectionDraft(
                        section_id="introduction",
                        content_template="现有研究多聚焦其他问题。" * 30,
                    )
                )
            else:
                sections.append(self.section_draft(spec))
        return ManuscriptSectionDraftBatch(sections=sections)


class SecondRoundRepairingGateway(FullManuscriptGateway):
    def __init__(self) -> None:
        self.introduction_calls = 0
        self.call_contexts = []

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.assert_writer_prompt(prompt_key)
        self.call_contexts.append(call_context)
        sections = []
        for spec in payload["section_specs"]:
            if spec["section_id"] == "introduction":
                self.introduction_calls += 1
                if self.introduction_calls < 3:
                    sections.append(
                        ManuscriptSectionDraft(
                            section_id="introduction",
                            content_template="现有研究多聚焦其他问题。" * 30,
                        )
                    )
                    continue
            sections.append(self.section_draft(spec))
        return ManuscriptSectionDraftBatch(sections=sections)


class AnchorRepairGateway(FullManuscriptGateway):
    def __init__(
        self,
        bad_initial_sections: set[str],
        *,
        fail_first_repair: bool = False,
        fail_all_repairs: bool = False,
    ) -> None:
        self.bad_initial_sections = set(bad_initial_sections)
        self.fail_first_repair = fail_first_repair
        self.fail_all_repairs = fail_all_repairs
        self.calls: list[list[str]] = []
        self.payloads: list[dict] = []
        self.call_contexts = []

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.assert_writer_prompt(prompt_key)
        section_ids = [spec["section_id"] for spec in payload["section_specs"]]
        self.calls.append(section_ids)
        self.payloads.append(payload)
        self.call_contexts.append(call_context)
        is_repair = any(
            "revision_feedback" in spec for spec in payload["section_specs"]
        )
        if is_repair and (self.fail_first_repair or self.fail_all_repairs):
            self.fail_first_repair = False
            raise RuntimeError("targeted repair timeout")
        sections = []
        for spec in payload["section_specs"]:
            section = self.section_draft(spec)
            if not is_repair and spec["section_id"] in self.bad_initial_sections:
                section.content_template += "\n[[STATEMENT:unknown-anchor]]"
            sections.append(section)
        return ManuscriptSectionDraftBatch(sections=sections)


class ExhaustedRepairBatchGateway(FullManuscriptGateway):
    def __init__(
        self,
        target_section: str,
        repairable_section: str,
    ) -> None:
        self.target_section = target_section
        self.problem_sections = {target_section, repairable_section}
        self.calls: list[list[str]] = []
        self.call_contexts = []

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.assert_writer_prompt(prompt_key)
        section_ids = [
            spec["section_id"] for spec in payload["section_specs"]
        ]
        self.calls.append(section_ids)
        self.call_contexts.append(call_context)
        is_repair = any(
            "revision_feedback" in spec for spec in payload["section_specs"]
        )
        if is_repair and self.target_section in section_ids:
            raise RuntimeError(
                f"逻辑模型调用 {call_context.logical_call_id} 已达最多 3 次尝试。"
            )
        sections = []
        for spec in payload["section_specs"]:
            section = self.section_draft(spec)
            if not is_repair and spec["section_id"] in self.problem_sections:
                section.content_template = "现有研究多聚焦其他问题。" * 30
            sections.append(section)
        return ManuscriptSectionDraftBatch(sections=sections)


class PersistentUnsafeWriterGateway(FullManuscriptGateway):
    def __init__(self, unsafe_sections: set[str]) -> None:
        self.unsafe_sections = set(unsafe_sections)
        self.calls: list[list[str]] = []
        self.call_contexts = []

    async def generate(
        self,
        prompt_key,
        payload,
        output_model,
        *,
        call_context=None,
    ):
        self.assert_writer_prompt(prompt_key)
        self.calls.append(
            [spec["section_id"] for spec in payload["section_specs"]]
        )
        self.call_contexts.append(call_context)
        return ManuscriptSectionDraftBatch(
            sections=[
                ManuscriptSectionDraft(
                    section_id=spec["section_id"],
                    content_template=(
                        "回归结果显示未经锚点授权的方向判断。" * 12
                        if spec["section_id"] in self.unsafe_sections
                        else self.section_draft(spec).content_template
                    ),
                )
                for spec in payload["section_specs"]
            ]
        )


class WorkflowEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "runs.db"
        self.repository = RunRepository(self.db_path)
        self.engine = WorkflowEngine(self.repository)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _to_h2(self, preset_case_id: str = "green-finance-did"):
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id=preset_case_id)
        )
        return await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="approve-h1"),
        )

    async def _to_h3(self, preset_case_id: str = "green-finance-did"):
        run = await self._to_h2(preset_case_id)
        return await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="approve-h2"),
        )

    async def test_explicit_v2_engine_builds_the_frozen_split_budget(self) -> None:
        engine = WorkflowEngine(
            self.repository,
            model_call_budget_mode="v2",
        )
        run = await engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )
        snapshot = engine._model_budget(run).snapshot()
        self.assertEqual(snapshot["budget_mode"], "v2")
        self.assertEqual(snapshot["provider_attempt_ceiling"], 40)
        self.assertEqual(snapshot["logical_call_ceiling"], 20)
        self.assertEqual(snapshot["group_counting_unit"], "logical_call")

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
        self.assertEqual(
            run.artifacts["evidence_figure_bundle"]["payload"]["status"],
            "not_generated",
        )
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
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertNotIn("sealed_output", run.artifacts)
        self.assertEqual(
            run.artifacts["publication_figure_bundle"]["payload"]["status"],
            "not_generated",
        )
        run = await self.engine.decide_gate(
            run.id,
            "H4",
            GateDecisionRequest(action="approve", idempotency_key="fixture-h4"),
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

    async def test_spatial_profile_and_h2_binding_freeze_visible_weights(self) -> None:
        root = Path(self.tempdir.name) / "spatial-profile"
        root.mkdir()
        data_path = root / "main_data.csv"
        data_path.write_text(
            "region,year,y,x,size\n"
            "A,2020,1,2,3\n"
            "A,2021,2,3,4\n"
            "B,2020,3,4,5\n"
            "B,2021,4,5,6\n",
            encoding="utf-8",
        )
        weights_path = root / "spatial_weights.csv"
        weights_path.write_text(
            "spatial_id,A,B\nA,0,1\nB,1,0\n",
            encoding="utf-8",
        )
        registry = DatasetRegistry(root / "datasets.json")

        def register(path: Path, role: str) -> DatasetRef:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            reference = DatasetRef(
                dataset_id=f"ds_{digest[:16]}",
                role=role,
                filename=path.name,
                sha256=digest,
                size_bytes=path.stat().st_size,
            )
            registry.register(reference, path)
            return reference

        data_ref = register(data_path, "main")
        weights_ref = register(weights_path, "supplementary")
        package = ResearchPackage(
            case_id="spatial-profile",
            title="空间面板测试",
            research_question="x 是否与本地及相邻地区的 y 相关？",
            hypotheses=[{"hypothesis_id": "H1", "statement": "存在空间关联。"}],
            unit_of_analysis="地区—年度",
            sample_period="2020—2021",
            data_structure_hint="spatial_panel",
            variables=[
                {"name": "region", "role": "id", "definition": "地区"},
                {"name": "region", "role": "spatial_id", "definition": "空间地区"},
                {"name": "year", "role": "time", "definition": "年份"},
                {"name": "y", "role": "outcome", "definition": "结果"},
                {"name": "x", "role": "exposure", "definition": "解释变量"},
                {"name": "size", "role": "control", "definition": "控制变量"},
            ],
            dataset_refs=[data_ref, weights_ref],
            input_conflicts=[],
            missing_required_information=[],
        )
        engine = WorkflowEngine(self.repository, dataset_registry=registry)

        profile = engine._profile(package)

        self.assertEqual(profile.readiness, "ready")
        self.assertEqual(profile.spatial_key, "region")
        self.assertTrue(any(weights_ref.sha256 in fact for fact in profile.confirmed_facts))

        plan = AnalysisPlan(
            plan_id="spatial-plan",
            plan_version=1,
            method_family="spatial",
            design_only=False,
            estimands=[],
            sample_rules=[],
            variable_construction=[],
            baseline_models=[
                ModelSpec(
                    step_id="spatial-baseline",
                    name="空间面板模型",
                    rationale="区分本地和跨地区关联",
                    estimator="Spatial Durbin panel model",
                    formula="y ~ x + size + W:y + W:x + W:size",
                    outcome="y",
                    treatments_or_exposures=["x"],
                    controls=["size"],
                    fixed_effects=["region", "year"],
                )
            ],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
            identification_assumptions=[],
            alternative_explanations=[],
            failure_conditions=[],
            stop_conditions=[],
            required_data_fields=["region", "year", "y", "x", "size"],
            unsupported_requested_analyses=[],
        )

        bound = engine._bind_spatial_assets(package, plan)
        engine._validate_spatial_plan(package, bound)

        probe_state = await engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )
        probe = engine._probe_candidate(
            probe_state,
            package,
            profile,
            MethodRoute(
                route_status="routed",
                research_goal="associational",
                primary_route="spatial",
                route_reason=["空间面板"],
                required_assumptions=[],
                testable_assumptions=[],
                untestable_assumptions=[],
                alternative_routes=[],
                rejected_routes=[],
                missing_information=[],
            ),
            DesignEnvelope(
                research_goal="associational",
                target_estimands=["本地直接关联", "跨地区间接关联"],
            ),
            "candidate-spatial-fe",
            bound,
        )
        panel_effects = next(
            check for check in probe.checks if check.check_id == "panel_effects"
        )
        self.assertEqual(panel_effects.status, "pass")

        parameters = bound.baseline_models[0].parameters
        self.assertEqual(parameters["spatial_weights_dataset_id"], weights_ref.dataset_id)
        self.assertEqual(parameters["spatial_weights_sha256"], weights_ref.sha256)
        self.assertEqual(parameters["spatially_lagged_covariates"], ["x", "size"])
        self.assertEqual(parameters["effect_decomposition"], ["direct", "indirect", "total"])

        sar_plan = plan.model_copy(
            update={
                "plan_id": "spatial-sar-plan",
                "baseline_models": [
                    plan.baseline_models[0].model_copy(
                        update={
                            "estimator": "Spatial lag panel model",
                            "formula": "y ~ x + size + W:y",
                            "parameters": {},
                        }
                    )
                ],
            }
        )
        bound_sar = engine._bind_spatial_assets(package, sar_plan)
        engine._validate_spatial_plan(package, bound_sar)
        self.assertEqual(bound_sar.baseline_models[0].parameters["spatial_model"], "sar")
        self.assertNotIn(
            "spatially_lagged_covariates",
            bound_sar.baseline_models[0].parameters,
        )

    async def test_h2_selects_one_viable_candidate_and_probe_never_reads_results(self) -> None:
        run = await self._to_h2()
        arena = DesignArena.model_validate(run.artifacts["design_arena"]["payload"])

        self.assertEqual(len(arena.candidates), 3)
        self.assertTrue(arena.recommended_candidate_ids)
        self.assertTrue(
            all(not candidate.probe_report.used_outcome_results for candidate in arena.candidates)
        )
        probe_step = next(step for step in run.steps if step.node_id == "probe_run")
        self.assertNotIn("p_value", str(probe_step.output))

        selected = arena.recommended_candidate_ids[-1]
        run = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(
                action="approve",
                selected_candidate_id=selected,
                idempotency_key="select-candidate-h2",
            ),
        )

        contract = FormalResearchContract.model_validate(
            run.artifacts["formal_research_contract"]["payload"]
        )
        selected_plan = next(
            candidate.plan for candidate in arena.candidates if candidate.candidate_id == selected
        )
        self.assertEqual(contract.approved_plan, selected_plan)
        self.assertEqual(run.decisions[-1].selected_candidate_id, selected)

    async def test_arena_keeps_only_viable_candidate_with_unknown_critical(
        self,
    ) -> None:
        with patch.object(
            self.engine,
            "_reviewer_gateway",
            return_value=CriticalUnknownReviewerGateway(),
        ):
            run = await self._to_h2("esg-panel")

        arena = DesignArena.model_validate(run.artifacts["design_arena"]["payload"])
        self.assertEqual(
            arena.recommended_candidate_ids,
            ["candidate-identification_first"],
        )
        self.assertEqual(
            arena.provisional_candidate_id,
            "candidate-identification_first",
        )
        selected = next(
            candidate
            for candidate in arena.candidates
            if candidate.candidate_id == arena.provisional_candidate_id
        )
        placeholder = next(
            step
            for step in selected.plan.robustness_tests
            if step.threat_id == "panel.future_technical_threat"
        )
        self.assertTrue(placeholder.required_for_admission)
        self.assertIn("not_executable", placeholder.not_executable_reason or "")
        critic = CriticReport.model_validate(
            run.artifacts["critic_report"]["payload"]
        )
        self.assertEqual(critic.verdict, "revise")

        advanced = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(
                action="approve",
                selected_candidate_id="candidate-identification_first",
                idempotency_key="critical-unknown-arena-h2",
            ),
        )
        self.assertEqual(
            (advanced.status, advanced.current_gate),
            ("waiting_human", "H3"),
        )
        self.assertIn("formal_research_contract", advanced.artifacts)

    async def test_h2_refresh_is_idempotent_with_unknown_reviewer_threat(self) -> None:
        run = await self._to_h2("esg-panel")
        arena = DesignArena.model_validate(run.artifacts["design_arena"]["payload"])
        selected = arena.provisional_candidate_id
        self.assertIsNotNone(selected)
        candidate_with_unknown = next(
            candidate
            for candidate in arena.candidates
            if candidate.candidate_id != selected
        )
        review = next(
            review
            for report in arena.reviewer_reports
            for review in report.candidate_reviews
            if review.candidate_id == candidate_with_unknown.candidate_id
        )
        review.issues.append(
            CriticIssue(
                issue_id="unknown-reviewer-issue",
                dimension="measurement",
                severity="minor",
                evidence="the threat is not in the frozen registry",
                why_it_matters="the issue must remain visible but not executable",
                required_fix="guess alternative_exposure=forbidden",
                return_stage="analysis_plan",
                repair_type="scientific",
                threat_id="panel.unregistered_threat",
            )
        )
        self.engine._put_artifact(run, "design_arena", arena)
        self.repository.save(run, expected_version=run.version)

        refreshed = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(
                action="approve",
                selected_candidate_id=selected,
                idempotency_key="unknown-threat-refresh",
            ),
        )
        self.assertEqual(
            (refreshed.status, refreshed.current_gate),
            ("waiting_human", "H2"),
        )
        refreshed_arena = DesignArena.model_validate(
            refreshed.artifacts["design_arena"]["payload"]
        )
        refreshed_candidate = next(
            candidate
            for candidate in refreshed_arena.candidates
            if candidate.candidate_id == candidate_with_unknown.candidate_id
        )
        placeholder = next(
            step
            for step in refreshed_candidate.plan.robustness_tests
            if step.threat_id == "panel.unregistered_threat"
        )
        self.assertEqual(placeholder.source_issue_ids, ["unknown-reviewer-issue"])
        self.assertTrue(placeholder.required_for_admission)
        self.assertIn("not_executable", placeholder.not_executable_reason or "")
        self.assertNotIn("alternative_exposure", str(placeholder.parameters))

        completed = await self.engine.decide_gate(
            refreshed.id,
            "H2",
            GateDecisionRequest(
                action="approve",
                selected_candidate_id=selected,
                idempotency_key="unknown-threat-approve",
            ),
        )
        self.assertEqual(
            (completed.status, completed.current_gate),
            ("waiting_human", "H3"),
        )
        self.assertIn("formal_research_contract", completed.artifacts)

    async def test_h2_keeps_critical_technical_unknown_threat_as_placeholder(
        self,
    ) -> None:
        run = await self._to_h2("esg-panel")
        arena = DesignArena.model_validate(run.artifacts["design_arena"]["payload"])
        selected = arena.provisional_candidate_id
        self.assertIsNotNone(selected)
        selected_review = next(
            review
            for report in arena.reviewer_reports
            for review in report.candidate_reviews
            if review.candidate_id == selected
        )
        selected_review.verdict = "revise"
        selected_review.issues.append(
            CriticIssue(
                issue_id="critical-technical-unknown",
                dimension="measurement",
                severity="critical",
                evidence="the model emitted an unregistered technical threat",
                why_it_matters="the issue must remain visible without blocking H2",
                required_fix="do not infer execution parameters from this prose",
                return_stage="analysis_plan",
                repair_type="technical",
                threat_id="panel.future_technical_threat",
            )
        )
        self.engine._put_artifact(run, "design_arena", arena)
        self.repository.save(run, expected_version=run.version)

        refreshed = await self.engine.decide_gate(
            run.id,
            "H2",
            GateDecisionRequest(
                action="approve",
                selected_candidate_id=selected,
                idempotency_key="critical-technical-refresh",
            ),
        )
        self.assertEqual(
            (refreshed.status, refreshed.current_gate),
            ("waiting_human", "H2"),
        )
        refreshed_arena = DesignArena.model_validate(
            refreshed.artifacts["design_arena"]["payload"]
        )
        refreshed_candidate = next(
            candidate
            for candidate in refreshed_arena.candidates
            if candidate.candidate_id == selected
        )
        placeholder = next(
            step
            for step in refreshed_candidate.plan.robustness_tests
            if step.threat_id == "panel.future_technical_threat"
        )
        self.assertTrue(placeholder.required_for_admission)
        self.assertIn("not_executable", placeholder.not_executable_reason or "")

        completed = await self.engine.decide_gate(
            refreshed.id,
            "H2",
            GateDecisionRequest(
                action="approve",
                selected_candidate_id=selected,
                idempotency_key="critical-technical-approve",
            ),
        )
        self.assertEqual(
            (completed.status, completed.current_gate),
            ("waiting_human", "H3"),
        )
        critic = CriticReport.model_validate(
            completed.artifacts["critic_report"]["payload"]
        )
        self.assertEqual(critic.verdict, "revise")
        self.assertIn("formal_research_contract", completed.artifacts)

    def test_reviewer_critical_issue_uses_code_owned_blocking_rules(
        self,
    ) -> None:
        technical = CriticIssue(
            issue_id="critical-technical",
            dimension="measurement",
            severity="critical",
            evidence="unregistered technical concern",
            why_it_matters="it must be preserved as a placeholder",
            required_fix="do not parse this prose",
            return_stage="analysis_plan",
            repair_type="technical",
            threat_id="panel.future_technical_threat",
        )
        human_required = technical.model_copy(
            update={
                "issue_id": "critical-human",
                "repair_type": "human_required",
                "return_stage": "human",
            }
        )
        registered = technical.model_copy(
            update={
                "issue_id": "critical-registered",
                "threat_id": THREAT_FE_CLUSTER_FEASIBILITY,
            }
        )
        scientific = technical.model_copy(
            update={
                "issue_id": "critical-policy-scientific",
                "repair_type": "scientific",
                "threat_id": "policy.entity_cluster_sensitivity",
            }
        )

        self.assertFalse(
            _is_reviewer_issue_blocking_design(
                technical,
                "panel_association",
            )
        )
        self.assertTrue(
            _is_reviewer_issue_blocking_design(
                human_required,
                "panel_association",
            )
        )
        self.assertTrue(
            _is_reviewer_issue_blocking_design(
                registered,
                "panel_association",
            )
        )
        self.assertFalse(
            _is_reviewer_issue_blocking_design(
                technical,
                "policy_causal",
            )
        )
        self.assertFalse(
            _is_reviewer_issue_blocking_design(
                scientific,
                "policy_causal",
            )
        )
        self.assertTrue(
            _is_reviewer_issue_blocking_design(
                human_required,
                "policy_causal",
            )
        )
        policy_replication_boundary = human_required.model_copy(
            update={
                "issue_id": "critical-policy-replication-boundary",
                "threat_id": "policy.independent_replication",
            }
        )
        self.assertFalse(
            _is_reviewer_issue_blocking_design(
                policy_replication_boundary,
                "policy_causal",
            )
        )

    def test_reproduction_comparison_ignores_run_ids_but_not_results(self) -> None:
        primary = ResearchRun(
            research_run_id="primary",
            case_id="case",
            contract_hash="plan",
            plan_version=1,
            execution_status="succeeded",
            scientific_status="limited",
            fixture_only=False,
            executions=[
                {
                    "execution_id": "execution-a",
                    "run_type": "baseline",
                    "plan_step_id": "model",
                    "execution_status": "succeeded",
                    "estimates": [{"term": "x", "coefficient": 0.25}],
                }
            ],
        )
        replication = primary.model_copy(deep=True)
        replication.research_run_id = "replication"
        replication.executions[0].execution_id = "execution-b"
        self.assertEqual(self.engine._research_run_differences(primary, replication), [])

        replication.executions[0].estimates[0]["coefficient"] = 0.5
        self.assertTrue(self.engine._research_run_differences(primary, replication))

    async def test_method_route_is_deterministic_and_schema_safe(self) -> None:
        run = await self._to_h2()
        route_step = next(step for step in run.steps if step.node_id == "method_route")

        self.assertEqual(route_step.status, "succeeded")
        self.assertEqual(route_step.prompts[0].role, "code")
        self.assertEqual(run.artifacts["method_route"]["payload"]["route_status"], "routed")

    async def test_external_policy_plan_binds_code_owned_executable_steps(self) -> None:
        package = ResearchPackage(
            case_id="policy-normalizer",
            title="policy normalizer",
            research_question="policy effect?",
            hypotheses=[{"hypothesis_id": "H1", "statement": "effect"}],
            variables=[
                {"name": "firm", "role": "id"},
                {"name": "year", "role": "time"},
                {"name": "group", "role": "exposure"},
                {"name": "y", "role": "outcome"},
                {"name": "y_alt", "role": "outcome"},
                {"name": "x", "role": "control"},
            ],
            dataset_refs=[
                {
                    "dataset_id": "policy-data",
                    "filename": "policy.csv",
                    "role": "main",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                }
            ],
            design_envelope=DesignEnvelope(
                benchmark_track="reproduction_aligned",
                research_goal="causal",
            ),
            policy_design={
                "policy_date": "2007-07",
                "group_field": "group",
                "time_field": "year",
                "fixed_effects": ["firm", "year"],
                "cluster_fields": ["firm", "year"],
                "event_reference_year": 2006,
                "event_years": [2005, 2007, 2008],
                "event_remote_pre_years": [2004],
            },
        )
        profile = DataProfile(
            profile_execution_status="succeeded",
            data_structure="panel",
            unit_of_observation="firm-year",
            entity_key=["firm"],
            time_key="year",
            readiness="ready",
        )
        plan = AnalysisPlan(
            plan_id="draft-policy",
            plan_version=1,
            method_family="policy_causal",
            design_only=True,
            estimands=[],
            sample_rules=[],
            variable_construction=[],
            baseline_models=[],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
            identification_assumptions=[],
            alternative_explanations=[],
            failure_conditions=[],
            stop_conditions=[],
            required_data_fields=[],
            unsupported_requested_analyses=[],
        )

        normalized = _normalize_external_policy_candidate_plan(
            plan,
            profile,
            package,
            "external",
            "identification_first",
        )

        self.assertFalse(normalized.design_only)
        self.assertEqual(normalized.baseline_models[0].outcome, "y")
        self.assertEqual(normalized.baseline_models[0].controls, ["x"])
        self.assertEqual(normalized.check_registry_version, "policy-did-v2")
        self.assertEqual(
            {item.step_id for item in normalized.falsification_tests},
            {
                "check-policy-event-study",
                "check-policy-placebo-time",
                "check-policy-permutation-placebo",
            },
        )
        self.assertTrue(
            {
                "check-policy-group-fixed-pre",
                "check-policy-group-stable-only",
                "check-policy-cluster-entity",
            }.issubset(
                {item.step_id for item in normalized.robustness_tests}
            )
        )
        policy_contract = normalized.baseline_models[0].parameters["policy_design"]
        self.assertEqual(policy_contract["placebo_repetitions"], 500)
        self.assertEqual(
            policy_contract["permutation_scheme"],
            "assignment_unit_label",
        )
        self.assertEqual(policy_contract["permutation_unit_field"], "firm")
        self.assertEqual(
            policy_contract["group_assignment_mode"],
            "observed_time_varying",
        )

        direct = _normalize_external_policy_candidate_plan(
            plan,
            profile,
            package,
            "external",
            "direct_baseline",
        )
        self.assertEqual(
            direct.baseline_models[0].controls,
            ["x"],
            "reproduction-aligned candidates must all preserve frozen controls",
        )
        self.assertEqual(
            direct.baseline_models[0].parameters["policy_design"][
                "event_remote_pre_years"
            ],
            [2004],
        )
        self.assertEqual(
            direct.baseline_models[0].parameters["policy_design"][
                "event_term_scaling"
            ],
            "binary_group_year_contrast",
        )

        state = RunState(
            case_id=package.case_id,
            case_name=package.title,
            mode="research",
            execution_mode="external",
            case_submission=CaseSubmission.model_validate(
                package.model_dump(
                    mode="json",
                    include=set(CaseSubmission.model_fields),
                )
            ),
        )
        route = MethodRoute(
            route_status="routed",
            research_goal="causal",
            primary_route="policy_causal",
            route_reason=["policy contract"],
            required_assumptions=[],
            testable_assumptions=[],
            untestable_assumptions=[],
            alternative_routes=[],
            rejected_routes=[],
            missing_information=[],
        )
        second_baseline = normalized.baseline_models[0].model_copy(
            update={"step_id": "model_baseline_secondary"}
        )
        invalid_plans = {
            "baseline_cardinality": normalized.model_copy(
                update={
                    "baseline_models": [
                        normalized.baseline_models[0],
                        second_baseline,
                    ]
                }
            ),
            "design_only": normalized.model_copy(update={"design_only": True}),
            "registry": normalized.model_copy(update={"check_registry_version": None}),
            "policy_design": normalized.model_copy(
                update={
                    "baseline_models": [
                        normalized.baseline_models[0].model_copy(
                            update={"parameters": {"policy_design": {}}}
                        )
                    ]
                }
            ),
        }
        for label, invalid_plan in invalid_plans.items():
            with self.subTest(label=label):
                probe = self.engine._probe_candidate(
                    state,
                    package,
                    profile,
                    route,
                    package.design_envelope,
                    f"candidate-{label}",
                    invalid_plan,
                )
                contract_check = next(
                    check
                    for check in probe.checks
                    if check.check_id == "policy_execution_contract"
                )
                self.assertEqual(contract_check.status, "fail")
                self.assertEqual(probe.verdict, "fail")
                self.assertFalse(probe.executor_ready)

        invalid_candidate = DesignCandidate(
            candidate_id="candidate-baseline-cardinality",
            strategy="measurement_robustness",
            rationale="regression fixture for the RC4 contract failure",
            plan=invalid_plans["baseline_cardinality"],
            probe_report=ProbeReport(
                report_id="stale-probe",
                candidate_id="candidate-baseline-cardinality",
                verdict="pass",
                checks=[],
                executor_ready=True,
            ),
        )
        arena = DesignArena(
            arena_id="stale-policy-arena",
            candidates=[invalid_candidate],
            reviewer_reports=[],
            recommended_candidate_ids=[invalid_candidate.candidate_id],
            provisional_candidate_id=invalid_candidate.candidate_id,
            selection_rationale=["simulate a stale pre-fix Probe artifact"],
        )
        self.engine._put_artifact(state, "research_package", package)
        self.engine._put_artifact(state, "design_arena", arena)
        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "exactly one baseline model",
        ):
            await self.engine._after_h2(
                state,
                DecisionRecord(
                    gate="H2",
                    action="approve",
                    actor="tester",
                    selected_candidate_id=invalid_candidate.candidate_id,
                ),
            )
        self.assertNotIn("formal_research_contract", state.artifacts)
        self.assertEqual(state.execution_status, "not_started")
        self.assertEqual(state.scientific_status, "not_evaluated")

    async def test_primary_research_run_status_survives_replication_failure(
        self,
    ) -> None:
        state = await self._to_h2("esg-panel")
        state.mode = "research"
        state.execution_mode = "external"
        arena = self.engine._artifact(state, "design_arena", DesignArena)

        class FailedPrimaryExecutor:
            executor_name = "failed primary executor"

            async def execute(self, contract):
                return ResearchRun(
                    research_run_id="failed-primary-run",
                    case_id=contract.case_id,
                    contract_hash=contract.approved_plan_hash,
                    plan_version=contract.approved_plan.plan_version,
                    execution_status="failed",
                    scientific_status="invalid",
                    fixture_only=False,
                    not_executed_reason="frozen execution failed",
                    failed_runs=["baseline failed"],
                )

        class FailedReproducer:
            reproducer_name = "failed reproducer"

            async def execute(self, contract):
                raise RuntimeError("replication unavailable")

        with (
            patch(
                "hypoweaver.engine.HttpResearchExecutor",
                return_value=FailedPrimaryExecutor(),
            ),
            patch(
                "hypoweaver.engine.HttpResearchReproducer",
                return_value=FailedReproducer(),
            ),
        ):
            await self.engine._after_h2(
                state,
                DecisionRecord(
                    gate="H2",
                    action="approve",
                    actor="tester",
                    selected_candidate_id=arena.provisional_candidate_id,
                ),
            )

        self.assertEqual(state.status, "blocked")
        self.assertEqual(state.current_node_id, "reproduction_audit")
        self.assertEqual(state.execution_status, "failed")
        self.assertEqual(state.scientific_status, "invalid")
        persisted_run = self.engine._artifact(state, "research_run", ResearchRun)
        self.assertEqual(persisted_run.research_run_id, "failed-primary-run")

    async def test_policy_critical_cluster_risk_reaches_h2_with_entity_sensitivity(
        self,
    ) -> None:
        package = ResearchPackage(
            case_id="policy-r2-regression",
            title="policy r2 regression",
            research_question="policy effect?",
            hypotheses=[{"hypothesis_id": "H1", "statement": "effect"}],
            variables=[
                {"name": "firm", "role": "id"},
                {"name": "year", "role": "time"},
                {"name": "group", "role": "exposure"},
                {"name": "y", "role": "outcome"},
                {"name": "y_alt", "role": "outcome"},
                {"name": "x", "role": "control"},
                {"name": "industry", "role": "cluster"},
            ],
            dataset_refs=[
                {
                    "dataset_id": "policy-r2-data",
                    "filename": "policy.csv",
                    "role": "main",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                }
            ],
            design_envelope=DesignEnvelope(
                benchmark_track="reproduction_aligned",
                research_goal="causal",
            ),
            policy_design={
                "policy_date": "2007-07",
                "group_field": "group",
                "time_field": "year",
                "fixed_effects": ["firm", "year"],
                "cluster_fields": ["industry", "year"],
                "event_reference_year": 2006,
                "event_years": [2005, 2007, 2008],
            },
        )
        profile = DataProfile(
            profile_execution_status="succeeded",
            data_structure="panel",
            unit_of_observation="firm-year",
            entity_key=["firm"],
            time_key="year",
            readiness="ready",
        )
        route = MethodRoute(
            route_status="routed",
            research_goal="causal",
            primary_route="policy_causal",
            route_reason=["frozen policy DID"],
            required_assumptions=[],
            testable_assumptions=[],
            untestable_assumptions=[],
            alternative_routes=[],
            rejected_routes=[],
            missing_information=[],
        )
        draft = AnalysisPlan(
            plan_id="draft-policy-r2",
            plan_version=1,
            method_family="policy_causal",
            design_only=True,
            estimands=[],
            sample_rules=[],
            variable_construction=[],
            baseline_models=[],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
            identification_assumptions=[],
            alternative_explanations=[],
            failure_conditions=[],
            stop_conditions=[],
            required_data_fields=[],
            unsupported_requested_analyses=[],
        )
        candidates = []
        for strategy in (
            "direct_baseline",
            "identification_first",
            "measurement_robustness",
        ):
            candidate_id = f"candidate-{strategy}"
            plan = _normalize_external_policy_candidate_plan(
                draft.model_copy(update={"plan_id": f"plan-{strategy}"}),
                profile,
                package,
                "external",
                strategy,
            )
            candidates.append(
                DesignCandidate(
                    candidate_id=candidate_id,
                    strategy=strategy,
                    rationale=strategy,
                    plan=plan,
                    probe_report=ProbeReport(
                        report_id=f"probe-{candidate_id}",
                        candidate_id=candidate_id,
                        verdict="warn",
                        checks=[
                            ProbeCheck(
                                check_id="policy_cluster_support",
                                status="warn",
                                evidence="many frozen interaction clusters are singletons",
                            )
                        ],
                        executor_ready=True,
                    ),
                )
            )
        candidate_set = CandidateDesignSet(
            candidate_set_id="policy-r2-candidates",
            candidates=candidates,
        )
        state = await self.engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )

        with patch.object(
            self.engine,
            "_reviewer_gateway",
            return_value=PolicyCriticalReviewerGateway(),
        ):
            await self.engine._review_design_arena(
                state,
                package,
                profile,
                route,
                package.design_envelope,
                candidate_set,
            )

        self.assertEqual((state.status, state.current_gate), ("waiting_human", "H2"))
        arena = DesignArena.model_validate(state.artifacts["design_arena"]["payload"])
        self.assertEqual(
            set(arena.recommended_candidate_ids),
            {candidate.candidate_id for candidate in candidates},
        )
        for candidate in arena.candidates:
            self.assertEqual(candidate.plan.baseline_models[0].controls, ["x"])
            entity_cluster = next(
                step
                for step in candidate.plan.robustness_tests
                if step.threat_id == "policy.entity_cluster_sensitivity"
            )
            self.assertTrue(entity_cluster.required_for_admission)
        statistical = next(
            report
            for report in arena.reviewer_reports
            if report.dimension == "statistical"
        )
        for review in statistical.candidate_reviews:
            self.assertIn(
                "policy.entity_cluster_sensitivity",
                {issue.threat_id for issue in review.issues},
            )
        critic = CriticReport.model_validate(state.artifacts["critic_report"]["payload"])
        self.assertEqual(critic.verdict, "revise")
        self.assertTrue(
            any(
                "disposition=delegated_to_frozen_test_dag_and_claim_gate" in risk
                for risk in critic.remaining_risks
            )
        )

    def test_blocked_arena_critic_aggregates_all_candidates(self) -> None:
        technical = CriticIssue(
            issue_id="technical-a",
            dimension="statistical",
            severity="critical",
            evidence="repairable",
            why_it_matters="risk",
            required_fix="frozen check",
            return_stage="analysis_plan",
            repair_type="technical",
            threat_id="policy.entity_cluster_sensitivity",
        )
        human = technical.model_copy(
            update={
                "issue_id": "human-b",
                "repair_type": "human_required",
                "return_stage": "human",
            }
        )
        candidates = [
            DesignCandidate(
                candidate_id=f"candidate-{suffix}",
                strategy=strategy,
                rationale=strategy,
                plan=AnalysisPlan(
                    plan_id=f"plan-{suffix}",
                    plan_version=1,
                    method_family="policy_causal",
                    design_only=True,
                    estimands=[],
                    sample_rules=[],
                    variable_construction=[],
                    baseline_models=[],
                    diagnostics=[],
                    robustness_tests=[],
                    falsification_tests=[],
                    mechanism_tests=[],
                    heterogeneity_tests=[],
                    identification_assumptions=[],
                    alternative_explanations=[],
                    failure_conditions=[],
                    stop_conditions=[],
                    required_data_fields=[],
                    unsupported_requested_analyses=[],
                ),
                probe_report=ProbeReport(
                    report_id=f"probe-{suffix}",
                    candidate_id=f"candidate-{suffix}",
                    verdict="warn",
                    checks=[],
                    executor_ready=True,
                ),
            )
            for suffix, strategy in (
                ("a", "direct_baseline"),
                ("b", "identification_first"),
            )
        ]
        arena = DesignArena(
            arena_id="blocked-arena",
            candidates=candidates,
            reviewer_reports=[
                {
                    "report_id": "blocked-statistical",
                    "dimension": "statistical",
                    "reviewer_policy": "fixture",
                    "candidate_reviews": [
                        {
                            "candidate_id": "candidate-a",
                            "verdict": "reject",
                            "issues": [technical],
                        },
                        {
                            "candidate_id": "candidate-b",
                            "verdict": "revise",
                            "issues": [human],
                        },
                    ],
                }
            ],
            recommended_candidate_ids=[],
            provisional_candidate_id=None,
            selection_rationale=[],
        )

        summary = self.engine._critic_report_for_blocked_arena(arena)

        self.assertEqual(summary.report_id, "arena-critic-all-candidates")
        self.assertEqual(summary.verdict, "blocked")
        self.assertEqual({issue.issue_id for issue in summary.issues}, {"technical-a", "human-b"})
        rendered = "\n".join(summary.remaining_risks)
        self.assertIn("candidate-a", rendered)
        self.assertIn("Reviewer reject=statistical", rendered)
        self.assertIn("candidate-b", rendered)
        self.assertIn("blocking issues=human-b", rendered)

    def test_policy_reviewer_prompt_lists_the_code_owned_threat_registry(self) -> None:
        prompt = get_prompt("reviewer_report_batch")
        self.assertEqual(prompt.version, "1.1.2")
        system = prompt.system
        for threat_id in (
            "policy.group_time_support",
            "policy.event_study_pretrends",
            "policy.placebo_timing",
            "policy.group_fixed_last_pre",
            "policy.group_stable_entities_only",
            "policy.entity_cluster_sensitivity",
            "policy.permutation_placebo",
            "policy.alternative_outcome",
            "policy.independent_replication",
        ):
            self.assertIn(threat_id, system)
        self.assertIn("repair_type=technical/scientific", system)
        self.assertIn("analysis-ready", system)
        self.assertIn("缺少更上游的原始数据清洗或 ETL 日志", system)

    async def test_failed_design_retry_reuses_completed_candidates(self) -> None:
        run = await self._to_h2()
        design_node = (
            "design_"
            + run.artifacts["method_route"]["payload"]["primary_route"]
        )
        completed_design_steps = len(
            [
                step
                for step in run.steps
                if step.node_id == design_node and step.status == "succeeded"
            ]
        )
        for key in (
            "candidate_design_set",
            "design_arena",
            "analysis_plan",
            "critic_report",
        ):
            run.artifacts.pop(key, None)
        run.status = "failed"
        run.current_gate = None
        run.current_node_id = design_node
        run.last_error = "temporary design failure"
        run = self.repository.save(run, expected_version=run.version)

        run = await self.engine.retry_design(run.id)

        self.assertEqual(
            (run.status, run.current_gate),
            ("waiting_human", "H2"),
            msg=run.last_error,
        )
        self.assertEqual(
            len(
                [
                    step
                    for step in run.steps
                    if step.node_id == design_node and step.status == "succeeded"
                ]
            ),
            completed_design_steps,
        )
        self.assertEqual(
            len(
                [
                    step
                    for step in run.steps
                    if step.node_id.startswith(f"{design_node}_batch_")
                    and step.status == "succeeded"
                ]
            ),
            2,
        )

    async def test_design_retry_only_calls_missing_candidate_batch(self) -> None:
        gateway = FailOnceBatchGateway(
            "candidate_plan_batch",
            "candidate_strategies",
            ["measurement_robustness"],
        )
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self._to_h2()
            self.assertEqual((run.status, run.current_gate), ("failed", None))
            run = await self.engine.retry_design(run.id)

        self.assertEqual(
            (run.status, run.current_gate),
            ("waiting_human", "H2"),
            msg=run.last_error,
        )
        candidate_calls = [
            call
            for call in gateway.calls
            if call["prompt_key"] == "candidate_plan_batch"
        ]
        direct_calls = [
            call
            for call in candidate_calls
            if call["payload"]["candidate_strategies"]
            == ["direct_baseline", "identification_first"]
        ]
        missing_calls = [
            call
            for call in candidate_calls
            if call["payload"]["candidate_strategies"]
            == ["measurement_robustness"]
        ]
        self.assertEqual(len(direct_calls), 1)
        self.assertEqual(len(missing_calls), 2)
        self.assertEqual(
            missing_calls[0]["call_context"].logical_call_id,
            missing_calls[1]["call_context"].logical_call_id,
        )
        self.assertEqual(
            missing_calls[0]["call_context"].logical_call_id,
            f"{run.id}:design_policy_causal_batch_2",
        )

    async def test_design_retry_only_calls_missing_reviewer_batch(self) -> None:
        gateway = FailOnceBatchGateway(
            "reviewer_report_batch",
            "dimensions",
            ["causal", "statistical"],
        )
        with patch.object(
            self.engine,
            "_reviewer_gateway",
            return_value=gateway,
        ):
            run = await self._to_h2()
            self.assertEqual((run.status, run.current_gate), ("failed", None))
            run = await self.engine.retry_design(run.id)

        self.assertEqual(
            (run.status, run.current_gate),
            ("waiting_human", "H2"),
            msg=run.last_error,
        )
        measurement_calls = [
            call
            for call in gateway.calls
            if call["payload"]["dimensions"]
            == ["measurement", "reproducibility"]
        ]
        causal_calls = [
            call
            for call in gateway.calls
            if call["payload"]["dimensions"] == ["causal", "statistical"]
        ]
        self.assertEqual(len(measurement_calls), 1)
        self.assertEqual(len(causal_calls), 2)
        self.assertEqual(
            causal_calls[0]["call_context"].logical_call_id,
            causal_calls[1]["call_context"].logical_call_id,
        )
        self.assertEqual(
            causal_calls[0]["call_context"].logical_call_id,
            f"{run.id}:design_reviewer_batch_2",
        )

    async def test_qwen_reviewer_batches_are_serial_without_network(self) -> None:
        run = await self._to_h2()
        package = self.engine._artifact(run, "research_package", ResearchPackage)
        profile = self.engine._artifact(run, "data_profile", DataProfile)
        route = self.engine._artifact(run, "method_route", MethodRoute)
        envelope = self.engine._artifact(run, "design_envelope", DesignEnvelope)
        candidate_set = self.engine._artifact(
            run,
            "candidate_design_set",
            CandidateDesignSet,
        )
        run.steps = [
            step
            for step in run.steps
            if not step.node_id.startswith("design_reviewer_batch_")
            and not step.node_id.startswith("critic_")
        ]
        run.model_provider = "qwen"
        gateway = ActiveCallTrackingGateway()

        with patch.object(
            self.engine,
            "_reviewer_gateway",
            return_value=gateway,
        ):
            await self.engine._review_design_arena(
                run,
                package,
                profile,
                route,
                envelope,
                candidate_set,
            )

        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.max_active, 1)

    async def test_qwen_reviewer_batches_fail_fast_without_starting_sibling(
        self,
    ) -> None:
        run = await self._to_h2()
        package = self.engine._artifact(run, "research_package", ResearchPackage)
        profile = self.engine._artifact(run, "data_profile", DataProfile)
        route = self.engine._artifact(run, "method_route", MethodRoute)
        envelope = self.engine._artifact(run, "design_envelope", DesignEnvelope)
        candidate_set = self.engine._artifact(
            run,
            "candidate_design_set",
            CandidateDesignSet,
        )
        run.steps = [
            step
            for step in run.steps
            if not step.node_id.startswith("design_reviewer_batch_")
            and not step.node_id.startswith("critic_")
        ]
        run.model_provider = "qwen"
        gateway = FailOnceBatchGateway(
            "reviewer_report_batch",
            "dimensions",
            ["measurement", "reproducibility"],
        )

        with (
            patch.object(
                self.engine,
                "_reviewer_gateway",
                return_value=gateway,
            ),
            self.assertRaisesRegex(RuntimeError, "fixture batch failure"),
        ):
            await self.engine._review_design_arena(
                run,
                package,
                profile,
                route,
                envelope,
                candidate_set,
            )

        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(
            gateway.calls[0]["payload"]["dimensions"],
            ["measurement", "reproducibility"],
        )

    async def test_qwen_design_retry_rejects_incomplete_receipt_evidence(self) -> None:
        gateway = FailOnceBatchGateway(
            "candidate_plan_batch",
            "candidate_strategies",
            ["measurement_robustness"],
        )
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self._to_h2()
        self.assertEqual((run.status, run.current_gate), ("failed", None))
        run.model_provider = "qwen"
        self.engine._put_artifact(
            run,
            "model_usage",
            {
                "llm_calls": 1,
                "call_receipts": [],
            },
        )
        run = self.repository.save(run, expected_version=run.version)

        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "receipt 数量不一致",
        ):
            await self.engine.retry_design(run.id)

    async def test_design_arena_blocks_when_one_required_candidate_batch_fails(
        self,
    ) -> None:
        original_llm_step = self.engine._llm_step

        async def fail_one_candidate(*args, **kwargs):
            prompt_key = args[2]
            payload = args[3]
            if (
                prompt_key == "candidate_plan_batch"
                and "direct_baseline" in payload["candidate_strategies"]
            ):
                raise RuntimeError("candidate timeout")
            return await original_llm_step(*args, **kwargs)

        with patch.object(self.engine, "_llm_step", side_effect=fail_one_candidate):
            run = await self._to_h2()

        self.assertEqual((run.status, run.current_gate), ("failed", None))
        self.assertTrue(run.current_node_id.startswith("design_policy_causal"))
        self.assertIn("candidate timeout", run.last_error or "")
        self.assertNotIn("candidate_design_set", run.artifacts)

    def test_explicit_mechanism_goal_wins_over_incidental_index_word(self) -> None:
        package = ResearchPackage(
            case_id="mechanism-with-index-provider",
            title="企业评级表现与融资期限",
            research_question="企业评级表现是否与融资期限有关？",
            hypotheses=[{"hypothesis_id": "H1", "statement": "二者存在关联。"}],
            unit_of_analysis="企业—年度",
            data_structure_hint="panel",
            variables=[
                {"name": "firm_id", "role": "id"},
                {"name": "year", "role": "time"},
                {"name": "y", "role": "outcome"},
                {"name": "x", "role": "exposure"},
                {"name": "m", "role": "mediator"},
            ],
            design_envelope=DesignEnvelope(research_goal="mechanism"),
            known_policy_facts=["该评级由某指数提供方发布。"],
        )
        profile = DataProfile(
            profile_execution_status="succeeded",
            data_structure="panel",
            unit_of_observation="企业—年度",
            entity_key=["firm_id"],
            time_key="year",
            readiness="ready",
        )

        route = MethodRoute.model_validate(
            FixtureModelGateway._route(
                {
                    "research_package": package.model_dump(mode="json"),
                    "data_profile": profile.model_dump(mode="json"),
                }
            )
        )

        self.assertEqual(route.primary_route, "mechanism_boundary")
        self.assertEqual(route.research_goal, "mechanism")

    def test_enterprise_mechanism_check_targets_only_declared_mechanism_claims(self) -> None:
        package = ResearchPackage(
            case_id="mechanism-scope",
            title="机制作用域",
            research_question="x 与 y 的关联边界是什么？",
            hypotheses=[
                {"hypothesis_id": "H1", "statement": "x 与 y 存在主关联。"},
                {
                    "hypothesis_id": "H2",
                    "statement": "m 可能刻画关联边界。",
                    "mechanism": "m 是预先声明的机制边界。",
                },
            ],
            variables=[
                {"name": "firm", "role": "id"},
                {"name": "year", "role": "time"},
                {"name": "y", "role": "outcome"},
                {"name": "x", "role": "exposure"},
                {"name": "m", "role": "mediator"},
            ],
        )
        plan = AnalysisPlan(
            plan_id="mechanism-scope-plan",
            plan_version=1,
            method_family="mechanism_boundary",
            design_only=False,
            estimands=[],
            sample_rules=[],
            variable_construction=[],
            baseline_models=[
                ModelSpec(
                    step_id="baseline",
                    name="baseline",
                    rationale="frozen baseline",
                    estimator="PanelOLS",
                    outcome="y",
                    treatments_or_exposures=["x"],
                    fixed_effects=["firm", "year"],
                    standard_error_strategy="clustered by firm",
                )
            ],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
            identification_assumptions=[],
            alternative_explanations=[],
            failure_conditions=[],
            stop_conditions=[],
            required_data_fields=["firm", "year", "y", "x", "m"],
            unsupported_requested_analyses=[],
        )

        compiled = self.engine._compile_enterprise_panel_plan(plan, package, [])
        mechanism = next(
            item
            for item in compiled.mechanism_tests
            if item.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
        )

        self.assertEqual(mechanism.target_claim_ids, ["claim-H2"])
        self.assertNotIn("claim-H1", mechanism.target_claim_ids)

    def test_analysis_design_prompt_distinguishes_model_and_planned_steps(self) -> None:
        prompt = get_prompt("analysis_design")

        self.assertEqual(prompt.version, "1.6.0")
        self.assertIn("baseline_models 的元素使用 ModelSpec", prompt.system)
        self.assertIn("必须严格使用 PlannedStep", prompt.system)
        self.assertIn("具体设置必须放入 parameters", prompt.system)
        self.assertIn("其余每个计划类别最多 1 个最关键步骤", prompt.system)

    def test_candidate_fingerprint_ignores_prose_but_tracks_execution(self) -> None:
        plan = AnalysisPlan(
            plan_id="candidate-a",
            plan_version=1,
            method_family="panel_association",
            design_only=False,
            estimands=[],
            sample_rules=[],
            variable_construction=[],
            baseline_models=[
                ModelSpec(
                    step_id="baseline-a",
                    name="文字名称 A",
                    rationale="文字理由 A",
                    estimator="PanelOLS",
                    outcome="y",
                    treatments_or_exposures=["x"],
                    fixed_effects=["firm", "year"],
                    standard_error_strategy="clustered by firm",
                )
            ],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
            identification_assumptions=["说明文字 A"],
            alternative_explanations=[],
            failure_conditions=[],
            stop_conditions=[],
            required_data_fields=["firm", "year", "y", "x"],
            unsupported_requested_analyses=[],
        )
        prose_only = plan.model_copy(
            update={
                "plan_id": "candidate-b",
                "identification_assumptions": ["完全不同的说明文字 B"],
                "baseline_models": [
                    plan.baseline_models[0].model_copy(
                        update={
                            "step_id": "baseline-b",
                            "name": "文字名称 B",
                            "rationale": "文字理由 B",
                        }
                    )
                ],
            }
        )
        executable_change = prose_only.model_copy(
            update={
                "baseline_models": [
                    prose_only.baseline_models[0].model_copy(
                        update={"standard_error_strategy": "heteroskedastic"}
                    )
                ]
            }
        )

        self.assertEqual(
            _plan_executable_fingerprint(plan),
            _plan_executable_fingerprint(prose_only),
        )
        self.assertNotEqual(
            _plan_executable_fingerprint(plan),
            _plan_executable_fingerprint(executable_change),
        )

    def test_external_panel_candidate_execution_fields_are_code_owned(self) -> None:
        plan = AnalysisPlan(
            plan_id="external-candidate",
            plan_version=1,
            method_family="mechanism_boundary",
            design_only=True,
            estimands=[],
            sample_rules=[],
            variable_construction=[],
            baseline_models=[
                ModelSpec(
                    step_id="baseline",
                    name="baseline",
                    rationale="frozen",
                    estimator="PanelOLS",
                    outcome="y",
                    treatments_or_exposures=["x"],
                    fixed_effects=["S", "YEAR"],
                    standard_error_strategy="cluster_by_S",
                )
            ],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
            identification_assumptions=[],
            alternative_explanations=[],
            failure_conditions=[],
            stop_conditions=[],
            required_data_fields=["S", "YEAR", "y", "x"],
            unsupported_requested_analyses=[],
        )
        profile = DataProfile(
            profile_execution_status="succeeded",
            data_structure="panel",
            unit_of_observation="firm-year",
            entity_key=["S"],
            time_key="YEAR",
            supported_method_families=[
                "panel_association",
                "mechanism_boundary",
            ],
            readiness="partially_ready",
        )
        package = ResearchPackage(
            case_id="external-case",
            title="external case",
            research_question="x and y",
            hypotheses=[{"hypothesis_id": "H1", "statement": "x and y"}],
            variables=[
                {"name": "S", "role": "id"},
                {"name": "YEAR", "role": "time"},
                {"name": "y", "role": "outcome"},
                {"name": "x", "role": "exposure"},
            ],
            dataset_refs=[
                DatasetRef(
                    dataset_id="main-data",
                    role="main",
                    filename="main.csv",
                    sha256="a" * 64,
                    size_bytes=1,
                )
            ],
        )

        normalized = _normalize_external_enterprise_candidate_plan(
            plan,
            profile,
            package,
            "external",
        )

        self.assertFalse(normalized.design_only)
        baseline = normalized.baseline_models[0]
        self.assertEqual(
            baseline.standard_error_strategy,
            "cluster_by_entity_finite_sample_correction",
        )
        self.assertEqual(baseline.parameters["cluster_variable"], "S")
        self.assertTrue(
            _normalize_external_enterprise_candidate_plan(
                plan,
                profile,
                package,
                "fixture",
            ).design_only
        )

    def test_data_section_receives_executed_sample_evidence(self) -> None:
        spec = next(
            item
            for item in MANUSCRIPT_SECTION_SPECS
            if item["section_id"] == "data_variables"
        )

        self.assertIn("executed_evidence", spec["evidence_keys"])

    def test_post_execution_prompts_interpret_interactions_by_frozen_term(self) -> None:
        for prompt_key in (
            "evidence_assessment",
            "scientific_audit",
        ):
            prompt = get_prompt(prompt_key)
            self.assertEqual(prompt.version, "1.1.0")
            self.assertIn("interaction_term", prompt.system)
            self.assertIn("主效应", prompt.system)
        claim_prompt = get_prompt("claim_ledger")
        self.assertEqual(claim_prompt.version, "1.2.0")
        self.assertIn("不得手抄任何阿拉伯数字", claim_prompt.system)
        self.assertIn("interaction_term", claim_prompt.system)

    def test_llm_package_excludes_unknown_fields_from_model_input(self) -> None:
        package = ResearchPackage(
            case_id="case-compact",
            title="通用面板案例",
            research_question="核心解释变量是否与结果变量相关？",
            hypotheses=[
                {
                    "hypothesis_id": "H1",
                    "statement": "二者存在关联。",
                }
            ],
            variables=[
                {"name": "y", "role": "outcome"},
                {"name": "x", "role": "exposure"},
                {"name": "unverified", "role": "unknown"},
            ],
        )

        payload = self.engine._llm_research_package(package)

        self.assertEqual([item["name"] for item in payload["variables"]], ["y", "x"])
        self.assertNotIn("field_inventory", payload)

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
        arena = DesignArena.model_validate(run.artifacts["design_arena"]["payload"])
        selected_id = arena.provisional_candidate_id
        selected_review = next(
            review
            for review in arena.reviewer_reports[0].candidate_reviews
            if review.candidate_id == selected_id
        )
        selected_review.issues.append(
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
        self.engine._put_artifact(run, "design_arena", arena)
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

    async def test_artifact_payload_tampering_is_rejected_by_all_read_paths(self) -> None:
        run = await self.engine.create_run(
            CreateRunRequest(preset_case_id="esg-panel")
        )
        run.artifacts["research_package"]["payload"]["title"] = "tampered"

        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "artifact sha256 mismatch: research_package",
        ):
            self.engine._artifact(run, "research_package", ResearchPackage)
        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "artifact sha256 mismatch: research_package",
        ):
            self.engine._gate_artifact_hashes(run, "H1")

    async def test_h4_rejects_tampered_source_payload_before_sealing(self) -> None:
        run = await self._to_h3()
        run = await self.engine.decide_gate(
            run.id,
            "H3",
            GateDecisionRequest(
                action="generate_plan_only",
                idempotency_key="tamper-h3",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "hold"}
                    for claim in run.claims
                ],
            ),
        )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))

        tampered = self.repository.get(run.id)
        tampered.artifacts["research_run"]["payload"]["warnings"].append(
            "tampered source payload"
        )
        self.repository.save(tampered, expected_version=tampered.version)

        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "artifact sha256 mismatch: research_run",
        ):
            await self.engine.decide_gate(
                run.id,
                "H4",
                GateDecisionRequest(
                    action="approve",
                    idempotency_key="tamper-h4",
                ),
            )
        persisted = self.repository.get(run.id)
        self.assertEqual((persisted.status, persisted.current_gate), ("waiting_human", "H4"))
        self.assertNotIn("sealed_output", persisted.artifacts)

        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "artifact sha256 mismatch: research_run",
        ):
            self.engine._seal_output(persisted)
        self.assertNotIn("sealed_output", persisted.artifacts)

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
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(
            run.artifacts["manuscript_package"]["payload"]["mode"],
            "research_plan_only",
        )

    async def test_executed_research_with_no_admitted_claim_seals_failure_report(self) -> None:
        run = await self._to_h3("green-finance-did")
        research_run = ResearchRun.model_validate(
            run.artifacts["research_run"]["payload"]
        )
        research_run.fixture_only = False
        research_run.execution_status = "succeeded"
        research_run.scientific_status = "limited"
        research_run.not_executed_reason = None
        research_run.executions[0].execution_status = "succeeded"
        self.engine._put_artifact(run, "research_run", research_run)
        self.engine._put_artifact(
            run,
            "reproduction_audit",
            ReproductionAudit(
                audit_id="reproduction-negative-result",
                primary_run_id=research_run.research_run_id,
                replication_run_id="replication-negative-result",
                status="matched",
                mode="independent_implementation",
                independence_scope="estimator_only",
                shared_components=[
                    "policy_causal analysis-table preparation",
                    "policy event/placebo regressor construction",
                ],
            ),
        )
        self.engine._put_artifact(
            run,
            "scientific_audit",
            ScientificAudit(
                verdict="limited",
                contract_compliant=True,
                critical_issues=["政策前各期系数显著为正。"],
                unresolved_risks=["模型自由文本中的未核验风险。"],
            ),
        )
        run.mode = "research"
        run.model_provider = "qwen"
        run.execution_mode = "external"
        run.execution_status = "succeeded"
        run.scientific_status = "limited"
        run.plan_only = False
        run = self.repository.save(run, expected_version=run.version)

        run = await self.engine.decide_gate(
            run.id,
            "H3",
            GateDecisionRequest(
                action="generate_identification_failure_report",
                idempotency_key="negative-result-h3",
                claims=[
                    {"claim_id": claim.claim_id, "decision": "reject"}
                    for claim in run.claims
                ],
            ),
        )

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(run.execution_status, "succeeded")
        self.assertEqual(run.scientific_status, "limited")
        self.assertFalse(run.plan_only)
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        self.assertEqual(manuscript.mode, "identification_failure_report")
        self.assertEqual(
            manuscript.empirical_findings_status,
            "executed_not_admissible",
        )
        self.assertEqual(manuscript.audit_result, "pass_with_no_critical_issues")
        self.assertTrue(
            all(not section.claim_ids for section in manuscript.manuscript_sections)
        )
        report_text = "\n".join(
            section.content_markdown for section in manuscript.manuscript_sections
        )
        self.assertNotIn("政策前各期系数显著为正", report_text)
        self.assertNotIn("模型自由文本中的未核验风险", report_text)
        serialized_manuscript = manuscript.model_dump_json()
        self.assertNotIn("政策前各期系数显著为正", serialized_manuscript)
        self.assertNotIn("模型自由文本中的未核验风险", serialized_manuscript)
        self.assertIn("仅覆盖估计器与协方差实现", report_text)
        self.assertIn("分析表准备", report_text)
        self.assertIn("事件研究和安慰剂变量构造", report_text)
        self.assertIn("allowed_strength=", report_text)
        self.assertIn("max_allowed_strength=", report_text)
        evidence_reasons = [
            item["reason"]
            for item in run.artifacts["evidence_registry"]["payload"]["evidence"]
            if item.get("reason")
        ]
        self.assertTrue(any(reason in report_text for reason in evidence_reasons))
        self.assertTrue(
            any("ReproductionAudit" in item for item in manuscript.disclosures)
        )

        run = await self.engine.decide_gate(
            run.id,
            "H4",
            GateDecisionRequest(action="approve", idempotency_key="negative-result-h4"),
        )
        self.assertEqual(run.status, "completed")
        self.assertIn("sealed_output", run.artifacts)

    async def _retryable_research_writer_run(
        self,
        preset_case_id: str = "green-finance-did",
    ):
        run = await self._to_h3(preset_case_id)
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

    async def test_full_manuscript_isolates_scientific_audit_free_text(self) -> None:
        run = await self._retryable_research_writer_run()
        critical_sentinel = "SCIENTIFIC_AUDIT_CRITICAL_SENTINEL"
        unresolved_sentinel = "SCIENTIFIC_AUDIT_UNRESOLVED_SENTINEL"
        self.engine._put_artifact(
            run,
            "scientific_audit",
            ScientificAudit(
                verdict="limited",
                contract_compliant=True,
                critical_issues=[critical_sentinel],
                unresolved_risks=[unresolved_sentinel],
            ),
        )
        run = self.repository.save(run, expected_version=run.version)
        gateway = ScientificAuditIsolationGateway()

        with patch.object(self.engine, "_gateway", return_value=gateway):
            result = await self.engine.advance(run.id)

        self.assertEqual((result.status, result.current_gate), ("waiting_human", "H4"))
        writer_payload = json.dumps(gateway.calls, ensure_ascii=False)
        manuscript = ManuscriptPackage.model_validate(
            result.artifacts["manuscript_package"]["payload"]
        )
        serialized_manuscript = manuscript.model_dump_json()
        for sentinel in (critical_sentinel, unresolved_sentinel):
            self.assertNotIn(sentinel, writer_payload)
            self.assertNotIn(sentinel, serialized_manuscript)
        serialized_audit = json.dumps(
            result.artifacts["scientific_audit"]["payload"],
            ensure_ascii=False,
        )
        self.assertIn(critical_sentinel, serialized_audit)
        self.assertIn(unresolved_sentinel, serialized_audit)
        self.assertTrue(
            any("独立第二意见工件" in item for item in manuscript.disclosures)
        )
        empirical = next(
            section
            for section in manuscript.manuscript_sections
            if section.section_id == "empirical_results"
        )
        self.assertNotIn("事件研究显示各期动态效应", empirical.content_markdown)

    def test_limited_event_study_language_is_neutralized(self) -> None:
        source = "事件研究显示各期动态效应如下。"
        self.assertEqual(
            _neutralize_limited_event_study_language(
                source,
                limited_or_mixed=True,
            ),
            "事件研究报告各事件期组间差异系数如下。",
        )
        self.assertEqual(
            _neutralize_limited_event_study_language(
                source,
                limited_or_mixed=False,
            ),
            source,
        )

    async def test_enterprise_panel_happy_path_uses_exact_nine_logical_calls(
        self,
    ) -> None:
        gateway = WorkflowCallRecordingGateway()
        with (
            patch.object(self.engine, "_gateway", return_value=gateway),
            patch.object(
                self.engine, "_reviewer_gateway", return_value=gateway
            ) as reviewer_gateway,
        ):
            run = await self._retryable_research_writer_run("esg-panel")
            research_run = self.engine._artifact(run, "research_run", ResearchRun)
            research_run.executions[0].estimates = [
                {
                    "term": "esg_score",
                    "coefficient": -9876.5432,
                    "standard_error": 0.0004,
                    "p_value": 0.0002,
                    "nobs": 29919,
                }
            ]
            research_run.executions[0].diagnostic_results = {
                "rows_used": 29919,
                "r_squared_within": 0.987654,
            }
            self.engine._put_artifact(run, "research_run", research_run)
            run = self.repository.save(run, expected_version=run.version)
            result = await self.engine.advance(run.id)

        self.assertEqual(reviewer_gateway.call_count, 3)

        self.assertEqual((result.status, result.current_gate), ("waiting_human", "H4"))
        self.assertEqual(
            [call["prompt_key"] for call in gateway.calls],
            [
                "hypothesis_decomposition",
                "candidate_plan_batch",
                "candidate_plan_batch",
                "reviewer_report_batch",
                "reviewer_report_batch",
                "evidence_claim_bundle",
                "scientific_audit",
                "manuscript_section_draft_batch",
                "manuscript_section_draft_batch",
            ],
        )
        self.assertEqual(
            [call["call_context"].call_group for call in gateway.calls],
            ["h1_h2"] * 5 + ["h3"] * 2 + ["h4"] * 2,
        )
        self.assertEqual(
            [
                call["payload"]["candidate_strategies"]
                for call in gateway.calls[1:3]
            ],
            [
                ["direct_baseline", "identification_first"],
                ["measurement_robustness"],
            ],
        )
        self.assertEqual(
            [call["payload"]["dimensions"] for call in gateway.calls[3:5]],
            [
                ["measurement", "reproducibility"],
                ["causal", "statistical"],
            ],
        )
        writer_calls = gateway.calls[7:]
        self.assertEqual(
            [
                [spec["section_id"] for spec in call["payload"]["section_specs"]]
                for call in writer_calls
            ],
            [
                [
                    "introduction",
                    "theory_hypotheses",
                    "data_variables",
                    "research_design",
                ],
                [
                    "empirical_results",
                    "discussion_limitations",
                    "conclusion",
                    "abstract",
                ],
            ],
        )
        serialized_writer_payload = json.dumps(
            [call["payload"] for call in writer_calls],
            ensure_ascii=False,
        )
        for forbidden in (
            "-9876.5432",
            "0.0004",
            "0.0002",
            "29919",
            "0.987654",
            "raw_value",
            "rendered_value",
            "protected_values",
            "text_template",
        ):
            self.assertNotIn(forbidden, serialized_writer_payload)
        for call in writer_calls:
            for spec in call["payload"]["section_specs"]:
                statement_ids = {
                    item["statement_id"] for item in spec["statement_catalog"]
                }
                self.assertEqual(
                    statement_ids,
                    set(spec["required_statement_ids"]),
                )
                self.assertTrue(
                    all(
                        set(item)
                        == {
                            "statement_id",
                            "statement_kind",
                            "claim_ids",
                            "execution_ids",
                            "instruction",
                        }
                        for item in spec["statement_catalog"]
                    )
                )
                if spec["section_id"] in {"introduction", "research_design"}:
                    self.assertEqual(spec["required_statement_ids"], [])
                    self.assertEqual(spec["statement_catalog"], [])
                    self.assertIn(
                        "禁止输出任何 [[STATEMENT:...]] 锚点",
                        spec["statement_anchor_policy"],
                    )

    async def test_failed_scientific_writer_retries_without_template_fallback(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertNotIn("sealed_output", run.artifacts)
        self.assertFalse(any(step.node_id == "scientific_writer_fallback" for step in run.steps))
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        self.assertEqual(len(manuscript.manuscript_sections), 8)
        self.assertEqual(manuscript.audit_result, "pass_with_no_critical_issues")

        run = await self.engine.decide_gate(
            run.id,
            "H4",
            GateDecisionRequest(action="approve", idempotency_key="writer-h4-v1"),
        )
        self.assertEqual(run.status, "completed")
        self.assertIn("sealed_output", run.artifacts)

        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            regenerated = await self.engine.retry_writing(run.id)
        self.assertEqual(
            (regenerated.status, regenerated.current_gate),
            ("waiting_human", "H4"),
        )
        self.assertEqual(
            regenerated.artifacts["manuscript_package"]["payload"]["version"],
            2,
        )

        regenerated = await self.engine.decide_gate(
            regenerated.id,
            "H4",
            GateDecisionRequest(action="approve", idempotency_key="writer-h4-v2"),
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
        gateway = RepairingManuscriptGateway()
        with patch.object(
            self.engine,
            "_gateway",
            return_value=gateway,
        ):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
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
        self.assertEqual(len(writer_steps), 12)
        writer_ids = {
            index: f"{run.id}:manuscript_section_draft_batch_{index}"
            for index in (1, 2)
        }
        primary_contexts = [
            context
            for context in gateway.call_contexts
            if context.attempt_type == "primary"
        ]
        repair_contexts = [
            context
            for context in gateway.call_contexts
            if context.attempt_type == "content_repair"
        ]
        self.assertCountEqual(
            [context.logical_call_id for context in primary_contexts],
            [writer_ids[1], writer_ids[2]],
        )
        self.assertEqual(
            [context.logical_call_id for context in repair_contexts],
            [writer_ids[1]],
        )

    async def test_h4_revise_requires_comment_and_rewrites_named_section(self) -> None:
        run = await self._retryable_research_writer_run()
        with patch.object(self.engine, "_gateway", return_value=FullManuscriptGateway()):
            run = await self.engine.advance(run.id)

        with self.assertRaisesRegex(
            WorkflowTransitionError,
            "requires a concrete review comment",
        ):
            await self.engine.decide_gate(
                run.id,
                "H4",
                GateDecisionRequest(
                    action="revise",
                    idempotency_key="h4-empty-review",
                ),
            )

        run = await self.engine.decide_gate(
            run.id,
            "H4",
            GateDecisionRequest(
                action="revise",
                comment="请重写结论，避免超过证据强度。",
                idempotency_key="h4-conclusion-review",
            ),
        )
        self.assertEqual((run.status, run.current_node_id), ("failed", "scientific_writer"))

        gateway = FeedbackTrackingGateway()
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.retry_writing(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(
            [
                spec["section_id"]
                for call in gateway.calls
                for spec in call["section_specs"]
            ],
            ["conclusion"],
        )
        feedback = gateway.calls[0]["section_specs"][0]["revision_feedback"][
            "problems"
        ]
        self.assertTrue(any("H4 人工审稿意见" in problem for problem in feedback))
        self.assertEqual(
            gateway.call_contexts[0].logical_call_id,
            f"{run.id}:manuscript_section_draft_batch_2",
        )
        self.assertEqual(
            gateway.call_contexts[0].attempt_type,
            "content_repair",
        )

    async def test_content_failure_can_use_second_bounded_repair_round(self) -> None:
        run = await self._retryable_research_writer_run()
        gateway = SecondRoundRepairingGateway()
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(gateway.introduction_calls, 3)
        writer_steps = [
            step for step in run.steps if step.node_id == "scientific_writer"
        ]
        self.assertEqual(len(writer_steps), 14)
        writer_id = f"{run.id}:manuscript_section_draft_batch_1"
        matching_contexts = [
            context
            for context in gateway.call_contexts
            if context.logical_call_id == writer_id
        ]
        self.assertEqual(
            [context.attempt_type for context in matching_contexts],
            ["primary", "content_repair", "content_repair"],
        )

    async def test_ir_compile_errors_repair_only_the_affected_sections(self) -> None:
        run = await self._retryable_research_writer_run()
        gateway = AnchorRepairGateway({"introduction", "conclusion"})
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(
            gateway.calls,
            [
                [
                    "introduction",
                    "theory_hypotheses",
                    "data_variables",
                    "research_design",
                ],
                [
                    "empirical_results",
                    "discussion_limitations",
                    "conclusion",
                    "abstract",
                ],
                ["introduction"],
                ["conclusion"],
            ],
        )
        requested_ids = [section_id for call in gateway.calls for section_id in call]
        for section_id in FULL_MANUSCRIPT_SECTION_IDS:
            expected_count = 2 if section_id in {"introduction", "conclusion"} else 1
            self.assertEqual(requested_ids.count(section_id), expected_count)
        writer_ids = {
            index: f"{run.id}:manuscript_section_draft_batch_{index}"
            for index in (1, 2)
        }
        repair_contexts = [
            context
            for context in gateway.call_contexts
            if context.attempt_type == "content_repair"
        ]
        self.assertCountEqual(
            [context.logical_call_id for context in repair_contexts],
            [writer_ids[1], writer_ids[2]],
        )
        repair_payload = json.dumps(gateway.payloads[2:], ensure_ascii=False)
        self.assertNotIn("unknown-anchor", repair_payload)
        self.assertIn("所有实证判断必须完全由本章获准锚点承担", repair_payload)

    async def test_exhausted_repair_batch_falls_back_only_for_target_section(
        self,
    ) -> None:
        run = await self._retryable_research_writer_run()
        gateway = ExhaustedRepairBatchGateway(
            "introduction",
            "conclusion",
        )

        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(
            gateway.calls,
            [
                [
                    "introduction",
                    "theory_hypotheses",
                    "data_variables",
                    "research_design",
                ],
                [
                    "empirical_results",
                    "discussion_limitations",
                    "conclusion",
                    "abstract",
                ],
                ["introduction"],
                ["conclusion"],
                ["introduction"],
            ],
        )
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        sections_by_id = {
            section.section_id: section
            for section in manuscript.manuscript_sections
        }
        self.assertEqual(set(sections_by_id), set(FULL_MANUSCRIPT_SECTION_IDS))
        self.assertTrue(
            sections_by_id["introduction"].content_markdown.startswith(
                DETERMINISTIC_SAFE_SECTION_TEXTS["introduction"][:80]
            )
        )
        for section_id in set(FULL_MANUSCRIPT_SECTION_IDS) - {"introduction"}:
            self.assertTrue(
                sections_by_id[section_id].content_markdown.startswith(
                    "本节依据研究问题"
                )
            )
        fallback_steps = [
            step
            for step in run.steps
            if isinstance(step.input, dict)
            and step.input.get("fallback_type")
            == "deterministic_safe_fallback"
        ]
        self.assertEqual(
            [step.input["section_id"] for step in fallback_steps],
            ["introduction"],
        )

    async def test_deterministic_safe_fallback_replaces_only_persistent_failures(
        self,
    ) -> None:
        run = await self._retryable_research_writer_run()
        target_ids = {
            "theory_hypotheses",
            "research_design",
            "discussion_limitations",
            "conclusion",
        }
        receipts_before = len(
            run.artifacts.get("model_usage", {})
            .get("payload", {})
            .get("call_receipts", [])
        )
        gateway = PersistentUnsafeWriterGateway(target_ids)

        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        requested_ids = [section_id for call in gateway.calls for section_id in call]
        for section_id in FULL_MANUSCRIPT_SECTION_IDS:
            self.assertEqual(
                requested_ids.count(section_id),
                3 if section_id in target_ids else 1,
            )
        fallback_steps = [
            step
            for step in run.steps
            if isinstance(step.input, dict)
            and step.input.get("fallback_type") == "deterministic_safe_fallback"
        ]
        self.assertEqual(
            {step.input["section_id"] for step in fallback_steps},
            target_ids,
        )
        self.assertTrue(all(step.status == "succeeded" for step in fallback_steps))
        self.assertTrue(
            all(
                "deterministic_safe_fallback" in " ".join(step.logs)
                and step.prompts
                and all(prompt.role == "code" for prompt in step.prompts)
                for step in fallback_steps
            )
        )
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        sections_by_id = {
            section.section_id: section
            for section in manuscript.manuscript_sections
        }
        for section_id in target_ids:
            self.assertTrue(
                sections_by_id[section_id].content_markdown.startswith(
                    DETERMINISTIC_SAFE_SECTION_TEXTS[section_id][:80]
                )
            )
            for forbidden in (
                "中介检验",
                "中介效应",
                "诊断待执行",
                "诊断步骤尚未执行",
                "机制步骤尚未执行",
            ):
                self.assertNotIn(
                    forbidden,
                    sections_by_id[section_id].content_markdown,
                )
        fallback_disclosure = next(
            disclosure
            for disclosure in manuscript.disclosures
            if "确定性安全模板" in disclosure
        )
        for section_id in target_ids:
            self.assertIn(section_id, fallback_disclosure)
        for section_id in set(FULL_MANUSCRIPT_SECTION_IDS) - target_ids:
            self.assertIn(
                "本节依据研究问题",
                sections_by_id[section_id].content_markdown,
            )
        receipts_after = len(
            run.artifacts.get("model_usage", {})
            .get("payload", {})
            .get("call_receipts", [])
        )
        self.assertEqual(receipts_after, receipts_before)

    async def test_all_sections_safe_fallback_meets_manuscript_length_floor(
        self,
    ) -> None:
        run = await self._retryable_research_writer_run()
        gateway = PersistentUnsafeWriterGateway(set(FULL_MANUSCRIPT_SECTION_IDS))

        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(len(gateway.calls), 6)
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        lengths_by_id = {
            section.section_id: len(section.content_markdown.strip())
            for section in manuscript.manuscript_sections
        }
        minimum_by_id = {
            spec["section_id"]: int(
                spec["target_characters"].split("-", 1)[0]
            )
            for spec in MANUSCRIPT_SECTION_SPECS
        }
        self.assertEqual(len(lengths_by_id), 8)
        self.assertTrue(
            all(
                lengths_by_id[section_id] >= minimum_by_id[section_id]
                for section_id in FULL_MANUSCRIPT_SECTION_IDS
            )
        )
        self.assertEqual(
            _deterministic_safe_fallback_quality_problems(
                list(FULL_MANUSCRIPT_SECTION_IDS)
            ),
            [],
        )
        fallback_steps = [
            step
            for step in run.steps
            if isinstance(step.input, dict)
            and step.input.get("fallback_type") == "deterministic_safe_fallback"
        ]
        self.assertEqual(len(fallback_steps), 8)

    def test_safe_fallback_quality_gate_rejects_short_or_repeated_filler(
        self,
    ) -> None:
        with patch.dict(
            DETERMINISTIC_SAFE_SECTION_TEXTS,
            {"abstract": "甲" * 449},
        ):
            self.assertTrue(
                _deterministic_safe_fallback_quality_problems(["abstract"])
            )

        distinct_left = "甲" * 650
        distinct_right = "甲" * 119 + "乙" * 531
        with patch.dict(
            DETERMINISTIC_SAFE_SECTION_TEXTS,
            {
                "introduction": distinct_left,
                "theory_hypotheses": distinct_right,
            },
        ):
            self.assertEqual(
                _deterministic_safe_fallback_quality_problems(
                    ["introduction", "theory_hypotheses"]
                ),
                [],
            )

        repeated_right = "甲" * 120 + "乙" * 530
        with patch.dict(
            DETERMINISTIC_SAFE_SECTION_TEXTS,
            {
                "introduction": distinct_left,
                "theory_hypotheses": repeated_right,
            },
        ):
            self.assertTrue(
                _deterministic_safe_fallback_quality_problems(
                    ["introduction", "theory_hypotheses"]
                )
            )

        anchor_padding = (
            "短文"
            + "[[STATEMENT:statement-shared]]" * 100
        )
        with patch.dict(
            DETERMINISTIC_SAFE_SECTION_TEXTS,
            {"abstract": anchor_padding},
        ):
            problems = _deterministic_safe_fallback_quality_problems(
                ["abstract"]
            )
        self.assertTrue(any("少于" in problem for problem in problems))

    async def test_transient_repair_failure_uses_second_bounded_round(self) -> None:
        run = await self._retryable_research_writer_run()
        gateway = AnchorRepairGateway(
            {"introduction"},
            fail_first_repair=True,
        )
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(
            gateway.calls[2:],
            [["introduction"], ["introduction"]],
        )
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        self.assertEqual(
            {section.section_id for section in manuscript.manuscript_sections},
            set(FULL_MANUSCRIPT_SECTION_IDS),
        )
        self.assertFalse(
            any(
                isinstance(step.input, dict)
                and step.input.get("fallback_type")
                == "deterministic_safe_fallback"
                for step in run.steps
            )
        )

    async def test_failed_repair_batches_fallback_for_multiple_sections(self) -> None:
        run = await self._retryable_research_writer_run()
        gateway = AnchorRepairGateway(
            {"introduction", "conclusion"},
            fail_all_repairs=True,
        )
        with patch.object(self.engine, "_gateway", return_value=gateway):
            run = await self.engine.advance(run.id)

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        fallback_steps = [
            step
            for step in run.steps
            if isinstance(step.input, dict)
            and step.input.get("fallback_type")
            == "deterministic_safe_fallback"
        ]
        self.assertEqual(
            {step.input["section_id"] for step in fallback_steps},
            {"introduction", "conclusion"},
        )
        manuscript = ManuscriptPackage.model_validate(
            run.artifacts["manuscript_package"]["payload"]
        )
        self.assertEqual(
            {section.section_id for section in manuscript.manuscript_sections},
            set(FULL_MANUSCRIPT_SECTION_IDS),
        )

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
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertEqual(writer_steps_after - writer_steps_before, 2)

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
                            "该模式极易被误判，也极易被模型误判，参数极度接近边界，"
                            "解释需极为谨慎，并检查 appropriateness 与残分布，"
                            "但不存在统计上显著的关联，核心解释变量 firm_size 需披露。" * 20
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
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
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
        self.assertNotIn("极易被误判", conclusion["content_markdown"])
        self.assertNotIn("极易被模型误判", conclusion["content_markdown"])
        self.assertNotIn("极度接近", conclusion["content_markdown"])
        self.assertNotIn("极为谨慎", conclusion["content_markdown"])
        self.assertNotIn("appropriateness", conclusion["content_markdown"])
        self.assertNotIn("不存在统计上显著的", conclusion["content_markdown"])
        self.assertNotIn("核心解释变量 firm_size", conclusion["content_markdown"])
        self.assertIn("可能被误判", conclusion["content_markdown"])
        self.assertIn("适用性", conclusion["content_markdown"])
        self.assertNotIn("未发现达到常用统计显著性阈值的", conclusion["content_markdown"])
        self.assertIn("相关证据边界由核验语句给出", conclusion["content_markdown"])
        self.assertIn("控制变量 firm_size", conclusion["content_markdown"])
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
        self.assertEqual(prompt.version, "2.9.7")
        content = prompt.system + prompt.user_template
        for directed_term in ("案例1", "ESG", "SDLA", "短债长用"):
            self.assertNotIn(directed_term, content)
        self.assertIn("completed_frozen_plan_categories", content)
        self.assertIn("pending_frozen_plan_categories", content)

    def test_manuscript_audit_requires_within_entity_and_conditional_theory(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="abstract",
                    title="摘要",
                    content_markdown="核心指标评分较高的企业对应较低的结果水平。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="theory_hypotheses",
                    title="理论分析",
                    content_markdown=(
                        "信息透明度是连接两个构念的关键路径。"
                        "透明度提升减少了信息摩擦，使资金提供方改变期限选择。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="empirical_results",
                    title="实证结果",
                    content_markdown=(
                        "核心解释变量每相差一个单位，对应结果变量平均相差0.2个单位。"
                    ),
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unbalanced"},
                "frozen_design": {"research_goal": "associational"},
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                            "diagnostic_results": {"entity_fixed_effects": True},
                        }
                    ],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertIn(
            "abstract 未按个体内随时间变化解释固定效应系数",
            problems,
        )
        self.assertIn(
            "empirical_results 未按个体内随时间变化解释固定效应系数",
            problems,
        )
        self.assertIn(
            "theory_hypotheses 将无文献支持的理论机制写成既定事实",
            problems,
        )

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

    def test_manuscript_content_audit_rejects_h3_withheld_estimate(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="empirical_results",
                    title="实证结果",
                    content_markdown="zFDI 的系数为 0.052，p=0.021。",
                    status="generated",
                )
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {"panel_balance": "unknown"},
                "frozen_design": {"planned_mechanisms": []},
                "executed_evidence": {
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                            "diagnostic_results": {"entity_fixed_effects": True},
                        }
                    ]
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                    "withheld_estimate_terms": ["zFDI"],
                },
            },
        )

        self.assertEqual(
            problems,
            ["empirical_results 写入了 H3 未授权估计项 zFDI"],
        )

        allowed_control_mention = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="discussion_limitations",
                    title="讨论与局限",
                    content_markdown=(
                        "模型控制了 zFDI 与 EPD 背景协变量后，"
                        "核心解释变量的直接关联未达到显著性阈值。"
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
                    "withheld_estimate_terms": ["zFDI", "EPD"],
                },
            },
        )
        self.assertEqual(allowed_control_mention, [])

    async def test_writing_evidence_only_exposes_h3_authorized_estimates(self) -> None:
        run = await self._retryable_research_writer_run()
        package = self.engine._artifact(run, "research_package", ResearchPackage)
        plan = self.engine._artifact(run, "analysis_plan", AnalysisPlan)
        plan.method_family = "panel_association"
        research_run = self.engine._artifact(run, "research_run", ResearchRun)
        exposure_name = plan.baseline_models[0].treatments_or_exposures[0]
        control_name = plan.baseline_models[0].controls[0]
        exposure = next(
            variable for variable in package.variables if variable.name == exposure_name
        )
        control = next(
            variable for variable in package.variables if variable.name == control_name
        )
        research_run.executions[0].estimates = [
            {"term": exposure.name, "coefficient": -0.2, "p_value": 0.01},
            {"term": control.name, "coefficient": 0.3, "p_value": 0.02},
        ]

        evidence = self.engine._writing_evidence_pack(
            run,
            package,
            plan,
            research_run,
            [
                {
                    "claim_id": "claim-esg",
                    "claim_text": "原始主张",
                    "final_text": f"{exposure.label}与结果变量存在初步关联。",
                    "supporting_runs": [research_run.executions[0].execution_id],
                    "opposing_runs": [],
                    "unresolved_risks": [],
                }
            ],
        )["writing_evidence_pack"]

        visible_terms = [
            estimate["term"]
            for estimate in evidence["executed_evidence"]["executions"][0][
                "estimates"
            ]
        ]
        self.assertEqual(visible_terms, [exposure.name])
        self.assertEqual(
            evidence["writing_requirements"]["withheld_estimate_terms"],
            [control.name],
        )

    async def test_writing_evidence_derives_bound_lead_term_from_frozen_plan(self) -> None:
        run = await self._retryable_research_writer_run()
        package = self.engine._artifact(run, "research_package", ResearchPackage)
        plan = self.engine._artifact(run, "analysis_plan", AnalysisPlan)
        plan.method_family = "panel_association"
        research_run = self.engine._artifact(run, "research_run", ResearchRun)
        exposure = plan.baseline_models[0].treatments_or_exposures[0]
        baseline_execution = research_run.executions[0]
        baseline_execution.estimates = [
            {"term": exposure, "coefficient": -0.2, "p_value": 0.01}
        ]
        lead_term = f"{exposure}_w_lead1"
        lead_step = PlannedStep(
            step_id="check-lead-bound",
            name="前导项",
            rationale="证伪时序",
            parameters={"lead_exposure": lead_term, "lead_source": exposure},
        )
        plan.falsification_tests.append(lead_step)
        research_run.executions.append(
            ExecutionRecord(
                execution_id="execution-lead-bound",
                run_type="falsification",
                plan_step_id=lead_step.step_id,
                check_id=lead_step.step_id,
                execution_status="succeeded",
                estimates=[
                    {"term": lead_term, "coefficient": 0.1, "p_value": 0.02}
                ],
                provenance=baseline_execution.provenance,
            )
        )

        evidence = self.engine._writing_evidence_pack(
            run,
            package,
            plan,
            research_run,
            [
                {
                    "claim_id": "claim-lead-mixed",
                    "claim_text": "原始主张",
                    "final_text": "前导项证据与主结果不一致，结论只能作混合关联解读。",
                    "supporting_runs": [baseline_execution.execution_id],
                    "opposing_runs": ["execution-lead-bound"],
                    "unresolved_risks": [],
                }
            ],
        )["writing_evidence_pack"]

        visible_by_execution = {
            item["execution_id"]: [
                estimate["term"] for estimate in item["estimates"]
            ]
            for item in evidence["executed_evidence"]["executions"]
        }
        self.assertEqual(
            visible_by_execution[baseline_execution.execution_id],
            [exposure],
        )
        self.assertEqual(
            visible_by_execution["execution-lead-bound"],
            [lead_term],
        )
        self.assertIn(
            lead_term,
            evidence["writing_requirements"]["authorized_estimate_terms"],
        )

    async def test_writing_evidence_authorizes_bound_policy_estimands(self) -> None:
        run = await self._retryable_research_writer_run()
        package = self.engine._artifact(run, "research_package", ResearchPackage)
        plan = self.engine._artifact(run, "analysis_plan", AnalysisPlan)
        plan.method_family = "policy_causal"
        exposure = "policy_exposure"
        baseline_step = plan.baseline_models[0]
        baseline_step.treatments_or_exposures = [exposure]
        baseline_execution = self.engine._artifact(
            run, "research_run", ResearchRun
        ).executions[0]
        baseline_execution.estimates = [
            {"term": exposure, "coefficient": -0.2, "p_value": 0.01},
            {"term": "control_term", "coefficient": 0.3, "p_value": 0.02},
        ]
        event_step = PlannedStep(
            step_id="check-policy-event-study",
            name="事件研究",
            rationale="冻结的政策前动态检验",
            parameters={"policy_event_study": True},
        )
        placebo_step = PlannedStep(
            step_id="check-policy-placebo-time",
            name="伪政策时点",
            rationale="冻结的证伪检验",
            parameters={"policy_placebo": True},
        )
        plan.falsification_tests.extend([event_step, placebo_step])
        research_run = self.engine._artifact(run, "research_run", ResearchRun)
        research_run.executions[0] = baseline_execution
        research_run.executions.extend(
            [
                ExecutionRecord(
                    execution_id="execution-policy-event",
                    run_type="falsification",
                    plan_step_id=event_step.step_id,
                    execution_status="succeeded",
                    estimates=[
                        {"term": "event_2004", "coefficient": 0.1, "p_value": 0.04},
                        {
                            "term": "event_remote_pre",
                            "coefficient": 0.2,
                            "p_value": 0.03,
                        },
                        {"term": "not_an_event", "coefficient": 0.5, "p_value": 0.01},
                    ],
                ),
                ExecutionRecord(
                    execution_id="execution-policy-placebo",
                    run_type="falsification",
                    plan_step_id=placebo_step.step_id,
                    execution_status="succeeded",
                    estimates=[
                        {
                            "term": "placebo_exposure_2004",
                            "coefficient": -0.1,
                            "p_value": 0.03,
                        }
                    ],
                ),
            ]
        )

        evidence = self.engine._writing_evidence_pack(
            run,
            package,
            plan,
            research_run,
            [
                {
                    "claim_id": "claim-policy-mixed",
                    "claim_text": "原始因果主张",
                    "final_text": "证据混合，只能作非因果解读。",
                    "supporting_runs": [
                        baseline_execution.execution_id,
                        "execution-policy-event",
                    ],
                    "opposing_runs": ["execution-policy-placebo"],
                    "unresolved_risks": [],
                }
            ],
        )["writing_evidence_pack"]

        visible_by_execution = {
            item["execution_id"]: [
                estimate["term"] for estimate in item["estimates"]
            ]
            for item in evidence["executed_evidence"]["executions"]
        }
        self.assertEqual(
            visible_by_execution[baseline_execution.execution_id],
            [exposure],
        )
        self.assertEqual(
            visible_by_execution["execution-policy-event"],
            ["event_2004", "event_remote_pre"],
        )
        self.assertEqual(
            visible_by_execution["execution-policy-placebo"],
            ["placebo_exposure_2004"],
        )
        self.assertEqual(
            evidence["writing_requirements"]["authorized_estimate_terms"],
            ["event_2004", "event_remote_pre", "placebo_exposure_2004", exposure],
        )
        self.assertIn(
            "control_term",
            evidence["writing_requirements"]["withheld_estimate_terms"],
        )
        self.assertIn(
            "not_an_event",
            evidence["writing_requirements"]["withheld_estimate_terms"],
        )

    async def test_writer_payload_never_contains_raw_statistics(self) -> None:
        run = await self._retryable_research_writer_run()
        research_run = self.engine._artifact(run, "research_run", ResearchRun)
        research_run.executions[0].estimates = [
            {
                "term": "EPD",
                "coefficient": 1234.5678,
                "standard_error": 0.0004,
                "p_value": 0.0002,
                "nobs": 29919,
            }
        ]
        research_run.executions[0].diagnostic_results = {
            "rows_used": 29919,
            "r_squared_within": 0.987654,
        }
        self.engine._put_artifact(run, "research_run", research_run)
        run = self.repository.save(run, expected_version=run.version)
        gateway = FeedbackTrackingGateway()

        with patch.object(self.engine, "_gateway", return_value=gateway):
            result = await self.engine.advance(run.id)

        self.assertEqual((result.status, result.current_gate), ("waiting_human", "H4"))
        serialized = json.dumps(gateway.calls, ensure_ascii=False)
        for forbidden in ("1234.5678", "0.0004", "0.0002", "29919", "0.987654"):
            self.assertNotIn(forbidden, serialized)
        for call in gateway.calls:
            for spec in call["section_specs"]:
                for execution in spec.get("safe_evidence", {}).get(
                    "executed_evidence", {}
                ).get("executions", []):
                    self.assertNotIn("estimates", execution)
                    self.assertNotIn("diagnostic_results", execution)

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
                        "当前输入未提供外生工具变量，因此只能解释为条件关联。"
                        "本研究缺乏外生工具变量，不能作因果推断。"
                        "不显著并不必然否定空间互动的存在。"
                        "该假设的检验依赖模型设定，且受限于未解决的内生性问题。"
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
                        "任何超出冻结计划的机制分析均需另行审批。"
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
                        "当前结果表明存在稳定的负向关联。"
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

        self.assertEqual(len(problems), 5)

    def test_manuscript_content_audit_allows_explicit_limits_on_diagnostics(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="abstract",
                    title="摘要",
                    content_markdown=(
                        "对同一个体而言，核心解释变量在不同时点相差一单位时，"
                        "结果变量对应相差0.2个单位。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown=(
                        "冻结计划中未单列内生性处理步骤。"
                        "残差分布检查不能据此验证整体模型设定的合理性。"
                        "证伪检验不能直接剥离或解决内生性问题。"
                        "组内 R² 不代表固定效应对模型解释力的贡献，"
                        "也不应归因于固定效应本身。"
                        "当前只有基准模型，尚不能判断结果是否稳定。"
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
                "executed_evidence": {
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                            "diagnostic_results": {"entity_fixed_effects": True},
                        }
                    ]
                },
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

    def test_manuscript_content_audit_rejects_sample_and_execution_misstatements(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="abstract",
                    title="摘要",
                    content_markdown=(
                        "对同一个体而言，解释变量相差一单位时，结果变量对应降低0.2单位。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="data_variables",
                    title="数据与变量",
                    content_markdown=(
                        "输入数据共30,311行，最终用于基准回归的有效样本为30,311行。"
                        "x_lead1已在输入数据中生成。"
                    ),
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="discussion_limitations",
                    title="讨论与局限",
                    content_markdown=(
                        "面板诊断尚待执行。模型已控制SOE_w。"
                        "信息环境交互边界模型中核心解释变量主效应不显著，故未获支持。"
                    ),
                    status="generated",
                ),
            ],
            {
                "research_context": {"known_policy_facts": []},
                "data_profile": {
                    "row_count": 30311,
                    "panel_balance": "unbalanced",
                    "measurement_risks": [],
                },
                "frozen_design": {
                    "research_goal": "associational",
                    "baseline_models": [{"controls": ["size"]}],
                    "variable_construction": [
                        {"parameters": {"target": "x_lead1"}}
                    ],
                    "planned_mechanisms": ["信息环境交互边界"],
                    "planned_falsification": [],
                },
                "executed_evidence": {
                    "scientific_status": "limited",
                    "executions": [
                        {
                            "run_type": "baseline",
                            "execution_status": "succeeded",
                            "diagnostic_results": {
                                "rows_used": 29919,
                                "entity_fixed_effects": True,
                            },
                        },
                        {
                            "run_type": "diagnostic",
                            "execution_status": "succeeded",
                            "diagnostic_results": {},
                        },
                    ],
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        expected = (
            "方向性变化",
            "输入总行数",
            "实际样本量",
            "执行时构造字段",
            "已成功执行的诊断",
            "冻结计划之外的 SOE_w",
            "主效应显著性",
        )
        for marker in expected:
            self.assertTrue(
                any(marker in problem for problem in problems),
                (marker, problems),
            )

    def test_manuscript_content_audit_rejects_unfrozen_control_label_and_endogeneity_plan(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="empirical_results",
                    title="实证结果",
                    content_markdown="基准回归纳入产权性质等控制变量。",
                    status="generated",
                ),
                ManuscriptSection(
                    section_id="conclusion",
                    title="结论",
                    content_markdown="后续研究需依据冻结计划进一步处理内生性问题。",
                    status="generated",
                ),
            ],
            {
                "research_context": {
                    "known_policy_facts": [],
                    "variables": [
                        {
                            "name": "SOE_w",
                            "label": "产权性质",
                            "role": "control",
                        }
                    ],
                },
                "data_profile": {
                    "panel_balance": "unknown",
                    "measurement_risks": [],
                },
                "frozen_design": {
                    "baseline_models": [{"controls": ["SIZE_w"]}],
                    "planned_mechanisms": [],
                },
                "executed_evidence": {"executions": []},
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertTrue(any("变量标签 产权性质" in problem for problem in problems))
        self.assertTrue(any("不存在的内生性步骤" in problem for problem in problems))

    def test_manuscript_content_audit_allows_missing_evidence_for_unexecuted_path(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="empirical_results",
                    title="实证结果",
                    content_markdown="该步骤未执行，因此这条潜在路径缺乏实证证据。",
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

    def test_manuscript_content_audit_allows_explicitly_completed_robustness(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="research_design",
                    title="研究设计",
                    content_markdown=(
                        "替代结果变量稳健性检验已经运行完毕。"
                        "稳健性方面暂无额外待执行项。"
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
                "executed_evidence": {
                    "executions": [
                        {
                            "run_type": "robustness",
                            "execution_status": "succeeded",
                            "diagnostic_results": {},
                        }
                    ]
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertEqual(problems, [])

    def test_manuscript_content_audit_rejects_interaction_and_execution_contradictions(self) -> None:
        problems = self.engine._manuscript_content_problems(
            [
                ManuscriptSection(
                    section_id="discussion_limitations",
                    title="讨论与局限",
                    content_markdown=(
                        "交互项显著，但核心解释变量主效应失去显著性，"
                        "因此无法确认该调节边界。\n\n"
                        "根据冻结计划，后续可执行的检验步骤包括诊断与稳健性检验。"
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
                    "planned_diagnostics": [{"step_id": "D1"}],
                    "planned_robustness": [{"step_id": "R1"}],
                    "planned_falsification": [],
                    "planned_mechanisms": [],
                    "planned_heterogeneity": [],
                },
                "executed_evidence": {
                    "executions": [
                        {
                            "run_type": "diagnostic",
                            "execution_status": "succeeded",
                            "diagnostic_results": {},
                        },
                        {
                            "run_type": "robustness",
                            "execution_status": "succeeded",
                            "diagnostic_results": {},
                        },
                    ]
                },
                "writing_requirements": {
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                },
            },
        )

        self.assertTrue(any("否认调节边界" in problem for problem in problems))
        self.assertTrue(any("整体列为后续执行" in problem for problem in problems))

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
        stage_node_ids = [
            node_id
            for stage in definition["stages"]
            for node_id in stage["node_ids"]
        ]
        self.assertEqual(set(stage_node_ids), node_ids)
        self.assertEqual(len(stage_node_ids), len(node_ids))
        self.assertIn("Dify YAML", definition["description"])


if __name__ == "__main__":
    unittest.main()
