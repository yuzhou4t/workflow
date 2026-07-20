from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import model_validator

from .benchmark_evaluator import evaluate_hard_metrics, verify_benchmark_packet
from .benchmark_faults import replay_ablations
from .benchmark_models import (
    BenchmarkDeliveryManifest,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkUsageReport,
    FaultReplayReport,
    FrozenBenchmarkProtocol,
    HardMetricReport,
    PairedReviewSummary,
)
from .benchmark_protocol import run_benchmark_delivery
from .recovery_campaign import (
    RecoveryCampaignStore,
    verify_recovery_environment,
    verify_recovery_comparison_artifacts,
    verify_recovery_round_artifacts,
)
from .recovery_identity import hypoweaver_source_sha256
from .recovery_models import (
    HARD_METRIC_IDS,
    RecoveryCallReceipt,
    RecoveryCampaign,
    RecoveryFreeze,
    RecoveryModel,
    RecoveryRound,
    RecoveryRoundSubmission,
    RecoveryUsage,
)
from .seal import canonical_sha256


class LocalRecoveryRoundContext(RecoveryModel):
    campaign_id: str
    reservation_id: str
    round_id: str
    call_limit: int
    freeze: RecoveryFreeze


class LocalRecoveryRoundResult(RecoveryModel):
    status: Literal["completed", "technical_failed", "invalidated"]
    started_at: str
    completed_at: str
    usage: RecoveryUsage
    receipts: tuple[RecoveryCallReceipt, ...] = ()
    packet: BenchmarkPacket | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "LocalRecoveryRoundResult":
        if self.status == "completed":
            if self.packet is None or self.reason_code is not None:
                raise ValueError("completed recovery round requires only a packet")
        elif self.packet is not None or not self.reason_code:
            raise ValueError("failed recovery round requires only a reason_code")
        if len(self.receipts) != self.usage.llm_calls:
            raise ValueError("recovery round result receipts must match llm_calls")
        return self


class LocalRecoveryComparisonContext(RecoveryModel):
    campaign_id: str
    reservation_id: str
    comparison_id: Literal["comparison-01"] = "comparison-01"
    call_reserve: Literal[26] = 26
    freeze: RecoveryFreeze


class LocalRecoveryComparisonResult(RecoveryModel):
    status: Literal["completed", "technical_failed"]
    qwen_single_pass: RecoveryUsage
    agent_laboratory: RecoveryUsage
    blind_reviews: RecoveryUsage
    receipts: tuple[RecoveryCallReceipt, ...] = ()
    started_at: str
    completed_at: str
    qwen_packet: BenchmarkPacket | None = None
    agent_laboratory_packet: BenchmarkPacket | None = None
    blind_summary: PairedReviewSummary | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "LocalRecoveryComparisonResult":
        artifacts = (
            self.qwen_packet,
            self.agent_laboratory_packet,
            self.blind_summary,
        )
        if self.status == "completed":
            if any(value is None for value in artifacts) or self.reason_code is not None:
                raise ValueError("completed comparison requires all comparison artifacts")
        elif not self.reason_code:
            raise ValueError("technical_failed comparison requires reason_code")
        total = (
            self.qwen_single_pass.llm_calls
            + self.agent_laboratory.llm_calls
            + self.blind_reviews.llm_calls
        )
        if len(self.receipts) != total:
            raise ValueError("comparison result receipts must match llm_calls")
        return self


class LocalRecoveryBackend(Protocol):
    """Model-facing adapter supplied by the caller; the controller makes no calls."""

    async def run_hypoweaver_round(
        self,
        context: LocalRecoveryRoundContext,
    ) -> LocalRecoveryRoundResult: ...

    async def run_comparison(
        self,
        context: LocalRecoveryComparisonContext,
        qualified_packet: BenchmarkPacket,
        reference_summary: str,
    ) -> LocalRecoveryComparisonResult: ...


