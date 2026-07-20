from __future__ import annotations

import unittest

from hypoweaver.claim_gate import (
    ClaimGateError,
    _tightened_evidence_status,
    apply_claim_gate,
    causal_wording_violations,
    permitted_h3_decisions,
    validate_h3_claim_decision,
)
from hypoweaver.models import (
    AnalysisPlan,
    ClaimLedger,
    ClaimRecord,
    CriticIssue,
    EvidenceObject,
    ExecutionProvenance,
    ExecutionRecord,
    FormalResearchContract,
    Hypothesis,
    ModelSpec,
    PlannedStep,
    ReproductionAudit,
    ResearchPackage,
    ResearchRun,
    ScientificAudit,
)
from hypoweaver.seal import canonical_sha256
from hypoweaver.test_dag import (
    THREAT_INDEPENDENT_REPLICATION,
    THREAT_LEAD_PLACEBO,
    build_evidence_registry,
    compile_enterprise_panel_test_dag,
    finalize_test_dag_executions,
    schedule_test_dag,
)


def _package() -> ResearchPackage:
    return ResearchPackage(
        case_id="case-panel",
        title="enterprise panel",
        research_question="x 是否与 y 相关？",
        hypotheses=[Hypothesis(hypothesis_id="H1", statement="x 与 y 存在关联。")],
        unit_of_analysis="firm-year",
        variables=[
            {"name": "firm", "role": "id"},
            {"name": "year", "role": "time"},
            {"name": "y", "role": "outcome"},
            {"name": "y_alt", "role": "outcome"},
            {"name": "x", "role": "exposure"},
            {"name": "x_alt", "role": "exposure"},
            {"name": "lead_x", "role": "exposure"},
        ],
        dataset_refs=[],
        input_conflicts=[],
        missing_required_information=[],
    )


def _compiled_plan(package: ResearchPackage) -> AnalysisPlan:
    plan = AnalysisPlan(
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
                rationale="primary association",
                estimator="PanelOLS",
                formula="y ~ x",
                outcome="y",
                treatments_or_exposures=["x"],
                fixed_effects=["firm", "year"],
                standard_error_strategy="clustered by firm",
            )
        ],
        diagnostics=[],
        robustness_tests=[
            PlannedStep(
                step_id="alternative-outcome",
                name="alternative outcome",
                rationale="measurement robustness",
                parameters={"alternative_outcome": "y_alt"},
            ),
            PlannedStep(
                step_id="sample-sensitivity",
                name="sample sensitivity",
                rationale="pre-registered sample boundary",
                parameters={"sample_filter": "year >= 2018"},
            ),
            PlannedStep(
                step_id="alternative-exposure",
                name="alternative exposure",
                rationale="measurement robustness",
                parameters={"alternative_exposure": "x_alt"},
            ),
        ],
        falsification_tests=[
            PlannedStep(
                step_id="lead-test",
                name="lead test",
                rationale="falsify timing",
                parameters={"lead_exposure": "lead_x", "alpha": 0.05},
            )
        ],
        mechanism_tests=[],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=["firm", "year", "y", "y_alt", "x", "lead_x"],
        unsupported_requested_analyses=[],
    )
    return compile_enterprise_panel_test_dag(plan, package.hypotheses)


def _candidate(run_id: str, *, text: str = "x 与 y 存在关联。") -> ClaimLedger:
    return ClaimLedger(
        ledger_id="candidate-ledger",
        case_id="case-panel",
        research_run_id=run_id,
        claims=[
            ClaimRecord(
                claim_id="claim-H1",
                hypothesis_id="H1",
                claim_text=text,
                evidence_status="supported",
                allowed_strength="causal_strong",
                supporting_runs=[],
                opposing_runs=[],
                scope="frozen sample",
                robustness_status="complete",
                unresolved_risks=[],
            )
        ],
        excluded_findings=[],
        unresolved_issues=[],
    )


