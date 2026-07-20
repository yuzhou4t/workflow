from __future__ import annotations

import unittest

from hypoweaver.models import (
    AnalysisPlan,
    ClaimRecord,
    CriticIssue,
    ExecutionRecord,
    ExecutionProvenance,
    Hypothesis,
    ModelSpec,
    PlannedStep,
    ReproductionAudit,
)
from hypoweaver.test_dag import (
    ENTERPRISE_PANEL_REGISTRY_VERSION,
    ENTERPRISE_PANEL_THREATS,
    THREAT_INDEPENDENT_REPLICATION,
    THREAT_LEAD_PLACEBO,
    THREAT_MECHANISM_INTERACTION_BOUNDARY,
    THREAT_POLICY_EVENT_STUDY,
    THREAT_POLICY_GROUP_FIXED_PRE,
    THREAT_POLICY_PLACEBO,
    THREAT_POLICY_PERMUTATION_PLACEBO,
    TestDagError,
    _execution_evidence_status,
    _reproduction_evidence_status,
    compile_enterprise_panel_test_dag,
    finalize_test_dag_executions,
    required_checks_for_claim,
    schedule_test_dag,
    select_primary_test_dag_with_budget,
    select_test_dag_with_budget,
    stable_claim_id,
    stable_claim_ids,
)
from hypoweaver.prompts import get_prompt


def _plan() -> AnalysisPlan:
    return AnalysisPlan(
        plan_id="plan-panel",
        plan_version=1,
        method_family="panel_association",
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
                formula="y ~ x",
                outcome="y",
                treatments_or_exposures=["x"],
                fixed_effects=["firm", "year"],
                standard_error_strategy="clustered by firm",
            )
        ],
        diagnostics=[
            PlannedStep(
                step_id="optional-diagnostic",
                name="optional diagnostic",
                priority="optional",
                rationale="an optional registered-independent diagnostic",
            )
        ],
        robustness_tests=[],
        falsification_tests=[],
        mechanism_tests=[],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=["firm", "year", "y", "x"],
        unsupported_requested_analyses=[],
    )


def _hypotheses() -> list[Hypothesis]:
    return [Hypothesis(hypothesis_id="H1", statement="x 与 y 存在关联。")]


