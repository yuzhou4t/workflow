from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from hypoweaver.adapters import FixtureModelGateway
from hypoweaver.case_import import DatasetRegistry, LocalCaseImporter
from hypoweaver.definition import build_app_a_definition
from hypoweaver.engine import (
    MANUSCRIPT_SECTION_SPECS,
    WorkflowEngine,
    WorkflowTransitionError,
)
from hypoweaver.models import (
    AnalysisPlan,
    CaseSubmission,
    ClaimLedger,
    CreateRunRequest,
    CriticIssue,
    CriticReport,
    DataProfile,
    DatasetRef,
    DesignEnvelope,
    DesignArena,
    GateDecisionRequest,
    FormalResearchContract,
    MethodRoute,
    ManuscriptPackage,
    ManuscriptSection,
    ModelSpec,
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


class FeedbackTrackingGateway(FullManuscriptGateway):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, prompt_key, payload, output_model):
        self.calls.append(payload)
        return await super().generate(prompt_key, payload, output_model)


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
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
        self.assertNotIn("sealed_output", run.artifacts)
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
        self.assertTrue(
            any(step.node_id == "design_candidate_reuse" for step in run.steps)
        )

    async def test_design_arena_continues_when_one_candidate_call_fails(self) -> None:
        original_llm_step = self.engine._llm_step

        async def fail_one_candidate(*args, **kwargs):
            prompt_key = args[2]
            payload = args[3]
            if (
                prompt_key == "analysis_design"
                and payload["candidate_strategy"] == "direct_baseline"
            ):
                raise RuntimeError("candidate timeout")
            return await original_llm_step(*args, **kwargs)

        with patch.object(self.engine, "_llm_step", side_effect=fail_one_candidate):
            run = await self._to_h2()

        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H2"))
        candidates = run.artifacts["candidate_design_set"]["payload"]["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertNotIn(
            "candidate-direct_baseline",
            {candidate["candidate_id"] for candidate in candidates},
        )

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

    def test_analysis_design_prompt_distinguishes_model_and_planned_steps(self) -> None:
        prompt = get_prompt("analysis_design")

        self.assertEqual(prompt.version, "1.6.0")
        self.assertIn("baseline_models 的元素使用 ModelSpec", prompt.system)
        self.assertIn("必须严格使用 PlannedStep", prompt.system)
        self.assertIn("具体设置必须放入 parameters", prompt.system)
        self.assertIn("其余每个计划类别最多 1 个最关键步骤", prompt.system)

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
            "claim_ledger",
        ):
            prompt = get_prompt(prompt_key)
            self.assertEqual(prompt.version, "1.1.0")
            self.assertIn("interaction_term", prompt.system)
            self.assertIn("主效应", prompt.system)

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
        with patch.object(
            self.engine,
            "_gateway",
            return_value=RepairingManuscriptGateway(),
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
        self.assertEqual(len(writer_steps), 9)

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
            [call["section_spec"]["section_id"] for call in gateway.calls],
            ["conclusion"],
        )
        feedback = gateway.calls[0]["revision_feedback"]["problems"]
        self.assertTrue(any("H4 人工审稿意见" in problem for problem in feedback))

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
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H4"))
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
        self.assertIn("未发现达到常用统计显著性阈值的", conclusion["content_markdown"])
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
        research_run = self.engine._artifact(run, "research_run", ResearchRun)
        exposure, control = package.variables[:2]
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
        self.assertIn("Dify YAML", definition["description"])


if __name__ == "__main__":
    unittest.main()