def _bundle() -> dict[str, object]:
    package = _package()
    plan = _compiled_plan(package)
    plan_hash = canonical_sha256(plan.model_dump(mode="json"))
    contract = FormalResearchContract(
        contract_id="contract-panel",
        case_id=package.case_id,
        approved_at="2026-07-16T00:00:00+00:00",
        approved_by="tester",
        decision_record_id="decision-h2",
        research_package_hash=canonical_sha256(package.model_dump(mode="json")),
        data_hashes=[],
        dataset_refs=[],
        approved_plan_hash=plan_hash,
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )
    contract_hash = canonical_sha256(contract.model_dump(mode="json"))
    provenance = ExecutionProvenance(
        implementation_id="linearmodels-panelols-v1",
        implementation_version="1.0.0",
        code_sha256="a" * 64,
        environment_sha256="b" * 64,
        contract_sha256=contract_hash,
        data_sha256=[],
    )
    executions: list[ExecutionRecord] = []
    for scheduled in schedule_test_dag(plan):
        if scheduled.run_type == "replication" or scheduled.step.not_executable_reason:
            continue
        estimates: list[dict[str, object]] = []
        diagnostics: dict[str, object] = {"feasible": True}
        if scheduled.run_type == "baseline":
            estimates = [{"term": "x", "coefficient": 0.5, "p_value": 0.01}]
        elif scheduled.run_type == "robustness":
            term = str(scheduled.step.parameters.get("alternative_exposure") or "x")
            estimates = [{"term": term, "coefficient": 0.4, "p_value": 0.02}]
        elif scheduled.run_type == "falsification":
            estimates = [{"term": "lead_x", "coefficient": 0.01, "p_value": 0.5}]
        executions.append(
            ExecutionRecord(
                execution_id=f"execution-{scheduled.step.step_id}",
                run_type=scheduled.run_type,
                plan_step_id=scheduled.step.step_id,
                check_id=scheduled.step.step_id,
                execution_status="succeeded",
                estimates=estimates,
                diagnostic_results=diagnostics,
                provenance=provenance,
            )
        )
    executions = finalize_test_dag_executions(plan, executions)
    run = ResearchRun(
        research_run_id="research-panel",
        case_id=package.case_id,
        contract_hash=plan_hash,
        plan_version=plan.plan_version,
        execution_status="succeeded",
        scientific_status="valid",
        fixture_only=False,
        executions=executions,
    )
    audit = ReproductionAudit(
        audit_id="reproduction-panel",
        primary_run_id=run.research_run_id,
        replication_run_id="research-panel-replica",
        status="matched",
        mode="independent_implementation",
        covered_plan_step_ids=[
            item.plan_step_id
            for item in run.executions
            if item.execution_status == "succeeded" and item.estimates
        ],
        primary_implementation_id="linearmodels-panelols-v1",
        replication_implementation_id="numpy-two-way-within-v1",
    )
    scientific = ScientificAudit(
        verdict="valid",
        contract_compliant=True,
        critical_issues=[],
        unresolved_risks=[],
    )
    candidate = _candidate(run.research_run_id)
    registry = build_evidence_registry(
        plan,
        run,
        candidate.claims,
        reproduction_audit=audit,
        scientific_audit=scientific,
    )
    return {
        "package": package,
        "plan": plan,
        "contract": contract,
        "run": run,
        "audit": audit,
        "scientific": scientific,
        "candidate": candidate,
        "registry": registry,
    }


def _gate(bundle: dict[str, object]):
    package = bundle["package"]
    assert isinstance(package, ResearchPackage)
    return apply_claim_gate(
        bundle["candidate"],  # type: ignore[arg-type]
        bundle["plan"],  # type: ignore[arg-type]
        bundle["run"],  # type: ignore[arg-type]
        bundle["registry"],  # type: ignore[arg-type]
        package.hypotheses,
        contract=bundle["contract"],  # type: ignore[arg-type]
        reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
        scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        research_package=package,
    )


