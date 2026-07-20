from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hypoweaver.benchmark_evaluator import seal_benchmark_packet
from hypoweaver.benchmark_models import (
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    FrozenBenchmarkProtocol,
    HardMetricReport,
    NormalizedDesign,
)
from hypoweaver.benchmark_protocol import seal_protocol
from hypoweaver.case_import import DatasetRegistry
from hypoweaver.engineering_replay import (
    EngineeringReplayController,
    EngineeringReplayProtocol,
    EngineeringReplayProtocolV2,
)
from hypoweaver.local_recovery_runner import LocalRecoveryRoundResult
from hypoweaver.models import CreateRunRequest, DatasetRef
from hypoweaver.recovery_campaign import (
    build_recovery_freeze,
    create_recovery_campaign,
    invalidate_recovery_campaign,
    seal_prior_usage_import,
)
from hypoweaver.recovery_models import (
    PriorUsageEvidence,
    PriorUsageImport,
    RecoveryCallReceipt,
    RecoveryUsage,
)
from hypoweaver.seal import canonical_sha256


NOW = "2026-07-17T00:00:00+00:00"
LATER = "2026-07-17T00:01:00+00:00"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FaultReport:
    full_system_outcomes = ()
    clean_false_block_count = 0

    def model_dump(self, *, mode: str):
        return {
            "protocol_version": "enterprise-panel-v1",
            "case_id": "seen-case-1",
            "clean_packet_sha256": "a" * 64,
            "full_system_outcomes": [],
            "clean_false_block_count": 0,
            "ablations": [],
        }


class _CrashSignal(BaseException):
    pass


