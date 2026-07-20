from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hypoweaver.benchmark_evaluator import (
    seal_benchmark_packet,
    summarize_paired_reviews,
)
from hypoweaver.benchmark_models import (
    ABLATION_IDS,
    FAULT_IDS,
    AblationReplayResult,
    BenchmarkDeliveryManifest,
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
    PairedReviewSummary,
)
from hypoweaver.benchmark_protocol import official_holdout_lock_id, seal_protocol
from hypoweaver.local_recovery_runner import (
    LocalRecoveryComparisonResult,
    LocalRecoveryRoundResult,
    LocalRecoveryRunner,
)
from hypoweaver.models import ModelCallReceipt
from hypoweaver.recovery_campaign import (
    RecoveryCampaignStore,
    build_recovery_freeze,
    create_recovery_call_receipt,
    create_recovery_campaign,
    cumulative_llm_calls,
    import_prior_usage,
    map_model_call_receipts,
)
from hypoweaver.recovery_identity import FIRST_ROUND_LOGICAL_SLOTS
from hypoweaver.recovery_models import HARD_METRIC_IDS, RecoveryUsage
from hypoweaver.seal import canonical_sha256


NOW = "2026-07-16T00:00:00+00:00"
LATER = "2026-07-16T00:01:00+00:00"


