from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from hypoweaver.benchmark_evaluator import (
    seal_benchmark_packet,
    summarize_paired_reviews,
)
from hypoweaver.benchmark_models import (
    ABLATION_IDS,
    FAULT_IDS,
    AblationReplayResult,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    FaultOutcome,
    FaultReplayReport,
    FrozenBenchmarkProtocol,
    HardMetric,
    HardMetricReport,
    NeurIPSRatings,
    NeurIPSReview,
    NormalizedDesign,
    OfficialAttemptBinding,
    PairedBlindCallReceipt,
    PairedEvaluationView,
    PairedReviewSummary,
)
from hypoweaver.benchmark_protocol import official_holdout_lock_id, seal_protocol
from hypoweaver.claim_gate import validate_h3_claim_decision
from hypoweaver.local_recovery_runner import (
    LocalRecoveryComparisonContext,
    LocalRecoveryComparisonResult,
    LocalRecoveryRoundContext,
    LocalRecoveryRoundResult,
    LocalRecoveryRunner,
)
from hypoweaver.models import (
    ClaimRecord,
    CreateRunRequest,
    ModelCallReceipt,
    RunState,
)
from hypoweaver.production_recovery_backend import (
    ProductionRecoveryBackend,
    _recovery_claim_decision,
    _usage_from_model_receipts,
    assert_recovery_paths_separate,
    load_recovery_source_configuration,
    recovery_receipts_from_agent_usage,
)
from hypoweaver.recovery_campaign import (
    RecoveryCampaignStore,
    build_recovery_freeze,
    canonical_recovery_campaign_path,
    create_recovery_call_receipt,
    create_recovery_campaign,
    cumulative_llm_calls,
    import_prior_usage_from_ledger,
    invalidate_recovery_campaign,
    map_model_call_receipts,
)
from hypoweaver.recovery_identity import FIRST_ROUND_LOGICAL_SLOTS
from hypoweaver.recovery_models import (
    HARD_METRIC_IDS,
    RecoveryCampaign,
    RecoveryRoundSubmission,
    RecoveryUsage,
)
from hypoweaver.recovery_run_cli import (
    PreparedRecoveryConfiguration,
    _import_failed_source_usage,
    prepare_recovery_campaign,
    run_prepared_recovery,
    verify_recovery_delivery_manifest,
    write_recovery_delivery,
)
from hypoweaver.repository import RunRepository
from hypoweaver.seal import canonical_sha256


NOW = "2026-07-16T00:00:00+00:00"
LATER = "2026-07-16T00:01:00+00:00"


class RecoveryClaimDecisionTests(unittest.TestCase):
    @staticmethod
    def _claim(
        claim_id: str,
        *,
        admission_status: str,
        allowed_strength: str,
        max_allowed_strength: str,
    ) -> ClaimRecord:
        return ClaimRecord.model_validate(
            {
                "claim_id": claim_id,
                "hypothesis_id": claim_id,
                "claim_text": "核心解释变量与结果变量存在关联。",
                "evidence_status": "inconclusive",
                "allowed_strength": allowed_strength,
                "supporting_runs": [],
                "opposing_runs": [],
                "scope": "frozen enterprise panel",
                "robustness_status": "code gated",
                "unresolved_risks": [],
                "claim_type": "associational",
                "admission_status": admission_status,
                "max_allowed_strength": max_allowed_strength,
            }
        )

    def test_replay03_four_claim_matrix_never_upgrades_insufficient_claim(self) -> None:
        matrix = (
            ("H1", "downgrade_required", "preliminary", "preliminary", "downgrade"),
            ("H2", "downgrade_required", "insufficient", "preliminary", "reject"),
            ("H3", "rejected", "prohibited", "prohibited", "reject"),
            ("H4", "prohibited", "prohibited", "prohibited", "reject"),
        )
        for (
            claim_id,
            admission_status,
            allowed_strength,
            max_allowed_strength,
            expected_decision,
        ) in matrix:
            with self.subTest(claim_id=claim_id):
                claim = self._claim(
                    claim_id,
                    admission_status=admission_status,
                    allowed_strength=allowed_strength,
                    max_allowed_strength=max_allowed_strength,
                )
                result = _recovery_claim_decision(claim)

                self.assertEqual(result.decision, expected_decision)
                if result.decision in {"approve", "downgrade"}:
                    validate_h3_claim_decision(
                        claim,
                        result.decision,
                        result.final_text,
                    )
                else:
                    self.assertIsNone(result.final_text)

    def test_recovery_text_uses_tighter_allowed_and_max_strength(self) -> None:
        for claim_id, allowed_strength, max_allowed_strength in (
            ("allowed-preliminary", "preliminary", "associational"),
            ("max-preliminary", "associational", "preliminary"),
        ):
            with self.subTest(claim_id=claim_id):
                claim = self._claim(
                    claim_id,
                    admission_status="admitted",
                    allowed_strength=allowed_strength,
                    max_allowed_strength=max_allowed_strength,
                )
                result = _recovery_claim_decision(claim)

                self.assertEqual(result.decision, "approve")
                self.assertRegex(result.final_text or "", "初步|有限")
                validate_h3_claim_decision(
                    claim,
                    result.decision,
                    result.final_text,
                )

    def test_mixed_recovery_text_passes_shared_h3_validator(self) -> None:
        claim = self._claim(
            "mixed-claim",
            admission_status="downgrade_required",
            allowed_strength="mixed",
            max_allowed_strength="associational",
        )

        result = _recovery_claim_decision(claim)

        self.assertEqual(result.decision, "downgrade")
        self.assertRegex(result.final_text or "", "混合|不一致")
        validate_h3_claim_decision(
            claim,
            result.decision,
            result.final_text,
        )


