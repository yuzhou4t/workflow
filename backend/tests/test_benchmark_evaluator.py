from __future__ import annotations

import unittest
from types import SimpleNamespace

from hypoweaver.benchmark_evaluator import (
    evaluate_hard_metrics,
    protected_numeric_consistency,
    seal_benchmark_packet,
    summarize_paired_reviews,
    verify_benchmark_packet,
)
from hypoweaver.benchmark_faults import replay_ablations
from hypoweaver.benchmark_models import (
    BenchmarkPacket,
    BenchmarkReference,
    FaultOutcome,
    NeurIPSRatings,
    NeurIPSReview,
    NormalizedClaim,
    NormalizedDesign,
    NormalizedExecution,
    NormalizedReproduction,
    NormalizedStatement,
)
from hypoweaver.benchmark_packets import build_agent_laboratory_packet
from hypoweaver.benchmark_packets import build_hypoweaver_packet
from hypoweaver.models import (
    ExecutionProvenance,
    ExecutionRecord,
    ManuscriptSection,
    ManuscriptStatement,
    ProtectedValue,
    ResearchRun,
)


FAULT_IDS = (
    "duplicate_merge_inflation",
    "time_leakage_or_lead_misuse",
    "unit_amplification",
    "variable_timing_shift",
    "wrong_clustering",
    "significant_subgroup_cherry_pick",
    "table_text_mismatch",
    "association_to_causation",
    "deleted_null_or_failure_branch",
)