class TestDagTests(unittest.TestCase):
    def test_fixed_pre_support_discloses_sample_composition_change(self) -> None:
        step = PlannedStep(
            step_id="check-policy-group-fixed-pre",
            name="fixed pre group",
            rationale="frozen sensitivity",
            threat_id=THREAT_POLICY_GROUP_FIXED_PRE,
            test_role="robustness",
        )
        execution = ExecutionRecord(
            execution_id="fixed-pre-execution",
            run_type="robustness",
            plan_step_id=step.step_id,
            check_id=step.step_id,
            execution_status="succeeded",
            estimates=[{"term": "policy_exposure", "coefficient": -0.5}],
            diagnostic_results={
                "rows_input": 249504,
                "rows_used": 155909,
                "entities_dropped_no_pre_policy_group": 45261,
            },
            provenance=ExecutionProvenance(
                implementation_id="policy-v2",
                implementation_version="2.0.0",
                code_sha256="a" * 64,
                environment_sha256="b" * 64,
                contract_sha256="c" * 64,
                data_sha256=[],
            ),
        )

        status, reason = _execution_evidence_status(
            execution,
            step,
            {"policy_exposure": -0.4},
            {"policy_exposure"},
            fixture_only=False,
            alpha=0.05,
        )

        self.assertEqual(status, "supporting")
        self.assertIn("155909/249504 rows", reason)
        self.assertIn("45261 entities", reason)
        self.assertIn("not same-sample robustness", reason)

    def test_estimator_only_reproduction_reason_names_shared_components(self) -> None:
        audit = ReproductionAudit(
            audit_id="reproduction-1",
            primary_run_id="primary-1",
            replication_run_id="replication-1",
            status="matched",
            mode="independent_implementation",
            independence_scope="estimator_only",
            shared_components=[
                "policy_causal analysis-table preparation",
                "policy event/placebo regressor construction",
            ],
            primary_implementation_id="primary-v1",
            replication_implementation_id="replica-v1",
        )

        status, reason = _reproduction_evidence_status(audit)

        self.assertEqual(status, "supporting")
        self.assertIn("independence_scope=estimator_only", reason)
        self.assertIn("analysis-table preparation", reason)
        self.assertIn("not end-to-end", reason)

    def test_fixed_pre_without_attrition_diagnostics_is_incomplete(self) -> None:
        step = PlannedStep(
            step_id="check-policy-group-fixed-pre",
            name="fixed pre group",
            rationale="frozen sensitivity",
            threat_id=THREAT_POLICY_GROUP_FIXED_PRE,
            test_role="robustness",
        )
        execution = ExecutionRecord(
            execution_id="fixed-pre-missing-flow",
            run_type="robustness",
            plan_step_id=step.step_id,
            execution_status="succeeded",
            estimates=[{"term": "policy_exposure", "coefficient": -0.5}],
            provenance=ExecutionProvenance(
                implementation_id="policy-v2",
                implementation_version="2.0.0",
                code_sha256="a" * 64,
                environment_sha256="b" * 64,
                contract_sha256="c" * 64,
                data_sha256=[],
            ),
        )

        status, reason = _execution_evidence_status(
            execution,
            step,
            {"policy_exposure": -0.4},
            {"policy_exposure"},
            fixture_only=False,
            alpha=0.05,
        )

        self.assertEqual(status, "incomplete")
        self.assertIn("attrition diagnostics", reason)

    def test_policy_fake_time_requires_clean_pre_policy_sample(self) -> None:
        step = PlannedStep(
            step_id="check-policy-placebo-time",
            name="fake timing",
            rationale="frozen fake timing",
            threat_id=THREAT_POLICY_PLACEBO,
            test_role="falsification",
        )
        provenance = ExecutionProvenance(
            implementation_id="policy-v2",
            implementation_version="2.0.0",
            code_sha256="a" * 64,
            environment_sha256="b" * 64,
            contract_sha256="c" * 64,
            data_sha256=[],
        )
        cases = (
            (1, 0.50, "invalid"),
            (0, 0.01, "opposing"),
            (0, 0.50, "supporting"),
        )
        for contamination, p_value, expected in cases:
            with self.subTest(contamination=contamination, p_value=p_value):
                execution = ExecutionRecord(
                    execution_id="fake-time-execution",
                    run_type="falsification",
                    plan_step_id=step.step_id,
                    check_id=step.step_id,
                    execution_status="succeeded",
                    estimates=[
                        {
                            "term": "placebo_exposure_2004",
                            "coefficient": -0.1,
                            "p_value": p_value,
                        }
                    ],
                    diagnostic_results={
                        "true_policy_contamination_rows": contamination,
                    },
                    provenance=provenance,
                )
                status, _ = _execution_evidence_status(
                    execution,
                    step,
                    {},
                    set(),
                    fixture_only=False,
                    alpha=0.05,
                )
                self.assertEqual(status, expected)

    def test_policy_event_study_requires_frozen_remote_pre_bin(self) -> None:
        step = PlannedStep(
            step_id="check-policy-event-study",
            name="event study",
            rationale="frozen event study",
            threat_id=THREAT_POLICY_EVENT_STUDY,
            test_role="falsification",
            parameters={
                "policy_design": {
                    "event_remote_pre_years": [1998, 1999, 2000, 2001],
                }
            },
        )
        execution = ExecutionRecord(
            execution_id="event-execution",
            run_type="falsification",
            plan_step_id=step.step_id,
            check_id=step.step_id,
            execution_status="succeeded",
            estimates=[
                {
                    "term": "event_remote_pre",
                    "coefficient": 0.1,
                    "p_value": 0.5,
                }
            ],
            diagnostic_results={
                "remote_pre_complete": False,
                "joint_pretrend_p_value": 0.5,
            },
            provenance=ExecutionProvenance(
                implementation_id="policy-v2",
                implementation_version="2.0.0",
                code_sha256="a" * 64,
                environment_sha256="b" * 64,
                contract_sha256="c" * 64,
                data_sha256=[],
            ),
        )

        status, reason = _execution_evidence_status(
            execution,
            step,
            {},
            set(),
            fixture_only=False,
            alpha=0.05,
        )

        self.assertEqual(status, "incomplete")
        self.assertIn("remote pre-period", reason)

    def test_permutation_placebo_uses_empirical_p_with_opposite_fake_timing_semantics(self) -> None:
        step = PlannedStep(
            step_id="check-policy-permutation-placebo",
            name="permutation",
            rationale="frozen permutation",
            threat_id=THREAT_POLICY_PERMUTATION_PLACEBO,
            test_role="falsification",
        )
        provenance = ExecutionProvenance(
            implementation_id="policy-v2",
            implementation_version="2.0.0",
            code_sha256="a" * 64,
            environment_sha256="b" * 64,
            contract_sha256="c" * 64,
            data_sha256=[],
        )
        for empirical_p, expected in ((0.01, "supporting"), (0.40, "opposing")):
            with self.subTest(empirical_p=empirical_p):
                execution = ExecutionRecord(
                    execution_id="permutation-execution",
                    run_type="falsification",
                    plan_step_id=step.step_id,
                    check_id=step.step_id,
                    execution_status="succeeded",
                    diagnostic_results={
                        "empirical_p_value": empirical_p,
                        "repetitions_requested": 500,
                        "repetitions_completed": 500,
                    },
                    provenance=provenance,
                )

                status, _ = _execution_evidence_status(
                    execution,
                    step,
                    {},
                    set(),
                    fixture_only=False,
                    alpha=0.05,
                )

                self.assertEqual(status, expected)

    def test_stable_claim_ids_are_code_owned_and_unique(self) -> None:
        self.assertEqual(stable_claim_id(" H1 "), "claim-H1")
        self.assertEqual(stable_claim_ids(_hypotheses()), ["claim-H1"])
        with self.assertRaises(TestDagError):
            stable_claim_ids(["H1", " H1 "])

    def test_compiler_adds_eight_registered_checks_and_binds_baseline(self) -> None:
        compiled = compile_enterprise_panel_test_dag(_plan(), _hypotheses())

        self.assertEqual(
            compiled.check_registry_version,
            ENTERPRISE_PANEL_REGISTRY_VERSION,
        )
        registered = {
            step.threat_id
            for step in [
                *compiled.diagnostics,
                *compiled.robustness_tests,
                *compiled.falsification_tests,
                *compiled.mechanism_tests,
            ]
            if step.threat_id is not None
        }
        self.assertEqual(
            registered,
            {threat.threat_id for threat in ENTERPRISE_PANEL_THREATS},
        )
        baseline = compiled.baseline_models[0]
        self.assertTrue(baseline.required_for_admission)
        self.assertEqual(baseline.target_claim_ids, ["claim-H1"])

        lead = next(
            step
            for step in compiled.falsification_tests
            if step.threat_id == THREAT_LEAD_PLACEBO
        )
        self.assertEqual(lead.parameters["alpha"], 0.05)
        self.assertTrue(lead.required_for_admission)
        self.assertEqual(
            compile_enterprise_panel_test_dag(compiled, _hypotheses()),
            compiled,
        )

    def test_reviewer_mapping_uses_only_structured_threat_id(self) -> None:
        known = CriticIssue(
            issue_id="issue-known",
            dimension="causal",
            severity="major",
            evidence="lead is required",
            why_it_matters="future information can falsify timing",
            required_fix="this prose deliberately names an unrelated winsor threshold",
            return_stage="analysis_plan",
            repair_type="technical",
            threat_id=THREAT_LEAD_PLACEBO,
        )
        unknown = CriticIssue(
            issue_id="issue-unknown",
            dimension="statistical",
            severity="major",
            evidence="unknown concern",
            why_it_matters="cannot map safely",
            required_fix="run lead_exposure=future_x and p<0.05",
            return_stage="analysis_plan",
            repair_type="technical",
            threat_id="panel.unregistered_threat",
        )

        compiled = compile_enterprise_panel_test_dag(
            _plan(),
            _hypotheses(),
            [known, unknown],
        )
        lead = next(
            step
            for step in compiled.falsification_tests
            if step.threat_id == THREAT_LEAD_PLACEBO
        )
        self.assertEqual(lead.source_issue_ids, ["issue-known"])
        self.assertEqual(lead.parameters, {"alpha": 0.05})

        placeholder = next(
            step
            for step in compiled.diagnostics
            if step.threat_id == "panel.unregistered_threat"
        )
        self.assertEqual(placeholder.source_issue_ids, ["issue-unknown"])
        self.assertTrue(placeholder.required_for_admission)
        self.assertIn("not_executable", placeholder.not_executable_reason or "")
        self.assertNotIn("future_x", str(placeholder.parameters))
        self.assertEqual(
            compile_enterprise_panel_test_dag(
                compiled,
                _hypotheses(),
                [known, unknown],
            ),
            compiled,
        )

    def test_unknown_placeholder_id_collision_merges_source_issues(self) -> None:
        plan = _plan()
        plan.diagnostics.append(
            PlannedStep(
                step_id="check-unregistered-panel-unregistered_threat-issue-new",
                name="existing unknown threat",
                priority="required",
                rationale="preserve the earlier reviewer source",
                threat_id="panel.unregistered_threat",
                test_role="exploratory",
                required_for_admission=True,
                source_issue_ids=["issue-existing"],
                not_executable_reason="not executable",
            )
        )
        issue = CriticIssue(
            issue_id="issue-new",
            dimension="statistical",
            severity="major",
            evidence="unknown concern",
            why_it_matters="cannot map safely",
            required_fix="run lead_exposure=future_x",
            return_stage="analysis_plan",
            repair_type="technical",
            threat_id="panel.unregistered_threat",
        )

        compiled = compile_enterprise_panel_test_dag(
            plan,
            _hypotheses(),
            [issue],
        )
        placeholders = [
            step
            for step in compiled.diagnostics
            if step.step_id
            == "check-unregistered-panel-unregistered_threat-issue-new"
        ]

        self.assertEqual(len(placeholders), 1)
        self.assertEqual(
            placeholders[0].source_issue_ids,
            ["issue-existing", "issue-new"],
        )
        self.assertTrue(placeholders[0].required_for_admission)
        self.assertIn("not_executable", placeholders[0].not_executable_reason or "")
        self.assertNotIn("future_x", str(placeholders[0].parameters))

    def test_unknown_placeholder_safe_collision_in_same_section_fails_closed(self) -> None:
        issues = [
            CriticIssue(
                issue_id="issue-collision",
                dimension="statistical",
                severity="major",
                evidence="first unknown threat",
                why_it_matters="the threats must remain distinct",
                required_fix="do not parse panel.future/a",
                return_stage="analysis_plan",
                repair_type="technical",
                threat_id="panel.future/a",
            ),
            CriticIssue(
                issue_id="issue-collision",
                dimension="statistical",
                severity="major",
                evidence="second unknown threat",
                why_it_matters="the threats must remain distinct",
                required_fix="do not parse panel.future:a",
                return_stage="analysis_plan",
                repair_type="technical",
                threat_id="panel.future:a",
            ),
        ]

        with self.assertRaisesRegex(TestDagError, "different threat ids"):
            compile_enterprise_panel_test_dag(_plan(), _hypotheses(), issues)

    def test_unknown_placeholder_safe_collision_across_sections_fails_closed(self) -> None:
        issues = [
            CriticIssue(
                issue_id="issue-collision",
                dimension="statistical",
                severity="major",
                evidence="diagnostic unknown threat",
                why_it_matters="the threats must remain distinct",
                required_fix="do not parse panel.future/a",
                return_stage="analysis_plan",
                repair_type="technical",
                threat_id="panel.future/a",
            ),
            CriticIssue(
                issue_id="issue-collision",
                dimension="causal",
                severity="major",
                evidence="falsification unknown threat",
                why_it_matters="the threats must remain distinct",
                required_fix="do not parse panel.future:a",
                return_stage="analysis_plan",
                repair_type="technical",
                threat_id="panel.future:a",
            ),
        ]

        with self.assertRaisesRegex(TestDagError, "different threat ids"):
            compile_enterprise_panel_test_dag(_plan(), _hypotheses(), issues)

    def test_reviewer_prompts_require_the_structured_registry(self) -> None:
        for prompt_key in (
            "design_reviewer",
            "reviewer_report_batch",
            "method_critic",
        ):
            prompt = get_prompt(prompt_key)
            self.assertIn("enterprise-panel-v1", prompt.system)
            self.assertIn("panel.lead_placebo", prompt.system)
            self.assertIn("panel.independent_replication", prompt.system)
            self.assertIn("不解析 required_fix", prompt.system)

    def test_mechanism_check_targets_only_code_identified_mechanism_claim(self) -> None:
        hypotheses = [
            Hypothesis(hypothesis_id="H1", statement="x 与 y 相关。"),
            Hypothesis(
                hypothesis_id="H2",
                statement="m 界定 x 与 y 的关联。",
                mechanism="m 是预先声明的机制边界。",
            ),
        ]
        compiled = compile_enterprise_panel_test_dag(_plan(), hypotheses)
        mechanism = next(
            step
            for step in compiled.mechanism_tests
            if step.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
        )
        self.assertEqual(mechanism.target_claim_ids, ["claim-H2"])

        common = dict(
            claim_text="claim",
            evidence_status="not_tested",
            allowed_strength="preliminary",
            supporting_runs=[],
            opposing_runs=[],
            scope="frozen",
            robustness_status="pending",
            unresolved_risks=[],
        )
        main_claim = ClaimRecord(
            claim_id="claim-H1",
            hypothesis_id="H1",
            claim_type="associational",
            **common,
        )
        mechanism_claim = ClaimRecord(
            claim_id="claim-H2",
            hypothesis_id="H2",
            claim_type="mechanism",
            **common,
        )
        self.assertNotIn(
            mechanism.step_id,
            required_checks_for_claim(compiled, main_claim),
        )
        self.assertIn(
            mechanism.step_id,
            required_checks_for_claim(compiled, mechanism_claim),
        )

    def test_association_plan_does_not_require_a_proposed_mechanism_check(self) -> None:
        hypothesis = Hypothesis(
            hypothesis_id="H1",
            statement="x 与 y 相关。",
            mechanism="m 是一个待检验的解释。",
        )
        compiled = compile_enterprise_panel_test_dag(
            _plan(),
            [hypothesis],
            mechanism_hypothesis_ids=[],
        )
        mechanism = next(
            step
            for step in compiled.mechanism_tests
            if step.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
        )
        self.assertEqual(mechanism.target_claim_ids, [])
        self.assertFalse(mechanism.required_for_admission)

    def test_required_checks_ignore_model_authored_known_subset(self) -> None:
        compiled = compile_enterprise_panel_test_dag(
            _plan(),
            [Hypothesis(hypothesis_id="H1", statement="x 与 y 相关。")],
        )
        claim = ClaimRecord(
            claim_id="claim-H1",
            hypothesis_id="H1",
            claim_type="associational",
            claim_text="x 与 y 相关。",
            evidence_status="supported",
            allowed_strength="associational",
            supporting_runs=[],
            opposing_runs=[],
            scope="frozen",
            robustness_status="complete",
            unresolved_risks=[],
            required_check_ids=["baseline"],
        )

        required = required_checks_for_claim(compiled, claim)
        lead = next(
            item.step_id
            for item in compiled.falsification_tests
            if item.threat_id == THREAT_LEAD_PLACEBO
        )
        replication = next(
            item.step_id
            for item in compiled.robustness_tests
            if item.threat_id == THREAT_INDEPENDENT_REPLICATION
        )

        self.assertIn(lead, required)
        self.assertIn(replication, required)
        self.assertGreater(len(required), 1)

    def test_schedule_enforces_required_then_optional_then_replication(self) -> None:
        compiled = compile_enterprise_panel_test_dag(_plan(), _hypotheses())
        scheduled = schedule_test_dag(compiled)
        run_types = [item.run_type for item in scheduled]

        first_baseline = run_types.index("baseline")
        self.assertTrue(
            all(item.run_type == "diagnostic" for item in scheduled[:first_baseline])
        )
        self.assertEqual(scheduled[-1].run_type, "replication")
        required_nonreplication = [
            index
            for index, item in enumerate(scheduled)
            if item.required and item.run_type != "replication"
        ]
        optional_nonreplication = [
            index
            for index, item in enumerate(scheduled)
            if not item.required and item.run_type != "replication"
        ]
        self.assertLess(max(required_nonreplication), min(optional_nonreplication))

    def test_budget_never_spends_on_not_executable_or_optional_before_required(self) -> None:
        compiled = compile_enterprise_panel_test_dag(_plan(), _hypotheses())
        schedule = select_test_dag_with_budget(compiled, max_executions=2)

        self.assertEqual(len(schedule.selected), 2)
        self.assertTrue(all(item.required for item in schedule.selected))
        self.assertTrue(
            all(item.step.not_executable_reason is None for item in schedule.selected)
        )
        self.assertTrue(
            any(item.step.not_executable_reason for item in schedule.omitted)
        )

    def test_primary_budget_excludes_the_independent_replication_service(self) -> None:
        compiled = compile_enterprise_panel_test_dag(_plan(), _hypotheses())
        schedule = select_primary_test_dag_with_budget(compiled, max_executions=20)

        self.assertTrue(all(item.run_type != "replication" for item in schedule.selected))
        self.assertTrue(all(item.run_type != "replication" for item in schedule.omitted))

    def test_invalid_sample_rule_is_not_executable_and_lead_alpha_is_frozen(self) -> None:
        plan = _plan()
        plan.robustness_tests = [
            PlannedStep(
                step_id="unsafe-sample-rule",
                name="unsafe sample rule",
                rationale="must not be guessed",
                parameters={"sample_filter": "year >= 2018 and significant == 1"},
            )
        ]
        plan.falsification_tests = [
            PlannedStep(
                step_id="lead-custom-alpha",
                name="lead",
                rationale="frozen falsification",
                parameters={"lead_exposure": "lead_x", "alpha": 0.2},
            )
        ]

        compiled = compile_enterprise_panel_test_dag(plan, _hypotheses())
        sample = next(
            item
            for item in compiled.robustness_tests
            if item.step_id == "unsafe-sample-rule"
        )
        lead = next(
            item
            for item in compiled.falsification_tests
            if item.step_id == "lead-custom-alpha"
        )

        self.assertIsNotNone(sample.not_executable_reason)
        self.assertEqual(lead.parameters["alpha"], 0.05)

    def test_unknown_robustness_parameters_cannot_be_silent_baseline_reruns(self) -> None:
        plan = _plan()
        plan.robustness_tests = [
            PlannedStep(
                step_id="rob-se-alternative",
                name="alternative clustering",
                rationale="model-authored unsupported robustness",
                parameters={"cluster_level": "industry_year"},
            ),
            PlannedStep(
                step_id="rob-exclude-outliers",
                name="trim outliers",
                rationale="model-authored incomplete trimming rule",
                parameters={"trim_percent": 5},
            ),
        ]

        compiled = compile_enterprise_panel_test_dag(plan, _hypotheses())
        steps = {
            item.step_id: item
            for item in compiled.robustness_tests
            if item.step_id in {"rob-se-alternative", "rob-exclude-outliers"}
        }

        self.assertEqual(set(steps), {"rob-se-alternative", "rob-exclude-outliers"})
        self.assertTrue(
            all(item.not_executable_reason is not None for item in steps.values())
        )

    def test_finalizer_emits_exactly_one_terminal_record_per_frozen_step(self) -> None:
        compiled = compile_enterprise_panel_test_dag(_plan(), _hypotheses())
        scheduled = schedule_test_dag(compiled)
        first = scheduled[0]
        executions = [
            ExecutionRecord(
                execution_id="execution-first",
                run_type=first.run_type,
                plan_step_id=first.step.step_id,
                execution_status="succeeded",
            )
        ]
        reason_codes = {
            item.step.step_id: "budget_exhausted"
            for item in scheduled[1:]
            if item.step.not_executable_reason is None
        }

        finalized = finalize_test_dag_executions(
            compiled,
            executions,
            reason_codes=reason_codes,
        )

        self.assertEqual(len(finalized), len(scheduled))
        self.assertEqual(
            {item.plan_step_id for item in finalized},
            {item.step.step_id for item in scheduled},
        )
        self.assertTrue(
            all(item.execution_status in {"succeeded", "not_executed"} for item in finalized)
        )
        for execution in finalized[1:]:
            planned = next(
                item.step for item in scheduled if item.step.step_id == execution.plan_step_id
            )
            expected = (
                "not_executable"
                if planned.not_executable_reason
                else "budget_exhausted"
            )
            self.assertEqual(execution.not_executed_reason_code, expected)

        with self.assertRaisesRegex(TestDagError, "multiple execution"):
            finalize_test_dag_executions(compiled, [executions[0], executions[0]])

    def test_global_step_ids_must_be_unique(self) -> None:
        plan = _plan()
        plan.diagnostics.append(
            PlannedStep(
                step_id="baseline",
                name="duplicate",
                rationale="invalid duplicate id",
            )
        )
        with self.assertRaisesRegex(TestDagError, "globally unique"):
            compile_enterprise_panel_test_dag(plan, _hypotheses())


if __name__ == "__main__":
    unittest.main()
