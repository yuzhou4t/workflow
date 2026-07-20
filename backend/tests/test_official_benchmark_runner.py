from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hypoweaver.benchmark_evaluator import (
    seal_benchmark_packet,
    summarize_paired_reviews,
)
from hypoweaver.benchmark_models import (
    ABLATION_NATIVE_ARTIFACTS,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    NeurIPSRatings,
    NeurIPSReview,
    NormalizedClaim,
    NormalizedDesign,
    NormalizedExecution,
    NormalizedReproduction,
    NormalizedStatement,
)
from hypoweaver.benchmark_protocol import (
    OFFICIAL_STATE_FILE,
    bind_official_packet_receipts,
    create_official_call_receipt,
)
from hypoweaver.claim_gate import validate_h3_claim_decision
from hypoweaver.models import ClaimRecord, utc_now
from hypoweaver.official_benchmark_runner import (
    OfficialBenchmarkConfiguration,
    OfficialBenchmarkOrchestrator,
    _claim_decision,
    _native_component_artifact_sha256,
    _receipts_from_usage,
    prepare_official_protocol,
)
from hypoweaver.seal import canonical_sha256


class _FakeOfficialSystems:
    def __init__(self, output_dir: Path, *, fail_agent: bool = False) -> None:
        self.output_dir = output_dir
        self.fail_agent = fail_agent
        self.calls: list[str] = []
        self.reference_summary: str | None = None

    async def preflight(self, case_request, protocol) -> None:
        self.calls.append("preflight")
        self.case_id = protocol.case_id
        self.visible_input_sha256 = protocol.visible_input_sha256
        self.data_sha256 = protocol.data_sha256

    async def run_qwen_single_pass(self, case_request, protocol, binding):
        self.calls.append("qwen")
        self._assert_begun(binding)
        return self._packet("qwen_single_pass", "packet-qwen", binding)

    async def run_agent_laboratory(self, case_request, protocol, binding):
        self.calls.append("agent")
        self._assert_begun(binding)
        if self.fail_agent:
            raise RuntimeError("model-owned detail must not be persisted")
        return self._packet("agent_laboratory", "packet-agent", binding)

    async def run_hypoweaver(self, case_request, protocol, binding):
        self.calls.append("hypoweaver")
        self._assert_begun(binding)
        return self._packet("hypoweaver", "packet-hypoweaver", binding)

    async def run_blind_reviews(
        self,
        hypoweaver_packet,
        agent_laboratory_packet,
        reference_summary,
        binding,
    ):
        self.calls.append("blind")
        self.reference_summary = reference_summary
        reviews = []
        for index in range(1, 6):
            assignment = "A_B" if index % 2 else "B_A"
            receipt = create_official_call_receipt(
                binding,
                provider="qwen",
                model="qwen-test",
                raw_response=f"blind-{index}",
                call_started_at=utc_now(),
                call_completed_at=utc_now(),
            )
            reviews.append(
                NeurIPSReview(
                    review_id=f"review-{index}",
                    sample_index=index,
                    label_order="A_B" if index % 2 else "B_A",
                    system_assignment=assignment,
                    ratings_a=(
                        _ratings(7) if assignment == "A_B" else _ratings(6)
                    ),
                    ratings_b=(
                        _ratings(6) if assignment == "A_B" else _ratings(7)
                    ),
                    preferred_label="A" if assignment == "A_B" else "B",
                    resource_usage=BenchmarkResourceUsage(llm_calls=1),
                    official_receipt=receipt,
                )
            )
        return summarize_paired_reviews(
            self.case_id,
            hypoweaver_packet.packet_id,
            agent_laboratory_packet.packet_id,
            reviews,
        )

    def _packet(self, system_id: str, packet_id: str, binding):
        is_hypoweaver = system_id == "hypoweaver"
        contract_sha256 = "c" * 64
        packet = seal_benchmark_packet(
            BenchmarkPacket(
                packet_id=packet_id,
                system_id=system_id,
                case_id=self.case_id,
                visible_input_sha256=self.visible_input_sha256,
                data_sha256=self.data_sha256,
                model_id="qwen-test",
                design=NormalizedDesign(
                    planned_check_ids=["check-baseline"] if is_hypoweaver else [],
                    required_check_ids=["check-baseline"] if is_hypoweaver else [],
                    standard_error_strategy=(
                        "clustered_by_entity" if is_hypoweaver else None
                    ),
                    source_artifact_sha256=("a" * 64 if is_hypoweaver else None),
                    contract_sha256=(contract_sha256 if is_hypoweaver else None),
                ),
                executions=(
                    [
                        NormalizedExecution(
                            execution_id="execution-baseline",
                            check_id="check-baseline",
                            execution_status="succeeded",
                            run_type="baseline",
                            estimates=[
                                {
                                    "term": "x",
                                    "coefficient": 1.0,
                                    "standard_error": 0.2,
                                    "ci_lower": 0.6,
                                    "ci_upper": 1.4,
                                }
                            ],
                            diagnostics={},
                            implementation_id="primary",
                            implementation_version="1",
                            code_sha256="d" * 64,
                            environment_sha256="e" * 64,
                            standard_error_strategy="clustered_by_entity",
                            contract_sha256=contract_sha256,
                            data_sha256=self.data_sha256,
                            source_artifact_sha256="f" * 64,
                        )
                    ]
                    if is_hypoweaver
                    else []
                ),
                claims=(
                    [
                        NormalizedClaim(
                            claim_id="claim-H1",
                            text="x 与 y 存在条件关联。",
                            strength="associational",
                            admission_status="admitted",
                            check_ids=["check-baseline"],
                            execution_ids=["execution-baseline"],
                        )
                    ]
                    if is_hypoweaver
                    else []
                ),
                statements=(
                    [
                        NormalizedStatement(
                            statement_id="statement-1",
                            text="系数为 1.0000。",
                            statement_kind="estimate_fact",
                            claim_ids=["claim-H1"],
                            execution_ids=["execution-baseline"],
                            protected_values=[
                                {
                                    "value_id": "value-1",
                                    "value_kind": "coefficient",
                                    "source_kind": "execution",
                                    "source_id": "execution-baseline",
                                    "source_path": "/estimates/0/coefficient",
                                    "raw_value": 1.0,
                                    "rendered_value": "1.0000",
                                }
                            ],
                        )
                    ]
                    if is_hypoweaver
                    else []
                ),
                manuscript_text="系数为 1.0000。" if is_hypoweaver else "",
                reproduction=(
                    NormalizedReproduction(
                        mode="independent_implementation",
                        status="matched",
                        covered_check_ids=["check-baseline"],
                        primary_implementation_id="primary",
                        replication_implementation_id="replication",
                    )
                    if is_hypoweaver
                    else NormalizedReproduction()
                ),
                resource_usage=BenchmarkResourceUsage(llm_calls=1),
                native_artifact_sha256=(
                    {
                        "candidate_design_set": "6" * 64,
                        "design_arena": "7" * 64,
                        "formal_research_contract": contract_sha256,
                        "reproduction_audit": "8" * 64,
                        "claim_gate_report": "9" * 64,
                        "manuscript_statement_registry": "a" * 64,
                    }
                    if is_hypoweaver
                    else {}
                ),
            )
        )
        receipt = create_official_call_receipt(
            binding,
            provider="qwen",
            model="qwen-test",
            raw_response=packet_id,
            call_started_at=utc_now(),
            call_completed_at=utc_now(),
        )
        return bind_official_packet_receipts(packet, [receipt])

    def _assert_begun(self, binding) -> None:
        state = json.loads(
            (self.output_dir / OFFICIAL_STATE_FILE).read_text(encoding="utf-8")
        )
        if state["status"] != "running" or state["attempt_id"] != binding.attempt_id:
            raise AssertionError("official calls were made before begin")


class OfficialBenchmarkRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_native_ablation_artifacts_are_read_and_hash_verified(self) -> None:
        artifacts = {}
        for artifact_key in ABLATION_NATIVE_ARTIFACTS.values():
            payload = {"artifact_key": artifact_key}
            artifacts[artifact_key] = {
                "payload": payload,
                "sha256": canonical_sha256(payload),
            }
        state = SimpleNamespace(artifacts=artifacts)

        hashes = _native_component_artifact_sha256(state)

        self.assertEqual(
            hashes,
            {
                key: value["sha256"]
                for key, value in artifacts.items()
            },
        )
        artifacts["claim_gate_report"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "claim_gate_report"):
            _native_component_artifact_sha256(state)

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.visible = self.root / "visible.json"
        self.reference_path = self.root / "reference.json"
        self.reference_summary = self.root / "reference-summary.txt"
        self.config_snapshot = self.root / "configuration.json"
        self.runtime_public = self.root / "runtime-public.json"
        self.protocol_path = self.root / "protocol.json"
        for name in ("hypoweaver.py", "agent.py", "harness.py"):
            (self.root / name).write_text(f"source:{name}\n", encoding="utf-8")
        visible_payload = {
            "definition_id": "app-a",
            "mode": "research",
            "model_provider": "qwen",
            "execution_mode": "external",
            "case": {
                "case_id": "case-official",
                "title": "Official fixture",
                "research_question": "x 与 y 是否相关？",
                "hypotheses": [
                    {
                        "hypothesis_id": "H1",
                        "statement": "x 与 y 相关。",
                        "expected_direction": "unspecified",
                    }
                ],
                "data_structure_hint": "panel",
                "variables": [
                    {"name": "firm", "role": "id"},
                    {"name": "year", "role": "time"},
                    {"name": "y", "role": "outcome"},
                    {"name": "x", "role": "exposure"},
                ],
            },
        }
        self.visible.write_text(
            json.dumps(visible_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        visible_sha256 = hashlib.sha256(self.visible.read_bytes()).hexdigest()
        reference = BenchmarkReference(
            case_id="case-official",
            visible_input_sha256=visible_sha256,
            data_sha256=[],
            expected_design={},
            required_check_ids=[],
            independently_reproducible_check_ids=[],
        )
        self.reference_path.write_text(
            reference.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        self.reference_summary.write_text(
            "Hidden reference enters only the paired review.", encoding="utf-8"
        )
        self.config_snapshot.write_text('{"frozen":true}\n', encoding="utf-8")
        self.runtime_public.write_text('{"runtime":"frozen"}\n', encoding="utf-8")
        self.output = self.root / "output"
        self.configuration = OfficialBenchmarkConfiguration(
            artifact_root=str(self.root),
            protocol_path="protocol.json",
            visible_input_path="visible.json",
            reference_path="reference.json",
            reference_summary_path="reference-summary.txt",
            runtime_public_path="runtime-public.json",
            source_artifact_paths={
                "hypoweaver": ["hypoweaver.py"],
                "agent_laboratory": ["agent.py"],
                "benchmark_harness": ["harness.py"],
            },
            configuration_artifact_paths=[
                "visible.json",
                "reference.json",
                "reference-summary.txt",
                "configuration.json",
                "runtime-public.json",
            ],
            output_dir=str(self.output),
            working_dir=str(self.root / "work"),
            official_state_root=str(self.root / "states"),
            agent_laboratory_root="agent-laboratory",
            poll_interval_seconds=0.01,
        )
        prepare_official_protocol(self.configuration)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_orchestrator_begins_once_and_hides_reference_from_systems(self) -> None:
        systems = _FakeOfficialSystems(self.output)

        manifest = await OfficialBenchmarkOrchestrator(
            self.configuration,
            system_runner=systems,
        ).run()

        self.assertTrue(manifest.official)
        self.assertFalse(manifest.claim_condition_met)
        self.assertEqual(
            systems.calls,
            ["preflight", "qwen", "agent", "hypoweaver", "blind"],
        )
        self.assertEqual(
            systems.reference_summary,
            "Hidden reference enters only the paired review.",
        )
        state = json.loads(
            (self.output / OFFICIAL_STATE_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "completed")

    async def test_pre_delivery_failure_is_terminal_and_redacted(self) -> None:
        systems = _FakeOfficialSystems(self.output, fail_agent=True)

        with self.assertRaisesRegex(RuntimeError, "model-owned detail"):
            await OfficialBenchmarkOrchestrator(
                self.configuration,
                system_runner=systems,
            ).run()

        state = json.loads(
            (self.output / OFFICIAL_STATE_FILE).read_text(encoding="utf-8")
        )
        failure_text = (self.output / "official_failure.json").read_text(
            encoding="utf-8"
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_type"], "RuntimeError")
        self.assertNotIn("model-owned detail", failure_text)
        self.assertEqual(systems.calls, ["preflight", "qwen", "agent"])

    async def test_code_owned_h3_policy_obeys_gate_action_matrix(self) -> None:
        admitted = _claim("admitted", "associational")
        mixed = _claim("downgrade_required", "mixed")
        preliminary = _claim("downgrade_required", "preliminary")
        preliminary.max_allowed_strength = "associational"
        prohibited = _claim("prohibited", "prohibited")
        self_calibrated = _claim("downgrade_required", "insufficient")
        self_calibrated.max_allowed_strength = "mixed"

        decisions = [
            _claim_decision(admitted),
            _claim_decision(mixed),
            _claim_decision(preliminary),
            _claim_decision(prohibited),
            _claim_decision(self_calibrated),
        ]

        self.assertEqual(
            [item.decision for item in decisions],
            ["approve", "downgrade", "downgrade", "reject", "reject"],
        )
        self.assertIn("证据混合", decisions[1].final_text or "")
        self.assertIn("统计关联只能作为受限关联报告", decisions[1].final_text or "")
        self.assertNotIn("条件关联未稳健", decisions[1].final_text or "")
        self.assertIn("检查未完成", decisions[2].final_text or "")
        self.assertIn("allowed_strength=insufficient", decisions[4].reason or "")
        self.assertIn("max_allowed_strength=mixed", decisions[4].reason or "")
        self.assertIn("preserves the tighter candidate calibration", decisions[4].reason or "")
        for claim, decision in zip(
            (admitted, mixed, preliminary, prohibited, self_calibrated),
            decisions,
            strict=True,
        ):
            validate_h3_claim_decision(
                claim,
                decision.decision,
                decision.final_text,
            )

    async def test_receipt_adapter_accepts_both_runtime_field_dialects(self) -> None:
        begun_at = utc_now()
        from hypoweaver.benchmark_models import OfficialAttemptBinding

        official = OfficialAttemptBinding(
            attempt_id="a" * 64,
            run_manifest_sha256="b" * 64,
            begun_at=begun_at,
        )
        receipts = _receipts_from_usage(
            official,
            {
                "call_receipts": [
                    {
                        "provider": "qwen",
                        "model": "model-a",
                        "response_sha256": "c" * 64,
                        "started_at": utc_now(),
                        "completed_at": utc_now(),
                    },
                    {
                        "provider": "qwen",
                        "model": "model-b",
                        "response_sha256": "d" * 64,
                        "call_started_at": utc_now(),
                        "call_completed_at": utc_now(),
                    },
                ]
            },
        )

        self.assertEqual([item.model for item in receipts], ["model-a", "model-b"])


def _ratings(overall: int) -> NeurIPSRatings:
    return NeurIPSRatings(
        quality=3,
        significance=3,
        clarity=3,
        soundness=3,
        presentation=3,
        contribution=3,
        overall=overall,
        confidence=4,
        recommendation="accept",
    )


def _claim(admission_status: str, strength: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=f"claim-{admission_status}-{strength}",
        hypothesis_id="H1",
        claim_text="x 与 y 存在条件关联。",
        evidence_status="mixed" if strength == "mixed" else "supported",
        allowed_strength=strength,
        supporting_runs=["execution-1"],
        opposing_runs=[],
        scope="frozen case",
        robustness_status="tested",
        unresolved_risks=[],
        admission_status=admission_status,
        max_allowed_strength=strength,
    )


if __name__ == "__main__":
    unittest.main()
