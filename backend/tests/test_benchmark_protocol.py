from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hypoweaver.benchmark_evaluator import seal_benchmark_packet, summarize_paired_reviews
from hypoweaver.benchmark_models import (
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    FrozenBenchmarkProtocol,
    NeurIPSRatings,
    NeurIPSReview,
    NormalizedClaim,
    NormalizedDesign,
    NormalizedExecution,
    NormalizedReproduction,
    NormalizedStatement,
)
from hypoweaver.benchmark_packets import build_qwen_single_pass_packet
from hypoweaver.benchmark_protocol import (
    OFFICIAL_RUN_MANIFEST_FILE,
    OFFICIAL_STATE_FILE,
    begin_official_attempt,
    bind_official_packet_receipts,
    create_official_call_receipt,
    fail_official_attempt,
    freeze_protocol,
    hash_protocol_artifacts,
    load_official_attempt_binding,
    official_holdout_lock_id,
    run_benchmark_delivery,
    seal_protocol,
    verify_protocol,
)
from hypoweaver.seal import canonical_sha256
from hypoweaver.models import utc_now


class BenchmarkProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_root = self.root / "canonical-state"
        self.artifact_root = self.root / "artifacts"
        self.source_artifact_paths = {
            "hypoweaver": ["hypoweaver/source.py"],
            "agent_laboratory": ["agent-laboratory/source.py"],
            "benchmark_harness": ["benchmark-harness/source.py"],
        }
        self.configuration_artifact_paths = ["configuration/frozen.json"]
        for relative_path in (
            *(
                path
                for paths in self.source_artifact_paths.values()
                for path in paths
            ),
            *self.configuration_artifact_paths,
        ):
            path = self.artifact_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"artifact:{relative_path}\n", encoding="utf-8")
        source_sha256, configuration_sha256 = hash_protocol_artifacts(
            artifact_root=self.artifact_root,
            source_artifact_paths=self.source_artifact_paths,
            configuration_artifact_paths=self.configuration_artifact_paths,
        )
        self.reference = _reference()
        self.protocol = seal_protocol(
            FrozenBenchmarkProtocol(
                case_id="case-1",
                visible_input_sha256="a" * 64,
                data_sha256=["b" * 64],
                reference_sha256=canonical_sha256(
                    self.reference.model_dump(mode="json")
                ),
                source_sha256=source_sha256,
                configuration_sha256=configuration_sha256,
                source_artifact_paths=self.source_artifact_paths,
                configuration_artifact_paths=self.configuration_artifact_paths,
            )
        )
        self.hypoweaver = _full_packet()
        self.agent_laboratory = _comparison_packet(
            "packet-agent-lab", "agent_laboratory", 6
        )
        self.qwen = build_qwen_single_pass_packet(
            packet_id="packet-qwen",
            case_id="case-1",
            output={
                "model_id": "qwen-test",
                "main_findings": ["x 与 y 呈负向关联。"],
                "resource_usage": {"llm_calls": 1},
            },
            visible_input_sha256="a" * 64,
            data_sha256=["b" * 64],
            report_text="单次输出。",
        )
        self.blind = _blind_summary(self.hypoweaver, self.agent_laboratory)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_freeze_protocol_is_sealed_and_refuses_overwrite(self) -> None:
        target = self.root / "frozen.json"

        frozen = freeze_protocol(self.protocol, target)

        verify_protocol(frozen)
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8"))["protocol_sha256"],
            frozen.protocol_sha256,
        )
        with self.assertRaises(FileExistsError):
            freeze_protocol(self.protocol, target)

    def test_verify_protocol_accepts_legacy_hash_without_artifact_path_fields(self) -> None:
        payload = self.protocol.model_dump(
            mode="json",
            exclude={
                "protocol_sha256",
                "source_artifact_paths",
                "configuration_artifact_paths",
            },
        )
        legacy = FrozenBenchmarkProtocol.model_validate(
            {
                **payload,
                "protocol_sha256": canonical_sha256(payload),
            }
        )

        verify_protocol(legacy)

    def test_official_begin_rejects_unfrozen_protocol(self) -> None:
        unfrozen = self.protocol.model_copy(update={"protocol_sha256": None})

        with self.assertRaisesRegex(ValueError, "not frozen"):
            begin_official_attempt(
                unfrozen,
                self.root / "unfrozen",
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )

    def test_official_delivery_writes_required_artifacts_and_is_one_shot(self) -> None:
        output = self.root / "official"
        state_path = begin_official_attempt(
            self.protocol,
            output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"],
            "running",
        )
        run_manifest_path = output / OFFICIAL_RUN_MANIFEST_FILE
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(run_manifest["source_sha256"], self.protocol.source_sha256)
        self.assertEqual(
            run_manifest["configuration_sha256"],
            self.protocol.configuration_sha256,
        )
        self.assertEqual(run_manifest_path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(len(run_manifest["attempt_id"]), 64)
        self.assertTrue(run_manifest["begun_at"])
        self.assertEqual(
            canonical_sha256(
                {
                    key: value
                    for key, value in run_manifest.items()
                    if key != "run_manifest_sha256"
                }
            ),
            run_manifest["run_manifest_sha256"],
        )
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))[
                "run_manifest_sha256"
            ],
            run_manifest["run_manifest_sha256"],
        )
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            begin_official_attempt(
                self.protocol,
                output,
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )

        self.qwen, self.agent_laboratory, self.hypoweaver, self.blind = (
            _bind_official_artifacts(
                output,
                self.qwen,
                self.agent_laboratory,
                self.hypoweaver,
                self.blind,
            )
        )

        manifest = run_benchmark_delivery(
            protocol=self.protocol,
            reference=self.reference,
            qwen_packet=self.qwen,
            agent_laboratory_packet=self.agent_laboratory,
            hypoweaver_packet=self.hypoweaver,
            blind_summary=self.blind,
            output_dir=output,
            official=True,
            official_state_root=self.state_root,
        )

        self.assertTrue(manifest.all_hard_gates_passed)
        self.assertTrue(manifest.claim_condition_met)
        for relative in (
            "frozen_protocol.json",
            "neutral_packets/qwen_single_pass.json",
            "neutral_packets/agent_laboratory.json",
            "neutral_packets/hypoweaver.json",
            "hard_metrics.json",
            "blind_reviews.json",
            "ablations.json",
            "resource_usage.json",
            "comparison_report_zh.md",
            "delivery_manifest.json",
        ):
            self.assertTrue((output / relative).is_file(), relative)
        state = json.loads((output / OFFICIAL_STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        canonical_state = json.loads(
            (
                self.state_root / f"{official_holdout_lock_id(self.protocol)}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(canonical_state, state)
        usage = json.loads((output / "resource_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(usage["total_llm_calls"], 20)
        replay = json.loads((output / "ablations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(replay["ablations"]), 6)
        self.assertTrue(all(item["llm_calls"] == 0 for item in replay["ablations"]))
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=self.qwen,
                agent_laboratory_packet=self.agent_laboratory,
                hypoweaver_packet=self.hypoweaver,
                blind_summary=self.blind,
                output_dir=output,
                official=True,
                official_state_root=self.state_root,
            )

    def test_official_delivery_rejects_fixture_receipt(self) -> None:
        output = self.root / "fixture-receipt"
        begin_official_attempt(
            self.protocol,
            output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )
        qwen, agent_laboratory, hypoweaver, blind = _bind_official_artifacts(
            output,
            self.qwen,
            self.agent_laboratory,
            self.hypoweaver,
            self.blind,
        )
        bad = qwen.official_receipts[0].model_copy(update={"provider": "fixture"})
        qwen = bind_official_packet_receipts(qwen, [bad])

        with self.assertRaisesRegex(ValueError, "qwen provider"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=qwen,
                agent_laboratory_packet=agent_laboratory,
                hypoweaver_packet=hypoweaver,
                blind_summary=blind,
                output_dir=output,
                official=True,
                official_state_root=self.state_root,
            )

    def test_official_begin_rejects_protocol_hash_not_backed_by_artifacts(self) -> None:
        wrong = seal_protocol(
            self.protocol.model_copy(
                update={
                    "source_sha256": {
                        **self.protocol.source_sha256,
                        "hypoweaver": "0" * 64,
                    },
                    "protocol_sha256": None,
                }
            )
        )
        output = self.root / "wrong-source"

        with self.assertRaisesRegex(ValueError, "source artifacts"):
            begin_official_attempt(
                wrong,
                output,
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )

        self.assertFalse((output / OFFICIAL_STATE_FILE).exists())
        self.assertFalse((output / OFFICIAL_RUN_MANIFEST_FILE).exists())
        self.assertFalse(
            (self.state_root / f"{official_holdout_lock_id(wrong)}.json").exists()
        )

    def test_official_delivery_rehashes_and_rejects_artifact_drift(self) -> None:
        output = self.root / "artifact-drift"
        begin_official_attempt(
            self.protocol,
            output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )
        configuration_path = (
            self.artifact_root / self.configuration_artifact_paths[0]
        )
        configuration_path.write_text("changed after begin\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "artifact drift"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=self.qwen,
                agent_laboratory_packet=self.agent_laboratory,
                hypoweaver_packet=self.hypoweaver,
                blind_summary=self.blind,
                output_dir=output,
                official=True,
                official_state_root=self.state_root,
            )

        state = json.loads((output / OFFICIAL_STATE_FILE).read_text("utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_type"], "ArtifactDriftError")

    def test_official_begin_rejects_output_inside_frozen_directory(self) -> None:
        source_paths = {
            **self.source_artifact_paths,
            "hypoweaver": ["hypoweaver"],
        }
        source_sha256, configuration_sha256 = hash_protocol_artifacts(
            artifact_root=self.artifact_root,
            source_artifact_paths=source_paths,
            configuration_artifact_paths=self.configuration_artifact_paths,
        )
        protocol = seal_protocol(
            self.protocol.model_copy(
                update={
                    "source_sha256": source_sha256,
                    "configuration_sha256": configuration_sha256,
                    "source_artifact_paths": source_paths,
                    "protocol_sha256": None,
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            begin_official_attempt(
                protocol,
                self.artifact_root / "hypoweaver" / "official-output",
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )

    def test_canonical_protocol_lock_cannot_be_bypassed_with_new_output_dir(self) -> None:
        first_output = self.root / "official-first"
        second_output = self.root / "official-second"

        begin_official_attempt(
            self.protocol,
            first_output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )

        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            begin_official_attempt(
                self.protocol,
                second_output,
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )
        canonical_path = (
            self.state_root / f"{official_holdout_lock_id(self.protocol)}.json"
        )
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        self.assertEqual(canonical["status"], "running")
        self.assertEqual(canonical["output_dir"], str(first_output.resolve()))

    def test_holdout_lock_cannot_be_redrawn_by_changing_protocol_hash(self) -> None:
        first_output = self.root / "holdout-first"
        second_output = self.root / "holdout-second"
        begin_official_attempt(
            self.protocol,
            first_output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )
        redrawn = seal_protocol(
            self.protocol.model_copy(
                update={
                    "reference_sha256": "f" * 64,
                    "protocol_sha256": None,
                }
            )
        )

        self.assertNotEqual(redrawn.protocol_sha256, self.protocol.protocol_sha256)
        self.assertEqual(
            official_holdout_lock_id(redrawn),
            official_holdout_lock_id(self.protocol),
        )
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            begin_official_attempt(
                redrawn,
                second_output,
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )

    def test_official_delivery_requires_begin_before_model_calls(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "begin_official_attempt"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=self.qwen,
                agent_laboratory_packet=self.agent_laboratory,
                hypoweaver_packet=self.hypoweaver,
                blind_summary=self.blind,
                output_dir=self.root / "not-begun",
                official=True,
                official_state_root=self.state_root,
            )

    def test_failed_official_delivery_is_terminal(self) -> None:
        output = self.root / "failed-official"
        begin_official_attempt(
            self.protocol,
            output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )
        wrong_input = seal_benchmark_packet(
            self.qwen.model_copy(
                update={"data_sha256": ["0" * 64], "packet_sha256": None}
            )
        )

        with self.assertRaisesRegex(ValueError, "input identity"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=wrong_input,
                agent_laboratory_packet=self.agent_laboratory,
                hypoweaver_packet=self.hypoweaver,
                blind_summary=self.blind,
                output_dir=output,
                official=True,
                official_state_root=self.state_root,
            )

    def test_pre_delivery_failure_is_sealed_without_error_message(self) -> None:
        output = self.root / "pre-delivery-failure"
        begin_official_attempt(
            self.protocol,
            output,
            state_root=self.state_root,
            artifact_root=self.artifact_root,
        )

        fail_official_attempt(
            self.protocol,
            output,
            RuntimeError("secret-bearing transport detail"),
            state_root=self.state_root,
        )

        state = json.loads(
            (output / OFFICIAL_STATE_FILE).read_text(encoding="utf-8")
        )
        failure = json.loads(
            (output / "official_failure.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_type"], "RuntimeError")
        self.assertEqual(failure["error_type"], "RuntimeError")
        self.assertNotIn("secret-bearing", json.dumps(failure))
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            begin_official_attempt(
                self.protocol,
                self.root / "another-output",
                state_root=self.state_root,
                artifact_root=self.artifact_root,
            )
        state = json.loads((output / OFFICIAL_STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        canonical_state = json.loads(
            (
                self.state_root / f"{official_holdout_lock_id(self.protocol)}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(canonical_state, state)
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=self.qwen,
                agent_laboratory_packet=self.agent_laboratory,
                hypoweaver_packet=self.hypoweaver,
                blind_summary=self.blind,
                output_dir=output,
                official=True,
                official_state_root=self.state_root,
            )

    def test_budget_violation_is_rejected_without_replaying_models(self) -> None:
        over_budget = seal_benchmark_packet(
            self.hypoweaver.model_copy(
                update={
                    "resource_usage": BenchmarkResourceUsage(llm_calls=21),
                    "packet_sha256": None,
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "call budget"):
            run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=self.qwen,
                agent_laboratory_packet=self.agent_laboratory,
                hypoweaver_packet=over_budget,
                blind_summary=_blind_summary(over_budget, self.agent_laboratory),
                output_dir=self.root / "budget",
                official=False,
            )

    def test_blind_technical_failures_are_reported_outside_scientific_scores(self) -> None:
        reviews = list(self.blind.reviews)
        reviews[0] = reviews[0].model_copy(
            update={
                "resource_usage": reviews[0].resource_usage.model_copy(
                    update={"technical_failures": ["transient-transport-error"]}
                )
            }
        )
        blind_with_failure = self.blind.model_copy(update={"reviews": reviews})
        output = self.root / "technical-failure"

        manifest = run_benchmark_delivery(
            protocol=self.protocol,
            reference=self.reference,
            qwen_packet=self.qwen,
            agent_laboratory_packet=self.agent_laboratory,
            hypoweaver_packet=self.hypoweaver,
            blind_summary=blind_with_failure,
            output_dir=output,
            official=False,
        )

        usage = json.loads((output / "resource_usage.json").read_text("utf-8"))
        self.assertEqual(
            usage["technical_failures"],
            ["transient-transport-error"],
        )
        self.assertEqual(
            usage["blind_reviews"]["technical_failures"],
            ["transient-transport-error"],
        )
        self.assertTrue(manifest.all_hard_gates_passed)

    def test_single_pass_adapter_does_not_invent_execution_or_traceability(self) -> None:
        self.assertEqual(self.qwen.resource_usage.llm_calls, 1)
        self.assertEqual(self.qwen.executions, [])
        self.assertEqual(self.qwen.statements, [])
        self.assertEqual(self.qwen.claims[0].check_ids, [])
        self.assertEqual(self.qwen.reproduction.status, "not_available")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            build_qwen_single_pass_packet(
                packet_id="bad",
                case_id="case-1",
                output={"resource_usage": {"llm_calls": 2}},
                visible_input_sha256="a" * 64,
                data_sha256=["b" * 64],
                report_text="bad",
            )


def _reference() -> BenchmarkReference:
    return BenchmarkReference(
        case_id="case-1",
        visible_input_sha256="a" * 64,
        data_sha256=["b" * 64],
        expected_design={
            "method_family": "panel_association",
            "outcomes": ["y"],
            "treatments_or_exposures": ["x"],
            "fixed_effects": ["firm", "year"],
            "standard_error_strategy": "clustered_by_entity",
            "frozen_before_execution": True,
        },
        expected_contract_sha256="2" * 64,
        required_check_ids=["check-baseline"],
        independently_reproducible_check_ids=["check-baseline"],
        clean_packet_ids=["packet-full"],
    )


def _full_packet() -> BenchmarkPacket:
    return seal_benchmark_packet(
        BenchmarkPacket(
            packet_id="packet-full",
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
                source_artifact_sha256="1" * 64,
                contract_sha256="2" * 64,
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
                    implementation_version="1.0",
                    code_sha256="3" * 64,
                    environment_sha256="4" * 64,
                    fixed_effects=["firm", "year"],
                    standard_error_strategy="clustered_by_entity",
                    contract_sha256="2" * 64,
                    data_sha256=["b" * 64],
                    source_artifact_sha256="5" * 64,
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
                "candidate_design_set": "6" * 64,
                "design_arena": "7" * 64,
                "formal_research_contract": "2" * 64,
                "reproduction_audit": "8" * 64,
                "claim_gate_report": "9" * 64,
                "manuscript_statement_registry": "a" * 64,
            },
        )
    )


def _comparison_packet(packet_id: str, system_id: str, calls: int) -> BenchmarkPacket:
    return seal_benchmark_packet(
        BenchmarkPacket(
            packet_id=packet_id,
            system_id=system_id,
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
                frozen_before_execution=True,
            ),
            claims=[
                NormalizedClaim(
                    claim_id="finding-1",
                    text="x 与 y 呈负向关联。",
                    strength="associational",
                )
            ],
            manuscript_text="对照报告。",
            resource_usage=BenchmarkResourceUsage(llm_calls=calls),
        )
    )


def _blind_summary(
    hypoweaver: BenchmarkPacket,
    agent_laboratory: BenchmarkPacket,
):
    reviews = []
    for index in range(1, 6):
        assignment = "A_B" if index % 2 else "B_A"
        reviews.append(
            NeurIPSReview(
                review_id=f"review-{index}",
                sample_index=index,
                label_order="A_B" if index % 2 else "B_A",
                system_assignment=assignment,
                ratings_a=(
                    _ratings(8, 4)
                    if assignment == "A_B"
                    else _ratings(6, 3)
                ),
                ratings_b=(
                    _ratings(6, 3)
                    if assignment == "A_B"
                    else _ratings(8, 4)
                ),
                preferred_label="A" if assignment == "A_B" else "B",
                diagnosis=["匿名系统一的证据闭环更完整。"],
                resource_usage=BenchmarkResourceUsage(
                    llm_calls=1,
                    input_tokens=10,
                    output_tokens=5,
                    wall_time_seconds=0.1,
                ),
            )
        )
    return summarize_paired_reviews(
        "case-1", hypoweaver.packet_id, agent_laboratory.packet_id, reviews
    )


def _bind_official_artifacts(
    output: Path,
    qwen: BenchmarkPacket,
    agent_laboratory: BenchmarkPacket,
    hypoweaver: BenchmarkPacket,
    blind,
):
    binding = load_official_attempt_binding(output)

    def receipts(label: str, count: int):
        return [
            create_official_call_receipt(
                binding,
                provider="qwen",
                model="qwen-test",
                raw_response=f"{label}-{index}",
                call_started_at=utc_now(),
                call_completed_at=utc_now(),
            )
            for index in range(count)
        ]

    qwen = bind_official_packet_receipts(
        qwen,
        receipts("qwen", qwen.resource_usage.llm_calls),
    )
    agent_laboratory = bind_official_packet_receipts(
        agent_laboratory,
        receipts("agent-laboratory", agent_laboratory.resource_usage.llm_calls),
    )
    hypoweaver = bind_official_packet_receipts(
        hypoweaver,
        receipts("hypoweaver", hypoweaver.resource_usage.llm_calls),
    )
    reviews = [
        review.model_copy(
            update={
                "official_receipt": receipts(
                    f"blind-{review.sample_index}", 1
                )[0]
            }
        )
        for review in blind.reviews
    ]
    blind = summarize_paired_reviews(
        blind.case_id,
        blind.packet_a_id,
        blind.packet_b_id,
        reviews,
    )
    return qwen, agent_laboratory, hypoweaver, blind


def _ratings(overall: int, soundness: int) -> NeurIPSRatings:
    return NeurIPSRatings(
        quality=4,
        significance=3,
        clarity=4,
        soundness=soundness,
        presentation=4,
        contribution=3,
        overall=overall,
        confidence=4,
        recommendation="accept",
    )


if __name__ == "__main__":
    unittest.main()