class LocalRecoveryRunner:
    """Non-official seen-case loop; it never begins or mutates an official attempt."""

    def __init__(
        self,
        *,
        store: RecoveryCampaignStore,
        backend: LocalRecoveryBackend,
        protocol: FrozenBenchmarkProtocol,
        reference: BenchmarkReference,
        source_artifact_root: Path,
        delivery_root: Path,
        visible_input_path: Path,
        data_paths: tuple[Path, ...],
        reference_path: Path,
        reference_summary_path: Path,
        protected_official_roots: tuple[Path, ...],
        predecessor_campaign_path: Path | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.protocol = protocol
        self.reference = reference
        self.source_artifact_root = source_artifact_root
        self.delivery_root = delivery_root
        self.visible_input_path = visible_input_path
        self.data_paths = data_paths
        self.reference_path = reference_path
        self.reference_summary_path = reference_summary_path
        self.reference_summary = reference_summary_path.read_text(encoding="utf-8")
        self.protected_official_roots = tuple(
            path.resolve() for path in protected_official_roots
        )
        self.predecessor_campaign_path = (
            predecessor_campaign_path.resolve(strict=True)
            if predecessor_campaign_path is not None
            else None
        )
        self._verify_path_isolation()
        self.owner_id = owner_id or f"local-recovery-{uuid4()}"

    async def run(self) -> RecoveryCampaign:
        campaign = self.store.load()
        try:
            self._verify_frozen_inputs(campaign)
            qualified_packet = self._verify_persisted_evidence(campaign)
        except Exception:
            return self.store.invalidate("recovery_frozen_evidence_verification_failed")

        while campaign.status == "open":
            try:
                self._verify_frozen_inputs(campaign)
            except Exception:
                return self.store.invalidate(
                    "recovery_environment_drift_before_round_call"
                )
            reservation = self.store.reserve_round(
                owner_id=self.owner_id,
                lease_seconds=7200,
            )
            if reservation is None:
                return self.store.load()
            campaign = self.store.load()
            context = LocalRecoveryRoundContext(
                campaign_id=campaign.campaign_id,
                reservation_id=reservation.reservation_id,
                round_id=reservation.round_id,
                call_limit=reservation.call_limit,
                freeze=campaign.freeze,
            )
            try:
                raw_result = await self.backend.run_hypoweaver_round(context)
                result = LocalRecoveryRoundResult.model_validate(raw_result)
            except Exception:
                return self.store.invalidate(
                    "round_backend_exception_without_usage_evidence",
                    charge_active_reservation=True,
                )
            try:
                campaign, qualified_packet = self._record_round(
                    campaign,
                    context,
                    result,
                )
            except Exception:
                return self.store.invalidate(
                    "round_result_contract_invalid",
                    charge_active_reservation=True,
                )

        if (
            campaign.status == "qualified_seen_case"
            and campaign.comparison is None
        ):
            try:
                self._verify_frozen_inputs(campaign)
            except Exception:
                return self.store.invalidate(
                    "recovery_environment_drift_before_comparison_call"
                )
            if qualified_packet is None:
                qualified_packet = self._load_qualified_packet(campaign)
            if qualified_packet is None:
                return self.store.invalidate("qualified_packet_artifact_missing")
            campaign = await self._run_comparison(campaign, qualified_packet)
        return campaign

    def _record_round(
        self,
        campaign: RecoveryCampaign,
        context: LocalRecoveryRoundContext,
        result: LocalRecoveryRoundResult,
    ) -> tuple[RecoveryCampaign, BenchmarkPacket | None]:
        if result.status != "completed":
            submission = RecoveryRoundSubmission(
                freeze_sha256=str(campaign.freeze.freeze_sha256),
                call_limit=context.call_limit,
                implementation_sha256=hypoweaver_source_sha256(),
                started_at=result.started_at,
                completed_at=result.completed_at,
                usage=result.usage,
                receipts=result.receipts,
                technical_failure=(
                    result.reason_code
                    if result.status == "technical_failed"
                    else None
                ),
                invalidation_reason=(
                    result.reason_code if result.status == "invalidated" else None
                ),
            )
            self._verify_frozen_inputs(self.store.load())
            return (
                self.store.finalize_terminal_round(
                    owner_id=self.owner_id,
                    reservation_id=context.reservation_id,
                    submission=submission,
                ),
                None,
            )

        packet = result.packet
        assert packet is not None
        try:
            self._verify_round_packet(campaign, packet, result.usage)
            replay = replay_ablations(packet)
            hard_report = evaluate_hard_metrics(
                packet,
                self.reference,
                fault_outcomes=replay.full_system_outcomes,
                clean_false_block_count=replay.clean_false_block_count,
            )
            if {metric.metric_id for metric in hard_report.metrics} != set(
                HARD_METRIC_IDS
            ):
                raise ValueError("hard metric registry mismatch")
            replay_payload = replay.model_dump(mode="json")
            hard_payload = hard_report.model_dump(mode="json")
            self._write_round_artifacts(
                campaign,
                context.round_id,
                packet,
                hard_payload,
                replay_payload,
            )
        except Exception:
            invalid = RecoveryRoundSubmission(
                freeze_sha256=str(campaign.freeze.freeze_sha256),
                call_limit=context.call_limit,
                implementation_sha256=hypoweaver_source_sha256(),
                started_at=result.started_at,
                completed_at=result.completed_at,
                usage=result.usage,
                receipts=result.receipts,
                invalidation_reason="round_artifact_or_evaluation_invalid",
            )
            self._verify_frozen_inputs(self.store.load())
            return (
                self.store.finalize_terminal_round(
                    owner_id=self.owner_id,
                    reservation_id=context.reservation_id,
                    submission=invalid,
                ),
                None,
            )
        self._verify_frozen_inputs(self.store.load())
        updated = self.store.finalize_evaluated_round(
            owner_id=self.owner_id,
            reservation_id=context.reservation_id,
            packet=packet,
            fault_replay=replay,
            hard_metric_report=hard_report,
            reference=self.reference,
            usage=result.usage,
            receipts=result.receipts,
            started_at=result.started_at,
            completed_at=result.completed_at,
        )
        return updated, packet if updated.status == "qualified_seen_case" else None

    async def _run_comparison(
        self,
        campaign: RecoveryCampaign,
        qualified_packet: BenchmarkPacket,
    ) -> RecoveryCampaign:
        if not self.reference_summary.strip():
            return self.store.invalidate("comparison_reference_summary_missing")
        reservation = self.store.reserve_comparison(
            owner_id=self.owner_id,
            lease_seconds=7200,
        )
        if reservation is None:
            return self.store.load()
        campaign = self.store.load()
        context = LocalRecoveryComparisonContext(
            campaign_id=campaign.campaign_id,
            reservation_id=reservation.reservation_id,
            freeze=campaign.freeze,
        )
        try:
            raw_result = await self.backend.run_comparison(
                context,
                qualified_packet,
                self.reference_summary,
            )
            result = LocalRecoveryComparisonResult.model_validate(raw_result)
        except Exception:
            return self.store.invalidate(
                "comparison_backend_exception_without_usage_evidence",
                charge_active_reservation=True,
            )

        common = {
            "qwen_single_pass": result.qwen_single_pass,
            "agent_laboratory": result.agent_laboratory,
            "blind_reviews": result.blind_reviews,
            "receipts": result.receipts,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
        }
        if result.status == "technical_failed":
            try:
                self._verify_frozen_inputs(self.store.load())
            except Exception:
                return self.store.invalidate(
                    "recovery_environment_drift_before_comparison_finalize"
                )
            return self.store.finalize_comparison(
                owner_id=self.owner_id,
                reservation_id=reservation.reservation_id,
                status="technical_failed",
                technical_failure=result.reason_code,
                **common,
            )

        qwen = result.qwen_packet
        agent = result.agent_laboratory_packet
        blind = result.blind_summary
        assert qwen is not None and agent is not None and blind is not None
        try:
            self._verify_comparison_artifacts(
                campaign,
                result,
                qualified_packet,
            )
            output_dir = self._campaign_artifact_root(campaign) / "comparison"
            manifest = run_benchmark_delivery(
                protocol=self.protocol,
                reference=self.reference,
                qwen_packet=qwen,
                agent_laboratory_packet=agent,
                hypoweaver_packet=qualified_packet,
                blind_summary=blind,
                output_dir=output_dir,
                official=False,
            )
            if manifest.official or not manifest.manifest_sha256:
                raise ValueError("recovery comparison produced an invalid manifest")
            (
                persisted_qwen,
                persisted_agent,
                persisted_hypoweaver,
                persisted_blind,
                persisted_manifest,
            ) = self._load_delivery_bundle(campaign)
            if (
                persisted_qwen != qwen
                or persisted_agent != agent
                or persisted_hypoweaver != qualified_packet
                or persisted_blind != blind
                or persisted_manifest != manifest
            ):
                raise ValueError("recovery delivery differs from its source artifacts")
        except Exception:
            try:
                self._verify_frozen_inputs(self.store.load())
            except Exception:
                return self.store.invalidate(
                    "recovery_environment_drift_before_comparison_finalize"
                )
            return self.store.finalize_comparison(
                owner_id=self.owner_id,
                reservation_id=reservation.reservation_id,
                status="technical_failed",
                technical_failure="comparison_delivery_validation_failed",
                **common,
            )
        try:
            self._verify_frozen_inputs(self.store.load())
        except Exception:
            return self.store.invalidate(
                "recovery_environment_drift_before_comparison_finalize"
            )
        return self.store.finalize_comparison(
            owner_id=self.owner_id,
            reservation_id=reservation.reservation_id,
            status="completed",
            qwen_packet=persisted_qwen,
            agent_packet=persisted_agent,
            qualified_packet=persisted_hypoweaver,
            blind_summary=persisted_blind,
            delivery_manifest=persisted_manifest,
            delivery_root=output_dir,
            **common,
        )

    def _verify_frozen_inputs(self, campaign: RecoveryCampaign) -> None:
        loaded_reference = verify_recovery_environment(
            campaign.freeze,
            self.protocol,
            artifact_root=self.source_artifact_root,
            visible_input_path=self.visible_input_path,
            data_paths=self.data_paths,
            reference_path=self.reference_path,
            reference_summary_path=self.reference_summary_path,
            predecessor_campaign_path=self.predecessor_campaign_path,
        )
        if loaded_reference != self.reference:
            raise ValueError("in-memory reference differs from its frozen file")
        current_summary = self.reference_summary_path.read_text(encoding="utf-8")
        if current_summary != self.reference_summary:
            raise ValueError("in-memory reference summary differs from its frozen file")

    def _verify_path_isolation(self) -> None:
        if not self.protected_official_roots:
            raise ValueError("protected_official_roots must identify the official outputs")
        writable_paths = (
            self.delivery_root.resolve(),
            self.store.path.resolve(),
            self.store.lock_path.resolve(),
        )
        if self.predecessor_campaign_path is not None and any(
            _paths_overlap(path, self.predecessor_campaign_path)
            for path in writable_paths
        ):
            raise ValueError("recovery writable path overlaps predecessor campaign")
        for protected in self.protected_official_roots:
            if any(_paths_overlap(path, protected) for path in writable_paths):
                raise ValueError("recovery writable path overlaps a protected official root")
        source_root = self.source_artifact_root.resolve()
        if any(_paths_overlap(path, source_root) for path in writable_paths):
            raise ValueError("recovery writable path overlaps the frozen source root")

    def _verify_persisted_evidence(
        self,
        campaign: RecoveryCampaign,
    ) -> BenchmarkPacket | None:
        qualified_packet: BenchmarkPacket | None = None
        for round_record in campaign.rounds:
            if round_record.status not in {
                "hard_gate_failed",
                "hard_gate_qualified",
            }:
                continue
            packet = self._load_round_artifacts(campaign, round_record)
            if round_record.status == "hard_gate_qualified":
                qualified_packet = packet
        comparison = campaign.comparison
        if comparison is None:
            return qualified_packet
        if comparison.status == "technical_failed":
            verify_recovery_comparison_artifacts(
                campaign,
                qwen_packet=None,
                agent_packet=None,
                qualified_packet=None,
                blind_summary=None,
                delivery_manifest=None,
            )
            return qualified_packet
        if qualified_packet is None:
            raise ValueError("completed comparison has no qualified packet")
        qwen, agent, delivered_hypoweaver, blind, manifest = (
            self._load_delivery_bundle(campaign)
        )
        if delivered_hypoweaver != qualified_packet:
            raise ValueError("delivered HypoWeaver packet differs from qualified round")
        verify_recovery_comparison_artifacts(
            campaign,
            qwen_packet=qwen,
            agent_packet=agent,
            qualified_packet=qualified_packet,
            blind_summary=blind,
            delivery_manifest=manifest,
        )
        return qualified_packet

    def _load_round_artifacts(
        self,
        campaign: RecoveryCampaign,
        round_record: RecoveryRound,
    ) -> BenchmarkPacket:
        root = self._campaign_artifact_root(campaign) / round_record.round_id
        packet = BenchmarkPacket.model_validate_json(
            (root / "hypoweaver_packet.json").read_text(encoding="utf-8")
        )
        hard_report = HardMetricReport.model_validate_json(
            (root / "hard_metrics.json").read_text(encoding="utf-8")
        )
        replay = FaultReplayReport.model_validate_json(
            (root / "fault_replay.json").read_text(encoding="utf-8")
        )
        verify_recovery_round_artifacts(
            campaign,
            round_record,
            packet=packet,
            fault_replay=replay,
            hard_metric_report=hard_report,
            reference=self.reference,
        )
        return packet

    def _load_delivery_bundle(
        self,
        campaign: RecoveryCampaign,
    ) -> tuple[
        BenchmarkPacket,
        BenchmarkPacket,
        BenchmarkPacket,
        PairedReviewSummary,
        BenchmarkDeliveryManifest,
    ]:
        root = (self._campaign_artifact_root(campaign) / "comparison").resolve()
        manifest = BenchmarkDeliveryManifest.model_validate_json(
            (root / "delivery_manifest.json").read_text(encoding="utf-8")
        )
        expected_paths = {
            "frozen_protocol.json",
            "neutral_packets/qwen_single_pass.json",
            "neutral_packets/agent_laboratory.json",
            "neutral_packets/hypoweaver.json",
            "hard_metrics.json",
            "ablations.json",
            "blind_reviews.json",
            *(f"blind_reviews/review-{index}.json" for index in range(1, 6)),
            "resource_usage.json",
            "comparison_report_zh.md",
        }
        if set(manifest.file_sha256) != expected_paths:
            raise ValueError("recovery delivery manifest file registry mismatch")
        for relative_path, expected_hash in manifest.file_sha256.items():
            artifact = (root / relative_path).resolve()
            if not artifact.is_relative_to(root) or not artifact.is_file():
                raise ValueError("recovery delivery artifact is missing or escapes its root")
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected_hash:
                raise ValueError(f"recovery delivery file hash mismatch: {relative_path}")
        unsigned = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
        if (
            manifest.manifest_sha256 is None
            or canonical_sha256(unsigned) != manifest.manifest_sha256
        ):
            raise ValueError("recovery delivery manifest sha256 mismatch")
        delivered_protocol = FrozenBenchmarkProtocol.model_validate_json(
            (root / "frozen_protocol.json").read_text(encoding="utf-8")
        )
        if delivered_protocol != self.protocol:
            raise ValueError("recovery delivery protocol differs from frozen protocol")
        qwen = BenchmarkPacket.model_validate_json(
            (root / "neutral_packets/qwen_single_pass.json").read_text(
                encoding="utf-8"
            )
        )
        agent = BenchmarkPacket.model_validate_json(
            (root / "neutral_packets/agent_laboratory.json").read_text(
                encoding="utf-8"
            )
        )
        hypoweaver = BenchmarkPacket.model_validate_json(
            (root / "neutral_packets/hypoweaver.json").read_text(encoding="utf-8")
        )
        blind = PairedReviewSummary.model_validate_json(
            (root / "blind_reviews.json").read_text(encoding="utf-8")
        )
        for review in blind.reviews:
            persisted = type(review).model_validate_json(
                (root / "blind_reviews" / f"review-{review.sample_index}.json").read_text(
                    encoding="utf-8"
                )
            )
            if persisted != review:
                raise ValueError("recovery delivery review differs from blind summary")
        usage = BenchmarkUsageReport.model_validate_json(
            (root / "resource_usage.json").read_text(encoding="utf-8")
        )
        if (
            usage.qwen_single_pass != qwen.resource_usage
            or usage.agent_laboratory != agent.resource_usage
            or usage.hypoweaver != hypoweaver.resource_usage
        ):
            raise ValueError("recovery delivery packet usage mismatch")
        return qwen, agent, hypoweaver, blind, manifest

    def _verify_round_packet(
        self,
        campaign: RecoveryCampaign,
        packet: BenchmarkPacket,
        usage: RecoveryUsage,
    ) -> None:
        verify_benchmark_packet(packet)
        if packet.system_id != "hypoweaver" or packet.official_receipts:
            raise ValueError("recovery round requires a non-official HypoWeaver packet")
        self._verify_packet_identity(campaign, packet)
        if _recovery_usage(packet.resource_usage.model_dump(mode="json")) != usage:
            raise ValueError("recovery round packet usage mismatch")

    def _verify_comparison_artifacts(
        self,
        campaign: RecoveryCampaign,
        result: LocalRecoveryComparisonResult,
        qualified_packet: BenchmarkPacket,
    ) -> None:
        assert result.qwen_packet is not None
        assert result.agent_laboratory_packet is not None
        assert result.blind_summary is not None
        for expected, packet, usage in (
            ("qwen_single_pass", result.qwen_packet, result.qwen_single_pass),
            ("agent_laboratory", result.agent_laboratory_packet, result.agent_laboratory),
        ):
            verify_benchmark_packet(packet)
            if packet.system_id != expected or packet.official_receipts:
                raise ValueError("recovery comparison packet provenance mismatch")
            self._verify_packet_identity(campaign, packet)
            if _recovery_usage(packet.resource_usage.model_dump(mode="json")) != usage:
                raise ValueError("recovery comparison packet usage mismatch")
        blind = result.blind_summary
        reviews_by_sample = {review.sample_index: review for review in blind.reviews}
        if (
            blind.case_id != campaign.freeze.case_id
            or blind.packet_a_id != qualified_packet.packet_id
            or blind.packet_b_id != result.agent_laboratory_packet.packet_id
            or len(blind.reviews) != 5
            or set(reviews_by_sample) != {1, 2, 3, 4, 5}
            or any(review.official_receipt is not None for review in blind.reviews)
        ):
            raise ValueError("recovery blind summary provenance mismatch")
        for sample_index in range(1, 6):
            review = reviews_by_sample[sample_index]
            if (
                review.label_order
                != campaign.freeze.sealed_label_orders[sample_index - 1]
                or review.system_assignment
                != campaign.freeze.sealed_system_assignments[sample_index - 1]
                or review.call_receipt is None
                or review.call_receipt.outcome != "succeeded"
                or review.call_receipt.provider != "qwen"
            ):
                raise ValueError("recovery blind schedule or receipt mismatch")
        recovery_blind_by_call = {
            receipt.call_id: receipt
            for receipt in result.receipts
            if receipt.phase == "blind_review"
        }
        if len(recovery_blind_by_call) != 5:
            raise ValueError("recovery blind receipts must contain five unique calls")
        for review in blind.reviews:
            source = review.call_receipt
            assert source is not None
            mapped = recovery_blind_by_call.get(source.call_id)
            if mapped is None or (
                mapped.provider != source.provider
                or mapped.model != source.model
                or mapped.response_sha256 != source.response_sha256
                or mapped.input_tokens != source.input_tokens
                or mapped.output_tokens != source.output_tokens
                or mapped.call_started_at != source.call_started_at
                or mapped.call_completed_at != source.call_completed_at
                or mapped.source_receipt_sha256
                != canonical_sha256(source.model_dump(mode="json"))
            ):
                raise ValueError("recovery blind receipt source binding mismatch")
        blind_usage = RecoveryUsage(
            llm_calls=sum(review.resource_usage.llm_calls for review in blind.reviews),
            input_tokens=sum(review.resource_usage.input_tokens for review in blind.reviews),
            output_tokens=sum(review.resource_usage.output_tokens for review in blind.reviews),
            wall_time_seconds=sum(
                review.resource_usage.wall_time_seconds for review in blind.reviews
            ),
            technical_failures=tuple(
                failure
                for review in blind.reviews
                for failure in review.resource_usage.technical_failures
            ),
        )
        if blind_usage != result.blind_reviews:
            raise ValueError("recovery blind review usage mismatch")

    def _verify_packet_identity(
        self,
        campaign: RecoveryCampaign,
        packet: BenchmarkPacket,
    ) -> None:
        if (
            packet.case_id != campaign.freeze.case_id
            or packet.visible_input_sha256 != campaign.freeze.visible_input_sha256
            or tuple(packet.data_sha256) != campaign.freeze.data_sha256
        ):
            raise ValueError("recovery packet input identity mismatch")

    def _write_round_artifacts(
        self,
        campaign: RecoveryCampaign,
        round_id: str,
        packet: BenchmarkPacket,
        hard_report: dict[str, object],
        replay: dict[str, object],
    ) -> None:
        root = self._campaign_artifact_root(campaign) / round_id
        _write_frozen_json(root / "hypoweaver_packet.json", packet.model_dump(mode="json"))
        _write_frozen_json(root / "hard_metrics.json", hard_report)
        _write_frozen_json(root / "fault_replay.json", replay)

    def _load_qualified_packet(
        self,
        campaign: RecoveryCampaign,
    ) -> BenchmarkPacket | None:
        qualified = next(
            (
                round_record
                for round_record in reversed(campaign.rounds)
                if round_record.status == "hard_gate_qualified"
            ),
            None,
        )
        if qualified is None:
            return None
        return self._load_round_artifacts(campaign, qualified)

    def _campaign_artifact_root(self, campaign: RecoveryCampaign) -> Path:
        return self.delivery_root / campaign.campaign_id


def _recovery_usage(payload: dict[str, object]) -> RecoveryUsage:
    return RecoveryUsage(
        llm_calls=int(payload.get("llm_calls", 0) or 0),
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        wall_time_seconds=float(payload.get("wall_time_seconds", 0) or 0),
        technical_failures=tuple(
            str(value) for value in (payload.get("technical_failures") or [])
        ),
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _write_frozen_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"recovery artifact is unreadable: {path}") from error
        if canonical_sha256(existing) != canonical_sha256(payload):
            raise ValueError(f"recovery artifact is append-only: {path}")
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RuntimeError(f"recovery artifact creation raced: {path}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _claim_call(path: Path, payload: object) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    return True