class _Backend:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.contexts = []

    async def run_hypoweaver_round(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.result


class EngineeringReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.visible_path = self.source_root / "visible.json"
        self.data_path = self.source_root / "panel.csv"
        self.reference_path = self.source_root / "reference.json"
        self.summary_path = self.source_root / "reference-summary.txt"
        self.runtime_public_path = self.source_root / "runtime-public.json"
        self.protocol_path = self.source_root / "official-protocol.json"
        self.hypo_source = self.source_root / "sources" / "hypo.py"
        self.agent_source = self.source_root / "sources" / "agent.py"
        self.harness_source = self.source_root / "sources" / "harness.py"
        self.freeze_config = self.source_root / "config" / "runtime.json"
        for path in (
            self.hypo_source,
            self.agent_source,
            self.harness_source,
            self.freeze_config,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture = {path.name!r}\n", encoding="utf-8")
        (self.source_root / "agent-lab").mkdir()
        self.data_path.write_text(
            "firm,year,x,y\n1,2023,1,2\n1,2024,2,3\n",
            encoding="utf-8",
        )
        dataset_ref = DatasetRef(
            dataset_id="ds_seen_case_1",
            role="main",
            filename=self.data_path.name,
            mime_type="text/csv",
            sha256=_file_sha256(self.data_path),
            size_bytes=self.data_path.stat().st_size,
        )
        request = CreateRunRequest.model_validate(
            {
                "mode": "research",
                "model_provider": "qwen",
                "execution_mode": "external",
                "case": {
                    "case_id": "seen-case-1",
                    "title": "Seen case",
                    "research_question": "x 与 y 是否相关？",
                    "hypotheses": [
                        {"hypothesis_id": "H1", "statement": "x 与 y 相关。"}
                    ],
                    "variables": [
                        {"name": "firm", "role": "id"},
                        {"name": "year", "role": "time"},
                        {"name": "y", "role": "outcome"},
                        {"name": "x", "role": "exposure"},
                    ],
                    "dataset_refs": [dataset_ref.model_dump(mode="json")],
                },
            }
        )
        self.visible_path.write_text(request.model_dump_json(), encoding="utf-8")
        self.summary_path.write_text("sealed seen-case summary\n", encoding="utf-8")
        self.runtime_public_path.write_text("{}\n", encoding="utf-8")
        self.reference = BenchmarkReference(
            case_id="seen-case-1",
            visible_input_sha256=_file_sha256(self.visible_path),
            data_sha256=[_file_sha256(self.data_path)],
            expected_design={},
            required_check_ids=[],
            independently_reproducible_check_ids=[],
        )
        self.reference_path.write_text(
            self.reference.model_dump_json(),
            encoding="utf-8",
        )
        self.protocol = seal_protocol(
            FrozenBenchmarkProtocol(
                case_id="seen-case-1",
                visible_input_sha256=_file_sha256(self.visible_path),
                data_sha256=[_file_sha256(self.data_path)],
                reference_sha256=canonical_sha256(
                    self.reference.model_dump(mode="json")
                ),
                source_sha256={
                    "hypoweaver": "1" * 64,
                    "agent_laboratory": "2" * 64,
                    "benchmark_harness": "3" * 64,
                },
                configuration_sha256="4" * 64,
                source_artifact_paths={
                    "hypoweaver": ["sources/hypo.py"],
                    "agent_laboratory": ["sources/agent.py"],
                    "benchmark_harness": ["sources/harness.py"],
                },
                configuration_artifact_paths=["config/runtime.json"],
                frozen_at=NOW,
            )
        )
        self.protocol_path.write_text(
            self.protocol.model_dump_json(), encoding="utf-8"
        )
        self.formal_output = self.root / "formal-output"
        self.formal_work = self.root / "formal-work"
        self.formal_state = self.root / "formal-state"
        for path in (self.formal_output, self.formal_work, self.formal_state):
            path.mkdir()
        self.source_config_path = self.root / "source-config.json"
        self.source_config_path.write_text(
            json.dumps(
                {
                    "artifact_root": str(self.source_root),
                    "protocol_path": self.protocol_path.name,
                    "visible_input_path": self.visible_path.name,
                    "reference_path": self.reference_path.name,
                    "reference_summary_path": self.summary_path.name,
                    "runtime_public_path": self.runtime_public_path.name,
                    "source_artifact_paths": self.protocol.source_artifact_paths,
                    "configuration_artifact_paths": (
                        self.protocol.configuration_artifact_paths
                    ),
                    "output_dir": str(self.formal_output),
                    "working_dir": str(self.formal_work),
                    "official_state_root": str(self.formal_state),
                    "agent_laboratory_root": "agent-lab",
                }
            ),
            encoding="utf-8",
        )
        self.registry_path = self.root / "datasets.json"
        DatasetRegistry(self.registry_path).register(dataset_ref, self.data_path)
        self.environment = patch.dict(
            os.environ,
            {"HYPOWEAVER_DATASET_REGISTRY_PATH": str(self.registry_path)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.predecessor_path = self._write_predecessor()
        self.delivery_root = self.root / "engineering-replay"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_predecessor(self) -> Path:
        freeze = build_recovery_freeze(
            self.protocol,
            artifact_root=self.source_root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            frozen_at=NOW,
        )
        prior = seal_prior_usage_import(
            PriorUsageImport(
                source_official_attempt_id="5" * 64,
                source_official_run_manifest_sha256="6" * 64,
                source_official_holdout_lock_id=(
                    freeze.source_official_holdout_lock_id
                ),
                usage=RecoveryUsage(),
                evidence=PriorUsageEvidence(
                    evidence_status="complete_receipts",
                    resource_ledger_sha256="7" * 64,
                    ledger_llm_calls=0,
                    verified_receipt_sha256=(),
                    missing_receipt_count=0,
                ),
                imported_at=NOW,
            )
        )
        campaign = create_recovery_campaign(freeze, prior, created_at=NOW)
        campaign = invalidate_recovery_campaign(
            campaign,
            "sealed_predecessor_failure",
            invalidated_at=LATER,
        )
        state_root = self.root / "replacement-03-state"
        state_root.mkdir()
        path = state_root / "replacement-03.json"
        path.write_text(campaign.model_dump_json(), encoding="utf-8")
        return path

    def _packet(self, calls: int = 0) -> BenchmarkPacket:
        return seal_benchmark_packet(
            BenchmarkPacket(
                packet_id="engineering-packet",
                system_id="hypoweaver",
                case_id="seen-case-1",
                visible_input_sha256=_file_sha256(self.visible_path),
                data_sha256=[_file_sha256(self.data_path)],
                model_id="qwen-test",
                design=NormalizedDesign(),
                resource_usage=BenchmarkResourceUsage(llm_calls=calls),
            )
        )

    def _completed_result(self) -> LocalRecoveryRoundResult:
        return LocalRecoveryRoundResult(
            status="completed",
            started_at=NOW,
            completed_at=LATER,
            usage=RecoveryUsage(),
            receipts=(),
            packet=self._packet(),
        )

    def _controller(
        self,
        backend: _Backend,
        *,
        delivery_root: Path | None = None,
        provider_attempt_ceiling: int = 20,
        predecessor_replay_delivery_root: Path | None = None,
        cumulative_call_ceiling: int | None = 20,
        cumulative_calls_before: int | None = 0,
        cumulative_calls_remaining: int | None = 20,
    ) -> EngineeringReplayController:
        return EngineeringReplayController(
            source_config_path=self.source_config_path,
            delivery_root=delivery_root or self.delivery_root,
            predecessor_campaign_path=self.predecessor_path,
            provider_attempt_ceiling=provider_attempt_ceiling,
            predecessor_replay_delivery_root=(
                predecessor_replay_delivery_root
            ),
            cumulative_call_ceiling=cumulative_call_ceiling,
            cumulative_calls_before=cumulative_calls_before,
            cumulative_calls_remaining=cumulative_calls_remaining,
            backend=backend,
        )

    async def _terminal_predecessor_replay(self) -> Path:
        delivery_root = self.root / "predecessor-engineering-replay"
        result = LocalRecoveryRoundResult(
            status="technical_failed",
            started_at=NOW,
            completed_at=LATER,
            usage=RecoveryUsage(),
            receipts=(),
            reason_code="fixture_technical_failure",
        )
        state = await self._controller(
            _Backend(result),
            delivery_root=delivery_root,
            cumulative_call_ceiling=120,
            cumulative_calls_before=100,
            cumulative_calls_remaining=20,
        ).run()
        self.assertEqual(state.status, "technical_failed")
        self.assertEqual(state.usage_evidence_status, "exact")
        return delivery_root

    async def _terminal_v2_predecessor_replay(self) -> Path:
        first_root = await self._terminal_predecessor_replay()
        delivery_root = self.root / "predecessor-engineering-replay-v2"
        result = LocalRecoveryRoundResult(
            status="technical_failed",
            started_at=NOW,
            completed_at=LATER,
            usage=RecoveryUsage(),
            receipts=(),
            reason_code="fixture_v2_technical_failure",
        )
        state = await self._controller(
            _Backend(result),
            delivery_root=delivery_root,
            provider_attempt_ceiling=20,
            predecessor_replay_delivery_root=first_root,
            cumulative_call_ceiling=120,
            cumulative_calls_before=100,
            cumulative_calls_remaining=20,
        ).run()
        self.assertEqual(state.status, "technical_failed")
        self.assertEqual(state.usage_evidence_status, "exact")
        return delivery_root

    async def test_success_is_one_nonbenchmark_run_and_writes_delivery(self) -> None:
        backend = _Backend(self._completed_result())
        hard_report = HardMetricReport(
            report_id="hard-pass",
            case_id="seen-case-1",
            packet_id="engineering-packet",
            metrics=[],
            all_hard_gates_passed=True,
            created_at=NOW,
        )
        with (
            patch(
                "hypoweaver.engineering_replay.replay_ablations",
                return_value=_FaultReport(),
            ),
            patch(
                "hypoweaver.engineering_replay.evaluate_hard_metrics",
                return_value=hard_report,
            ),
        ):
            state = await self._controller(backend).run()

        self.assertEqual(state.status, "completed_hard_gate_passed")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(backend.contexts[0].call_limit, 20)
        protocol = json.loads(
            (self.delivery_root / "engineering-replay-protocol.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(protocol["official"])
        self.assertFalse(protocol["benchmark_eligible"])
        self.assertTrue(protocol["seen_case"])
        self.assertFalse(protocol["comparison_allowed"])
        self.assertEqual(protocol["max_runs"], 1)
        self.assertEqual(protocol["provider_attempt_ceiling"], 20)
        self.assertFalse(protocol["predecessor_usage_inherited"])
        self.assertNotIn("predecessor_binding", protocol["freeze"])
        for name in (
            "round-result.json",
            "benchmark-packet.json",
            "fault-ablation-report.json",
            "hard-metrics.json",
            "model-call-receipts.json",
            "resource-usage.json",
            "中文工程回放报告.md",
            "hash-manifest.json",
        ):
            self.assertTrue((self.delivery_root / name).is_file(), name)

    async def test_hard_metric_failure_is_terminal_without_second_run(self) -> None:
        backend = _Backend(self._completed_result())
        hard_report = HardMetricReport(
            report_id="hard-fail",
            case_id="seen-case-1",
            packet_id="engineering-packet",
            metrics=[],
            all_hard_gates_passed=False,
            created_at=NOW,
        )
        controller = self._controller(backend)
        with (
            patch(
                "hypoweaver.engineering_replay.replay_ablations",
                return_value=_FaultReport(),
            ),
            patch(
                "hypoweaver.engineering_replay.evaluate_hard_metrics",
                return_value=hard_report,
            ),
        ):
            first = await controller.run()
            second = await controller.run()

        self.assertEqual(first.status, "completed_hard_gate_failed")
        self.assertEqual(second, first)
        self.assertEqual(backend.calls, 1)

    async def test_exact_technical_failure_preserves_usage_and_receipts(self) -> None:
        receipt = RecoveryCallReceipt(
            call_id="engineering-failed-call",
            campaign_id="engineering-replay-test",
            round_id="engineering-run-01",
            phase="recovery_round",
            logical_slot_id="h1_h2:reviewer_report_batch:1",
            logical_call_id="reviewer-batch-1",
            call_group="h1_h2",
            prompt_key="reviewer_report_batch",
            prompt_version="1.0.0",
            attempt_type="transport_retry",
            attempt_index=3,
            max_attempts=3,
            outcome="transport_failure",
            provider="qwen",
            model="qwen-test",
            response_sha256="8" * 64,
            source_receipt_sha256="9" * 64,
            error_type="APIConnectionError",
            error_category="proxy",
            call_started_at=NOW,
            call_completed_at=LATER,
        )
        result = LocalRecoveryRoundResult(
            status="technical_failed",
            started_at=NOW,
            completed_at=LATER,
            usage=RecoveryUsage(
                llm_calls=1,
                technical_failures=("APIConnectionError",),
            ),
            receipts=(receipt,),
            reason_code="transport_failure",
        )
        backend = _Backend(result)

        state = await self._controller(backend).run()

        self.assertEqual(state.status, "technical_failed")
        self.assertEqual(state.usage_evidence_status, "exact")
        self.assertEqual(state.reason_code, "transport_failure")
        self.assertEqual(backend.calls, 1)
        receipts = json.loads(
            (self.delivery_root / "model-call-receipts.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipts["evidence_status"], "exact")
        self.assertEqual(len(receipts["receipts"]), 1)
        self.assertEqual(receipts["receipts"][0]["error_category"], "proxy")
        self.assertNotIn("raw_error", receipts["receipts"][0])
        self.assertNotIn("request_url", receipts["receipts"][0])

    async def test_backend_exception_invalidates_unknown_usage(self) -> None:
        backend = _Backend(error=RuntimeError("provider disconnected"))
        controller = self._controller(backend)

        first = await controller.run()
        second = await controller.run()

        self.assertEqual(first.status, "invalidated_unknown_usage")
        self.assertEqual(first.usage_evidence_status, "unknown")
        self.assertEqual(second, first)
        self.assertEqual(backend.calls, 1)

    async def test_interrupted_running_state_is_fail_closed_without_retry(self) -> None:
        backend = _Backend(error=_CrashSignal())
        controller = self._controller(backend)

        with self.assertRaises(_CrashSignal):
            await controller.run()
        state = await controller.run()

        self.assertEqual(state.status, "invalidated_unknown_usage")
        self.assertEqual(state.usage_evidence_status, "unknown")
        self.assertEqual(backend.calls, 1)
        self.assertTrue(
            (self.delivery_root / "invalidation-hash-manifest.json").is_file()
        )

    async def test_prepare_is_idempotent_and_does_not_touch_predecessor(self) -> None:
        backend = _Backend(self._completed_result())
        controller = EngineeringReplayController(
            source_config_path=self.source_config_path,
            delivery_root=self.delivery_root,
            predecessor_campaign_path=self.predecessor_path,
            backend=backend,
        )
        predecessor_before = self.predecessor_path.read_bytes()

        first = controller.prepare()
        second = controller.prepare()

        self.assertEqual(first, second)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.predecessor_path.read_bytes(), predecessor_before)
        self.assertEqual(
            first.predecessor_campaign_file_sha256,
            hashlib.sha256(predecessor_before).hexdigest(),
        )
        self.assertIsInstance(first, EngineeringReplayProtocol)
        self.assertNotIsInstance(first, EngineeringReplayProtocolV2)
        raw_protocol = json.loads(
            (self.delivery_root / "engineering-replay-protocol.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(raw_protocol["protocol_version"], 1)
        self.assertNotIn("budget_binding", raw_protocol)
        self.assertNotIn("predecessor_replay_binding", raw_protocol)
        EngineeringReplayProtocol.model_validate(raw_protocol)

    async def test_new_nonterminal_v1_blocks_before_provider(self) -> None:
        backend = _Backend(self._completed_result())
        controller = EngineeringReplayController(
            source_config_path=self.source_config_path,
            delivery_root=self.delivery_root,
            predecessor_campaign_path=self.predecessor_path,
            backend=backend,
        )
        controller.prepare()

        with self.assertRaisesRegex(
            ValueError,
            "requires a v2 cumulative budget binding",
        ):
            await controller.run()

        state = json.loads(
            (
                self.delivery_root.with_name(f"{self.delivery_root.name}-state")
                / "engineering-replay-state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["run_count"], 0)
        self.assertFalse(state["provider_call_started"])
        self.assertEqual(backend.calls, 0)

    async def test_legacy_v1_terminal_remains_readable(self) -> None:
        backend = _Backend(self._completed_result())
        controller = EngineeringReplayController(
            source_config_path=self.source_config_path,
            delivery_root=self.delivery_root,
            predecessor_campaign_path=self.predecessor_path,
            backend=backend,
        )
        controller.prepare()
        state_path = (
            self.delivery_root.with_name(f"{self.delivery_root.name}-state")
            / "engineering-replay-state.json"
        )
        legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
        legacy_state.update(
            {
                "status": "technical_failed",
                "run_count": 1,
                "provider_call_started": True,
                "usage_evidence_status": "exact",
                "usage": RecoveryUsage().model_dump(mode="json"),
                "reason_code": "legacy_v1_terminal_fixture",
                "updated_at": LATER,
                "state_sha256": None,
            }
        )
        legacy_state["state_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in legacy_state.items()
                if key != "state_sha256"
            }
        )
        state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

        state = await controller.run()

        self.assertEqual(state.status, "technical_failed")
        self.assertEqual(state.reason_code, "legacy_v1_terminal_fixture")
        self.assertIsInstance(controller.prepare(), EngineeringReplayProtocol)
        self.assertEqual(backend.calls, 0)

    async def test_v2_freezes_full_budget_and_predecessor(self) -> None:
        predecessor_root = await self._terminal_predecessor_replay()
        delivery_root = self.root / "engineering-replay-v2"
        backend = _Backend(self._completed_result())
        hard_report = HardMetricReport(
            report_id="hard-pass-v2",
            case_id="seen-case-1",
            packet_id="engineering-packet",
            metrics=[],
            all_hard_gates_passed=True,
            created_at=NOW,
        )
        controller = self._controller(
            backend,
            delivery_root=delivery_root,
            provider_attempt_ceiling=20,
            predecessor_replay_delivery_root=predecessor_root,
            cumulative_call_ceiling=120,
            cumulative_calls_before=100,
            cumulative_calls_remaining=20,
        )
        with (
            patch(
                "hypoweaver.engineering_replay.replay_ablations",
                return_value=_FaultReport(),
            ),
            patch(
                "hypoweaver.engineering_replay.evaluate_hard_metrics",
                return_value=hard_report,
            ),
        ):
            state = await controller.run()

        self.assertEqual(state.status, "completed_hard_gate_passed")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(backend.contexts[0].call_limit, 20)
        protocol = EngineeringReplayProtocolV2.model_validate_json(
            (delivery_root / "engineering-replay-protocol.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(protocol.provider_attempt_ceiling, 20)
        self.assertEqual(protocol.budget_binding.cumulative_call_ceiling, 120)
        self.assertEqual(protocol.budget_binding.cumulative_calls_before, 100)
        self.assertEqual(protocol.budget_binding.cumulative_calls_remaining, 20)
        self.assertIsNotNone(protocol.predecessor_replay_binding)
        self.assertEqual(
            protocol.predecessor_replay_binding.delivery_root,
            str(predecessor_root.resolve()),
        )
        self.assertIsNone(protocol.freeze.predecessor_binding)
        usage = json.loads(
            (delivery_root / "resource-usage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(usage["provider_attempt_ceiling"], 20)

    async def test_v2_insufficient_budget_blocks_before_provider(self) -> None:
        predecessor_root = await self._terminal_predecessor_replay()
        cases = (
            ("ceiling", 19, 100, 30),
            ("remaining", 19, 100, 19),
        )
        for name, provider_ceiling, calls_before, calls_remaining in cases:
            with self.subTest(name=name):
                delivery_root = self.root / f"engineering-replay-v2-{name}"
                backend = _Backend(self._completed_result())
                controller = self._controller(
                    backend,
                    delivery_root=delivery_root,
                    provider_attempt_ceiling=provider_ceiling,
                    predecessor_replay_delivery_root=predecessor_root,
                    cumulative_call_ceiling=calls_before + calls_remaining,
                    cumulative_calls_before=calls_before,
                    cumulative_calls_remaining=calls_remaining,
                )
                controller.prepare()

                with self.assertRaisesRegex(
                    ValueError,
                    "requires a full 20-attempt provider budget",
                ):
                    await controller.run()

                state = json.loads(
                    (
                        delivery_root.with_name(f"{delivery_root.name}-state")
                        / "engineering-replay-state.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(state["status"], "prepared")
                self.assertEqual(state["run_count"], 0)
                self.assertFalse(state["provider_call_started"])
                self.assertEqual(backend.calls, 0)

    async def test_legacy_low_budget_v2_terminal_remains_readable(self) -> None:
        predecessor_root = await self._terminal_predecessor_replay()
        delivery_root = self.root / "legacy-low-budget-v2"
        backend = _Backend(self._completed_result())
        controller = self._controller(
            backend,
            delivery_root=delivery_root,
            provider_attempt_ceiling=12,
            predecessor_replay_delivery_root=predecessor_root,
            cumulative_call_ceiling=112,
            cumulative_calls_before=100,
            cumulative_calls_remaining=12,
        )
        controller.prepare()
        state_path = (
            delivery_root.with_name(f"{delivery_root.name}-state")
            / "engineering-replay-state.json"
        )
        legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
        legacy_state.update(
            {
                "status": "technical_failed",
                "run_count": 1,
                "provider_call_started": True,
                "usage_evidence_status": "exact",
                "usage": RecoveryUsage().model_dump(mode="json"),
                "reason_code": "legacy_terminal_fixture",
                "updated_at": LATER,
                "state_sha256": None,
            }
        )
        legacy_state["state_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in legacy_state.items()
                if key != "state_sha256"
            }
        )
        state_path.write_text(
            json.dumps(legacy_state),
            encoding="utf-8",
        )

        state = await controller.run()

        self.assertEqual(state.status, "technical_failed")
        self.assertEqual(state.reason_code, "legacy_terminal_fixture")
        self.assertEqual(state.provider_attempt_ceiling, 12)
        self.assertEqual(backend.calls, 0)

    async def test_predecessor_replay_tampering_blocks_before_provider(self) -> None:
        predecessor_root = await self._terminal_predecessor_replay()
        predecessor_state_root = predecessor_root.with_name(
            f"{predecessor_root.name}-state"
        )
        protected_paths = (
            predecessor_root / "engineering-replay-protocol.json",
            predecessor_state_root / "engineering-replay-state.json",
            predecessor_root / "hash-manifest.json",
            predecessor_root / "resource-usage.json",
            predecessor_root / "model-call-receipts.json",
        )
        for index, protected_path in enumerate(protected_paths):
            with self.subTest(path=protected_path.name):
                backend = _Backend(self._completed_result())
                controller = self._controller(
                    backend,
                    delivery_root=self.root / f"engineering-replay-v2-{index}",
                    provider_attempt_ceiling=20,
                    predecessor_replay_delivery_root=predecessor_root,
                    cumulative_call_ceiling=120,
                    cumulative_calls_before=100,
                    cumulative_calls_remaining=20,
                )
                controller.prepare()
                original = protected_path.read_bytes()
                try:
                    protected_path.write_bytes(original + b"\n")
                    with self.assertRaises(ValueError):
                        await controller.run()
                    self.assertEqual(backend.calls, 0)
                finally:
                    protected_path.write_bytes(original)

    async def test_v2_roots_cannot_overlap_predecessor_replay(self) -> None:
        predecessor_root = await self._terminal_predecessor_replay()
        backend = _Backend(self._completed_result())
        controller = self._controller(
            backend,
            delivery_root=predecessor_root / "nested-replay",
            provider_attempt_ceiling=20,
            predecessor_replay_delivery_root=predecessor_root,
            cumulative_call_ceiling=120,
            cumulative_calls_before=100,
            cumulative_calls_remaining=20,
        )

        with self.assertRaisesRegex(ValueError, "overlap predecessor roots"):
            controller.prepare()
        self.assertEqual(backend.calls, 0)

    def test_v2_budget_must_be_additive_and_fit_provider_ceiling(self) -> None:
        with self.assertRaises(ValueError):
            self._controller(
                _Backend(),
                provider_attempt_ceiling=12,
                cumulative_call_ceiling=120,
                cumulative_calls_before=108,
                cumulative_calls_remaining=11,
            )
        with self.assertRaisesRegex(
            ValueError,
            "provider attempt ceiling exceeds cumulative calls remaining",
        ):
            self._controller(
                _Backend(),
                provider_attempt_ceiling=13,
                cumulative_call_ceiling=120,
                cumulative_calls_before=108,
                cumulative_calls_remaining=12,
            )

    async def test_v2_successor_budget_continues_predecessor_exact_usage(self) -> None:
        predecessor_root = await self._terminal_v2_predecessor_replay()
        backend = _Backend(self._completed_result())
        controller = self._controller(
            backend,
            delivery_root=self.root / "engineering-replay-v2-successor",
            provider_attempt_ceiling=12,
            predecessor_replay_delivery_root=predecessor_root,
            cumulative_call_ceiling=120,
            cumulative_calls_before=1,
            cumulative_calls_remaining=119,
        )

        with self.assertRaisesRegex(
            ValueError,
            "successor cumulative calls before does not continue predecessor usage",
        ):
            controller.prepare()
        self.assertEqual(backend.calls, 0)

    async def test_source_drift_invalidates_before_backend_call(self) -> None:
        backend = _Backend(self._completed_result())
        controller = self._controller(backend)
        controller.prepare()
        self.harness_source.write_text("fixture = 'drifted'\n", encoding="utf-8")

        state = await controller.run()

        self.assertEqual(state.status, "invalidated_source_drift")
        self.assertEqual(state.usage_evidence_status, "not_started")
        self.assertEqual(backend.calls, 0)

    async def test_missing_predecessor_after_freeze_invalidates_on_restart(self) -> None:
        backend = _Backend(self._completed_result())
        self._controller(backend).prepare()
        self.predecessor_path.unlink()
        restarted = EngineeringReplayController(
            source_config_path=self.source_config_path,
            delivery_root=self.delivery_root,
            predecessor_campaign_path=self.predecessor_path,
            cumulative_call_ceiling=20,
            cumulative_calls_before=0,
            cumulative_calls_remaining=20,
            backend=backend,
        )

        state = await restarted.run()

        self.assertEqual(state.status, "invalidated_source_drift")
        self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()