class ClaimGateTests(unittest.TestCase):
    def test_code_registry_status_is_authoritative_over_candidate_status(self) -> None:
        pairs = (
            ("mixed", "inconclusive"),
            ("inconclusive", "mixed"),
            ("supported", "mixed"),
            ("not_tested", "supported"),
            ("mixed", "contradicted"),
        )
        for statuses in pairs:
            with self.subTest(statuses=statuses):
                self.assertEqual(
                    _tightened_evidence_status(*statuses),
                    statuses[0],
                )

    def test_clean_required_evidence_is_admitted_but_capped_at_association(self) -> None:
        ledger, report = _gate(_bundle())

        claim = ledger.claims[0]
        self.assertEqual(claim.admission_status, "admitted")
        self.assertEqual(claim.max_allowed_strength, "associational")
        self.assertEqual(claim.allowed_strength, "associational")
        self.assertEqual(claim.evidence_status, "supported")
        self.assertTrue(claim.required_check_ids)
        self.assertEqual(report.results[0].admission_status, "admitted")

    def test_model_copied_ledger_envelope_is_audited_and_code_rebound(self) -> None:
        bundle = _bundle()
        candidate = bundle["candidate"]
        run = bundle["run"]
        assert isinstance(candidate, ClaimLedger)
        assert isinstance(run, ResearchRun)
        candidate.case_id = "case-panel-typo"

        ledger, report = _gate(bundle)

        self.assertEqual(candidate.case_id, "case-panel-typo")
        self.assertEqual(candidate.research_run_id, run.research_run_id)
        self.assertEqual(ledger.case_id, run.case_id)
        self.assertEqual(ledger.research_run_id, run.research_run_id)
        self.assertEqual(ledger.claims[0].admission_status, "admitted")
        self.assertEqual(report.results[0].admission_status, "admitted")
        self.assertTrue(
            any(
                "case_id was non-authoritative" in item
                and "case-panel-typo" in item
                and run.case_id in item
                for item in ledger.unresolved_issues
            )
        )
        first_gate_id = report.gate_id

        second_bundle = _bundle()
        second_candidate = second_bundle["candidate"]
        assert isinstance(second_candidate, ClaimLedger)
        second_candidate.case_id = "another-case-panel-typo"
        _, second_report = _gate(second_bundle)
        self.assertNotEqual(first_gate_id, second_report.gate_id)

    def test_model_copied_research_run_id_mismatch_remains_fail_closed(self) -> None:
        bundle = _bundle()
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        candidate.research_run_id = "research-panel-typo"

        ledger, report = _gate(bundle)

        self.assertEqual(ledger.claims[0].admission_status, "prohibited")
        self.assertEqual(report.results[0].admission_status, "prohibited")
        self.assertTrue(
            any(
                "research_run_id does not match" in reason
                for reason in ledger.claims[0].gate_reasons
            )
        )

    def test_candidate_scientific_status_is_advisory(self) -> None:
        for evidence_status in (
            "mixed",
            "contradicted",
            "inconclusive",
            "not_tested",
        ):
            with self.subTest(evidence_status=evidence_status):
                bundle = _bundle()
                candidate = bundle["candidate"]
                assert isinstance(candidate, ClaimLedger)
                candidate.claims[0].evidence_status = evidence_status  # type: ignore[assignment]

                ledger, report = _gate(bundle)

                claim = ledger.claims[0]
                self.assertEqual(
                    (claim.admission_status, claim.max_allowed_strength),
                    ("admitted", "associational"),
                )
                self.assertEqual(claim.evidence_status, "supported")
                self.assertEqual(report.results[0].admission_status, "admitted")
                self.assertTrue(
                    any("advisory assessment" in reason for reason in claim.gate_reasons)
                )

    def test_significant_lead_requires_mixed_downgrade(self) -> None:
        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        lead = next(item for item in run.executions if item.plan_step_id == "lead-test")
        lead.estimates[0]["p_value"] = 0.049
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        bundle["registry"] = build_evidence_registry(
            bundle["plan"],  # type: ignore[arg-type]
            run,
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertEqual(ledger.claims[0].admission_status, "downgrade_required")
        self.assertEqual(ledger.claims[0].max_allowed_strength, "mixed")
        self.assertEqual(ledger.claims[0].evidence_status, "mixed")

    def test_known_required_subset_cannot_bypass_lead_or_replication(self) -> None:
        bundle = _bundle()
        plan = bundle["plan"]
        run = bundle["run"]
        candidate = bundle["candidate"]
        audit = bundle["audit"]
        assert isinstance(plan, AnalysisPlan)
        assert isinstance(run, ResearchRun)
        assert isinstance(candidate, ClaimLedger)
        assert isinstance(audit, ReproductionAudit)
        candidate.claims[0].required_check_ids = ["baseline"]
        lead = next(item for item in run.executions if item.plan_step_id == "lead-test")
        lead.estimates[0]["p_value"] = 0.049
        bundle["registry"] = build_evidence_registry(
            plan,
            run,
            candidate.claims,
            reproduction_audit=audit,
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        replication = next(
            item.step_id
            for item in plan.robustness_tests
            if item.threat_id == THREAT_INDEPENDENT_REPLICATION
        )
        self.assertEqual(ledger.claims[0].admission_status, "downgrade_required")
        self.assertEqual(ledger.claims[0].max_allowed_strength, "mixed")
        self.assertIn("lead-test", ledger.claims[0].required_check_ids)
        self.assertIn(replication, ledger.claims[0].required_check_ids)

        failed_bundle = _bundle()
        failed_plan = failed_bundle["plan"]
        failed_run = failed_bundle["run"]
        failed_candidate = failed_bundle["candidate"]
        failed_audit = failed_bundle["audit"]
        assert isinstance(failed_plan, AnalysisPlan)
        assert isinstance(failed_run, ResearchRun)
        assert isinstance(failed_candidate, ClaimLedger)
        assert isinstance(failed_audit, ReproductionAudit)
        failed_candidate.claims[0].required_check_ids = ["baseline"]
        failed_audit = failed_audit.model_copy(update={"status": "failed"})
        failed_bundle["audit"] = failed_audit
        failed_bundle["registry"] = build_evidence_registry(
            failed_plan,
            failed_run,
            failed_candidate.claims,
            reproduction_audit=failed_audit,
            scientific_audit=failed_bundle["scientific"],  # type: ignore[arg-type]
        )

        failed_ledger, _ = _gate(failed_bundle)

        self.assertEqual(failed_ledger.claims[0].admission_status, "prohibited")
        self.assertIn(replication, failed_ledger.claims[0].required_check_ids)

    def test_reversed_robustness_requires_mixed_downgrade(self) -> None:
        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        robustness = next(
            item for item in run.executions if item.plan_step_id == "alternative-outcome"
        )
        robustness.estimates[0]["coefficient"] = -0.4
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        bundle["registry"] = build_evidence_registry(
            bundle["plan"],  # type: ignore[arg-type]
            run,
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertEqual(ledger.claims[0].max_allowed_strength, "mixed")

    def test_alternative_outcome_ignores_control_sign_changes(self) -> None:
        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        baseline = next(item for item in run.executions if item.run_type == "baseline")
        baseline.estimates.append(
            {"term": "control", "coefficient": 0.3, "p_value": 0.01}
        )
        robustness = next(
            item for item in run.executions if item.plan_step_id == "alternative-outcome"
        )
        robustness.estimates.append(
            {"term": "control", "coefficient": -0.2, "p_value": 0.02}
        )
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        bundle["registry"] = build_evidence_registry(
            bundle["plan"],  # type: ignore[arg-type]
            run,
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertNotIn(
            "Robustness estimate reverses the baseline sign.",
            ledger.claims[0].gate_reasons,
        )

    def test_reversed_alternative_exposure_uses_frozen_baseline_mapping(self) -> None:
        bundle = _bundle()
        plan = bundle["plan"]
        assert isinstance(plan, AnalysisPlan)
        step = next(
            item
            for item in plan.robustness_tests
            if item.step_id == "alternative-exposure"
        )
        self.assertEqual(step.parameters["replaces_exposure"], "x")
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        robustness = next(
            item
            for item in run.executions
            if item.plan_step_id == "alternative-exposure"
        )
        robustness.estimates[0]["coefficient"] = -0.4
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        bundle["registry"] = build_evidence_registry(
            plan,
            run,
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertEqual(ledger.claims[0].max_allowed_strength, "mixed")

    def test_budget_exhaustion_caps_claim_at_preliminary(self) -> None:
        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        check = next(
            item for item in run.executions if item.plan_step_id == "sample-sensitivity"
        )
        check.execution_status = "not_executed"
        check.estimates = []
        check.not_executed_reason_code = "budget_exhausted"
        check.error = "budget exhausted"
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        bundle["registry"] = build_evidence_registry(
            bundle["plan"],  # type: ignore[arg-type]
            run,
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertEqual(ledger.claims[0].admission_status, "downgrade_required")
        self.assertEqual(ledger.claims[0].max_allowed_strength, "preliminary")

    def test_fixture_contract_and_reproduction_failures_are_prohibited(self) -> None:
        for mutation in ("fixture", "contract", "reproduction"):
            with self.subTest(mutation=mutation):
                bundle = _bundle()
                run = bundle["run"]
                assert isinstance(run, ResearchRun)
                if mutation == "fixture":
                    run = run.model_copy(
                        update={
                            "fixture_only": True,
                            "execution_status": "fixture_only",
                            "scientific_status": "not_evaluated",
                            "executions": [
                                item.model_copy(
                                    update={
                                        "execution_status": "not_executed",
                                        "estimates": [],
                                        "diagnostic_results": {},
                                        "provenance": None,
                                    }
                                )
                                for item in run.executions
                            ],
                        }
                    )
                    bundle["run"] = run
                elif mutation == "contract":
                    bundle["run"] = run.model_copy(update={"contract_hash": "bad"})
                else:
                    audit = bundle["audit"]
                    assert isinstance(audit, ReproductionAudit)
                    bundle["audit"] = audit.model_copy(update={"status": "failed"})
                candidate = bundle["candidate"]
                assert isinstance(candidate, ClaimLedger)
                bundle["registry"] = build_evidence_registry(
                    bundle["plan"],  # type: ignore[arg-type]
                    bundle["run"],  # type: ignore[arg-type]
                    candidate.claims,
                    reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
                    scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
                )

                ledger, _ = _gate(bundle)

                self.assertEqual(ledger.claims[0].admission_status, "prohibited")
                self.assertEqual(ledger.claims[0].allowed_strength, "prohibited")

    def test_free_text_scientific_audit_is_advisory(self) -> None:
        bundle = _bundle()
        bundle["scientific"] = ScientificAudit(
            verdict="invalid",
            contract_compliant=False,
            critical_issues=["incorrectly claims independent reproduction was missing"],
            unresolved_risks=[],
        )
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        bundle["registry"] = build_evidence_registry(
            bundle["plan"],  # type: ignore[arg-type]
            bundle["run"],  # type: ignore[arg-type]
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertEqual(ledger.claims[0].admission_status, "admitted")
        self.assertFalse(
            any(
                evidence.source_kind == "scientific_audit"
                for evidence in bundle["registry"].evidence  # type: ignore[union-attr]
            )
        )

    def test_unknown_claim_execution_and_check_references_are_rejected(self) -> None:
        bundle = _bundle()
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        candidate.claims[0].supporting_runs = ["execution-does-not-exist"]
        candidate.claims.append(
            candidate.claims[0].model_copy(
                update={"claim_id": "claim-invented", "hypothesis_id": "invented"}
            )
        )

        ledger, report = _gate(bundle)

        self.assertEqual(ledger.claims[0].admission_status, "rejected")
        self.assertEqual([item.claim_id for item in ledger.claims], ["claim-H1"])
        invented = next(item for item in report.results if item.claim_id == "claim-invented")
        self.assertEqual(invented.admission_status, "rejected")

        bundle = _bundle()
        candidate = bundle["candidate"]
        assert isinstance(candidate, ClaimLedger)
        candidate.claims[0].required_check_ids = ["check-does-not-exist"]
        ledger, _ = _gate(bundle)
        self.assertEqual(ledger.claims[0].admission_status, "rejected")
        self.assertTrue(
            any(
                "unknown Check ids" in reason
                for reason in ledger.claims[0].gate_reasons
            )
        )

        bundle = _bundle()
        registry = bundle["registry"]
        assert hasattr(registry, "evidence")
        registry.evidence.append(  # type: ignore[attr-defined]
            EvidenceObject(
                evidence_id="evidence-unknown-check",
                claim_id="claim-H1",
                check_id="check-does-not-exist",
                execution_id="execution-does-not-exist",
                source_kind="execution",
                status="supporting",
                reason="malicious dangling reference",
            )
        )
        ledger, _ = _gate(bundle)
        self.assertEqual(ledger.claims[0].admission_status, "prohibited")

    def test_malicious_causal_text_is_rejected_but_disclaimer_is_allowed(self) -> None:
        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        bundle["candidate"] = _candidate(run.research_run_id, text="x 促进了 y。")
        ledger, _ = _gate(bundle)
        self.assertEqual(ledger.claims[0].admission_status, "rejected")

        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        bundle["candidate"] = _candidate(
            run.research_run_id,
            text="x 与 y 存在关联，不支持因果解释。",
        )
        ledger, _ = _gate(bundle)
        self.assertEqual(ledger.claims[0].admission_status, "admitted")

    def test_causal_wording_guard_distinguishes_disclaimers_and_zero_effect_claims(self) -> None:
        self.assertEqual(
            causal_wording_violations("该结果不支持因果解释。", "associational"),
            [],
        )
        self.assertEqual(
            causal_wording_violations("未发现 x 促进 y 的证据。", "associational"),
            [],
        )
        self.assertEqual(
            causal_wording_violations(
                "该关联不能确认为政策的因果效应。",
                "mixed",
            ),
            [],
        )
        self.assertEqual(
            causal_wording_violations(
                "政策实施与污染强度的降低存在统计关联，"
                "但不支持因果解释。",
                "mixed",
            ),
            [],
        )
        self.assertIn("导致", causal_wording_violations("x 不导致 y。", "associational"))
        self.assertIn("影响", causal_wording_violations("x 影响 y。", "mixed"))
        for text in (
            "ESG提高融资效率。",
            "ESG提升企业绩效。",
            "ESG有助于降低融资成本。",
            "ESG促使企业调整债务。",
            "ESG使融资成本下降。",
            "ESG推动治理改善。",
            "ESG加剧融资约束。",
            "ESG削弱融资约束。",
            "ESG改变债务结构。",
            "ESG带来更低成本。",
        ):
            with self.subTest(text=text):
                self.assertTrue(causal_wording_violations(text, "associational"))
        self.assertEqual(
            causal_wording_violations(
                "未发现 ESG 提高融资效率的证据。",
                "associational",
            ),
            [],
        )

    def test_causal_wording_guard_handles_english_denials_without_hiding_assertions(self) -> None:
        safe_denials = (
            "The estimate likely reflects pre-existing trends rather than "
            "the causal effect of the policy.",
            "The estimate cannot be confirmed as a causal effect.",
            "The estimate could not be established as the causal effect.",
            (
                "Baseline estimates suggest a negative association, but causal "
                "interpretation is severely limited and likely captures pre-existing "
                "differential trends rather than the causal effect of the policy."
            ),
        )
        for text in safe_denials:
            with self.subTest(text=text):
                self.assertEqual(causal_wording_violations(text, "mixed"), [])

        unsafe_zero_or_positive_claims = (
            "The policy has no causal effect on emissions.",
            "There is no causal effect of the policy.",
            "The policy does not have a causal effect.",
            "The causal effect is zero.",
            "The policy has a causal effect.",
            "The policy caused lower emissions.",
        )
        for text in unsafe_zero_or_positive_claims:
            with self.subTest(text=text):
                self.assertTrue(causal_wording_violations(text, "mixed"))

        combined = (
            "It cannot be confirmed as a causal effect; however, "
            "the policy caused lower emissions."
        )
        self.assertIn("caused", causal_wording_violations(combined, "mixed"))

    def test_causal_wording_guard_detects_high_confidence_english_policy_assertions(self) -> None:
        causal_assertions = (
            "The policy reduced emissions.",
            "The policy significantly reduced emissions.",
            "However, the treatment lowered pollution.",
            "The policy did not reduce emissions.",
            "The evidence supports a causal interpretation.",
            "The results strongly support a causal interpretation.",
            "This supports a causal interpretation.",
        )
        for text in causal_assertions:
            with self.subTest(text=text):
                self.assertTrue(causal_wording_violations(text, "associational"))

        noncausal_descriptions = (
            "Emissions decreased.",
            "Emissions under the policy decreased sharply.",
            "Under the policy, emissions decreased.",
            "The policy is associated with reduced emissions.",
            "The policy reduced-form estimate is negative.",
            "The policy reduced form estimate is negative.",
            "The policy was designed to reduce emissions.",
            "The evidence does not support a causal interpretation.",
            "No evidence supports a causal interpretation.",
            "Nothing in the evidence supports a causal interpretation.",
        )
        for text in noncausal_descriptions:
            with self.subTest(text=text):
                self.assertEqual(
                    causal_wording_violations(text, "associational"),
                    [],
                )

        combined = (
            "The evidence does not support a causal interpretation.\n"
            "However, the policy reduced emissions."
        )
        self.assertTrue(causal_wording_violations(combined, "associational"))

    def test_identifiers_with_digits_are_not_mistaken_for_statistical_values(self) -> None:
        bundle = _bundle()
        run = bundle["run"]
        assert isinstance(run, ResearchRun)
        bundle["candidate"] = _candidate(
            run.research_run_id,
            text="ABSDA1 交互边界与 CO2 构念存在初步关联。",
        )
        ledger, _ = _gate(bundle)
        self.assertEqual(ledger.claims[0].admission_status, "admitted")
        validate_h3_claim_decision(
            ledger.claims[0],
            "approve",
            "ABSDA1 交互边界与 CO2 构念存在初步关联。",
        )

        with self.assertRaisesRegex(ClaimGateError, "unprotected numeric"):
            validate_h3_claim_decision(
                ledger.claims[0],
                "approve",
                "系数为 0.12，样本量为 100。",
            )

    def test_unknown_reviewer_threat_placeholder_caps_at_preliminary(self) -> None:
        bundle = _bundle()
        package = bundle["package"]
        plan = bundle["plan"]
        contract = bundle["contract"]
        run = bundle["run"]
        candidate = bundle["candidate"]
        assert isinstance(package, ResearchPackage)
        assert isinstance(plan, AnalysisPlan)
        assert isinstance(contract, FormalResearchContract)
        assert isinstance(run, ResearchRun)
        assert isinstance(candidate, ClaimLedger)
        issue = CriticIssue(
            issue_id="issue-unknown-threat",
            dimension="reproducibility",
            severity="major",
            evidence="structured but unregistered threat",
            why_it_matters="must remain visible",
            required_fix="do not parse this prose",
            return_stage="analysis_plan",
            repair_type="scientific",
            threat_id="panel.future_unknown_threat",
        )
        plan = compile_enterprise_panel_test_dag(
            plan,
            package.hypotheses,
            [issue],
        )
        placeholder = next(
            item
            for item in plan.robustness_tests
            if item.threat_id == "panel.future_unknown_threat"
        )
        self.assertTrue(placeholder.required_for_admission)
        self.assertIsNotNone(placeholder.not_executable_reason)
        plan_hash = canonical_sha256(plan.model_dump(mode="json"))
        contract.approved_plan = plan
        contract.approved_plan_hash = plan_hash
        run.contract_hash = plan_hash
        run.executions = finalize_test_dag_executions(plan, run.executions)
        provenance_hash = canonical_sha256(contract.model_dump(mode="json"))
        for execution in run.executions:
            if execution.execution_status == "succeeded" and execution.provenance:
                execution.provenance.contract_sha256 = provenance_hash
        bundle["plan"] = plan
        bundle["contract"] = contract
        bundle["run"] = run
        bundle["registry"] = build_evidence_registry(
            plan,
            run,
            candidate.claims,
            reproduction_audit=bundle["audit"],  # type: ignore[arg-type]
            scientific_audit=bundle["scientific"],  # type: ignore[arg-type]
        )

        ledger, _ = _gate(bundle)

        self.assertEqual(ledger.claims[0].admission_status, "downgrade_required")
        self.assertEqual(ledger.claims[0].max_allowed_strength, "preliminary")

    def test_h3_action_matrix_and_calibration_are_enforced(self) -> None:
        ledger, _ = _gate(_bundle())
        admitted = ledger.claims[0]
        self.assertEqual(
            permitted_h3_decisions(admitted),
            ("approve", "downgrade", "reject", "hold"),
        )
        validate_h3_claim_decision(admitted, "approve", "x 与 y 存在关联。")
        with self.assertRaisesRegex(ClaimGateError, "causal wording"):
            validate_h3_claim_decision(admitted, "approve", "x 影响 y。")
        with self.assertRaisesRegex(ClaimGateError, "unprotected numeric"):
            validate_h3_claim_decision(
                admitted,
                "approve",
                "x 与 y 存在关联，系数为 9999。",
            )

        mixed = admitted.model_copy(
            update={
                "admission_status": "downgrade_required",
                "allowed_strength": "mixed",
                "max_allowed_strength": "mixed",
            }
        )
        self.assertEqual(
            permitted_h3_decisions(mixed),
            ("downgrade", "reject", "hold"),
        )
        with self.assertRaises(ClaimGateError):
            validate_h3_claim_decision(mixed, "approve", "x 与 y 相关。")
        with self.assertRaisesRegex(ClaimGateError, "conflicting evidence"):
            validate_h3_claim_decision(mixed, "downgrade", "x 与 y 相关。")
        validate_h3_claim_decision(
            mixed,
            "downgrade",
            "证据混合：基准相关但前导检验不一致。",
        )

        preliminary = mixed.model_copy(
            update={
                "allowed_strength": "preliminary",
                "max_allowed_strength": "preliminary",
            }
        )
        with self.assertRaisesRegex(ClaimGateError, "calibration"):
            validate_h3_claim_decision(
                preliminary,
                "downgrade",
                "x 与 y 存在关联。",
            )
        validate_h3_claim_decision(
            preliminary,
            "downgrade",
            "初步证据表明 x 与 y 存在有限关联。",
        )

        prohibited = admitted.model_copy(
            update={
                "admission_status": "prohibited",
                "allowed_strength": "prohibited",
                "max_allowed_strength": "prohibited",
            }
        )
        self.assertEqual(permitted_h3_decisions(prohibited), ("reject", "hold"))


if __name__ == "__main__":
    unittest.main()