class BenchmarkEvaluatorTests(unittest.TestCase):
    def test_all_hard_metrics_pass_for_traceable_independently_reproduced_packet(self) -> None:
        packet = seal_benchmark_packet(_packet())
        report = evaluate_hard_metrics(
            packet,
            _reference(),
            fault_outcomes=[
                FaultOutcome(
                    fault_id=fault_id,
                    detected=True,
                    action="block",
                    evidence=[fault_id],
                )
                for fault_id in FAULT_IDS
            ],
        )

        self.assertTrue(report.all_hard_gates_passed)
        self.assertTrue(all(metric.passed for metric in report.metrics))

    def test_missing_reference_contract_hash_uses_packet_internal_contract_consistency(self) -> None:
        report = evaluate_hard_metrics(
            seal_benchmark_packet(_packet()),
            _reference().model_copy(update={"expected_contract_sha256": None}),
            fault_outcomes=_detected_faults(),
        )

        metric = next(
            item
            for item in report.metrics
            if item.metric_id == "contract_execution_fidelity"
        )
        self.assertTrue(metric.passed)
        self.assertEqual(metric.evidence, [])

    def test_required_terminal_steps_are_derived_from_packet_when_reference_is_empty(self) -> None:
        report = evaluate_hard_metrics(
            seal_benchmark_packet(_packet()),
            _reference().model_copy(update={"required_check_ids": []}),
            fault_outcomes=_detected_faults(),
        )

        metric = next(
            item
            for item in report.metrics
            if item.metric_id == "required_step_terminal_rate"
        )
        self.assertTrue(metric.passed)
        self.assertEqual(metric.numerator, 1)
        self.assertEqual(metric.denominator, 1)

    def test_terminal_failure_does_not_count_as_required_evidence_completion(self) -> None:
        base = _packet()
        for status, expected_evidence in (
            ("failed", "check-baseline:failed"),
            ("succeeded", "check-baseline:succeeded_without_evidence"),
        ):
            with self.subTest(status=status):
                execution = base.executions[0].model_copy(
                    update={
                        "execution_status": status,
                        "estimates": [],
                        "diagnostics": {},
                    }
                )
                packet = seal_benchmark_packet(
                    base.model_copy(
                        update={
                            "executions": [execution],
                            "packet_sha256": None,
                        }
                    )
                )
                report = evaluate_hard_metrics(
                    packet,
                    _reference(),
                    fault_outcomes=_detected_faults(),
                )
                by_id = {metric.metric_id: metric for metric in report.metrics}

                self.assertTrue(by_id["required_step_terminal_rate"].passed)
                self.assertFalse(by_id["required_evidence_completion"].passed)
                self.assertEqual(
                    by_id["required_evidence_completion"].evidence,
                    [expected_evidence],
                )

    def test_replication_scope_is_derived_from_successful_estimative_executions(self) -> None:
        reference = _reference().model_copy(
            update={"independently_reproducible_check_ids": []}
        )
        complete_report = evaluate_hard_metrics(
            seal_benchmark_packet(_packet()),
            reference,
            fault_outcomes=_detected_faults(),
        )
        complete_metric = next(
            item
            for item in complete_report.metrics
            if item.metric_id == "independent_replication_rate"
        )
        self.assertTrue(complete_metric.passed)
        self.assertEqual(complete_metric.numerator, 1)
        self.assertEqual(complete_metric.denominator, 1)

        base = _packet()
        uncovered = seal_benchmark_packet(
            base.model_copy(
                update={
                    "reproduction": base.reproduction.model_copy(
                        update={"covered_check_ids": []}
                    ),
                    "packet_sha256": None,
                }
            )
        )
        uncovered_report = evaluate_hard_metrics(
            uncovered,
            reference,
            fault_outcomes=_detected_faults(),
        )
        uncovered_metric = next(
            item
            for item in uncovered_report.metrics
            if item.metric_id == "independent_replication_rate"
        )
        self.assertFalse(uncovered_metric.passed)
        self.assertEqual(uncovered_metric.denominator, 1)
        self.assertEqual(uncovered_metric.evidence, ["check-baseline"])

    def test_missing_required_threat_fails_contract_fidelity(self) -> None:
        base = _packet()
        packet = seal_benchmark_packet(
            base.model_copy(
                update={
                    "design": base.design.model_copy(
                        update={"check_threat_ids": {}}
                    ),
                    "packet_sha256": None,
                }
            )
        )
        report = evaluate_hard_metrics(
            packet,
            _reference(),
            fault_outcomes=_detected_faults(),
        )

        metric = next(
            item
            for item in report.metrics
            if item.metric_id == "contract_execution_fidelity"
        )
        self.assertFalse(metric.passed)
        self.assertIn("design.required_threat_ids_present", metric.evidence)

    def test_tamper_and_overreach_fail_closed(self) -> None:
        packet = seal_benchmark_packet(_packet())
        tampered = packet.model_copy(update={"manuscript_text": "被修改"})
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            verify_benchmark_packet(tampered)

        unsafe_claim = packet.claims[0].model_copy(
            update={"text": "ESG 导致短债长用下降。"}
        )
        unsafe = seal_benchmark_packet(
            packet.model_copy(
                update={"claims": [unsafe_claim], "packet_sha256": None}
            )
        )
        report = evaluate_hard_metrics(
            unsafe,
            _reference(),
            fault_outcomes=[],
            clean_false_block_count=1,
        )

        by_id = {metric.metric_id: metric for metric in report.metrics}
        self.assertFalse(by_id["causal_overreach_escape_count"].passed)
        self.assertFalse(by_id["fatal_fault_detection_rate"].passed)
        self.assertFalse(by_id["clean_false_block_count"].passed)

        unsafe_body = seal_benchmark_packet(
            packet.model_copy(
                update={
                    "manuscript_text": (
                        packet.manuscript_text + "\n\nESG推动企业改善期限结构。"
                    ),
                    "manuscript_section_texts": {
                        "conclusion": (
                            packet.manuscript_text
                            + "\n\nESG推动企业改善期限结构。"
                        )
                    },
                    "packet_sha256": None,
                }
            )
        )
        body_report = evaluate_hard_metrics(
            unsafe_body,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        body_metric = next(
            item
            for item in body_report.metrics
            if item.metric_id == "causal_overreach_escape_count"
        )
        self.assertFalse(body_metric.passed)
        self.assertIn("manuscript:conclusion:推动", body_metric.evidence)

    def test_rejected_and_prohibited_claims_are_audit_records_not_escapes(self) -> None:
        base = _packet()
        audit_claims = [
            NormalizedClaim(
                claim_id="claim-rejected",
                text="ESG 导致 SDLA 下降。",
                strength="prohibited",
                admission_status="rejected",
                gate_reasons=["causal wording prohibited"],
            ),
            NormalizedClaim(
                claim_id="claim-prohibited",
                text="ESG 抑制 SDLA。",
                strength="prohibited",
                admission_status="prohibited",
                gate_reasons=["not tested"],
            ),
        ]
        packet = seal_benchmark_packet(
            base.model_copy(
                update={
                    "claims": [*base.claims, *audit_claims],
                    "packet_sha256": None,
                }
            )
        )

        report = evaluate_hard_metrics(
            packet,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        metric = next(
            item
            for item in report.metrics
            if item.metric_id == "causal_overreach_escape_count"
        )
        self.assertTrue(metric.passed)
        self.assertEqual(metric.numerator, 0)
        self.assertEqual(packet.claims[-2].gate_reasons, ["causal wording prohibited"])

    def test_unassessed_causal_claim_still_counts_as_escape(self) -> None:
        base = _packet()
        packet = seal_benchmark_packet(
            base.model_copy(
                update={
                    "claims": [
                        *base.claims,
                        NormalizedClaim(
                            claim_id="claim-unassessed",
                            text="ESG 导致 SDLA 下降。",
                            strength="associational",
                            admission_status="unassessed",
                        ),
                    ],
                    "packet_sha256": None,
                }
            )
        )

        report = evaluate_hard_metrics(
            packet,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        metric = next(
            item
            for item in report.metrics
            if item.metric_id == "causal_overreach_escape_count"
        )
        self.assertFalse(metric.passed)
        self.assertIn("claim-unassessed", metric.evidence)

    def test_rejected_claim_cannot_authorize_manuscript_statement(self) -> None:
        base = _packet()
        rejected = NormalizedClaim(
            claim_id="claim-rejected",
            text="ESG 导致 SDLA 下降。",
            strength="prohibited",
            admission_status="rejected",
        )
        statement = base.statements[0].model_copy(
            update={"claim_ids": [rejected.claim_id]}
        )
        packet = seal_benchmark_packet(
            base.model_copy(
                update={
                    "claims": [*base.claims, rejected],
                    "statements": [statement, *base.statements[1:]],
                    "packet_sha256": None,
                }
            )
        )

        report = evaluate_hard_metrics(
            packet,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        metric = next(
            item
            for item in report.metrics
            if item.metric_id == "statement_traceability"
        )
        self.assertFalse(metric.passed)
        self.assertIn("empirical_results:statement-claim", metric.evidence)

    def test_contract_fidelity_rejects_unknown_duplicate_settings_and_provenance(self) -> None:
        base = _packet()
        execution = base.executions[0]
        mutations = {
            "unknown_check": [execution.model_copy(update={"check_id": "check-unknown"})],
            "duplicate_check": [
                execution,
                execution.model_copy(update={"execution_id": "exec-duplicate"}),
            ],
            "fixed_effects": [execution.model_copy(update={"fixed_effects": ["firm"]})],
            "clustering": [
                execution.model_copy(update={"standard_error_strategy": "unclustered"})
            ],
            "missing_contract_hash": [
                execution.model_copy(update={"contract_sha256": None})
            ],
            "wrong_contract_hash": [
                execution.model_copy(update={"contract_sha256": "d" * 64})
            ],
            "missing_data_hash": [execution.model_copy(update={"data_sha256": []})],
            "wrong_data_hash": [
                execution.model_copy(update={"data_sha256": ["e" * 64]})
            ],
        }

        for label, executions in mutations.items():
            with self.subTest(label=label):
                packet = seal_benchmark_packet(
                    base.model_copy(
                        update={"executions": executions, "packet_sha256": None}
                    )
                )
                report = evaluate_hard_metrics(
                    packet,
                    _reference(),
                    fault_outcomes=_detected_faults(),
                )
                metric = next(
                    item
                    for item in report.metrics
                    if item.metric_id == "contract_execution_fidelity"
                )
                self.assertFalse(metric.passed)
                self.assertTrue(metric.evidence)

        missing_reference_hash = evaluate_hard_metrics(
            seal_benchmark_packet(base),
            _reference().model_copy(update={"expected_contract_sha256": None}),
            fault_outcomes=_detected_faults(),
        )
        missing_hash_metric = next(
            item
            for item in missing_reference_hash.metrics
            if item.metric_id == "contract_execution_fidelity"
        )
        self.assertTrue(missing_hash_metric.passed)

        missing_optional_packet = seal_benchmark_packet(
            base.model_copy(
                update={
                    "design": base.design.model_copy(
                        update={
                            "planned_check_ids": [
                                "check-baseline",
                                "check-optional",
                            ]
                        }
                    ),
                    "packet_sha256": None,
                }
            )
        )
        missing_optional_report = evaluate_hard_metrics(
            missing_optional_packet,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        missing_optional_metric = next(
            item
            for item in missing_optional_report.metrics
            if item.metric_id == "contract_execution_fidelity"
        )
        self.assertFalse(missing_optional_metric.passed)
        self.assertIn(
            "execution.check_ids_complete",
            missing_optional_metric.evidence,
        )

    def test_manuscript_occurrences_are_bound_to_rendered_protected_values(self) -> None:
        base = _packet()
        changed_body = seal_benchmark_packet(
            base.model_copy(
                update={
                    "manuscript_text": base.manuscript_text.replace(
                        "-0.1512", "9.9999"
                    ),
                    "packet_sha256": None,
                }
            )
        )
        changed_report = evaluate_hard_metrics(
            changed_body,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        changed_metrics = {item.metric_id: item for item in changed_report.metrics}
        self.assertFalse(changed_metrics["protected_numeric_consistency"].passed)
        self.assertFalse(changed_metrics["statement_traceability"].passed)

        deleted_registry = seal_benchmark_packet(
            base.model_copy(update={"statements": [], "packet_sha256": None})
        )
        deleted_report = evaluate_hard_metrics(
            deleted_registry,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        deleted_metrics = {item.metric_id: item for item in deleted_report.metrics}
        self.assertFalse(deleted_metrics["protected_numeric_consistency"].passed)
        self.assertFalse(deleted_metrics["statement_traceability"].passed)
        self.assertIn(
            "manuscript:unprotected_numeric_text",
            deleted_metrics["statement_traceability"].evidence,
        )

        claim_registry_deleted = seal_benchmark_packet(
            base.model_copy(
                update={"statements": base.statements[1:], "packet_sha256": None}
            )
        )
        claim_deleted_report = evaluate_hard_metrics(
            claim_registry_deleted,
            _reference(),
            fault_outcomes=_detected_faults(),
        )
        claim_traceability = next(
            item
            for item in claim_deleted_report.metrics
            if item.metric_id == "statement_traceability"
        )
        self.assertFalse(claim_traceability.passed)
        self.assertIn(
            "manuscript:untracked_authorized_claim",
            claim_traceability.evidence,
        )

    def test_five_paired_reviews_are_aggregated_per_anonymous_label(self) -> None:
        reviews = [
            NeurIPSReview(
                review_id=f"review-{index}",
                sample_index=index,
                label_order="A_B" if index % 2 else "B_A",
                ratings_a=_ratings(overall=8, soundness=4),
                ratings_b=_ratings(overall=6, soundness=3),
                preferred_label="A",
                diagnosis=["A 的证据追踪更完整。"],
            )
            for index in range(1, 6)
        ]

        summary = summarize_paired_reviews("case-1", "packet-a", "packet-b", reviews)

        self.assertEqual(summary.median_scores["A"]["overall"], 8)
        self.assertEqual(summary.median_scores["B"]["overall"], 6)
        self.assertEqual(summary.preference_counts, {"A": 5, "B": 0, "tie": 0})
        self.assertTrue(summary.model_only)

    def test_paired_reviews_are_unblinded_to_packet_identity_before_aggregation(self) -> None:
        reviews = []
        for index in range(1, 6):
            assignment = "A_B" if index % 2 else "B_A"
            reviews.append(
                NeurIPSReview(
                    review_id=f"mapped-{index}",
                    sample_index=index,
                    label_order="A_B" if index % 2 else "B_A",
                    system_assignment=assignment,
                    ratings_a=(
                        _ratings(overall=8, soundness=4)
                        if assignment == "A_B"
                        else _ratings(overall=6, soundness=3)
                    ),
                    ratings_b=(
                        _ratings(overall=6, soundness=3)
                        if assignment == "A_B"
                        else _ratings(overall=8, soundness=4)
                    ),
                    preferred_label="A" if assignment == "A_B" else "B",
                )
            )

        summary = summarize_paired_reviews(
            "case-1", "packet-a", "packet-b", reviews
        )

        self.assertEqual(summary.median_scores["A"]["overall"], 8)
        self.assertEqual(summary.median_scores["B"]["overall"], 6)
        self.assertEqual(summary.preference_counts, {"A": 5, "B": 0, "tie": 0})

    def test_agent_laboratory_adapter_preserves_absent_traceability(self) -> None:
        packet = build_agent_laboratory_packet(
            packet_id="packet-agent-lab",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            report_text="报告正文",
            output={
                "case_id": "case-1",
                "model": {"name": "qwen3.7-plus"},
                "analysis_plan": {
                    "method_family": "panel FE",
                    "variables": {
                        "outcome": ["SDLA"],
                        "treatment_or_exposure": ["ESG"],
                        "controls": [],
                    },
                    "fixed_effects": ["firm", "year"],
                    "standard_errors": "cluster by firm",
                    "required_diagnostics": ["missingness"],
                    "robustness_checks": [],
                    "falsification_tests": ["lead"],
                },
                "research_run": {
                    "execution_status": "success",
                    "parsed_result": {
                        "execution_status": "success",
                        "models": {"baseline_H1": {"coef": -0.15}},
                        "diagnostics": {"n_obs": 100},
                    }
                },
                "result_interpretation": {
                    "main_findings": ["ESG 与 SDLA 呈负相关。"],
                    "allowed_claim_strength": "associational",
                },
                "execution_cost": {"llm_calls": 6},
                "manuscript": {"sha256": "c" * 64},
            },
        )

        verify_benchmark_packet(packet)
        self.assertEqual(packet.system_id, "agent_laboratory")
        self.assertEqual(packet.executions[0].execution_status, "succeeded")
        self.assertEqual(packet.claims[0].admission_status, "unassessed")
        self.assertEqual(packet.statements, [])
        self.assertEqual(packet.reproduction.status, "not_available")

    def test_agent_laboratory_adapter_preserves_failed_terminal_branches(self) -> None:
        failed_with_stale_model = build_agent_laboratory_packet(
            packet_id="packet-agent-lab-failed",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            report_text="失败报告",
            output={
                "case_id": "case-1",
                "model": "qwen-test",
                "analysis_plan": {},
                "research_run": {
                    "execution_status": "failed",
                    "parsed_result": {
                        "execution_status": "success",
                        "models": {"baseline_H1": {"coef": -0.15}},
                    },
                },
            },
        )
        self.assertEqual(len(failed_with_stale_model.executions), 1)
        self.assertEqual(
            failed_with_stale_model.executions[0].execution_status,
            "failed",
        )
        self.assertEqual(failed_with_stale_model.executions[0].estimates, [])

        failed_without_models = build_agent_laboratory_packet(
            packet_id="packet-agent-lab-no-models",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            report_text="失败报告",
            output={
                "case_id": "case-1",
                "model": "qwen-test",
                "analysis_plan": {},
                "research_run": {"execution_status": "timeout"},
            },
        )
        self.assertEqual(len(failed_without_models.executions), 1)
        self.assertEqual(
            failed_without_models.executions[0].check_id,
            "workflow",
        )
        self.assertEqual(
            failed_without_models.executions[0].execution_status,
            "failed",
        )

    def test_hypoweaver_adapter_emits_rendered_section_occurrences(self) -> None:
        baseline = SimpleNamespace(
            step_id="check-baseline",
            required_for_admission=True,
            outcome="SDLA",
            treatments_or_exposures=["ESG"],
            controls=[],
            fixed_effects=["firm", "year"],
            standard_error_strategy="clustered_by_entity",
        )
        plan = _DumpingNamespace(
            method_family="panel_association",
            baseline_models=[baseline],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
        )
        execution = ExecutionRecord(
            execution_id="exec-baseline",
            check_id="check-baseline",
            plan_step_id="check-baseline",
            execution_status="succeeded",
            run_type="baseline",
            estimates=[
                {
                    "term": "ESG",
                    "coefficient": -0.1512,
                    "standard_error": 0.0336,
                }
            ],
            diagnostic_results={
                "entity_fixed_effects": True,
                "time_fixed_effects": True,
                "standard_errors": "clustered_by_entity",
                "rows_used": 100,
            },
            provenance=ExecutionProvenance(
                implementation_id="linearmodels-panelols-v1",
                implementation_version="1.0",
                code_sha256="1" * 64,
                environment_sha256="2" * 64,
                contract_sha256="c" * 64,
                data_sha256=["b" * 64],
            ),
        )
        run = ResearchRun(
            research_run_id="run-1",
            case_id="case-1",
            contract_hash="contract-1",
            plan_version=1,
            execution_status="succeeded",
            scientific_status="pending_review",
            fixture_only=False,
            executions=[execution],
        )
        value = ProtectedValue(
            value_id="value-1",
            value_kind="coefficient",
            source_kind="execution",
            source_id="exec-baseline",
            source_path="/executions/0/estimates/0/coefficient",
            raw_value=-0.1512,
            rendered_value="-0.1512",
        )
        statement = ManuscriptStatement(
            statement_id="statement-1",
            statement_kind="estimate_fact",
            text_template="基准系数为 [[VALUE:value-1]]。",
            protected_values=[value],
            execution_ids=["exec-baseline"],
        )
        sample_value = ProtectedValue(
            value_id="value-sample",
            value_kind="count",
            source_kind="execution",
            source_id="exec-baseline",
            source_path="/executions/0/diagnostic_results/rows_used",
            raw_value=100,
            rendered_value="100",
        )
        sample_statement = ManuscriptStatement(
            statement_id="statement-sample",
            statement_kind="sample_fact",
            text_template="有效样本量为 [[VALUE:value-sample]]。",
            protected_values=[sample_value],
            execution_ids=["exec-baseline"],
        )
        manuscript = _DumpingNamespace(
            manuscript_sections=[
                ManuscriptSection(
                    section_id="empirical_results",
                    title="结果",
                    content_markdown="基准系数为 -0.1512。\n\n有效样本量为 100。",
                    status="generated",
                    statements=[statement, sample_statement],
                )
            ]
        )

        packet = build_hypoweaver_packet(
            packet_id="packet-rendered",
            case_id="case-1",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            model_id="qwen-test",
            plan=plan,
            research_run=run,
            claim_ledger=_DumpingNamespace(
                claims=[
                    _DumpingNamespace(
                        claim_id="claim-tightened",
                        claim_text="ESG 与 SDLA 存在初步关联。",
                        final_text=None,
                        allowed_strength="preliminary",
                        max_allowed_strength="associational",
                        admission_status="downgrade_required",
                        required_check_ids=["check-baseline"],
                        supporting_runs=["exec-baseline"],
                        opposing_runs=[],
                        gate_reasons=["人工进一步收紧"],
                    )
                ]
            ),
            manuscript=manuscript,
            reproduction_audit=SimpleNamespace(
                mode="independent_implementation",
                status="matched",
                covered_plan_step_ids=["check-baseline"],
                primary_implementation_id="linearmodels-panelols-v1",
                replication_implementation_id="numpy-two-way-within-v1",
                independence_scope="estimator_only",
                shared_components=["analysis_table"],
            ),
            component_artifact_sha256={
                "candidate_design_set": "3" * 64,
                "design_arena": "4" * 64,
                "formal_research_contract": "c" * 64,
                "reproduction_audit": "6" * 64,
                "claim_gate_report": "7" * 64,
                "manuscript_statement_registry": "8" * 64,
            },
        )

        self.assertEqual(packet.statements[0].text, "基准系数为 -0.1512。")
        self.assertEqual(packet.statements[0].section_id, "empirical_results")
        self.assertEqual(
            packet.statements[0].protected_values[0]["source_path"],
            "/estimates/0/coefficient",
        )
        self.assertEqual(
            packet.statements[1].protected_values[0]["source_path"],
            "/diagnostics/rows_used",
        )
        self.assertEqual(protected_numeric_consistency(packet)[:2], (2, 2))
        self.assertEqual(packet.executions[0].fixed_effects, ["firm", "year"])
        self.assertEqual(packet.executions[0].contract_sha256, "c" * 64)
        self.assertEqual(packet.executions[0].implementation_version, "1.0")
        self.assertEqual(packet.executions[0].code_sha256, "1" * 64)
        self.assertEqual(packet.executions[0].environment_sha256, "2" * 64)
        self.assertEqual(packet.claims[0].strength, "preliminary")
        self.assertEqual(packet.reproduction.independence_scope, "estimator_only")
        self.assertEqual(packet.reproduction.shared_components, ["analysis_table"])
        replay = replay_ablations(packet)
        self.assertEqual(sum(item.detected for item in replay.full_system_outcomes), 9)
        self.assertTrue(all(item.target_fault_degraded for item in replay.ablations))

    def test_packet_adapters_canonicalize_explicit_entity_clustering(self) -> None:
        baseline = SimpleNamespace(
            step_id="check-baseline",
            required_for_admission=True,
            outcome="SDLA",
            treatments_or_exposures=["ESG"],
            controls=[],
            fixed_effects=["S", "YEAR"],
            standard_error_strategy="cluster(S)",
        )
        plan = _DumpingNamespace(
            method_family="panel_association",
            baseline_models=[baseline],
            diagnostics=[],
            robustness_tests=[],
            falsification_tests=[],
            mechanism_tests=[],
            heterogeneity_tests=[],
        )
        execution = ExecutionRecord(
            execution_id="exec-baseline",
            check_id="check-baseline",
            plan_step_id="check-baseline",
            execution_status="succeeded",
            run_type="baseline",
            estimates=[],
            diagnostic_results={"standard_errors": "cluster(S)"},
        )
        packet = build_hypoweaver_packet(
            packet_id="packet-cluster",
            case_id="case-1",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            model_id="qwen-test",
            plan=plan,
            research_run=ResearchRun(
                research_run_id="run-cluster",
                case_id="case-1",
                contract_hash="plan",
                plan_version=1,
                execution_status="succeeded",
                scientific_status="limited",
                fixture_only=False,
                executions=[execution],
            ),
            claim_ledger=_DumpingNamespace(claims=[]),
            manuscript=_DumpingNamespace(manuscript_sections=[]),
            reproduction_audit=SimpleNamespace(
                mode="independent_implementation",
                status="matched",
                covered_plan_step_ids=[],
                primary_implementation_id="primary",
                replication_implementation_id="replica",
            ),
        )
        self.assertEqual(
            packet.design.standard_error_strategy,
            "clustered_by_entity",
        )
        self.assertEqual(
            packet.executions[0].standard_error_strategy,
            "clustered_by_entity",
        )

    def test_hypoweaver_packet_preserves_h3_rejected_claims_for_audit(self) -> None:
        packet = build_hypoweaver_packet(
            packet_id="packet-rejected-claim",
            case_id="case-1",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            model_id="qwen-test",
            plan=_DumpingNamespace(
                method_family="panel_association",
                baseline_models=[],
                diagnostics=[],
                robustness_tests=[],
                falsification_tests=[],
                mechanism_tests=[],
                heterogeneity_tests=[],
            ),
            research_run=ResearchRun(
                research_run_id="run-rejected-claim",
                case_id="case-1",
                contract_hash="plan",
                plan_version=1,
                execution_status="succeeded",
                scientific_status="limited",
                fixture_only=False,
                executions=[],
            ),
            claim_ledger=_DumpingNamespace(
                claims=[
                    _DumpingNamespace(
                        claim_id="claim-admitted",
                        claim_text="ESG 与 SDLA 存在初步关联。",
                        final_text="ESG 与 SDLA 存在初步关联。",
                        allowed_strength="preliminary",
                        admission_status="downgrade_required",
                        approval_status="downgraded",
                        required_check_ids=[],
                        supporting_runs=[],
                        opposing_runs=[],
                        gate_reasons=[],
                    ),
                    _DumpingNamespace(
                        claim_id="claim-rejected",
                        claim_text="ESG 抑制 SDLA。",
                        final_text=None,
                        allowed_strength="prohibited",
                        admission_status="rejected",
                        approval_status="rejected",
                        required_check_ids=[],
                        supporting_runs=[],
                        opposing_runs=[],
                        gate_reasons=["causal wording prohibited"],
                    ),
                ]
            ),
            manuscript=_DumpingNamespace(manuscript_sections=[]),
            reproduction_audit=SimpleNamespace(
                mode="independent_implementation",
                status="matched",
                covered_plan_step_ids=[],
                primary_implementation_id="primary",
                replication_implementation_id="replica",
            ),
        )

        self.assertEqual(
            [claim.claim_id for claim in packet.claims],
            ["claim-admitted", "claim-rejected"],
        )
        self.assertEqual(packet.claims[1].admission_status, "rejected")
        self.assertEqual(
            packet.claims[1].gate_reasons,
            ["causal wording prohibited"],
        )


def _ratings(*, overall: int, soundness: int) -> NeurIPSRatings:
    return NeurIPSRatings(
        quality=4,
        significance=3,
        clarity=4,
        soundness=soundness,
        presentation=4,
        contribution=3,
        overall=overall,
        confidence=4,
        recommendation="accept" if overall >= 6 else "reject",
    )


class _DumpingNamespace(SimpleNamespace):
    def model_dump(self, **_: object) -> dict[str, object]:
        def normalize(value: object) -> object:
            if isinstance(value, _DumpingNamespace):
                return value.model_dump()
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            if isinstance(value, SimpleNamespace):
                return {key: normalize(item) for key, item in vars(value).items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        return {key: normalize(value) for key, value in vars(self).items()}


def _packet() -> BenchmarkPacket:
    execution = NormalizedExecution(
        execution_id="exec-baseline",
        check_id="check-baseline",
        execution_status="succeeded",
        run_type="baseline",
        estimates=[{"term": "ESG", "coefficient": -0.1512, "standard_error": 0.0336}],
        implementation_id="linearmodels-panelols-v1",
        implementation_version="1.0",
        code_sha256="1" * 64,
        environment_sha256="2" * 64,
        fixed_effects=["firm", "year"],
        standard_error_strategy="clustered_by_entity",
        contract_sha256="c" * 64,
        data_sha256=["b" * 64],
        source_artifact_sha256="3" * 64,
    )
    return BenchmarkPacket(
        packet_id="packet-hypoweaver",
        system_id="hypoweaver",
        case_id="case-1",
        visible_input_sha256="a" * 64,
        data_sha256=["b" * 64],
        model_id="qwen3.7-plus",
        design=NormalizedDesign(
            method_family="panel_association",
            outcomes=["SDLA"],
            treatments_or_exposures=["ESG"],
            fixed_effects=["firm", "year"],
            standard_error_strategy="clustered_by_entity",
            planned_check_ids=["check-baseline"],
            required_check_ids=["check-baseline"],
            check_threat_ids={
                "check-baseline": "panel.fe_cluster_feasibility"
            },
            frozen_before_execution=True,
            source_artifact_sha256="f" * 64,
            contract_sha256="c" * 64,
        ),
        executions=[execution],
        claims=[
            NormalizedClaim(
                claim_id="claim-H1",
                text="ESG 与短债长用呈负向关联。",
                strength="associational",
                admission_status="admitted",
                check_ids=["check-baseline"],
                execution_ids=["exec-baseline"],
            )
        ],
        statements=[
            NormalizedStatement(
                statement_id="statement-claim",
                text="ESG 与短债长用呈负向关联。",
                statement_kind="authorized_claim",
                section_id="empirical_results",
                claim_ids=["claim-H1"],
            ),
            NormalizedStatement(
                statement_id="statement-1",
                text="基准系数为 -0.1512。",
                statement_kind="estimate_fact",
                section_id="empirical_results",
                execution_ids=["exec-baseline"],
                protected_values=[
                    {
                        "value_id": "value-1",
                        "value_kind": "coefficient",
                        "source_id": "exec-baseline",
                        "source_path": "/estimates/0/coefficient",
                        "raw_value": -0.1512,
                        "rendered_value": "-0.1512",
                    }
                ],
            )
        ],
        manuscript_text="ESG 与短债长用呈负向关联。\n\n基准系数为 -0.1512。",
        reproduction=NormalizedReproduction(
            mode="independent_implementation",
            status="matched",
            covered_check_ids=["check-baseline"],
            primary_implementation_id="linearmodels-panelols-v1",
            replication_implementation_id="numpy-two-way-within-v1",
        ),
        native_artifact_sha256={
            "formal_research_contract": "c" * 64,
        },
    )


def _reference() -> BenchmarkReference:
    return BenchmarkReference(
        case_id="case-1",
        visible_input_sha256="a" * 64,
        data_sha256=["b" * 64],
        expected_design={
            "method_family": "panel_association",
            "outcomes": ["SDLA"],
            "treatments_or_exposures": ["ESG"],
            "fixed_effects": ["firm", "year"],
            "standard_error_strategy": "clustered_by_entity",
            "frozen_before_execution": True,
        },
        expected_contract_sha256="c" * 64,
        required_check_ids=["check-baseline"],
        required_threat_ids=["panel.fe_cluster_feasibility"],
        independently_reproducible_check_ids=["check-baseline"],
        clean_packet_ids=["packet-clean"],
    )


def _detected_faults() -> list[FaultOutcome]:
    return [
        FaultOutcome(
            fault_id=fault_id,
            detected=True,
            action="block",
            evidence=[fault_id],
        )
        for fault_id in FAULT_IDS
    ]


if __name__ == "__main__":
    unittest.main()
