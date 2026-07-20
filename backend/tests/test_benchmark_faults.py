from __future__ import annotations

import unittest
from unittest.mock import patch

import hypoweaver.benchmark_faults as fault_module
from hypoweaver.benchmark_evaluator import (
    evaluate_hard_metrics,
    protected_numeric_consistency,
    seal_benchmark_packet,
    verify_benchmark_packet,
)
from hypoweaver.benchmark_faults import (
    derive_ablation_packet,
    detect_injected_fault,
    inject_fault,
    replay_ablations,
    replay_faults,
)
from hypoweaver.benchmark_models import (
    ABLATION_IDS,
    ABLATION_NATIVE_ARTIFACTS,
    FAULT_IDS,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    NormalizedClaim,
    NormalizedDesign,
    NormalizedExecution,
    NormalizedReproduction,
    NormalizedStatement,
)


class BenchmarkFaultReplayTests(unittest.TestCase):
    def test_fixed_nine_faults_are_real_packet_mutations_and_all_detected(self) -> None:
        clean = _packet()

        outcomes = replay_faults(clean)

        self.assertEqual([item.fault_id for item in outcomes], list(FAULT_IDS))
        self.assertTrue(all(item.detected for item in outcomes))
        for fault_id in FAULT_IDS:
            injected = inject_fault(clean, fault_id)
            verify_benchmark_packet(injected)
            self.assertNotEqual(injected.packet_sha256, clean.packet_sha256)
            self.assertEqual(injected.visible_input_sha256, clean.visible_input_sha256)
            self.assertEqual(injected.data_sha256, clean.data_sha256)

    def test_six_ablations_reuse_fixture_without_model_calls(self) -> None:
        report = replay_ablations(_packet())

        self.assertEqual(
            {item.ablation_id for item in report.ablations}, set(ABLATION_IDS)
        )
        self.assertEqual(report.clean_false_block_count, 0)
        self.assertEqual(sum(item.detected for item in report.full_system_outcomes), 9)
        for ablation in report.ablations:
            packet = derive_ablation_packet(_packet(), ablation.ablation_id)
            verify_benchmark_packet(packet)
            self.assertEqual(ablation.llm_calls, 0)
            self.assertTrue(ablation.reused_frozen_fixture)
            self.assertTrue(ablation.target_fault_degraded)
            self.assertNotIn(
                ABLATION_NATIVE_ARTIFACTS[ablation.ablation_id],
                packet.native_artifact_sha256,
            )
            self.assertEqual(packet.visible_input_sha256, _packet().visible_input_sha256)
            self.assertEqual(packet.data_sha256, _packet().data_sha256)
            by_id = {item.fault_id: item for item in ablation.fault_outcomes}
            self.assertTrue(
                all(not by_id[fault_id].detected for fault_id in ablation.target_fault_ids)
            )
            self.assertTrue(
                all(
                    by_id[fault_id].detected
                    for fault_id in FAULT_IDS
                    if fault_id not in ablation.target_fault_ids
                )
            )

            metric_report = evaluate_hard_metrics(
                packet,
                _reference(),
                fault_outcomes=ablation.fault_outcomes,
            )
            fault_metric = next(
                metric
                for metric in metric_report.metrics
                if metric.metric_id == "fatal_fault_detection_rate"
            )
            self.assertFalse(fault_metric.passed)
            self.assertEqual(
                fault_metric.numerator,
                len(FAULT_IDS) - len(ablation.target_fault_ids),
            )

    def test_ablations_remove_outputs_while_fault_mutations_remain(self) -> None:
        clean = _packet()

        no_probe = derive_ablation_packet(
            inject_fault(clean, "duplicate_merge_inflation"),
            "without_probe",
        )
        self.assertEqual(no_probe.executions[0].diagnostics["n_obs"], 103)
        self.assertNotIn(
            "duplicate_primary_key_count", no_probe.executions[0].diagnostics
        )

        no_reviewer = derive_ablation_packet(
            inject_fault(clean, "significant_subgroup_cherry_pick"),
            "without_reviewer",
        )
        self.assertTrue(
            any(claim.claim_id == "fault-post-hoc-subgroup" for claim in no_reviewer.claims)
        )
        self.assertNotIn("subgroup_selection", no_reviewer.executions[0].diagnostics)

        no_h2 = derive_ablation_packet(
            inject_fault(clean, "time_leakage_or_lead_misuse"),
            "without_h2",
        )
        self.assertEqual(no_h2.executions[0].diagnostics["lead_offset"], 1)
        self.assertNotIn("expected_lead_offset", no_h2.executions[0].diagnostics)
        self.assertFalse(no_h2.design.frozen_before_execution)
        self.assertEqual(no_h2.design.required_check_ids, [])

        no_replication = derive_ablation_packet(
            inject_fault(clean, "unit_amplification"),
            "without_independent_replication",
        )
        self.assertEqual(
            no_replication.executions[0].estimates[0]["coefficient"], -151.2
        )
        self.assertEqual(no_replication.reproduction.status, "not_available")

        no_gate = derive_ablation_packet(
            inject_fault(clean, "association_to_causation"),
            "without_claim_gate",
        )
        self.assertIn("导致", no_gate.claims[0].text)
        self.assertEqual(no_gate.claims[0].admission_status, "unassessed")

        no_ir = derive_ablation_packet(
            inject_fault(clean, "table_text_mismatch"),
            "without_manuscript_ir",
        )
        self.assertIn("999.0000", no_ir.manuscript_text)
        self.assertEqual(no_ir.statements, [])

    def test_faults_route_through_production_dag_replication_and_ir_checks(self) -> None:
        clean = _packet()
        with patch.object(
            fault_module,
            "finalize_test_dag_executions",
            wraps=fault_module.finalize_test_dag_executions,
        ) as dag_check:
            outcome = detect_injected_fault(
                clean,
                inject_fault(clean, "deleted_null_or_failure_branch"),
                "deleted_null_or_failure_branch",
            )
        self.assertTrue(outcome.detected)
        dag_check.assert_called_once()

        with patch.object(
            fault_module,
            "compare_panel_reproduction",
            wraps=fault_module.compare_panel_reproduction,
        ) as reproduction_check:
            unit = detect_injected_fault(
                clean,
                inject_fault(clean, "unit_amplification"),
                "unit_amplification",
            )
            clustering = detect_injected_fault(
                clean,
                inject_fault(clean, "wrong_clustering"),
                "wrong_clustering",
            )
        self.assertTrue(unit.detected)
        self.assertTrue(clustering.detected)
        self.assertEqual(reproduction_check.call_count, 2)
        self.assertTrue(any("超出容差" in item for item in unit.evidence))
        self.assertTrue(any("standard_errors" in item for item in clustering.evidence))

        with patch.object(
            fault_module,
            "protected_numeric_consistency",
            wraps=protected_numeric_consistency,
        ) as ir_check:
            mismatch = detect_injected_fault(
                clean,
                inject_fault(clean, "table_text_mismatch"),
                "table_text_mismatch",
            )
        self.assertTrue(mismatch.detected)
        ir_check.assert_called_once()

        with patch.object(
            fault_module,
            "causal_wording_violations",
            wraps=fault_module.causal_wording_violations,
        ) as claim_guard:
            causal = detect_injected_fault(
                clean,
                inject_fault(clean, "association_to_causation"),
                "association_to_causation",
            )
        self.assertTrue(causal.detected)
        claim_guard.assert_called_once()

    def test_explicit_required_not_executed_is_a_clean_terminal_record(self) -> None:
        clean = _packet()
        terminal = clean.executions[0].model_copy(
            update={
                "execution_status": "not_executed",
                "estimates": [],
                "not_executed_reason_code": "budget_exhausted",
            }
        )
        packet = seal_benchmark_packet(
            clean.model_copy(
                update={"executions": [terminal], "packet_sha256": None}
            )
        )

        outcome = detect_injected_fault(
            packet,
            packet,
            "deleted_null_or_failure_branch",
        )

        self.assertFalse(outcome.detected)
        self.assertEqual(outcome.action, "missed")

    def test_table_text_fault_mutates_body_not_protected_metadata(self) -> None:
        clean = _packet()
        injected = inject_fault(clean, "table_text_mismatch")

        self.assertNotEqual(injected.manuscript_text, clean.manuscript_text)
        self.assertEqual(
            injected.statements[0].protected_values,
            clean.statements[0].protected_values,
        )
        self.assertIn("999.0000", injected.manuscript_text)

    def test_unit_fault_finds_baseline_after_diagnostic_execution(self) -> None:
        clean = _packet()
        diagnostic = NormalizedExecution(
            execution_id="exec-diagnostic",
            check_id="check-diagnostic",
            execution_status="not_executed",
            run_type="diagnostic",
            estimates=[],
            diagnostics={"n_obs": 100},
            not_executed_reason_code="not_executable",
            implementation_id="linearmodels-panelols-v1",
            fixed_effects=["firm", "year"],
            standard_error_strategy="clustered_by_entity",
            contract_sha256="c" * 64,
            data_sha256=["b" * 64],
        )
        optional_failure = NormalizedExecution(
            execution_id="exec-optional-failure",
            check_id="check-optional-failure",
            execution_status="failed",
            run_type="robustness",
            estimates=[],
            diagnostics={},
            not_executed_reason_code="dependency_failed",
            implementation_id="linearmodels-panelols-v1",
            fixed_effects=["firm", "year"],
            standard_error_strategy="clustered_by_entity",
            contract_sha256="c" * 64,
            data_sha256=["b" * 64],
        )
        packet = seal_benchmark_packet(
            clean.model_copy(
                update={
                    "design": clean.design.model_copy(
                        update={
                            "planned_check_ids": [
                                "check-diagnostic",
                                "check-baseline",
                                "check-optional-failure",
                            ],
                            "required_check_ids": [
                                "check-diagnostic",
                                "check-baseline",
                            ],
                        }
                    ),
                    "executions": [
                        diagnostic,
                        *clean.executions,
                        optional_failure,
                    ],
                    "packet_sha256": None,
                }
            )
        )

        injected = inject_fault(packet, "unit_amplification")
        report = replay_ablations(packet)

        self.assertEqual(
            [item.execution_id for item in injected.executions],
            [
                "exec-diagnostic",
                "exec-baseline",
                "exec-optional-failure",
            ],
        )
        self.assertEqual(injected.executions[0].estimates, [])
        self.assertEqual(
            injected.executions[1].estimates[0]["coefficient"],
            -151.2,
        )
        self.assertEqual(
            sum(item.detected for item in report.full_system_outcomes),
            9,
        )
        self.assertEqual(report.clean_false_block_count, 0)