class ProductionRecoveryBackendTests(unittest.IsolatedAsyncioTestCase):
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
        self.protocol_path = self.source_root / "protocol.json"
        self.hypo_source = self.source_root / "sources" / "hypo.py"
        self.agent_source = self.source_root / "sources" / "agent.py"
        self.harness_source = self.source_root / "sources" / "harness.py"
        self.freeze_config = self.source_root / "config" / "recovery.json"
        for path in (
            self.hypo_source,
            self.agent_source,
            self.harness_source,
            self.freeze_config,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture = {path.name!r}\n", encoding="utf-8")
        (self.source_root / "agent-lab").mkdir()
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
                        {"name": "y", "role": "outcome"},
                        {"name": "x", "role": "exposure"},
                    ],
                },
            }
        )
        self.visible_path.write_text(request.model_dump_json(), encoding="utf-8")
        self.data_path.write_text("firm,year,x,y\n1,2024,1,2\n", encoding="utf-8")
        self.summary_path.write_text("sealed comparison summary\n", encoding="utf-8")
        self.runtime_public_path.write_text("{}\n", encoding="utf-8")
        visible_sha = _file_sha256(self.visible_path)
        data_sha = _file_sha256(self.data_path)
        self.reference = BenchmarkReference(
            case_id="seen-case-1",
            visible_input_sha256=visible_sha,
            data_sha256=[data_sha],
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
                visible_input_sha256=visible_sha,
                data_sha256=[data_sha],
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
                configuration_artifact_paths=["config/recovery.json"],
                frozen_at=NOW,
            )
        )
        self.protocol_path.write_text(
            self.protocol.model_dump_json(),
            encoding="utf-8",
        )
        self.freeze = build_recovery_freeze(
            self.protocol,
            artifact_root=self.source_root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            frozen_at=NOW,
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
                    "protocol_path": "protocol.json",
                    "visible_input_path": "visible.json",
                    "reference_path": "reference.json",
                    "reference_summary_path": "reference-summary.txt",
                    "runtime_public_path": "runtime-public.json",
                    "source_artifact_paths": self.protocol.source_artifact_paths,
                    "configuration_artifact_paths": (
                        self.protocol.configuration_artifact_paths
                    ),
                    "output_dir": str(self.formal_output),
                    "working_dir": str(self.formal_work),
                    "official_state_root": str(self.formal_state),
                    "agent_laboratory_root": "agent-lab",
                    "agent_timeout_seconds": 1,
                    "poll_interval_seconds": 0.001,
                }
            ),
            encoding="utf-8",
        )
        self.recovery_work = self.root / "recovery-work"
        self.recovery_delivery = self.root / "recovery-delivery"
        self.recovery_state = self.root / "recovery-state"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _backend(self, **kwargs) -> ProductionRecoveryBackend:
        return ProductionRecoveryBackend(
            source_config_path=self.source_config_path,
            protocol=self.protocol,
            working_root=self.recovery_work,
            delivery_root=self.recovery_delivery,
            state_root=self.recovery_state,
            **kwargs,
        )

    def _round_context(self) -> LocalRecoveryRoundContext:
        return LocalRecoveryRoundContext(
            campaign_id="campaign-test",
            reservation_id="reservation-test",
            round_id="round-01",
            call_limit=20,
            freeze=self.freeze,
        )

    def _comparison_context(self) -> LocalRecoveryComparisonContext:
        return LocalRecoveryComparisonContext(
            campaign_id="campaign-test",
            reservation_id="comparison-reservation",
            freeze=self.freeze,
        )

    def _write_complete_comparison_stages(
        self,
        backend: ProductionRecoveryBackend,
        context: LocalRecoveryComparisonContext,
        qualified: BenchmarkPacket,
        *,
        tamper_qwen_packet: bool = False,
    ) -> None:
        root = backend._comparison_root(context)
        root.mkdir(parents=True, exist_ok=True)
        self.runtime_public_path.write_text(
            json.dumps({"qwen_review_model": "qwen-test"}),
            encoding="utf-8",
        )
        qwen = _packet("qwen_single_pass", "qwen-sealed", self.freeze, 1)
        qwen_usage = RecoveryUsage(llm_calls=1)
        qwen_receipt = _recovery_receipt(context, "qwen_single_pass", 1)
        qwen_payload = {
            "packet": qwen.model_dump(mode="json"),
            "usage": qwen_usage.model_dump(mode="json"),
            "receipt": qwen_receipt.model_dump(mode="json"),
        }
        if tamper_qwen_packet:
            qwen_payload["packet"]["packet_id"] = "tampered-without-resealing"
        (root / "qwen-stage.json").write_text(
            json.dumps(qwen_payload),
            encoding="utf-8",
        )

        agent = _packet("agent_laboratory", "agent-sealed", self.freeze, 1)
        agent_usage = RecoveryUsage(llm_calls=1)
        agent_receipt = _recovery_receipt(context, "agent_laboratory", 1)
        (root / "agent-stage.json").write_text(
            json.dumps(
                {
                    "packet": agent.model_dump(mode="json"),
                    "usage": agent_usage.model_dump(mode="json"),
                    "receipts": [agent_receipt.model_dump(mode="json")],
                }
            ),
            encoding="utf-8",
        )

        summary = _blind_summary(self.freeze, qualified, agent)
        view = PairedEvaluationView(
            id="paired-sealed",
            case_id=self.freeze.case_id,
            packet_a_id=qualified.packet_id,
            packet_b_id=agent.packet_id,
            status="completed",
            sealed_label_orders=list(self.freeze.sealed_label_orders),
            sealed_system_assignments=list(
                self.freeze.sealed_system_assignments
            ),
            review_resource_usage=[
                item.resource_usage for item in summary.reviews
            ],
            review_call_receipts=[
                item.call_receipt
                for item in summary.reviews
                if item.call_receipt is not None
            ],
            receipt_count=5,
            result=summary,
            created_at=NOW,
            updated_at=LATER,
        )
        blind_usage = RecoveryUsage(llm_calls=5)
        blind_receipts = [
            _mapped_blind_receipt(context, item.call_receipt)
            for item in summary.reviews
        ]
        (root / "blind-stage.json").write_text(
            json.dumps(
                {
                    "view": view.model_dump(mode="json"),
                    "usage": blind_usage.model_dump(mode="json"),
                    "receipts": [
                        item.model_dump(mode="json") for item in blind_receipts
                    ],
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _sensitive_validation_error() -> ValidationError:
        try:
            RecoveryUsage.model_validate(
                {"llm_calls": {"secret": "must-not-be-persisted"}}
            )
        except ValidationError as error:
            return error
        raise AssertionError("fixture must raise ValidationError")

    async def test_round_maps_exact_nine_slots_and_resume_does_not_repeat(self) -> None:
        calls = 0

        async def driver(context):
            nonlocal calls
            calls += 1
            return (
                _packet("hypoweaver", "round-packet", self.freeze, 9),
                _model_receipts(context.round_id),
                None,
            )

        backend = self._backend(test_mode=True, round_driver=driver)
        first = await backend.run_hypoweaver_round(self._round_context())
        resumed = await backend.run_hypoweaver_round(self._round_context())

        self.assertEqual(first.status, "completed")
        self.assertEqual(first.usage.llm_calls, 9)
        self.assertEqual(len(first.receipts), 9)
        self.assertEqual(
            {item.logical_slot_id for item in first.receipts},
            set(FIRST_ROUND_LOGICAL_SLOTS),
        )
        self.assertEqual(resumed, first)
        self.assertEqual(calls, 1)

    async def test_round_partial_failure_keeps_only_real_receipts(self) -> None:
        async def driver(context):
            return None, _model_receipts(context.round_id)[:4], "schema_failure"

        result = await self._backend(
            test_mode=True,
            round_driver=driver,
        ).run_hypoweaver_round(self._round_context())

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.usage.llm_calls, 4)
        self.assertEqual(len(result.receipts), 4)
        self.assertEqual(result.reason_code, "schema_failure")

    def test_agent_receipt_copy_keeps_only_sanitized_error_category(self) -> None:
        usage = {
            "llm_calls": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "call_receipts": [
                {
                    "status": "failed",
                    "provider": "qwen",
                    "model": "qwen-test",
                    "response_sha256": "a" * 64,
                    "started_at": NOW,
                    "completed_at": LATER,
                    "error_category": "connection_reset",
                }
            ],
        }

        receipts = recovery_receipts_from_agent_usage(
            usage,
            campaign_id="campaign-test",
            round_id="comparison-01",
        )

        self.assertEqual(receipts[0].error_category, "connection_reset")
        payload = receipts[0].model_dump(mode="json")
        self.assertNotIn("raw_error", payload)
        self.assertNotIn("request_url", payload)

    async def test_internal_runtime_error_preserves_seven_receipt_usage(self) -> None:
        receipts = _model_receipts("round-01")[:7]
        receipts[0] = receipts[0].model_copy(
            update={
                "outcome": "schema_failure",
                "error_type": "ValidationError",
                "error_category": "schema",
            }
        )
        for index, category in ((1, "dns"), (2, "read_timeout")):
            receipts[index] = receipts[index].model_copy(
                update={
                    "outcome": "transport_failure",
                    "error_type": "APIConnectionError",
                    "error_category": category,
                }
            )

        async def driver(_context):
            return None, receipts, "schema_failure"

        usage_calls = 0

        def fail_once(values):
            nonlocal usage_calls
            usage_calls += 1
            if usage_calls == 1:
                raise RuntimeError("internal orchestration failure")
            return _usage_from_model_receipts(values)

        store, runner, context = self._terminal_round_harness()
        with patch(
            "hypoweaver.production_recovery_backend._usage_from_model_receipts",
            side_effect=fail_once,
        ):
            result = await self._backend(
                test_mode=True,
                round_driver=driver,
            ).run_hypoweaver_round(context)

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.reason_code, "round_RuntimeError")
        self.assertEqual(result.usage.llm_calls, 7)
        self.assertEqual(
            result.usage.technical_failures,
            ("ValidationError", "APIConnectionError", "APIConnectionError"),
        )
        self.assertEqual(
            [item.error_category for item in result.receipts[:3]],
            ["schema", "dns", "read_timeout"],
        )
        with patch.object(runner, "_verify_frozen_inputs", return_value=None):
            campaign, _ = runner._record_round(store.load(), context, result)

        self.assertEqual(campaign.status, "open", campaign.status_reason)
        self.assertEqual(campaign.rounds[0].status, "technical_failed")
        self.assertEqual(
            [item.error_category for item in campaign.rounds[0].receipts[:3]],
            ["schema", "dns", "read_timeout"],
        )
        self.assertEqual(campaign.rounds[0].usage.llm_calls, 7)
        self.assertNotEqual(
            campaign.status_reason,
            "round_terminal_usage_evidence_invalid",
        )

    async def test_internal_runtime_error_without_receipts_has_zero_usage(self) -> None:
        async def driver(_context):
            raise RuntimeError("internal orchestration failure before provider call")

        store, runner, context = self._terminal_round_harness()
        result = await self._backend(
            test_mode=True,
            round_driver=driver,
        ).run_hypoweaver_round(context)

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.reason_code, "round_RuntimeError")
        self.assertEqual(result.usage.llm_calls, 0)
        self.assertEqual(result.usage.technical_failures, ())
        self.assertEqual(result.receipts, ())
        with patch.object(runner, "_verify_frozen_inputs", return_value=None):
            campaign, _ = runner._record_round(store.load(), context, result)

        self.assertEqual(campaign.status, "open", campaign.status_reason)
        self.assertEqual(campaign.rounds[0].status, "technical_failed")
        self.assertEqual(campaign.rounds[0].usage.llm_calls, 0)
        self.assertNotEqual(
            campaign.status_reason,
            "round_terminal_usage_evidence_invalid",
        )

    async def test_exact_ten_database_receipts_produce_terminal_round_result(self) -> None:
        backend = self._backend()
        store, runner, context = self._terminal_round_harness()
        receipts = _model_receipts(context.round_id)
        reviewer_index = next(
            index
            for index, receipt in enumerate(receipts)
            if receipt.prompt_key == "reviewer_report_batch"
        )
        receipts[reviewer_index] = receipts[reviewer_index].model_copy(
            update={
                "outcome": "transport_failure",
                "error_type": "APIConnectionError",
                "provider_response_id_sha256": None,
                "response_sha256": "e" * 64,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        )
        receipts.append(
            receipts[reviewer_index].model_copy(
                update={
                    "call_id": f"{context.round_id}-call-10",
                    "attempt_index": 2,
                    "attempt_type": "transport_retry",
                    "started_at": _timestamp(20),
                    "completed_at": _timestamp(21),
                    "response_sha256": "f" * 64,
                }
            )
        )
        usage_payload = {
            "llm_calls": len(receipts),
            "input_tokens": sum(item.input_tokens for item in receipts),
            "output_tokens": sum(item.output_tokens for item in receipts),
            "wall_time_seconds": 10,
            "technical_failures": [
                str(item.error_type)
                for item in receipts
                if item.error_type is not None
            ],
            "call_receipts": [
                item.model_dump(mode="json") for item in receipts
            ],
        }

        async def fail_after_persisting_receipts(_context, _frozen_runtime):
            request = CreateRunRequest.model_validate_json(
                self.visible_path.read_text(encoding="utf-8")
            )
            assert request.case is not None
            repository = RunRepository(
                backend._round_root(context) / "hypoweaver.db"
            )
            repository.create(
                RunState(
                    id="exact-terminal-source-run",
                    case_id=request.case.case_id,
                    case_name=request.case.title,
                    mode="research",
                    model_provider="qwen",
                    execution_mode="external",
                    status="failed",
                    case_submission=request.case,
                    artifacts={
                        "model_usage": {
                            "artifact_id": "exact-terminal-source-run:model_usage",
                            "kind": "model_usage",
                            "sha256": canonical_sha256(usage_payload),
                            "payload": usage_payload,
                        }
                    },
                    last_error="fixture orchestration failure",
                )
            )
            raise RuntimeError("fixture orchestration failure")

        with (
            patch.object(
                backend,
                "preflight",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                backend,
                "_execute_round",
                new=AsyncMock(side_effect=fail_after_persisting_receipts),
            ),
        ):
            result = await backend.run_hypoweaver_round(context)

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.reason_code, "round_RuntimeError")
        self.assertEqual(result.usage.llm_calls, 10)
        self.assertEqual(len(result.receipts), 10)
        self.assertTrue(
            (backend._round_root(context) / "round-result.json").is_file()
        )
        with patch.object(runner, "_verify_frozen_inputs", return_value=None):
            campaign, _ = runner._record_round(store.load(), context, result)
        self.assertEqual(campaign.rounds[0].status, "technical_failed")
        self.assertIsNone(campaign.invalidation)

    async def test_unknown_round_accounting_invalidates_with_full_reserve(self) -> None:
        backend = self._backend()
        store = self._open_store()
        runner = LocalRecoveryRunner(
            store=store,
            backend=backend,
            protocol=self.protocol,
            reference=self.reference,
            source_artifact_root=self.source_root,
            delivery_root=self.recovery_delivery,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            protected_official_roots=(
                self.formal_output,
                self.formal_work,
                self.formal_state,
            ),
        )
        with (
            patch.object(runner, "_verify_frozen_inputs", return_value=None),
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch.object(
                backend,
                "_execute_round",
                new=AsyncMock(side_effect=RuntimeError("receipt not durable")),
            ),
        ):
            campaign = await runner.run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.rounds, ())
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 20)
        self.assertEqual(cumulative_llm_calls(campaign), 42)

    async def test_orphaned_round_marker_never_restarts_model_work(self) -> None:
        backend = self._backend()
        context = self._round_context()
        marker = backend._round_root(context) / "round-started.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")
        preflight = AsyncMock(return_value=object())
        execute = AsyncMock()
        with (
            patch.object(backend, "preflight", new=preflight),
            patch.object(backend, "_execute_round", new=execute),
            self.assertRaisesRegex(RuntimeError, "unknown call evidence"),
        ):
            await backend.run_hypoweaver_round(context)
        preflight.assert_not_awaited()
        execute.assert_not_awaited()

    async def test_comparison_driver_records_one_agent_and_five_blind_calls_once(self) -> None:
        calls = 0

        async def driver(context, qualified_packet, reference_summary):
            nonlocal calls
            calls += 1
            agent = _packet("agent_laboratory", "agent-packet", self.freeze, 1)
            qwen = _packet("qwen_single_pass", "qwen-packet", self.freeze, 1)
            blind = _blind_summary(self.freeze, qualified_packet, agent)
            receipts = (
                _recovery_receipt(context, "qwen_single_pass", 1),
                _recovery_receipt(context, "agent_laboratory", 1),
                *(
                    _recovery_receipt(context, "blind_review", index)
                    for index in range(1, 6)
                ),
            )
            return LocalRecoveryComparisonResult(
                status="completed",
                qwen_single_pass=RecoveryUsage(llm_calls=1),
                agent_laboratory=RecoveryUsage(llm_calls=1),
                blind_reviews=RecoveryUsage(llm_calls=5),
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                qwen_packet=qwen,
                agent_laboratory_packet=agent,
                blind_summary=blind,
            )

        backend = self._backend(test_mode=True, comparison_driver=driver)
        context = self._comparison_context()
        qualified = _packet("hypoweaver", "qualified", self.freeze, 9)
        result = await backend.run_comparison(context, qualified, "summary")
        resumed = await backend.run_comparison(context, qualified, "summary")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.receipts), 7)
        self.assertEqual(result.qwen_single_pass.llm_calls, 1)
        self.assertEqual(result.agent_laboratory.llm_calls, 1)
        self.assertEqual(result.blind_reviews.llm_calls, 5)
        self.assertEqual(resumed, result)
        self.assertEqual(calls, 1)

    async def test_complete_sealed_stages_recover_late_validation_error(self) -> None:
        backend = self._backend()
        context = self._comparison_context()
        qualified = _packet("hypoweaver", "qualified-sealed", self.freeze, 9)
        self._write_complete_comparison_stages(
            backend,
            context,
            qualified,
        )
        late_error = self._sensitive_validation_error()

        with (
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch.object(
                backend,
                "_run_blind_reviews",
                new=AsyncMock(side_effect=late_error),
            ),
        ):
            result = await backend.run_comparison(context, qualified, "summary")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.receipts), 7)
        self.assertEqual(result.qwen_single_pass.llm_calls, 1)
        self.assertEqual(result.agent_laboratory.llm_calls, 1)
        self.assertEqual(result.blind_reviews.llm_calls, 5)
        resumed = await backend.run_comparison(context, qualified, "summary")
        self.assertEqual(resumed, result)

    async def test_tampered_complete_stage_cannot_recover_as_completed(self) -> None:
        backend = self._backend()
        context = self._comparison_context()
        qualified = _packet("hypoweaver", "qualified-tamper", self.freeze, 9)
        self._write_complete_comparison_stages(
            backend,
            context,
            qualified,
            tamper_qwen_packet=True,
        )

        with (
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch.object(
                backend,
                "_run_blind_reviews",
                new=AsyncMock(side_effect=self._sensitive_validation_error()),
            ),
        ):
            result = await backend.run_comparison(context, qualified, "summary")

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.reason_code, "comparison_ValidationError")
        self.assertEqual(len(result.receipts), 7)

    async def test_incomplete_stages_cannot_recover_as_completed(self) -> None:
        backend = self._backend()
        context = self._comparison_context()
        qualified = _packet("hypoweaver", "qualified-incomplete", self.freeze, 9)
        self._write_complete_comparison_stages(backend, context, qualified)
        (backend._comparison_root(context) / "blind-stage.json").unlink()

        with (
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch.object(
                backend,
                "_run_blind_reviews",
                new=AsyncMock(side_effect=self._sensitive_validation_error()),
            ),
        ):
            result = await backend.run_comparison(context, qualified, "summary")

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(len(result.receipts), 2)

    def test_comparison_error_record_excludes_messages_and_inputs(self) -> None:
        backend = self._backend()
        context = self._comparison_context()
        error = self._sensitive_validation_error()
        self.assertIn("must-not-be-persisted", str(error.errors()))

        backend._write_comparison_assembly_error(context, error)

        path = (
            backend._comparison_root(context) / "comparison-assembly-error.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        rendered = json.dumps(payload)
        self.assertEqual(payload["error_type"], "ValidationError")
        self.assertEqual(
            set(payload["validation_errors"][0]),
            {"loc", "type"},
        )
        self.assertNotIn("must-not-be-persisted", rendered)
        self.assertNotIn("input", rendered)
        self.assertNotIn("msg", rendered)

    async def test_completed_delivery_indexes_three_packets_and_five_reviews(self) -> None:
        store, qualified = self._qualified_store()

        async def driver(context, qualified_packet, reference_summary):
            agent = _packet("agent_laboratory", "agent-delivery", self.freeze, 1)
            qwen = _packet("qwen_single_pass", "qwen-delivery", self.freeze, 1)
            blind = _blind_summary(self.freeze, qualified_packet, agent)
            blind_receipts = tuple(
                _mapped_blind_receipt(context, review.call_receipt)
                for review in blind.reviews
            )
            return LocalRecoveryComparisonResult(
                status="completed",
                qwen_single_pass=RecoveryUsage(llm_calls=1),
                agent_laboratory=RecoveryUsage(llm_calls=1),
                blind_reviews=RecoveryUsage(llm_calls=5),
                receipts=(
                    _recovery_receipt(context, "qwen_single_pass", 1),
                    _recovery_receipt(context, "agent_laboratory", 1),
                    *blind_receipts,
                ),
                started_at=NOW,
                completed_at=LATER,
                qwen_packet=qwen,
                agent_laboratory_packet=agent,
                blind_summary=blind,
            )

        backend = self._backend(test_mode=True, comparison_driver=driver)
        runner = LocalRecoveryRunner(
            store=store,
            backend=backend,
            protocol=self.protocol,
            reference=self.reference,
            source_artifact_root=self.source_root,
            delivery_root=self.recovery_delivery,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            protected_official_roots=(
                self.formal_output,
                self.formal_work,
                self.formal_state,
            ),
        )
        runner._write_round_artifacts(
            store.load(),
            "round-01",
            qualified,
            _hard_report(qualified).model_dump(mode="json"),
            _replay(qualified).model_dump(mode="json"),
        )
        with (
            patch.object(runner, "_verify_frozen_inputs", return_value=None),
            patch(
                "hypoweaver.benchmark_protocol.replay_ablations",
                return_value=_replay(qualified),
            ),
            patch(
                "hypoweaver.benchmark_protocol.evaluate_hard_metrics",
                side_effect=lambda packet, *args, **kwargs: _hard_report(packet),
            ),
        ):
            campaign = await runner._run_comparison(store.load(), qualified)
        self.assertEqual(campaign.comparison.status, "completed")

        write_recovery_delivery(campaign, delivery_root=self.recovery_delivery)
        index = json.loads(
            (self.recovery_delivery / "recovery-artifact-index.json").read_text(
                encoding="utf-8"
            )
        )
        comparison = index["comparison"]
        self.assertEqual(set(comparison["files"]["neutral_packets"]), {
            "qwen_single_pass",
            "agent_laboratory",
            "hypoweaver",
        })
        self.assertEqual(len(comparison["files"]["blind_reviews"]), 5)
        self.assertTrue(
            all(
                (self.recovery_delivery / relative).is_file()
                for relative in comparison["files"]["blind_reviews"]
            )
        )
        report = (
            self.recovery_delivery / "中文恢复评测报告.md"
        ).read_text(encoding="utf-8")
        self.assertIn("不声明“科学可靠性更高”", report)
        delivery_manifest = verify_recovery_delivery_manifest(
            self.recovery_delivery,
            expected_campaign=campaign,
        )
        sealed_files = set(delivery_manifest["file_sha256"])
        self.assertIn(
            f"{campaign.campaign_id}/round-01/hypoweaver_packet.json",
            sealed_files,
        )
        self.assertIn(
            f"{campaign.campaign_id}/comparison/neutral_packets/hypoweaver.json",
            sealed_files,
        )
        self.assertIn(
            f"{campaign.campaign_id}/comparison/resource_usage.json",
            sealed_files,
        )
        self.assertNotIn("recovery-delivery-manifest.json", sealed_files)

    async def test_known_comparison_partial_returns_exact_qwen_usage(self) -> None:
        backend = self._backend()
        context = self._comparison_context()
        qwen_usage = RecoveryUsage(llm_calls=1, input_tokens=3, output_tokens=2)
        qwen_receipt = _recovery_receipt(
            context,
            "qwen_single_pass",
            1,
            input_tokens=3,
            output_tokens=2,
        )
        qwen_packet = _packet("qwen_single_pass", "qwen-partial", self.freeze, 1)

        async def qwen_stage(*args):
            path = backend._comparison_root(context) / "qwen-stage.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "packet": qwen_packet.model_dump(mode="json"),
                        "usage": qwen_usage.model_dump(mode="json"),
                        "receipt": qwen_receipt.model_dump(mode="json"),
                    }
                ),
                encoding="utf-8",
            )
            return qwen_packet, qwen_usage, qwen_receipt

        with (
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch.object(backend, "_run_qwen_baseline", side_effect=qwen_stage),
            patch.object(
                backend,
                "_run_agent_baseline",
                new=AsyncMock(side_effect=ValueError("pre-call validation")),
            ),
        ):
            result = await backend.run_comparison(
                context,
                _packet("hypoweaver", "qualified", self.freeze, 9),
                "summary",
            )

        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.qwen_single_pass.llm_calls, 1)
        self.assertEqual(result.agent_laboratory.llm_calls, 0)
        self.assertEqual(len(result.receipts), 1)

    async def test_agent_started_without_receipt_evidence_raises_and_charges_26(self) -> None:
        backend = self._backend()
        store, qualified = self._qualified_store()
        context_holder = {}
        qwen_usage = RecoveryUsage(llm_calls=1)

        async def qwen_stage(context, *args):
            context_holder["context"] = context
            receipt = _recovery_receipt(context, "qwen_single_pass", 1)
            packet = _packet("qwen_single_pass", "qwen-known", self.freeze, 1)
            path = backend._comparison_root(context) / "qwen-stage.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "packet": packet.model_dump(mode="json"),
                        "usage": qwen_usage.model_dump(mode="json"),
                        "receipt": receipt.model_dump(mode="json"),
                    }
                ),
                encoding="utf-8",
            )
            return packet, qwen_usage, receipt

        async def agent_started(context, *args):
            path = backend._comparison_root(context) / "agent-started.json"
            path.write_text("{}\n", encoding="utf-8")
            raise TimeoutError("agent state is still running")

        runner = LocalRecoveryRunner(
            store=store,
            backend=backend,
            protocol=self.protocol,
            reference=self.reference,
            source_artifact_root=self.source_root,
            delivery_root=self.recovery_delivery,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            protected_official_roots=(
                self.formal_output,
                self.formal_work,
                self.formal_state,
            ),
        )
        with (
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch.object(backend, "_run_qwen_baseline", side_effect=qwen_stage),
            patch.object(backend, "_run_agent_baseline", side_effect=agent_started),
        ):
            campaign = await runner._run_comparison(store.load(), qualified)

        self.assertTrue(context_holder)
        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.invalidation.reservation_scope, "comparison")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 26)
        self.assertEqual(cumulative_llm_calls(campaign), 57)

    async def test_orphaned_qwen_marker_never_restarts_baseline(self) -> None:
        backend = self._backend()
        context = self._comparison_context()
        marker = backend._comparison_root(context) / "qwen-started.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")
        with (
            patch.object(backend, "preflight", new=AsyncMock(return_value=object())),
            patch(
                "hypoweaver.production_recovery_backend.QwenSinglePassRunner",
                side_effect=AssertionError("must not restart"),
            ) as runner_type,
            self.assertRaisesRegex(RuntimeError, "unknown call evidence"),
        ):
            await backend.run_comparison(
                context,
                _packet("hypoweaver", "qualified", self.freeze, 9),
                "summary",
            )
        runner_type.assert_not_called()

    def test_recovery_roots_cannot_contain_one_another(self) -> None:
        source = load_recovery_source_configuration(self.source_config_path)
        with self.assertRaisesRegex(ValueError, "contain one another"):
            assert_recovery_paths_separate(
                source,
                working_root=self.recovery_delivery / "work",
                delivery_root=self.recovery_delivery,
                state_root=self.recovery_state,
            )

    def _prior(self):
        return import_prior_usage_from_ledger(
            OfficialAttemptBinding(
                attempt_id="8" * 64,
                run_manifest_sha256="9" * 64,
                begun_at=NOW,
            ),
            source_official_holdout_lock_id=(
                self.freeze.source_official_holdout_lock_id
            ),
            usage=RecoveryUsage(llm_calls=22, input_tokens=100, output_tokens=20),
            resource_ledger_sha256="a" * 64,
            verified_receipt_sha256=tuple(
                f"{index + 1000:064x}" for index in range(21)
            ),
            token_usage_status="lower_bound",
            imported_at=NOW,
        )

    def _open_store(self) -> RecoveryCampaignStore:
        store = RecoveryCampaignStore(self.root / "campaign-open.json")
        store.create(
            create_recovery_campaign(self.freeze, self._prior(), created_at=NOW)
        )
        return store

    def _terminal_round_harness(self):
        store = self._open_store()
        owner_id = "terminal-round-test-owner"
        reservation = store.reserve_round(
            owner_id=owner_id,
            lease_seconds=7200,
        )
        assert reservation is not None
        campaign = store.load()
        context = LocalRecoveryRoundContext(
            campaign_id=campaign.campaign_id,
            reservation_id=reservation.reservation_id,
            round_id=reservation.round_id,
            call_limit=reservation.call_limit,
            freeze=campaign.freeze,
        )
        runner = LocalRecoveryRunner(
            store=store,
            backend=self._backend(test_mode=True),
            protocol=self.protocol,
            reference=self.reference,
            source_artifact_root=self.source_root,
            delivery_root=self.recovery_delivery,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            protected_official_roots=(
                self.formal_output,
                self.formal_work,
                self.formal_state,
            ),
            owner_id=owner_id,
        )
        return store, runner, context

    def _qualified_store(self):
        store = RecoveryCampaignStore(self.root / "campaign-qualified.json")
        store.create(
            create_recovery_campaign(self.freeze, self._prior(), created_at=NOW)
        )
        campaign = store.load()
        reservation = store.reserve_round(
            owner_id="round-owner",
            lease_seconds=7200,
            now=NOW,
        )
        assert reservation is not None
        model_receipts = _model_receipts("round-01")
        receipts = map_model_call_receipts(
            model_receipts,
            campaign_id=campaign.campaign_id,
            round_id="round-01",
        )
        usage = RecoveryUsage(
            llm_calls=9,
            input_tokens=sum(item.input_tokens for item in model_receipts),
            output_tokens=sum(item.output_tokens for item in model_receipts),
        )
        packet = _packet("hypoweaver", "qualified", self.freeze, 9)
        packet = seal_benchmark_packet(
            packet.model_copy(
                update={
                    "resource_usage": BenchmarkResourceUsage(
                        llm_calls=9,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    ),
                    "packet_sha256": None,
                }
            )
        )
        replay = _replay(packet)
        hard = _hard_report(packet)
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch(
                "hypoweaver.recovery_campaign.evaluate_hard_metrics",
                return_value=hard,
            ),
        ):
            qualified = store.finalize_evaluated_round(
                owner_id="round-owner",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=hard,
                reference=self.reference,
                usage=usage,
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )
        self.assertEqual(qualified.status, "qualified_seen_case")
        return store, packet


class RecoveryPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = ProductionRecoveryBackendTests(methodName="runTest")
        self.backend.setUp()
        self.addCleanup(self.backend.tearDown)
        self.source = load_recovery_source_configuration(
            self.backend.source_config_path
        )
        self._write_failed_formal_source()

    def _write_failed_formal_source(self) -> None:
        protocol = self.backend.protocol
        output = self.backend.formal_output.resolve()
        holdout = official_holdout_lock_id(protocol)
        unsigned = {
            "manifest_version": 1,
            "attempt_id": "b" * 64,
            "begun_at": NOW,
            "protocol_sha256": protocol.protocol_sha256,
            "holdout_lock_id": holdout,
            "output_dir": str(output),
            "artifact_root": str(self.backend.source_root.resolve()),
            "source_artifact_paths": protocol.source_artifact_paths,
            "configuration_artifact_paths": protocol.configuration_artifact_paths,
            "source_sha256": protocol.source_sha256,
            "configuration_sha256": protocol.configuration_sha256,
        }
        manifest = {**unsigned, "run_manifest_sha256": canonical_sha256(unsigned)}
        state = {
            "status": "failed",
            "protocol_sha256": protocol.protocol_sha256,
            "holdout_lock_id": holdout,
            "output_dir": str(output),
            "attempt_id": manifest["attempt_id"],
            "run_manifest_sha256": manifest["run_manifest_sha256"],
            "begun_at": NOW,
            "error_type": "RuntimeError",
        }
        failure = {
            "status": "failed",
            "error_type": "RuntimeError",
            "failed_at": LATER,
            "attempt_id": manifest["attempt_id"],
            "run_manifest_sha256": manifest["run_manifest_sha256"],
            "begun_at": NOW,
        }
        self.backend.formal_output.mkdir(exist_ok=True)
        (self.backend.formal_output / ".official-benchmark-run-manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        (self.backend.formal_output / ".official-benchmark-state.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        (self.backend.formal_output / "official_failure.json").write_text(
            json.dumps(failure),
            encoding="utf-8",
        )
        self.backend.formal_state.mkdir(exist_ok=True)
        (self.backend.formal_state / f"{holdout}.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        ledger_root = Path(f"{self.backend.formal_output}-failed-delivery")
        ledger_root.mkdir(exist_ok=True)
        (ledger_root / "resource_usage.json").write_text(
            json.dumps(
                {
                    "agent_laboratory": {
                        "technical_failures": ["AgentFailure"]
                    },
                    "hypoweaver": {
                        "technical_failures": ["APIConnectionError"]
                    },
                    "known_totals": {
                        "llm_calls": 22,
                        "input_tokens_lower_bound": 157728,
                        "output_tokens_lower_bound": 23559,
                    },
                    "official_attempt_elapsed_seconds": 580.5,
                }
            ),
            encoding="utf-8",
        )

    def test_import_requires_22_calls_and_21_receipts(self) -> None:
        with patch(
            "hypoweaver.recovery_run_cli._source_receipt_hashes",
            return_value=tuple(f"{index + 2000:064x}" for index in range(21)),
        ):
            prior = _import_failed_source_usage(self.source)

        self.assertEqual(prior.usage.llm_calls, 22)
        self.assertEqual(prior.evidence.missing_receipt_count, 1)
        self.assertEqual(prior.evidence.token_usage_status, "lower_bound")

    def test_legacy_receipt_import_is_deterministic(self) -> None:
        legacy_receipts, agent_receipt = self._write_legacy_source_receipts()

        first = _import_failed_source_usage(self.source)
        second = _import_failed_source_usage(self.source)

        excluded = {"imported_at", "import_sha256"}
        self.assertEqual(
            first.model_dump(mode="json", exclude=excluded),
            second.model_dump(mode="json", exclude=excluded),
        )
        expected_hashes = tuple(
            canonical_sha256(receipt)
            for receipt in (*legacy_receipts, agent_receipt)
        )
        self.assertEqual(
            first.evidence.verified_receipt_sha256,
            expected_hashes,
        )
        self.assertEqual(
            second.evidence.verified_receipt_sha256,
            expected_hashes,
        )
        self.assertEqual(len(first.evidence.verified_receipt_sha256), 21)
        self.assertEqual(first.evidence.missing_receipt_count, 1)

    def test_legacy_receipt_import_rejects_invalid_structure(self) -> None:
        legacy_receipts, _agent_receipt = self._write_legacy_source_receipts()
        legacy_receipts[0].pop("provider")
        self._write_hypoweaver_usage(legacy_receipts)

        with self.assertRaisesRegex(ValueError, "provider"):
            _import_failed_source_usage(self.source)

    def test_tampered_manifest_stops_before_prepare(self) -> None:
        path = self.backend.formal_output / ".official-benchmark-run-manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["attempt_id"] = "c" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        with (
            patch(
                "hypoweaver.recovery_run_cli._source_receipt_hashes",
                return_value=tuple(f"{index + 2000:064x}" for index in range(21)),
            ),
            self.assertRaisesRegex(ValueError, "manifest"),
        ):
            _import_failed_source_usage(self.source)

    def test_tampered_canonical_state_stops_before_prepare(self) -> None:
        path = next(self.backend.formal_state.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "running"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with (
            patch(
                "hypoweaver.recovery_run_cli._source_receipt_hashes",
                return_value=tuple(f"{index + 2000:064x}" for index in range(21)),
            ),
            self.assertRaisesRegex(ValueError, "canonical states"),
        ):
            _import_failed_source_usage(self.source)

    def test_prepare_makes_zero_calls_and_imports_prior_22(self) -> None:
        delivery = self.backend.root / "prepared-delivery"
        with (
            patch(
                "hypoweaver.recovery_run_cli._source_receipt_hashes",
                return_value=tuple(f"{index + 2000:064x}" for index in range(21)),
            ),
            patch(
                "hypoweaver.recovery_run_cli.build_recovery_freeze",
                return_value=self.backend.freeze,
            ) as freeze_builder,
            patch("hypoweaver.recovery_run_cli.verify_recovery_environment"),
        ):
            campaign = prepare_recovery_campaign(
                source_config_path=self.backend.source_config_path,
                delivery_root=delivery,
            )

        self.assertEqual(campaign.prior_usage.usage.llm_calls, 22)
        self.assertEqual(cumulative_llm_calls(campaign), 22)
        self.assertEqual(campaign.rounds, ())
        self.assertIsNone(campaign.comparison)
        self.assertEqual(freeze_builder.call_count, 1)
        self.assertTrue((delivery / "recovery-run-config.json").is_file())

    def test_unqualified_delivery_is_complete_and_never_claims_advantage(self) -> None:
        delivery = self.backend.root / "unqualified-delivery"
        campaign = create_recovery_campaign(
            self.backend.freeze,
            self.backend._prior(),
            created_at=NOW,
        )

        write_recovery_delivery(campaign, delivery_root=delivery)

        index = json.loads(
            (delivery / "recovery-artifact-index.json").read_text(encoding="utf-8")
        )
        selection = json.loads(
            (delivery / "selected-round.json").read_text(encoding="utf-8")
        )
        report = (delivery / "中文恢复评测报告.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(index["rounds"], [])
        self.assertEqual(index["comparison"]["status"], "not_started")
        self.assertIsNone(selection["selected_round_id"])
        self.assertIn("原正式 benchmark：失败且不可重跑", report)
        self.assertIn("不作比较优势宣称", report)
        first_manifest = (
            delivery / "recovery-delivery-manifest.json"
        ).read_bytes()
        verify_recovery_delivery_manifest(delivery, expected_campaign=campaign)

        write_recovery_delivery(campaign, delivery_root=delivery)

        self.assertEqual(
            (delivery / "recovery-delivery-manifest.json").read_bytes(),
            first_manifest,
        )

    def test_invalidated_round_delivery_preserves_rejected_terminal_evidence(self) -> None:
        campaign, working_root, result_path = self._rejected_terminal_fixture()
        delivery = self.backend.root / "rejected-terminal-delivery"
        result_sha256 = _file_sha256(result_path)

        write_recovery_delivery(
            campaign,
            delivery_root=delivery,
            working_root=working_root,
        )

        rejected = json.loads(
            (delivery / "rejected-terminal-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        cumulative = json.loads(
            (delivery / "cumulative-resource-usage.json").read_text(
                encoding="utf-8"
            )
        )
        statuses = json.loads(
            (delivery / "round-statuses.json").read_text(encoding="utf-8")
        )
        index = json.loads(
            (delivery / "recovery-artifact-index.json").read_text(
                encoding="utf-8"
            )
        )
        receipts = json.loads(
            (delivery / "all-round-receipts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(campaign.rounds, ())
        self.assertEqual(index["rounds"], [])
        self.assertFalse(statuses[0]["finalized_round"])
        self.assertEqual(statuses[0]["status"], "rejected_terminal_evidence")
        self.assertEqual(rejected["admission_status"], "rejected_not_finalized_round")
        self.assertEqual(
            rejected["source_files"]["round_result"]["sha256"],
            result_sha256,
        )
        self.assertEqual(_file_sha256(result_path), result_sha256)
        self.assertEqual(len(rejected["receipts"]), 2)
        self.assertFalse(
            rejected["usage_diagnostics"]["technical_failures_match"]
        )
        self.assertEqual(
            rejected["usage_diagnostics"]["reported_only_failures"],
            ["RuntimeError"],
        )
        self.assertIn("rejected_terminal_evidence:round-01", receipts)
        self.assertEqual(cumulative["llm_calls"], 42)
        self.assertEqual(cumulative["conservative_invalidation_calls"], 20)
        self.assertEqual(cumulative["predecessor_carryover_calls"], 0)
        self.assertEqual(cumulative["predecessor_started_round_count"], 0)
        self.assertEqual(cumulative["started_round_count"], 1)
        self.assertEqual(
            cumulative["rejected_terminal_known_usage"]["llm_calls"],
            2,
        )
        verify_recovery_delivery_manifest(delivery, expected_campaign=campaign)

    def test_rejected_terminal_receipt_binding_tamper_blocks_delivery(self) -> None:
        campaign, working_root, result_path = self._rejected_terminal_fixture()
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["receipts"][0]["source_receipt_sha256"] = "f" * 64
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        delivery = self.backend.root / "rejected-terminal-tampered-delivery"

        with self.assertRaisesRegex(ValueError, "receipt source binding mismatch"):
            write_recovery_delivery(
                campaign,
                delivery_root=delivery,
                working_root=working_root,
            )

        self.assertFalse((delivery / "recovery-delivery-manifest.json").exists())

    def test_rejected_terminal_receipts_cannot_exceed_conservative_charge(self) -> None:
        campaign, working_root, result_path = self._rejected_terminal_fixture()
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["receipts"] = [payload["receipts"][0]] * 21
        payload["usage"]["llm_calls"] = 21
        payload["usage"]["input_tokens"] = 210
        payload["usage"]["output_tokens"] = 42
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        delivery = self.backend.root / "rejected-terminal-overcharge-delivery"

        with self.assertRaisesRegex(ValueError, "exceeds conservative charge"):
            write_recovery_delivery(
                campaign,
                delivery_root=delivery,
                working_root=working_root,
            )

        self.assertFalse((delivery / "recovery-delivery-manifest.json").exists())

    def test_invalidated_round_without_result_keeps_unknown_terminal_placeholder(
        self,
    ) -> None:
        store = RecoveryCampaignStore(
            self.backend.root / "unknown-terminal-campaign.json"
        )
        store.create(
            create_recovery_campaign(
                self.backend.freeze,
                self.backend._prior(),
                created_at=NOW,
            )
        )
        reservation = store.reserve_round(
            owner_id="unknown-terminal-owner",
            lease_seconds=7200,
            now=NOW,
        )
        assert reservation is not None
        campaign = store.invalidate("round_terminal_usage_evidence_missing")
        working_root = self.backend.root / "unknown-terminal-work"
        delivery = self.backend.root / "unknown-terminal-delivery"

        write_recovery_delivery(
            campaign,
            delivery_root=delivery,
            working_root=working_root,
        )

        statuses = json.loads(
            (delivery / "round-statuses.json").read_text(encoding="utf-8")
        )
        cumulative = json.loads(
            (delivery / "cumulative-resource-usage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            statuses,
            [
                {
                    "round_id": "round-01",
                    "status": "invalidated_unknown_terminal_evidence",
                    "finalized_round": False,
                    "receipt_count": 0,
                    "campaign_invalidation": {
                        "reason": "round_terminal_usage_evidence_missing",
                        "invalidation_sha256": (
                            campaign.invalidation.invalidation_sha256
                        ),
                        "unknown_call_evidence": True,
                        "conservative_llm_call_charge": 20,
                    },
                }
            ],
        )
        self.assertEqual(cumulative["llm_calls"], 42)
        self.assertEqual(cumulative["started_round_count"], 1)
        self.assertEqual(cumulative["conservative_invalidation_calls"], 20)
        self.assertIsNone(cumulative["rejected_terminal_known_usage"])
        self.assertFalse((delivery / "rejected-terminal-evidence.json").exists())
        verify_recovery_delivery_manifest(delivery, expected_campaign=campaign)

    def test_cumulative_started_rounds_include_predecessor_carryover(self) -> None:
        predecessor_store = RecoveryCampaignStore(
            self.backend.root / "started-predecessor.json"
        )
        predecessor_store.create(
            create_recovery_campaign(
                self.backend.freeze,
                self.backend._prior(),
                created_at=NOW,
            )
        )
        reservation = predecessor_store.reserve_round(
            owner_id="started-predecessor-owner",
            lease_seconds=7200,
            now=NOW,
        )
        assert reservation is not None
        predecessor = predecessor_store.invalidate(
            "round_terminal_usage_evidence_missing"
        )
        replacement_freeze = build_recovery_freeze(
            self.backend.protocol,
            artifact_root=self.backend.source_root,
            visible_input_path=self.backend.visible_path,
            data_paths=(self.backend.data_path,),
            reference_path=self.backend.reference_path,
            reference_summary_path=self.backend.summary_path,
            predecessor_campaign=predecessor,
            frozen_at=LATER,
        )
        replacement = create_recovery_campaign(
            replacement_freeze,
            self.backend._prior(),
            created_at=LATER,
        )
        delivery = self.backend.root / "started-replacement-delivery"

        write_recovery_delivery(replacement, delivery_root=delivery)

        cumulative = json.loads(
            (delivery / "cumulative-resource-usage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(cumulative["llm_calls"], 42)
        self.assertEqual(cumulative["predecessor_carryover_calls"], 20)
        self.assertEqual(cumulative["predecessor_started_round_count"], 1)
        self.assertEqual(cumulative["started_round_count"], 1)
        self.assertEqual(cumulative["conservative_invalidation_calls"], 0)
        self.assertEqual(cumulative["token_usage_status"], "lower_bound")

    def test_cumulative_usage_keeps_known_predecessor_usage_separate(self) -> None:
        first_store = RecoveryCampaignStore(
            self.backend.root / "known-usage-first.json"
        )
        first_store.create(
            create_recovery_campaign(
                self.backend.freeze,
                self.backend._prior(),
                created_at=NOW,
            )
        )
        self.assertIsNotNone(first_store.reserve_round(owner_id="first", now=NOW))
        first_invalidated = first_store.invalidate("first_unknown_attempt")
        replacement_freeze = build_recovery_freeze(
            self.backend.protocol,
            artifact_root=self.backend.source_root,
            visible_input_path=self.backend.visible_path,
            data_paths=(self.backend.data_path,),
            reference_path=self.backend.reference_path,
            reference_summary_path=self.backend.summary_path,
            predecessor_campaign=first_invalidated,
            frozen_at=LATER,
        )
        replacement = create_recovery_campaign(
            replacement_freeze,
            self.backend._prior(),
            created_at=LATER,
        )
        second_store = RecoveryCampaignStore(
            self.backend.root / "known-usage-second.json"
        )
        second_store.create(replacement)
        reservation = second_store.reserve_round(owner_id="known-round")
        self.assertIsNotNone(reservation)
        model_receipts = _model_receipts("known-round")[:2]
        model_receipts[1] = model_receipts[1].model_copy(
            update={
                "outcome": "transport_failure",
                "error_type": "APIConnectionError",
                "input_tokens": 0,
                "output_tokens": 0,
            }
        )
        receipts = map_model_call_receipts(
            model_receipts,
            campaign_id=replacement.campaign_id,
            round_id=reservation.round_id,
            require_complete=False,
        )
        second_store.finalize_terminal_round(
            owner_id="known-round",
            reservation_id=reservation.reservation_id,
            submission=RecoveryRoundSubmission(
                freeze_sha256=str(replacement.freeze.freeze_sha256),
                call_limit=reservation.call_limit,
                implementation_sha256=replacement.freeze.hypoweaver_source_sha256,
                started_at=NOW,
                completed_at=LATER,
                usage=RecoveryUsage(
                    llm_calls=2,
                    input_tokens=10,
                    output_tokens=2,
                    wall_time_seconds=4.5,
                    technical_failures=("APIConnectionError",),
                ),
                receipts=receipts,
                technical_failure="fixture_TestDagError",
            ),
        )
        self.assertIsNotNone(second_store.reserve_round(owner_id="second-unknown"))
        predecessor = second_store.invalidate("second_unknown_attempt")
        successor_freeze = build_recovery_freeze(
            self.backend.protocol,
            artifact_root=self.backend.source_root,
            visible_input_path=self.backend.visible_path,
            data_paths=(self.backend.data_path,),
            reference_path=self.backend.reference_path,
            reference_summary_path=self.backend.summary_path,
            predecessor_campaign=predecessor,
            frozen_at=LATER,
        )
        successor = create_recovery_campaign(
            successor_freeze,
            self.backend._prior(),
            created_at=LATER,
        )
        delivery = self.backend.root / "known-usage-successor-delivery"

        write_recovery_delivery(successor, delivery_root=delivery)

        cumulative = json.loads(
            (delivery / "cumulative-resource-usage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(cumulative["llm_calls"], 64)
        self.assertEqual(cumulative["input_tokens"], 110)
        self.assertEqual(cumulative["output_tokens"], 22)
        self.assertEqual(cumulative["wall_time_seconds"], 4.5)
        self.assertEqual(cumulative["technical_failures"], ["APIConnectionError"])
        self.assertEqual(cumulative["predecessor_carryover_calls"], 42)
        self.assertEqual(cumulative["predecessor_known_usage"]["llm_calls"], 2)
        self.assertEqual(cumulative["predecessor_unknown_calls"], 40)
        self.assertEqual(cumulative["predecessor_started_round_count"], 3)
        self.assertEqual(cumulative["token_usage_status"], "lower_bound")
        verify_recovery_delivery_manifest(delivery, expected_campaign=successor)

    def test_cumulative_resource_tamper_cannot_be_overwritten(self) -> None:
        self._assert_sealed_delivery_rejects_tamper(
            "cumulative-resource-usage.json"
        )

    def test_round_receipt_tamper_cannot_be_overwritten(self) -> None:
        self._assert_sealed_delivery_rejects_tamper("all-round-receipts.json")

    def test_artifact_index_tamper_cannot_be_overwritten(self) -> None:
        self._assert_sealed_delivery_rejects_tamper("recovery-artifact-index.json")

    def test_chinese_report_tamper_cannot_be_overwritten(self) -> None:
        self._assert_sealed_delivery_rejects_tamper("中文恢复评测报告.md")

    def _assert_sealed_delivery_rejects_tamper(self, relative_path: str) -> None:
        delivery = self.backend.root / f"tamper-{relative_path.replace('/', '-')}"
        campaign = create_recovery_campaign(
            self.backend.freeze,
            self.backend._prior(),
            created_at=NOW,
        )
        write_recovery_delivery(campaign, delivery_root=delivery)
        target = delivery / relative_path
        tampered = target.read_bytes() + b"\n"
        target.write_bytes(tampered)

        with self.assertRaisesRegex(ValueError, "file hash mismatch"):
            verify_recovery_delivery_manifest(
                delivery,
                expected_campaign=campaign,
            )
        with self.assertRaisesRegex(ValueError, "file hash mismatch"):
            write_recovery_delivery(campaign, delivery_root=delivery)

        self.assertEqual(target.read_bytes(), tampered)

    def test_partial_prepare_resumes_existing_campaign_without_refreezing(self) -> None:
        delivery = self.backend.root / "resume-delivery"
        receipt_hashes = tuple(f"{index + 2000:064x}" for index in range(21))
        with (
            patch(
                "hypoweaver.recovery_run_cli._source_receipt_hashes",
                return_value=receipt_hashes,
            ),
            patch(
                "hypoweaver.recovery_run_cli.build_recovery_freeze",
                return_value=self.backend.freeze,
            ),
            patch("hypoweaver.recovery_run_cli.verify_recovery_environment"),
        ):
            first = prepare_recovery_campaign(
                source_config_path=self.backend.source_config_path,
                delivery_root=delivery,
            )
        (delivery / "recovery-run-config.json").unlink()
        (delivery / "campaign-protocol.json").unlink()
        (delivery / "prior-official-usage-import.json").unlink()

        with (
            patch(
                "hypoweaver.recovery_run_cli._source_receipt_hashes",
                return_value=receipt_hashes,
            ),
            patch(
                "hypoweaver.recovery_run_cli.build_recovery_freeze",
                side_effect=AssertionError("must reuse existing freeze"),
            ),
            patch("hypoweaver.recovery_run_cli.verify_recovery_environment"),
        ):
            resumed = prepare_recovery_campaign(
                source_config_path=self.backend.source_config_path,
                delivery_root=delivery,
            )

        self.assertEqual(resumed.campaign_sha256, first.campaign_sha256)
        self.assertTrue((delivery / "recovery-run-config.json").is_file())

    def test_replacement_prepare_inherits_predecessor_and_roundtrips_paths(self) -> None:
        delivery, predecessor_path, campaign = self._prepare_replacement()
        prepared = PreparedRecoveryConfiguration.model_validate_json(
            (delivery / "recovery-run-config.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            Path(prepared.predecessor_campaign_path),
            predecessor_path.resolve(),
        )
        self.assertEqual(
            prepared.predecessor_state_root,
            str(predecessor_path.parent.resolve()),
        )
        self.assertEqual(
            campaign.freeze.sealed_label_orders,
            RecoveryCampaignStore(predecessor_path).load().freeze.sealed_label_orders,
        )
        self.assertEqual(
            campaign.freeze.sealed_system_assignments,
            RecoveryCampaignStore(
                predecessor_path
            ).load().freeze.sealed_system_assignments,
        )
        self.assertNotEqual(
            campaign.campaign_id,
            RecoveryCampaignStore(predecessor_path).load().campaign_id,
        )
        self.assertEqual(cumulative_llm_calls(campaign), 22)

    def test_run_rejects_prepared_delivery_root_rebinding(self) -> None:
        delivery, _predecessor_path, _campaign = self._prepare_replacement()
        moved = self.backend.root / "moved-delivery"
        moved.mkdir()
        (moved / "recovery-run-config.json").write_bytes(
            (delivery / "recovery-run-config.json").read_bytes()
        )

        with self.assertRaisesRegex(ValueError, "delivery_root mismatch"):
            asyncio.run(run_prepared_recovery(delivery_root=moved))

    def test_run_rechecks_canonical_campaign_and_protects_predecessor_roots(self) -> None:
        delivery, predecessor_path, campaign = self._prepare_replacement()
        with (
            patch("hypoweaver.recovery_run_cli.ProductionRecoveryBackend"),
            patch("hypoweaver.recovery_run_cli.LocalRecoveryRunner") as runner_type,
            patch(
                "hypoweaver.recovery_run_cli.write_recovery_delivery"
            ) as delivery_writer,
            patch("hypoweaver.recovery_run_cli.verify_recovery_delivery_manifest"),
        ):
            runner_type.return_value.run = AsyncMock(return_value=campaign)
            loaded = asyncio.run(run_prepared_recovery(delivery_root=delivery))

        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)
        prepared = PreparedRecoveryConfiguration.model_validate_json(
            (delivery / "recovery-run-config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            delivery_writer.call_args.kwargs["working_root"],
            Path(prepared.working_root),
        )
        protected = runner_type.call_args.kwargs["protected_official_roots"]
        predecessor_state = predecessor_path.parent.resolve()
        predecessor_delivery = predecessor_state.with_name(
            predecessor_state.name.removesuffix("-state")
        )
        self.assertIn(predecessor_state, protected)
        self.assertIn(predecessor_delivery, protected)
        self.assertIn(
            predecessor_delivery.with_name(f"{predecessor_delivery.name}-work"),
            protected,
        )

        prepared_path = delivery / "recovery-run-config.json"
        payload = json.loads(prepared_path.read_text(encoding="utf-8"))
        payload["campaign_id"] = "tampered-campaign"
        prepared_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "campaign binding mismatch"):
            asyncio.run(run_prepared_recovery(delivery_root=delivery))

    def test_recovery_modules_do_not_import_formal_mutators(self) -> None:
        package = Path(__file__).parents[1] / "src" / "hypoweaver"
        sources = "\n".join(
            (package / filename).read_text(encoding="utf-8")
            for filename in (
                "production_recovery_backend.py",
                "recovery_run_cli.py",
            )
        )
        for forbidden in (
            "official_benchmark_runner",
            "OfficialBenchmarkOrchestrator",
            "begin_official_attempt",
            "fail_official_attempt",
            "complete_official_attempt",
            "official=True",
        ):
            self.assertNotIn(forbidden, sources)

    def _prepare_replacement(self):
        receipt_hashes = tuple(f"{index + 2000:064x}" for index in range(21))
        with patch(
            "hypoweaver.recovery_run_cli._source_receipt_hashes",
            return_value=receipt_hashes,
        ):
            predecessor_prior = _import_failed_source_usage(self.source)
        predecessor = invalidate_recovery_campaign(
            create_recovery_campaign(
                self.backend.freeze,
                predecessor_prior,
                created_at=NOW,
            ),
            "receipt_binding_defect",
            invalidated_at=LATER,
        )
        predecessor_state = self.backend.root / "predecessor-state"
        predecessor_path = canonical_recovery_campaign_path(
            predecessor_state,
            predecessor.freeze,
        )
        if not predecessor_path.exists():
            RecoveryCampaignStore(predecessor_path).create(predecessor)
        replacement_freeze = build_recovery_freeze(
            self.backend.protocol,
            artifact_root=self.backend.source_root,
            visible_input_path=self.backend.visible_path,
            data_paths=(self.backend.data_path,),
            reference_path=self.backend.reference_path,
            reference_summary_path=self.backend.summary_path,
            predecessor_campaign=predecessor,
            frozen_at=LATER,
        )
        delivery = self.backend.root / "replacement-delivery"
        with (
            patch(
                "hypoweaver.recovery_run_cli._source_receipt_hashes",
                return_value=receipt_hashes,
            ),
            patch(
                "hypoweaver.recovery_run_cli.build_recovery_freeze",
                return_value=replacement_freeze,
            ) as freeze_builder,
            patch("hypoweaver.recovery_run_cli.verify_recovery_environment"),
        ):
            campaign = prepare_recovery_campaign(
                source_config_path=self.backend.source_config_path,
                delivery_root=delivery,
                predecessor_campaign_path=predecessor_path,
            )
        self.assertEqual(
            freeze_builder.call_args.kwargs["predecessor_campaign"].campaign_sha256,
            predecessor.campaign_sha256,
        )
        return delivery, predecessor_path, campaign

    def _rejected_terminal_fixture(
        self,
    ) -> tuple[RecoveryCampaign, Path, Path]:
        store = RecoveryCampaignStore(self.backend.root / "rejected-campaign.json")
        store.create(
            create_recovery_campaign(
                self.backend.freeze,
                self.backend._prior(),
                created_at=NOW,
            )
        )
        reservation = store.reserve_round(
            owner_id="rejected-terminal-owner",
            lease_seconds=7200,
            now=NOW,
        )
        assert reservation is not None
        campaign = store.invalidate("round_terminal_usage_evidence_invalid")
        working_root = self.backend.root / "rejected-terminal-work"
        round_root = working_root / campaign.campaign_id / reservation.round_id
        round_root.mkdir(parents=True)

        source_receipts = _model_receipts(reservation.round_id)[:2]
        source_receipts[1] = source_receipts[1].model_copy(
            update={
                "outcome": "transport_failure",
                "error_type": "APIConnectionError",
                "provider_response_id_sha256": None,
            }
        )
        mapped_receipts = map_model_call_receipts(
            source_receipts,
            campaign_id=campaign.campaign_id,
            round_id=reservation.round_id,
            require_complete=False,
        )
        usage = RecoveryUsage(
            llm_calls=2,
            input_tokens=20,
            output_tokens=4,
            wall_time_seconds=2,
            technical_failures=("APIConnectionError", "RuntimeError"),
        )
        result = LocalRecoveryRoundResult(
            status="technical_failed",
            started_at=NOW,
            completed_at=LATER,
            usage=usage,
            receipts=mapped_receipts,
            reason_code="round_RuntimeError",
        )
        result_path = round_root / "round-result.json"
        result_path.write_text(result.model_dump_json(), encoding="utf-8")
        (round_root / "round-started.json").write_text(
            json.dumps(
                {
                    "event": "model_facing_round_started",
                    "campaign_id": campaign.campaign_id,
                    "round_id": reservation.round_id,
                    "reservation_id": reservation.reservation_id,
                    "call_limit": reservation.call_limit,
                    "started_at": NOW,
                }
            ),
            encoding="utf-8",
        )
        request = CreateRunRequest.model_validate_json(
            self.backend.visible_path.read_text(encoding="utf-8")
        )
        assert request.case is not None
        RunRepository(round_root / "hypoweaver.db").create(
            RunState(
                id="rejected-terminal-source-run",
                case_id=request.case.case_id,
                case_name=request.case.title,
                mode="research",
                model_provider="qwen",
                execution_mode="external",
                status="failed",
                case_submission=request.case,
                artifacts={
                    "model_usage": {
                        "payload": {
                            "llm_calls": 2,
                            "input_tokens": 20,
                            "output_tokens": 4,
                            "wall_time_seconds": 2,
                            "technical_failures": ["APIConnectionError"],
                            "call_receipts": [
                                item.model_dump(mode="json")
                                for item in source_receipts
                            ],
                        }
                    }
                },
                last_error="round_RuntimeError",
            )
        )
        return campaign, working_root, result_path

    def _write_legacy_source_receipts(
        self,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        legacy_receipts = [
            {
                "provider": "qwen",
                "model": "legacy-model",
                "started_at": _timestamp(index),
                "completed_at": _timestamp(index + 1),
                "response_sha256": f"{index + 100:064x}",
                "input_tokens": 10,
                "output_tokens": 2,
            }
            for index in range(1, 21)
        ]
        self._write_hypoweaver_usage(legacy_receipts)
        agent_receipt = {
            "provider": "qwen",
            "model": "legacy-agent-model",
            "started_at": NOW,
            "completed_at": LATER,
            "response_sha256": "f" * 64,
        }
        agent_usage_path = (
            self.backend.formal_work
            / "agent-laboratory"
            / "legacy-run"
            / "output"
            / "seen-case-1"
            / "legacy-run"
            / "model_usage.json"
        )
        agent_usage_path.parent.mkdir(parents=True, exist_ok=True)
        agent_usage_path.write_text(
            json.dumps({"llm_calls": 1, "call_receipts": [agent_receipt]}),
            encoding="utf-8",
        )
        return legacy_receipts, agent_receipt

    def _write_hypoweaver_usage(
        self,
        legacy_receipts: list[dict[str, object]],
    ) -> None:
        repository = RunRepository(self.backend.formal_work / "hypoweaver.db")
        repository.delete_all_for_tests()
        request = CreateRunRequest.model_validate_json(
            self.backend.visible_path.read_text(encoding="utf-8")
        )
        assert request.case is not None
        repository.create(
            RunState(
                id="legacy-source-run",
                case_id=request.case.case_id,
                case_name=request.case.title,
                mode="research",
                model_provider="qwen",
                execution_mode="external",
                case_submission=request.case,
                artifacts={
                    "model_usage": {
                        "payload": {
                            "llm_calls": len(legacy_receipts),
                            "call_receipts": legacy_receipts,
                        }
                    }
                },
            )
        )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(index: int) -> str:
    return (
        datetime(2026, 7, 16, tzinfo=timezone.utc) + timedelta(seconds=index)
    ).isoformat()


def _model_receipts(round_id: str) -> list[ModelCallReceipt]:
    receipts = []
    for index, slot in enumerate(FIRST_ROUND_LOGICAL_SLOTS, start=1):
        group, prompt_key, _slot_index = slot.split(":")
        receipts.append(
            ModelCallReceipt(
                call_id=f"{round_id}-call-{index}",
                logical_call_id=f"{round_id}-logical-{index}",
                call_group=group,
                prompt_key=prompt_key,
                prompt_version="1.0.0",
                attempt_index=1,
                max_attempts=3,
                attempt_type="primary",
                outcome="succeeded",
                provider="qwen",
                model="qwen-test",
                started_at=_timestamp(index),
                completed_at=_timestamp(index + 1),
                response_sha256=f"{index + 10:064x}",
                input_sha256=f"{index + 30:064x}",
                output_schema_sha256=f"{index + 50:064x}",
                provider_response_id_sha256=f"{index + 70:064x}",
                input_tokens=10,
                output_tokens=2,
            )
        )
    return receipts


def _packet(system_id: str, packet_id: str, freeze, calls: int) -> BenchmarkPacket:
    native_artifacts = {}
    if system_id == "qwen_single_pass":
        native_artifacts = {
            "visible_input": freeze.visible_input_sha256,
            "single_pass_prompt": "a" * 64,
            "single_pass_config": "b" * 64,
            "single_pass_raw_response": canonical_sha256(
                {"phase": "qwen_single_pass", "index": 1}
            ),
        }
    elif system_id == "agent_laboratory":
        native_artifacts = {"benchmark_output": "c" * 64}
    return seal_benchmark_packet(
        BenchmarkPacket(
            packet_id=packet_id,
            system_id=system_id,
            case_id=freeze.case_id,
            visible_input_sha256=freeze.visible_input_sha256,
            data_sha256=list(freeze.data_sha256),
            model_id="qwen-test",
            design=NormalizedDesign(),
            resource_usage=BenchmarkResourceUsage(llm_calls=calls),
            native_artifact_sha256=native_artifacts,
        )
    )


def _recovery_receipt(
    context,
    phase: str,
    index: int,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
):
    return create_recovery_call_receipt(
        campaign_id=context.campaign_id,
        round_id=context.comparison_id,
        phase=phase,
        provider="qwen",
        model="qwen-test",
        call_started_at=_timestamp(index),
        call_completed_at=_timestamp(index + 1),
        raw_response={"phase": phase, "index": index},
        call_id=f"{phase}-{index}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_sha256=(
            context.freeze.visible_input_sha256
            if phase == "qwen_single_pass"
            else None
        ),
        source_receipt_sha256=canonical_sha256(
            {"source": phase, "index": index}
        ),
    )


def _ratings() -> NeurIPSRatings:
    return NeurIPSRatings(
        quality=3,
        significance=3,
        clarity=3,
        soundness=3,
        presentation=3,
        contribution=3,
        overall=7,
        confidence=4,
        recommendation="accept",
    )


def _mapped_blind_receipt(context, source):
    assert source is not None
    return create_recovery_call_receipt(
        campaign_id=context.campaign_id,
        round_id=context.comparison_id,
        phase="blind_review",
        provider=source.provider,
        model=source.model,
        call_started_at=source.call_started_at,
        call_completed_at=source.call_completed_at,
        raw_response_sha256=source.response_sha256,
        call_id=source.call_id,
        input_tokens=source.input_tokens,
        output_tokens=source.output_tokens,
        source_receipt_sha256=canonical_sha256(source.model_dump(mode="json")),
    )


def _blind_summary(freeze, hypoweaver, agent) -> PairedReviewSummary:
    reviews = []
    for sample_index in range(1, 6):
        receipt = PairedBlindCallReceipt(
            call_id=f"blind-source-{sample_index}",
            sample_index=sample_index,
            provider="qwen",
            model="qwen-test",
            outcome="succeeded",
            response_sha256=f"{sample_index + 300:064x}",
            call_started_at=_timestamp(sample_index),
            call_completed_at=_timestamp(sample_index + 1),
        )
        reviews.append(
            NeurIPSReview(
                review_id=f"review-{sample_index}",
                sample_index=sample_index,
                label_order=freeze.sealed_label_orders[sample_index - 1],
                system_assignment=freeze.sealed_system_assignments[
                    sample_index - 1
                ],
                ratings_a=_ratings(),
                ratings_b=_ratings(),
                preferred_label="tie",
                resource_usage=BenchmarkResourceUsage(llm_calls=1),
                call_receipt=receipt,
            )
        )
    return summarize_paired_reviews(
        hypoweaver.case_id,
        hypoweaver.packet_id,
        agent.packet_id,
        reviews,
    )


def _replay(packet: BenchmarkPacket) -> FaultReplayReport:
    outcomes = [
        FaultOutcome(
            fault_id=fault_id,
            detected=True,
            action="block",
            evidence=[fault_id],
        )
        for fault_id in FAULT_IDS
    ]
    return FaultReplayReport(
        case_id=packet.case_id,
        clean_packet_sha256=str(packet.packet_sha256),
        full_system_outcomes=outcomes,
        clean_false_block_count=0,
        ablations=[
            AblationReplayResult(
                ablation_id=ablation_id,
                disabled_component=f"component-{index}",
                packet_sha256=str(packet.packet_sha256),
                target_fault_ids=[FAULT_IDS[index]],
                fault_outcomes=outcomes,
                detected_fault_count=9,
                target_fault_degraded=True,
            )
            for index, ablation_id in enumerate(ABLATION_IDS)
        ],
    )


def _hard_report(packet: BenchmarkPacket) -> HardMetricReport:
    return HardMetricReport(
        report_id=f"hard-{packet.packet_id}",
        case_id=packet.case_id,
        packet_id=packet.packet_id,
        metrics=[
            HardMetric(
                metric_id=metric_id,
                numerator=1,
                denominator=1,
                value=1.0,
                target="test",
                passed=True,
            )
            for metric_id in HARD_METRIC_IDS
        ],
        all_hard_gates_passed=True,
        created_at=NOW,
    )


if __name__ == "__main__":
    unittest.main()