class _FakeRecoveryBackend:
    def __init__(self, *, raise_round: bool = False) -> None:
        self.raise_round = raise_round
        self.round_calls = 0
        self.comparison_calls = 0

    async def run_hypoweaver_round(self, context):
        self.round_calls += 1
        if self.raise_round:
            raise RuntimeError("provider failed before a receipt journal was returned")
        model_receipts = _model_receipts(context.round_id)
        receipts = map_model_call_receipts(
            model_receipts,
            campaign_id=context.campaign_id,
            round_id=context.round_id,
        )
        usage = RecoveryUsage(
            llm_calls=9,
            input_tokens=sum(item.input_tokens for item in model_receipts),
            output_tokens=sum(item.output_tokens for item in model_receipts),
        )
        return LocalRecoveryRoundResult(
            status="completed",
            started_at=NOW,
            completed_at=LATER,
            usage=usage,
            receipts=receipts,
            packet=_packet(
                system_id="hypoweaver",
                packet_id=f"hypoweaver-{context.round_id}",
                freeze=context.freeze,
                calls=9,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        )

    async def run_comparison(self, context, qualified_packet, reference_summary):
        self.comparison_calls += 1
        qwen = _packet(
            system_id="qwen_single_pass",
            packet_id="qwen-comparison",
            freeze=context.freeze,
            calls=1,
        )
        agent = _packet(
            system_id="agent_laboratory",
            packet_id="agent-comparison",
            freeze=context.freeze,
            calls=20,
        )
        blind = _blind_summary(context.freeze, qualified_packet, agent)
        blind_receipts = tuple(
            _map_blind_receipt(
                context.campaign_id,
                context.comparison_id,
                review.call_receipt,
            )
            for review in blind.reviews
        )
        return LocalRecoveryComparisonResult(
            status="completed",
            qwen_single_pass=RecoveryUsage(llm_calls=1),
            agent_laboratory=RecoveryUsage(llm_calls=20),
            blind_reviews=RecoveryUsage(llm_calls=5),
            receipts=(
                *_comparison_receipts(
                    context.campaign_id,
                    context.comparison_id,
                    "qwen_single_pass",
                    1,
                    input_sha256=context.freeze.visible_input_sha256,
                ),
                *_comparison_receipts(
                    context.campaign_id,
                    context.comparison_id,
                    "agent_laboratory",
                    20,
                ),
                *blind_receipts,
            ),
            started_at=NOW,
            completed_at=LATER,
            qwen_packet=qwen,
            agent_laboratory_packet=agent,
            blind_summary=blind,
        )


class _TamperedBlindBackend(_FakeRecoveryBackend):
    async def run_comparison(self, context, qualified_packet, reference_summary):
        result = await super().run_comparison(
            context,
            qualified_packet,
            reference_summary,
        )
        summary = result.blind_summary
        reviews = list(summary.reviews)
        first = reviews[0]
        reviews[0] = first.model_copy(
            update={"label_order": "B_A" if first.label_order == "A_B" else "A_B"}
        )
        return result.model_copy(
            update={"blind_summary": summary.model_copy(update={"reviews": reviews})}
        )


class _DriftAfterRoundBackend(_FakeRecoveryBackend):
    def __init__(self, drift_path: Path) -> None:
        super().__init__()
        self.drift_path = drift_path

    async def run_hypoweaver_round(self, context):
        result = await super().run_hypoweaver_round(context)
        self.drift_path.write_text("AGENT = 'drifted-after-call'\n", encoding="utf-8")
        return result


class _DriftAfterComparisonBackend(_FakeRecoveryBackend):
    def __init__(self, drift_path: Path) -> None:
        super().__init__()
        self.drift_path = drift_path

    async def run_comparison(self, context, qualified_packet, reference_summary):
        result = await super().run_comparison(
            context,
            qualified_packet,
            reference_summary,
        )
        self.drift_path.write_text("AGENT = 'drifted-after-comparison'\n", encoding="utf-8")
        return result


class _ForgedQwenBindingBackend(_FakeRecoveryBackend):
    async def run_comparison(self, context, qualified_packet, reference_summary):
        result = await super().run_comparison(
            context,
            qualified_packet,
            reference_summary,
        )
        receipts = list(result.receipts)
        index = next(
            index
            for index, receipt in enumerate(receipts)
            if receipt.phase == "qwen_single_pass"
        )
        receipts[index] = receipts[index].model_copy(
            update={
                "source_receipt_sha256": "f" * 64,
                "response_sha256": "e" * 64,
            }
        )
        return result.model_copy(update={"receipts": receipts})


class LocalRecoveryRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source_root = self.root / "source-root"
        self.delivery_root = self.root / "delivery-root"
        self.official_root = self.root / "protected-official-root"
        self.official_root.mkdir(parents=True)
        self.visible_path = self.root / "case" / "visible.txt"
        self.data_path = self.root / "case" / "panel.csv"
        self.reference_path = self.root / "case" / "reference.json"
        self.summary_path = self.root / "case" / "summary.md"
        self.agent_source = self.source_root / "sources" / "agent.py"
        self.harness_source = self.source_root / "sources" / "harness.py"
        self.hypo_source = self.source_root / "sources" / "hypo.py"
        self.config_path = self.source_root / "config" / "recovery.json"
        for path in (
            self.visible_path,
            self.data_path,
            self.reference_path,
            self.summary_path,
            self.agent_source,
            self.harness_source,
            self.hypo_source,
            self.config_path,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.visible_path.write_text("visible\n", encoding="utf-8")
        self.data_path.write_text("firm,year,y\n1,2024,1\n", encoding="utf-8")
        self.summary_path.write_text("frozen summary\n", encoding="utf-8")
        self.agent_source.write_text("AGENT = 1\n", encoding="utf-8")
        self.harness_source.write_text("HARNESS = 1\n", encoding="utf-8")
        self.hypo_source.write_text("HYPO = 1\n", encoding="utf-8")
        self.config_path.write_text('{"model":"qwen"}\n', encoding="utf-8")

        self.reference = BenchmarkReference(
            case_id="seen-case-1",
            visible_input_sha256=_sha(self.visible_path),
            data_sha256=[_sha(self.data_path)],
            expected_design={},
            required_check_ids=[],
            independently_reproducible_check_ids=[],
        )
        self.reference_path.write_text(
            json.dumps(self.reference.model_dump(mode="json")),
            encoding="utf-8",
        )
        self.protocol = seal_protocol(
            FrozenBenchmarkProtocol(
                case_id=self.reference.case_id,
                visible_input_sha256=self.reference.visible_input_sha256,
                data_sha256=self.reference.data_sha256,
                reference_sha256=canonical_sha256(
                    self.reference.model_dump(mode="json")
                ),
                source_sha256={
                    "hypoweaver": "d" * 64,
                    "agent_laboratory": "e" * 64,
                    "benchmark_harness": "f" * 64,
                },
                configuration_sha256="1" * 64,
                source_artifact_paths={
                    "hypoweaver": ["sources/hypo.py"],
                    "agent_laboratory": ["sources/agent.py"],
                    "benchmark_harness": ["sources/harness.py"],
                },
                configuration_artifact_paths=["config/recovery.json"],
                frozen_at=NOW,
            )
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
        binding = OfficialAttemptBinding(
            attempt_id="4" * 64,
            run_manifest_sha256="5" * 64,
            begun_at=NOW,
        )
        prior = import_prior_usage(
            binding,
            source_official_holdout_lock_id=official_holdout_lock_id(self.protocol),
            usage=RecoveryUsage(),
            official_receipt_sha256=(),
            imported_at=NOW,
        )
        self.store = RecoveryCampaignStore(self.root / "campaign.json")
        self.store.create(create_recovery_campaign(self.freeze, prior, created_at=NOW))

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_runs_to_first_qualification_and_comparison_once(self) -> None:
        backend = _FakeRecoveryBackend()
        runner = self._runner(backend)

        def hard(packet, reference, **kwargs):
            return _hard_report(packet, passed=packet.packet_id.endswith("round-02"))

        with (
            patch("hypoweaver.local_recovery_runner.replay_ablations", side_effect=_replay),
            patch("hypoweaver.recovery_campaign.replay_ablations", side_effect=_replay),
            patch("hypoweaver.local_recovery_runner.evaluate_hard_metrics", side_effect=hard),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", side_effect=hard),
            patch("hypoweaver.benchmark_protocol.replay_ablations", side_effect=_replay),
            patch("hypoweaver.benchmark_protocol.evaluate_hard_metrics", side_effect=hard),
        ):
            campaign = await runner.run()
            unchanged = await runner.run()

        self.assertEqual(campaign.status, "qualified_seen_case")
        self.assertEqual(
            [item.status for item in campaign.rounds],
            ["hard_gate_failed", "hard_gate_qualified"],
        )
        self.assertEqual(backend.round_calls, 2)
        self.assertEqual(backend.comparison_calls, 1)
        self.assertIsNotNone(campaign.comparison)
        self.assertEqual(campaign.comparison.status, "completed")
        self.assertEqual(unchanged.campaign_sha256, campaign.campaign_sha256)
        self.assertTrue(
            (
                self.delivery_root
                / campaign.campaign_id
                / "round-02"
                / "hard_metrics.json"
            ).exists()
        )
        self.assertFalse(any(self.source_root.rglob("round-02")))

    async def test_backend_exception_charges_reserved_calls_without_fabricating_receipts(self) -> None:
        backend = _FakeRecoveryBackend(raise_round=True)
        campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.rounds, ())
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 20)
        self.assertEqual(cumulative_llm_calls(campaign), 20)
        self.assertEqual(backend.round_calls, 1)

    async def test_evaluator_failure_records_known_nine_calls_as_invalidated_round(self) -> None:
        backend = _FakeRecoveryBackend()
        with patch(
            "hypoweaver.local_recovery_runner.replay_ablations",
            side_effect=ValueError("invalid packet"),
        ):
            campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(len(campaign.rounds), 1)
        self.assertEqual(campaign.rounds[0].usage.llm_calls, 9)
        self.assertEqual(len(campaign.rounds[0].receipts), 9)
        self.assertEqual(campaign.invalidation, None)

    async def test_source_drift_blocks_before_backend_and_invalidates(self) -> None:
        self.agent_source.write_text("AGENT = 2\n", encoding="utf-8")
        backend = _FakeRecoveryBackend()
        campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(
            campaign.status_reason,
            "recovery_frozen_evidence_verification_failed",
        )
        self.assertEqual(backend.round_calls, 0)

    async def test_blind_schedule_tampering_is_not_accepted_as_completed_comparison(self) -> None:
        backend = _TamperedBlindBackend()
        runner = self._runner(backend)

        def hard(packet, reference, **kwargs):
            return _hard_report(packet, passed=True)

        with (
            patch("hypoweaver.local_recovery_runner.replay_ablations", side_effect=_replay),
            patch("hypoweaver.recovery_campaign.replay_ablations", side_effect=_replay),
            patch("hypoweaver.local_recovery_runner.evaluate_hard_metrics", side_effect=hard),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", side_effect=hard),
        ):
            campaign = await runner.run()

        self.assertEqual(campaign.status, "qualified_seen_case")
        self.assertIsNotNone(campaign.comparison)
        self.assertEqual(campaign.comparison.status, "technical_failed")
        self.assertEqual(
            campaign.comparison.technical_failure,
            "comparison_delivery_validation_failed",
        )

    async def test_live_reservation_makes_second_runner_busy_without_invalidation(self) -> None:
        reservation = self.store.reserve_round(
            owner_id="first-runner",
            lease_seconds=7200,
        )
        self.assertIsNotNone(reservation)
        backend = _FakeRecoveryBackend()
        campaign = await self._runner(backend, owner_id="second-runner").run()

        self.assertEqual(campaign.status, "open")
        self.assertIsNotNone(campaign.active_round_reservation)
        self.assertEqual(backend.round_calls, 0)

    async def test_resume_recomputes_and_tampered_hard_report_blocks_comparison(self) -> None:
        await self._seed_qualified_round_artifacts()
        campaign = self.store.load()
        hard_path = (
            self.delivery_root
            / campaign.campaign_id
            / "round-01"
            / "hard_metrics.json"
        )
        payload = json.loads(hard_path.read_text(encoding="utf-8"))
        payload["metrics"][0]["passed"] = False
        hard_path.write_text(json.dumps(payload), encoding="utf-8")
        backend = _FakeRecoveryBackend()
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", side_effect=_replay),
            patch(
                "hypoweaver.recovery_campaign.evaluate_hard_metrics",
                side_effect=lambda packet, reference, **kwargs: _hard_report(packet, passed=True),
            ),
        ):
            campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(
            campaign.status_reason,
            "recovery_frozen_evidence_verification_failed",
        )
        self.assertEqual(backend.round_calls, 0)
        self.assertEqual(backend.comparison_calls, 0)

    async def test_resume_rechecks_hard_gate_failed_round_before_new_calls(self) -> None:
        await self._seed_evaluated_round_artifacts(passed=False)
        campaign = self.store.load()
        packet_path = (
            self.delivery_root
            / campaign.campaign_id
            / "round-01"
            / "hypoweaver_packet.json"
        )
        payload = json.loads(packet_path.read_text(encoding="utf-8"))
        payload["packet_id"] = "tampered-failed-round"
        packet_path.write_text(json.dumps(payload), encoding="utf-8")
        backend = _FakeRecoveryBackend()

        with self._gate_patches(passed=False):
            campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(backend.round_calls, 0)
        self.assertEqual(backend.comparison_calls, 0)

    async def test_resume_rechecks_completed_comparison_files_before_any_calls(self) -> None:
        backend = _FakeRecoveryBackend()
        with self._gate_patches(passed=True):
            campaign = await self._runner(backend).run()
        self.assertEqual(campaign.comparison.status, "completed")
        packet_path = (
            self.delivery_root
            / campaign.campaign_id
            / "comparison"
            / "neutral_packets"
            / "qwen_single_pass.json"
        )
        packet_path.write_text(
            packet_path.read_text(encoding="utf-8") + " ",
            encoding="utf-8",
        )
        resumed_backend = _FakeRecoveryBackend()

        with self._gate_patches(passed=True):
            resumed = await self._runner(resumed_backend).run()

        self.assertEqual(resumed.status, "invalidated")
        self.assertEqual(resumed_backend.round_calls, 0)
        self.assertEqual(resumed_backend.comparison_calls, 0)

    async def test_round_environment_drift_after_call_is_checked_before_finalize(self) -> None:
        backend = _DriftAfterRoundBackend(self.agent_source)
        with self._gate_patches(passed=True):
            campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.rounds, ())
        self.assertEqual(campaign.invalidation.reservation_scope, "round")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 20)

    async def test_comparison_environment_drift_after_calls_charges_full_reserve(self) -> None:
        backend = _DriftAfterComparisonBackend(self.agent_source)
        with self._gate_patches(passed=True):
            campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.rounds[0].status, "hard_gate_qualified")
        self.assertIsNone(campaign.comparison)
        self.assertEqual(campaign.invalidation.reservation_scope, "comparison")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 26)

    async def test_forged_nonempty_qwen_source_hash_cannot_finalize_comparison(self) -> None:
        backend = _ForgedQwenBindingBackend()
        with self._gate_patches(passed=True):
            campaign = await self._runner(backend).run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertIsNone(campaign.comparison)
        self.assertEqual(campaign.invalidation.reservation_scope, "comparison")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 26)

    async def test_store_rereads_delivery_files_at_comparison_finalize(self) -> None:
        backend = _FakeRecoveryBackend()
        runner = self._runner(backend)
        original_loader = runner._load_delivery_bundle

        def load_then_remove(campaign):
            loaded = original_loader(campaign)
            path = (
                self.delivery_root
                / campaign.campaign_id
                / "comparison"
                / "resource_usage.json"
            )
            path.unlink()
            return loaded

        with (
            self._gate_patches(passed=True),
            patch.object(
                runner,
                "_load_delivery_bundle",
                side_effect=load_then_remove,
            ),
        ):
            campaign = await runner.run()

        self.assertEqual(campaign.status, "invalidated")
        self.assertIsNone(campaign.comparison)
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 26)

    async def test_recovery_writes_cannot_overlap_protected_official_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "protected official root"):
            self._runner(
                _FakeRecoveryBackend(),
                delivery_root=self.official_root / "recovery-output",
            )

    def _runner(self, backend, *, owner_id=None, delivery_root=None):
        return LocalRecoveryRunner(
            store=self.store,
            backend=backend,
            protocol=self.protocol,
            reference=self.reference,
            source_artifact_root=self.source_root,
            delivery_root=delivery_root or self.delivery_root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            protected_official_roots=(self.official_root,),
            owner_id=owner_id,
        )

    def _gate_patches(self, *, passed: bool) -> ExitStack:
        stack = ExitStack()

        def hard(packet, reference, **kwargs):
            return _hard_report(packet, passed=passed)

        for target, replacement in (
            ("hypoweaver.local_recovery_runner.replay_ablations", _replay),
            ("hypoweaver.recovery_campaign.replay_ablations", _replay),
            ("hypoweaver.benchmark_protocol.replay_ablations", _replay),
            ("hypoweaver.local_recovery_runner.evaluate_hard_metrics", hard),
            ("hypoweaver.recovery_campaign.evaluate_hard_metrics", hard),
            ("hypoweaver.benchmark_protocol.evaluate_hard_metrics", hard),
        ):
            stack.enter_context(patch(target, side_effect=replacement))
        return stack

    async def _seed_qualified_round_artifacts(self) -> None:
        await self._seed_evaluated_round_artifacts(passed=True)

    async def _seed_evaluated_round_artifacts(self, *, passed: bool) -> None:
        backend = _FakeRecoveryBackend()
        campaign = self.store.load()
        reservation = self.store.reserve_round(
            owner_id="seed",
            lease_seconds=7200,
            now=NOW,
        )
        context = type(
            "Context",
            (),
            {
                "campaign_id": campaign.campaign_id,
                "round_id": "round-01",
                "freeze": campaign.freeze,
            },
        )()
        result = await backend.run_hypoweaver_round(context)
        packet = result.packet
        replay = _replay(packet)
        hard = _hard_report(packet, passed=passed)
        root = self.delivery_root / campaign.campaign_id / "round-01"
        root.mkdir(parents=True, exist_ok=True)
        (root / "hypoweaver_packet.json").write_text(
            json.dumps(packet.model_dump(mode="json")), encoding="utf-8"
        )
        (root / "fault_replay.json").write_text(
            json.dumps(replay.model_dump(mode="json")), encoding="utf-8"
        )
        (root / "hard_metrics.json").write_text(
            json.dumps(hard.model_dump(mode="json")), encoding="utf-8"
        )
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", return_value=hard),
        ):
            self.store.finalize_evaluated_round(
                owner_id="seed",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=hard,
                reference=self.reference,
                usage=result.usage,
                receipts=result.receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(index: int) -> str:
    return (
        datetime(2026, 7, 16, tzinfo=timezone.utc) + timedelta(seconds=index)
    ).isoformat()


def _model_receipts(round_id: str) -> list[ModelCallReceipt]:
    values = []
    for index, slot in enumerate(FIRST_ROUND_LOGICAL_SLOTS, start=1):
        group, prompt_key, _slot_index = slot.split(":")
        values.append(
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
                model="qwen-plus",
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
    return values


def _packet(*, system_id, packet_id, freeze, calls, input_tokens=0, output_tokens=0):
    native_artifacts = {}
    if system_id == "qwen_single_pass":
        native_artifacts = {
            "visible_input": freeze.visible_input_sha256,
            "single_pass_prompt": "a" * 64,
            "single_pass_config": "b" * 64,
            "single_pass_raw_response": canonical_sha256(
                {"phase": "qwen_single_pass", "index": 0}
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
            model_id="qwen-plus",
            design=NormalizedDesign(),
            resource_usage=BenchmarkResourceUsage(
                llm_calls=calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            native_artifact_sha256=native_artifacts,
        )
    )


def _replay(packet):
    outcomes = [
        FaultOutcome(fault_id=fault_id, detected=True, action="block")
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


def _hard_report(packet, *, passed):
    metrics = [
        HardMetric(
            metric_id=metric_id,
            numerator=int(passed or index > 0),
            denominator=1,
            value=float(passed or index > 0),
            target="test",
            passed=(passed or index > 0),
        )
        for index, metric_id in enumerate(HARD_METRIC_IDS)
    ]
    return HardMetricReport(
        report_id=f"hard-{packet.packet_id}",
        case_id=packet.case_id,
        packet_id=packet.packet_id,
        metrics=metrics,
        all_hard_gates_passed=all(item.passed for item in metrics),
        created_at=NOW,
    )


def _ratings():
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


def _blind_summary(freeze, hypoweaver, agent):
    reviews = []
    for sample_index in range(1, 6):
        receipt = PairedBlindCallReceipt(
            call_id=f"blind-{sample_index}",
            sample_index=sample_index,
            provider="qwen",
            model="qwen-plus",
            outcome="succeeded",
            response_sha256=f"{sample_index + 200:064x}",
            call_started_at=_timestamp(sample_index),
            call_completed_at=_timestamp(sample_index + 1),
        )
        reviews.append(
            NeurIPSReview(
                review_id=f"review-{sample_index}",
                sample_index=sample_index,
                label_order=freeze.sealed_label_orders[sample_index - 1],
                system_assignment=freeze.sealed_system_assignments[sample_index - 1],
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


def _comparison_receipts(
    campaign_id,
    comparison_id,
    phase,
    count,
    *,
    input_sha256=None,
):
    return tuple(
        create_recovery_call_receipt(
            campaign_id=campaign_id,
            round_id=comparison_id,
            phase=phase,
            provider="qwen",
            model="qwen-plus",
            call_started_at=NOW,
            call_completed_at=LATER,
            raw_response={"phase": phase, "index": index},
            call_id=f"{phase}-{index}",
            input_sha256=input_sha256,
            source_receipt_sha256=canonical_sha256(
                {"source_phase": phase, "source_index": index}
            ),
        )
        for index in range(count)
    )


def _map_blind_receipt(campaign_id, comparison_id, source):
    return create_recovery_call_receipt(
        campaign_id=campaign_id,
        round_id=comparison_id,
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


def _delivery_manifest(protocol):
    manifest = BenchmarkDeliveryManifest(
        protocol_sha256=str(protocol.protocol_sha256),
        case_id=protocol.case_id,
        official=False,
        file_sha256={},
        all_hard_gates_passed=True,
        claim_condition_met=True,
    )
    return manifest.model_copy(
        update={
            "manifest_sha256": canonical_sha256(
                manifest.model_dump(mode="json", exclude={"manifest_sha256"})
            )
        }
    )


if __name__ == "__main__":
    unittest.main()