def _packet() -> BenchmarkPacket:
    return seal_benchmark_packet(
        BenchmarkPacket(
            packet_id="packet-clean",
            system_id="hypoweaver",
            case_id="case-1",
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            model_id="qwen-test",
            design=NormalizedDesign(
                method_family="panel_association",
                outcomes=["y"],
                treatments_or_exposures=["x"],
                fixed_effects=["firm", "year"],
                standard_error_strategy="clustered_by_entity",
                planned_check_ids=["check-baseline"],
                required_check_ids=["check-baseline"],
                frozen_before_execution=True,
                source_artifact_sha256="f" * 64,
            ),
            executions=[
                NormalizedExecution(
                    execution_id="exec-baseline",
                    check_id="check-baseline",
                    execution_status="succeeded",
                    run_type="baseline",
                    estimates=[
                        {
                            "term": "x",
                            "coefficient": -0.1512,
                            "standard_error": 0.0336,
                            "ci_lower": -0.2171,
                            "ci_upper": -0.0853,
                        }
                    ],
                    diagnostics={"n_obs": 100},
                    implementation_id="linearmodels-panelols-v1",
                    fixed_effects=["firm", "year"],
                    standard_error_strategy="clustered_by_entity",
                    contract_sha256="c" * 64,
                    data_sha256=["b" * 64],
                )
            ],
            claims=[
                NormalizedClaim(
                    claim_id="claim-H1",
                    text="x 与 y 呈负向关联。",
                    strength="associational",
                    admission_status="admitted",
                    check_ids=["check-baseline"],
                    execution_ids=["exec-baseline"],
                )
            ],
            statements=[
                NormalizedStatement(
                    statement_id="statement-1",
                    text="基准系数为 -0.1512。",
                    statement_kind="estimate_fact",
                    section_id="empirical_results",
                    claim_ids=["claim-H1"],
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
            manuscript_text="基准系数为 -0.1512。",
            reproduction=NormalizedReproduction(
                mode="independent_implementation",
                status="matched",
                covered_check_ids=["check-baseline"],
                primary_implementation_id="linearmodels-panelols-v1",
                replication_implementation_id="numpy-two-way-within-v1",
            ),
            resource_usage=BenchmarkResourceUsage(llm_calls=8),
            native_artifact_sha256={
                "candidate_design_set": "1" * 64,
                "design_arena": "2" * 64,
                "formal_research_contract": "c" * 64,
                "reproduction_audit": "3" * 64,
                "claim_gate_report": "4" * 64,
                "manuscript_statement_registry": "5" * 64,
            },
        )
    )


def _reference() -> BenchmarkReference:
    return BenchmarkReference(
        case_id="case-1",
        visible_input_sha256="a" * 64,
        data_sha256=["b" * 64],
        expected_design={},
        required_check_ids=[],
        independently_reproducible_check_ids=[],
    )


if __name__ == "__main__":
    unittest.main()
