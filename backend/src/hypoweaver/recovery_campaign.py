from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel

from .benchmark_evaluator import evaluate_hard_metrics, verify_benchmark_packet
from .benchmark_faults import replay_ablations
from .benchmark_models import (
    ABLATION_IDS,
    FAULT_IDS,
    BenchmarkDeliveryManifest,
    BenchmarkPacket,
    BenchmarkReference,
    FaultReplayReport,
    FrozenBenchmarkProtocol,
    HardMetricReport,
    OfficialAttemptBinding,
    PairedReviewSummary,
)
from .benchmark_protocol import (
    hash_protocol_artifacts,
    official_holdout_lock_id,
    verify_protocol,
)
from .recovery_identity import (
    FIRST_ROUND_LOGICAL_SLOTS,
    evaluator_identity_sha256,
    fault_matrix_identity_sha256,
    hypoweaver_source_sha256,
    prompt_registry_identity_sha256,
    research_runtime_identity_sha256,
)
from .models import ModelCallErrorCategory, ModelCallReceipt
from .recovery_models import (
    RecoveryCallReceipt,
    RecoveryCampaign,
    RecoveryComparison,
    RecoveryComparisonReservation,
    RecoveryComparisonSubmission,
    RecoveryFreeze,
    RecoveryInvalidation,
    RecoveryPredecessorBinding,
    RecoveryPredecessorCarryover,
    RecoveryRound,
    RecoveryRoundReservation,
    RecoveryRoundStatus,
    RecoveryRoundSubmission,
    RecoveryUsage,
    PriorUsageEvidence,
    PriorUsageImport,
)
from .seal import canonical_sha256


TOTAL_CALL_CEILING = 120
COMPARISON_CALL_RESERVE = 26
RECOVERY_ROUND_MIN_CALLS = 9
RECOVERY_ROUND_MAX_CALLS = 20
MAX_RECOVERY_ROUNDS = 6


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_recovery_freeze(
    protocol: FrozenBenchmarkProtocol,
    *,
    artifact_root: Path,
    visible_input_path: Path,
    data_paths: tuple[Path, ...],
    reference_path: Path,
    reference_summary_path: Path,
    predecessor_campaign: RecoveryCampaign | None = None,
    frozen_at: str | None = None,
) -> RecoveryFreeze:
    """Freeze the live recovery inputs while retaining the old official binding."""

    verify_protocol(protocol)
    if (
        set(protocol.source_artifact_paths)
        != {"hypoweaver", "agent_laboratory", "benchmark_harness"}
        or any(not paths for paths in protocol.source_artifact_paths.values())
        or not protocol.configuration_artifact_paths
    ):
        raise ValueError("recovery freeze requires explicit source and config paths")
    visible_sha256 = _file_sha256(visible_input_path)
    data_sha256 = tuple(_file_sha256(path) for path in data_paths)
    if visible_sha256 != protocol.visible_input_sha256:
        raise ValueError("visible recovery input differs from the official case")
    if data_sha256 != tuple(protocol.data_sha256):
        raise ValueError("recovery data differs from the official case")
    predecessor_binding = None
    label_orders = _random_balanced_orders()
    system_assignments = _random_balanced_orders()
    if predecessor_campaign is not None:
        _validate_recovery_predecessor(
            predecessor_campaign,
            protocol=protocol,
            visible_input_sha256=visible_sha256,
            data_sha256=data_sha256,
        )
        predecessor_binding = RecoveryPredecessorBinding(
            predecessor_campaign_id=predecessor_campaign.campaign_id,
            predecessor_campaign_sha256=predecessor_campaign.campaign_sha256,
            predecessor_freeze_sha256=str(
                predecessor_campaign.freeze.freeze_sha256
            ),
            predecessor_invalidation_sha256=(
                predecessor_campaign.invalidation.invalidation_sha256
            ),
            predecessor_cumulative_llm_calls=cumulative_llm_calls(
                predecessor_campaign
            ),
            predecessor_incremental_llm_calls=(
                cumulative_llm_calls(predecessor_campaign)
                - predecessor_campaign.prior_usage.usage.llm_calls
            ),
            predecessor_started_round_count=(
                _predecessor_started_round_count(predecessor_campaign)
            ),
            predecessor_prior_usage_content_sha256=(
                _prior_usage_content_sha256(predecessor_campaign.prior_usage)
            ),
            predecessor_known_usage=_predecessor_known_usage(
                predecessor_campaign
            ),
            predecessor_unknown_llm_calls=_predecessor_unknown_llm_calls(
                predecessor_campaign
            ),
        )
        label_orders = predecessor_campaign.freeze.sealed_label_orders
        system_assignments = (
            predecessor_campaign.freeze.sealed_system_assignments
        )
    reference = BenchmarkReference.model_validate(_load_json(reference_path))
    reference_sha256 = canonical_sha256(reference.model_dump(mode="json"))
    if reference_sha256 != protocol.reference_sha256:
        raise ValueError("recovery reference differs from the official case")
    _verify_reference_identity(reference, protocol)
    current_sources, current_configuration = hash_protocol_artifacts(
        artifact_root=artifact_root,
        source_artifact_paths=protocol.source_artifact_paths,
        configuration_artifact_paths=protocol.configuration_artifact_paths,
    )
    freeze = RecoveryFreeze(
        case_id=protocol.case_id,
        visible_input_sha256=visible_sha256,
        data_sha256=data_sha256,
        reference_sha256=reference_sha256,
        reference_summary_sha256=_file_sha256(reference_summary_path),
        evaluator_sha256=evaluator_identity_sha256(),
        fault_matrix_sha256=fault_matrix_identity_sha256(),
        prompt_registry_sha256=prompt_registry_identity_sha256(),
        research_runtime_identity_sha256=research_runtime_identity_sha256(),
        configuration_sha256=current_configuration,
        benchmark_harness_sha256=current_sources["benchmark_harness"],
        hypoweaver_source_sha256=hypoweaver_source_sha256(),
        agent_laboratory_sha256=current_sources["agent_laboratory"],
        source_official_protocol_sha256=str(protocol.protocol_sha256),
        source_official_holdout_lock_id=official_holdout_lock_id(protocol),
        sealed_label_orders=label_orders,
        sealed_system_assignments=system_assignments,
        predecessor_binding=predecessor_binding,
        frozen_at=frozen_at or _utc_now(),
    )
    return seal_recovery_freeze(freeze)


def _random_balanced_orders() -> tuple[str, ...]:
    random_source = secrets.SystemRandom()
    values = ["A_B", "B_A", *(random_source.choice(("A_B", "B_A")) for _ in range(3))]
    random_source.shuffle(values)
    return tuple(values)


def _validate_recovery_predecessor(
    predecessor: RecoveryCampaign,
    *,
    protocol: FrozenBenchmarkProtocol,
    visible_input_sha256: str,
    data_sha256: tuple[str, ...],
) -> None:
    verify_recovery_campaign(predecessor)
    if predecessor.status != "invalidated" or predecessor.invalidation is None:
        raise ValueError("recovery predecessor must be invalidated")
    if (
        predecessor.comparison is not None
        or predecessor.active_round_reservation is not None
        or predecessor.active_comparison_reservation is not None
    ):
        raise ValueError("recovery predecessor cannot have active or comparison work")
    incremental_calls = (
        cumulative_llm_calls(predecessor)
        - predecessor.prior_usage.usage.llm_calls
    )
    inherited_calls = _predecessor_carryover_calls(predecessor)
    finalized_calls = sum(item.usage.llm_calls for item in predecessor.rounds)
    immediate_charge = predecessor.invalidation.conservative_llm_call_charge
    if incremental_calls != inherited_calls + finalized_calls + immediate_charge:
        raise ValueError("recovery predecessor incremental accounting mismatch")
    if immediate_charge and (
        predecessor.invalidation.reservation_scope != "round"
        or not predecessor.invalidation.unknown_call_evidence
    ):
        raise ValueError("recovery predecessor carryover is not conservative round usage")
    known_usage = _predecessor_known_usage(predecessor)
    unknown_calls = _predecessor_unknown_llm_calls(predecessor)
    if known_usage.llm_calls + unknown_calls != incremental_calls:
        raise ValueError("recovery predecessor known usage accounting mismatch")
    if (
        predecessor.freeze.case_id != protocol.case_id
        or predecessor.freeze.visible_input_sha256 != visible_input_sha256
        or predecessor.freeze.data_sha256 != data_sha256
        or predecessor.freeze.source_official_protocol_sha256
        != protocol.protocol_sha256
        or predecessor.freeze.source_official_holdout_lock_id
        != official_holdout_lock_id(protocol)
    ):
        raise ValueError("recovery predecessor is bound to another frozen case")


def verify_recovery_predecessor_binding(
    freeze: RecoveryFreeze,
    predecessor: RecoveryCampaign,
    *,
    protocol: FrozenBenchmarkProtocol,
) -> None:
    binding = freeze.predecessor_binding
    if binding is None:
        raise ValueError("recovery freeze has no predecessor binding")
    _validate_recovery_predecessor(
        predecessor,
        protocol=protocol,
        visible_input_sha256=freeze.visible_input_sha256,
        data_sha256=freeze.data_sha256,
    )
    legacy_binding = (
        binding.predecessor_invalidation_sha256 is None
        and binding.predecessor_started_round_count is None
        and binding.predecessor_prior_usage_content_sha256 is None
    )
    usage_accounting_present = (
        binding.predecessor_known_usage is not None
        and binding.predecessor_unknown_llm_calls is not None
    )
    expected = RecoveryPredecessorBinding(
        predecessor_campaign_id=predecessor.campaign_id,
        predecessor_campaign_sha256=predecessor.campaign_sha256,
        predecessor_freeze_sha256=str(predecessor.freeze.freeze_sha256),
        predecessor_invalidation_sha256=(
            None
            if legacy_binding
            else predecessor.invalidation.invalidation_sha256
        ),
        predecessor_cumulative_llm_calls=cumulative_llm_calls(predecessor),
        predecessor_incremental_llm_calls=(
            cumulative_llm_calls(predecessor)
            - predecessor.prior_usage.usage.llm_calls
        ),
        predecessor_started_round_count=(
            None
            if legacy_binding
            else _predecessor_started_round_count(predecessor)
        ),
        predecessor_prior_usage_content_sha256=(
            None
            if legacy_binding
            else _prior_usage_content_sha256(predecessor.prior_usage)
        ),
        predecessor_known_usage=(
            _predecessor_known_usage(predecessor)
            if usage_accounting_present
            else None
        ),
        predecessor_unknown_llm_calls=(
            _predecessor_unknown_llm_calls(predecessor)
            if usage_accounting_present
            else None
        ),
    )
    if binding != expected:
        raise ValueError("recovery predecessor binding mismatch")


def _predecessor_started_round_count(predecessor: RecoveryCampaign) -> int:
    inherited = (
        predecessor.predecessor_carryover.started_round_count
        if predecessor.predecessor_carryover is not None
        else 0
    )
    current_invalidation = int(
        predecessor.invalidation is not None
        and predecessor.invalidation.reservation_scope == "round"
    )
    return inherited + len(predecessor.rounds) + current_invalidation


def _predecessor_known_usage(predecessor: RecoveryCampaign) -> RecoveryUsage:
    inherited = (
        predecessor.predecessor_carryover.known_usage
        if predecessor.predecessor_carryover is not None
        and predecessor.predecessor_carryover.known_usage is not None
        else RecoveryUsage()
    )
    usages = (inherited, *(item.usage for item in predecessor.rounds))
    return RecoveryUsage(
        llm_calls=sum(item.llm_calls for item in usages),
        input_tokens=sum(item.input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        wall_time_seconds=sum(item.wall_time_seconds for item in usages),
        technical_failures=tuple(
            failure
            for item in usages
            for failure in item.technical_failures
        ),
    )


def _predecessor_unknown_llm_calls(predecessor: RecoveryCampaign) -> int:
    inherited = 0
    if predecessor.predecessor_carryover is not None:
        inherited = (
            predecessor.predecessor_carryover.unknown_llm_calls
            if predecessor.predecessor_carryover.unknown_llm_calls is not None
            else predecessor.predecessor_carryover.conservative_llm_calls
        )
    immediate = (
        predecessor.invalidation.conservative_llm_call_charge
        if predecessor.invalidation is not None
        else 0
    )
    return inherited + immediate


def _prior_usage_content_sha256(prior: PriorUsageImport) -> str:
    return canonical_sha256(
        prior.model_dump(
            mode="json",
            exclude={"imported_at", "import_sha256"},
        )
    )


def verify_recovery_environment(
    freeze: RecoveryFreeze,
    protocol: FrozenBenchmarkProtocol,
    *,
    artifact_root: Path,
    visible_input_path: Path,
    data_paths: tuple[Path, ...],
    reference_path: Path,
    reference_summary_path: Path,
    predecessor_campaign_path: Path | None = None,
) -> BenchmarkReference:
    """Re-read and re-hash every frozen input before a recovery model call."""

    verify_recovery_freeze(freeze)
    verify_protocol(protocol)
    if freeze.predecessor_binding is None:
        if predecessor_campaign_path is not None:
            raise ValueError("unbound recovery freeze cannot accept a predecessor")
    else:
        if predecessor_campaign_path is None:
            raise ValueError("bound recovery freeze requires its predecessor campaign")
        predecessor_payload = _load_json(
            predecessor_campaign_path.resolve(strict=True)
        )
        predecessor = RecoveryCampaign.model_validate(predecessor_payload)
        verify_recovery_predecessor_binding(
            freeze,
            predecessor,
            protocol=protocol,
        )
    if (
        freeze.source_official_protocol_sha256 != protocol.protocol_sha256
        or freeze.source_official_holdout_lock_id != official_holdout_lock_id(protocol)
        or freeze.case_id != protocol.case_id
    ):
        raise ValueError("recovery freeze is bound to another official protocol")
    if _file_sha256(visible_input_path) != freeze.visible_input_sha256:
        raise ValueError("frozen visible input hash drift")
    if tuple(_file_sha256(path) for path in data_paths) != freeze.data_sha256:
        raise ValueError("frozen recovery data hash drift")
    reference = BenchmarkReference.model_validate(_load_json(reference_path))
    if canonical_sha256(reference.model_dump(mode="json")) != freeze.reference_sha256:
        raise ValueError("frozen reference hash drift")
    _verify_reference_identity(reference, protocol)
    if _file_sha256(reference_summary_path) != freeze.reference_summary_sha256:
        raise ValueError("frozen reference summary hash drift")
    current_sources, current_configuration = hash_protocol_artifacts(
        artifact_root=artifact_root,
        source_artifact_paths=protocol.source_artifact_paths,
        configuration_artifact_paths=protocol.configuration_artifact_paths,
    )
    current_identities = {
        "configuration": current_configuration,
        "benchmark_harness": current_sources["benchmark_harness"],
        "hypoweaver": hypoweaver_source_sha256(),
        "agent_laboratory": current_sources["agent_laboratory"],
        "evaluator": evaluator_identity_sha256(),
        "fault_matrix": fault_matrix_identity_sha256(),
        "prompt_registry": prompt_registry_identity_sha256(),
        "research_runtime": research_runtime_identity_sha256(),
    }
    frozen_identities = {
        "configuration": freeze.configuration_sha256,
        "benchmark_harness": freeze.benchmark_harness_sha256,
        "hypoweaver": freeze.hypoweaver_source_sha256,
        "agent_laboratory": freeze.agent_laboratory_sha256,
        "evaluator": freeze.evaluator_sha256,
        "fault_matrix": freeze.fault_matrix_sha256,
        "prompt_registry": freeze.prompt_registry_sha256,
        "research_runtime": freeze.research_runtime_identity_sha256,
    }
    drifted = sorted(
        name
        for name, expected in frozen_identities.items()
        if current_identities[name] != expected
    )
    if drifted:
        raise ValueError("recovery environment hash drift: " + ", ".join(drifted))
    return reference


def seal_recovery_freeze(freeze: RecoveryFreeze) -> RecoveryFreeze:
    payload = freeze.model_dump(mode="json", exclude={"freeze_sha256"})
    expected = canonical_sha256(payload)
    if freeze.freeze_sha256 is not None and freeze.freeze_sha256 != expected:
        raise ValueError("recovery freeze sha256 mismatch")
    return freeze.model_copy(update={"freeze_sha256": expected})


def verify_recovery_freeze(freeze: RecoveryFreeze) -> None:
    if freeze.freeze_sha256 is None:
        raise ValueError("recovery freeze is not sealed")
    payload = freeze.model_dump(mode="json", exclude={"freeze_sha256"})
    if canonical_sha256(payload) != freeze.freeze_sha256:
        raise ValueError("recovery freeze sha256 mismatch")


def import_prior_usage(
    binding: OfficialAttemptBinding,
    *,
    source_official_holdout_lock_id: str,
    usage: RecoveryUsage,
    official_receipt_sha256: tuple[str, ...],
    imported_at: str | None = None,
) -> PriorUsageImport:
    ledger_sha256 = canonical_sha256(usage.model_dump(mode="json"))
    evidence = PriorUsageEvidence(
        evidence_status="complete_receipts",
        resource_ledger_sha256=ledger_sha256,
        ledger_llm_calls=usage.llm_calls,
        verified_receipt_sha256=official_receipt_sha256,
        missing_receipt_count=0,
    )
    prior = PriorUsageImport(
        source_official_attempt_id=binding.attempt_id,
        source_official_run_manifest_sha256=binding.run_manifest_sha256,
        source_official_holdout_lock_id=source_official_holdout_lock_id,
        usage=usage,
        evidence=evidence,
        imported_at=imported_at or _utc_now(),
    )
    return seal_prior_usage_import(prior)


def import_prior_usage_from_ledger(
    binding: OfficialAttemptBinding,
    *,
    source_official_holdout_lock_id: str,
    usage: RecoveryUsage,
    resource_ledger_sha256: str,
    verified_receipt_sha256: tuple[str, ...] = (),
    token_usage_status: str = "exact",
    imported_at: str | None = None,
) -> PriorUsageImport:
    """Import an old resource ledger while preserving, never filling, receipt gaps."""

    missing = usage.llm_calls - len(verified_receipt_sha256)
    if missing < 0:
        raise ValueError("verified receipt count exceeds ledger llm_calls")
    evidence_status = "complete_receipts"
    limitation_codes: list[str] = []
    if missing:
        evidence_status = (
            "ledger_only" if not verified_receipt_sha256 else "partial_receipts"
        )
        limitation_codes.append("legacy_official_receipts_unavailable")
    if token_usage_status == "lower_bound":
        limitation_codes.append("legacy_single_pass_tokens_unavailable")
    prior = PriorUsageImport(
        source_official_attempt_id=binding.attempt_id,
        source_official_run_manifest_sha256=binding.run_manifest_sha256,
        source_official_holdout_lock_id=source_official_holdout_lock_id,
        usage=usage,
        evidence=PriorUsageEvidence(
            evidence_status=evidence_status,
            resource_ledger_sha256=resource_ledger_sha256,
            ledger_llm_calls=usage.llm_calls,
            verified_receipt_sha256=verified_receipt_sha256,
            missing_receipt_count=missing,
            token_usage_status=token_usage_status,
            limitation_codes=tuple(limitation_codes),
        ),
        imported_at=imported_at or _utc_now(),
    )
    return seal_prior_usage_import(prior)


def seal_prior_usage_import(prior: PriorUsageImport) -> PriorUsageImport:
    payload = prior.model_dump(mode="json", exclude={"import_sha256"})
    expected = canonical_sha256(payload)
    if prior.import_sha256 is not None and prior.import_sha256 != expected:
        raise ValueError("prior usage import sha256 mismatch")
    return prior.model_copy(update={"import_sha256": expected})


def verify_prior_usage_import(prior: PriorUsageImport) -> None:
    if prior.import_sha256 is None:
        raise ValueError("prior usage import is not sealed")
    payload = prior.model_dump(mode="json", exclude={"import_sha256"})
    if canonical_sha256(payload) != prior.import_sha256:
        raise ValueError("prior usage import sha256 mismatch")


def campaign_id_for_freeze(freeze: RecoveryFreeze) -> str:
    identity = {
        "campaign_identity_version": 1,
        "provenance_scope": "seen_case_recovery_non_official",
        "source_official_holdout_lock_id": freeze.source_official_holdout_lock_id,
    }
    if freeze.predecessor_binding is not None:
        identity = {
            **identity,
            "campaign_identity_version": 2,
            "predecessor_binding": freeze.predecessor_binding.model_dump(
                mode="json"
            ),
        }
    digest = canonical_sha256(identity)
    return f"recovery-campaign-{digest[:32]}"


def canonical_recovery_campaign_path(
    state_root: Path,
    freeze: RecoveryFreeze,
) -> Path:
    """Return the separate stable path for one seen-case recovery campaign."""

    return state_root / f"{campaign_id_for_freeze(freeze)}.json"


def create_recovery_campaign(
    freeze: RecoveryFreeze,
    prior_usage: PriorUsageImport,
    *,
    created_at: str | None = None,
) -> RecoveryCampaign:
    freeze = seal_recovery_freeze(freeze)
    prior_usage = seal_prior_usage_import(prior_usage)
    if (
        freeze.source_official_holdout_lock_id
        != prior_usage.source_official_holdout_lock_id
    ):
        raise ValueError("prior usage import is bound to another official holdout")
    predecessor_carryover = None
    if freeze.predecessor_binding is not None:
        binding = freeze.predecessor_binding
        prior_content_sha256 = _prior_usage_content_sha256(prior_usage)
        if (
            binding.predecessor_prior_usage_content_sha256 is not None
            and prior_content_sha256
            != binding.predecessor_prior_usage_content_sha256
        ):
            raise ValueError("replacement prior usage provenance differs from predecessor")
        predecessor_prior_calls = (
            binding.predecessor_cumulative_llm_calls
            - binding.predecessor_incremental_llm_calls
        )
        if prior_usage.usage.llm_calls != predecessor_prior_calls:
            raise ValueError("replacement campaign prior usage differs from predecessor")
        if binding.predecessor_incremental_llm_calls:
            if (
                binding.predecessor_invalidation_sha256 is None
                or binding.predecessor_started_round_count is None
            ):
                raise ValueError("replacement predecessor accounting is incomplete")
            predecessor_carryover = RecoveryPredecessorCarryover(
                predecessor_campaign_id=binding.predecessor_campaign_id,
                predecessor_campaign_sha256=binding.predecessor_campaign_sha256,
                predecessor_invalidation_sha256=(
                    binding.predecessor_invalidation_sha256
                ),
                conservative_llm_calls=(
                    binding.predecessor_incremental_llm_calls
                ),
                started_round_count=binding.predecessor_started_round_count,
                known_usage=binding.predecessor_known_usage,
                unknown_llm_calls=binding.predecessor_unknown_llm_calls,
            )
    carryover_calls = (
        predecessor_carryover.conservative_llm_calls
        if predecessor_carryover is not None
        else 0
    )
    if (
        prior_usage.usage.llm_calls
        + carryover_calls
        + COMPARISON_CALL_RESERVE
        > TOTAL_CALL_CEILING
    ):
        raise ValueError("prior usage leaves no valid cumulative budget")
    timestamp = created_at or _utc_now()
    capacity = (
        TOTAL_CALL_CEILING
        - prior_usage.usage.llm_calls
        - carryover_calls
        - COMPARISON_CALL_RESERVE
    )
    inherited_started_rounds = (
        predecessor_carryover.started_round_count
        if predecessor_carryover is not None
        else 0
    )
    if inherited_started_rounds >= MAX_RECOVERY_ROUNDS:
        status = "exhausted"
        reason = "max_rounds_reached"
    elif capacity < RECOVERY_ROUND_MIN_CALLS:
        status = "exhausted"
        reason = "insufficient_recovery_pool"
    else:
        status = "open"
        reason = None
    payload = {
        "campaign_id": campaign_id_for_freeze(freeze),
        "freeze": freeze.model_dump(mode="json"),
        "prior_usage": prior_usage.model_dump(mode="json"),
        **(
            {
                "predecessor_carryover": predecessor_carryover.model_dump(
                    mode="json"
                )
            }
            if predecessor_carryover is not None
            else {}
        ),
        "status": status,
        "protocol_status": _protocol_status(status),
        "status_reason": reason,
        "cumulative_token_usage_status": (
            "lower_bound"
            if predecessor_carryover is not None
            else prior_usage.evidence.token_usage_status
        ),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    campaign = RecoveryCampaign(
        **payload,
        campaign_sha256=canonical_sha256(
            {
                "campaign_version": 1,
                "provenance_scope": "seen_case_recovery_non_official",
                "official": False,
                **payload,
                "total_call_ceiling": TOTAL_CALL_CEILING,
                "comparison_call_reserve": COMPARISON_CALL_RESERVE,
                "recovery_round_min_calls": RECOVERY_ROUND_MIN_CALLS,
                "recovery_round_max_calls": RECOVERY_ROUND_MAX_CALLS,
                "max_rounds": MAX_RECOVERY_ROUNDS,
                "rounds": [],
                "active_round_reservation": None,
                "active_comparison_reservation": None,
                "comparison": None,
                "invalidation": None,
            }
        ),
    )
    verify_recovery_campaign(campaign)
    return campaign


def recovery_pool_capacity(campaign: RecoveryCampaign) -> int:
    return (
        campaign.total_call_ceiling
        - campaign.comparison_call_reserve
        - campaign.prior_usage.usage.llm_calls
        - _predecessor_carryover_calls(campaign)
    )


def recovery_pool_remaining(campaign: RecoveryCampaign) -> int:
    remaining = recovery_pool_capacity(campaign) - sum(
        round_record.usage.llm_calls for round_record in campaign.rounds
    )
    if campaign.active_round_reservation is not None:
        remaining -= campaign.active_round_reservation.call_limit
    elif (
        campaign.invalidation is not None
        and campaign.invalidation.reservation_scope == "round"
    ):
        remaining -= campaign.invalidation.conservative_llm_call_charge
    return remaining


def _committed_recovery_pool_remaining(campaign: RecoveryCampaign) -> int:
    return recovery_pool_capacity(campaign) - sum(
        round_record.usage.llm_calls for round_record in campaign.rounds
    )


def cumulative_llm_calls(campaign: RecoveryCampaign) -> int:
    comparison_calls = 0
    if campaign.comparison is not None:
        comparison_calls = _comparison_calls(campaign.comparison)
    conservative_charge = (
        campaign.invalidation.conservative_llm_call_charge
        if campaign.invalidation is not None
        else 0
    )
    return (
        campaign.prior_usage.usage.llm_calls
        + _predecessor_carryover_calls(campaign)
        + sum(round_record.usage.llm_calls for round_record in campaign.rounds)
        + comparison_calls
        + conservative_charge
    )


def _predecessor_carryover_calls(campaign: RecoveryCampaign) -> int:
    carryover = campaign.predecessor_carryover
    return carryover.conservative_llm_calls if carryover is not None else 0


def _committed_started_rounds(campaign: RecoveryCampaign) -> int:
    inherited = (
        campaign.predecessor_carryover.started_round_count
        if campaign.predecessor_carryover is not None
        else 0
    )
    return inherited + len(campaign.rounds)


def append_recovery_round(
    campaign: RecoveryCampaign,
    submission: RecoveryRoundSubmission,
    *,
    updated_at: str | None = None,
) -> RecoveryCampaign:
    raise RuntimeError("recovery rounds require atomic reserve/finalize")


def _append_finalized_round(
    campaign: RecoveryCampaign,
    submission: RecoveryRoundSubmission,
    *,
    reservation_id: str | None,
    updated_at: str | None = None,
) -> RecoveryCampaign:
    verify_recovery_campaign(campaign)
    if campaign.status != "open":
        raise RuntimeError(f"recovery campaign is terminal: {campaign.status}")
    if submission.freeze_sha256 != campaign.freeze.freeze_sha256:
        return invalidate_recovery_campaign(
            campaign,
            "recovery_round_freeze_mismatch",
            invalidated_at=updated_at,
        )
    remaining_before = _committed_recovery_pool_remaining(campaign)
    if submission.call_limit > remaining_before:
        raise ValueError("round call_limit exceeds the remaining recovery pool")
    if _committed_started_rounds(campaign) >= campaign.max_rounds:
        raise RuntimeError("recovery campaign has reached max_rounds")

    round_index = len(campaign.rounds) + 1
    round_id = f"round-{round_index:02d}"
    _validate_recovery_receipt_binding(
        submission.receipts,
        campaign_id=campaign.campaign_id,
        round_id=round_id,
    )
    _reject_duplicate_call_ids(campaign, submission.receipts)
    status = _round_status(submission)
    previous_round_sha256 = (
        campaign.rounds[-1].round_sha256 if campaign.rounds else None
    )
    round_payload = {
        "reservation_id": reservation_id,
        "round_id": round_id,
        "round_index": round_index,
        "status": status,
        **submission.model_dump(mode="json"),
        "previous_round_sha256": previous_round_sha256,
    }
    round_record = RecoveryRound(
        **round_payload,
        round_sha256=canonical_sha256(round_payload),
    )
    rounds = (*campaign.rounds, round_record)
    remaining_after = remaining_before - submission.usage.llm_calls
    if status == "hard_gate_qualified":
        campaign_status = "qualified_seen_case"
        reason = "first_hard_gate_qualified"
    elif status == "invalidated":
        campaign_status = "invalidated"
        reason = submission.invalidation_reason
    elif (
        _committed_started_rounds(campaign) + 1
        >= campaign.max_rounds
    ):
        campaign_status = "exhausted"
        reason = "max_rounds_reached"
    elif remaining_after < campaign.recovery_round_min_calls:
        campaign_status = "exhausted"
        reason = "recovery_pool_exhausted"
    else:
        campaign_status = "open"
        reason = None
    updated = _updated_campaign(
        campaign,
        rounds=rounds,
        active_round_reservation=None,
        status=campaign_status,
        status_reason=reason,
        updated_at=updated_at or _utc_now(),
    )
    verify_recovery_campaign(updated)
    return updated


def invalidate_recovery_campaign(
    campaign: RecoveryCampaign,
    reason: str,
    *,
    invalidated_at: str | None = None,
    conservative_llm_call_charge: int = 0,
) -> RecoveryCampaign:
    verify_recovery_campaign(campaign)
    if campaign.status == "invalidated":
        raise RuntimeError("recovery campaign is already invalidated")
    active_round = campaign.active_round_reservation
    active_comparison = campaign.active_comparison_reservation
    if active_round is not None and active_comparison is not None:
        raise ValueError("campaign cannot hold round and comparison reservations")
    active_call_limit = (
        active_round.call_limit
        if active_round is not None
        else active_comparison.call_limit
        if active_comparison is not None
        else None
    )
    if active_call_limit is not None:
        if conservative_llm_call_charge not in (0, active_call_limit):
            raise ValueError(
                "unknown reservation usage must charge the full active call_limit"
            )
        conservative_llm_call_charge = active_call_limit
    elif conservative_llm_call_charge:
        raise ValueError("cannot conservatively charge without an active reservation")
    timestamp = invalidated_at or _utc_now()
    reservation_id = (
        active_round.reservation_id
        if active_round is not None
        else active_comparison.reservation_id
        if active_comparison is not None
        else None
    )
    reservation_scope = (
        "round"
        if active_round is not None
        else "comparison"
        if active_comparison is not None
        else None
    )
    invalidation_payload = {
        "reason": reason,
        "reservation_id": reservation_id,
        "reservation_scope": reservation_scope,
        "unknown_call_evidence": bool(conservative_llm_call_charge),
        "conservative_llm_call_charge": conservative_llm_call_charge,
        "invalidated_at": timestamp,
    }
    invalidation = RecoveryInvalidation(
        **invalidation_payload,
        invalidation_sha256=canonical_sha256(invalidation_payload),
    )
    updated = _updated_campaign(
        campaign,
        invalidation=invalidation,
        active_round_reservation=None,
        active_comparison_reservation=None,
        status="invalidated",
        status_reason=reason,
        updated_at=timestamp,
    )
    verify_recovery_campaign(updated)
    return updated


def record_recovery_comparison(
    campaign: RecoveryCampaign,
    submission: RecoveryComparisonSubmission,
    *,
    updated_at: str | None = None,
) -> RecoveryCampaign:
    if campaign.active_comparison_reservation is not None:
        raise RuntimeError("active comparison reservation must be finalized atomically")
    raise RuntimeError("recovery comparison requires atomic reserve/finalize")


def _record_finalized_comparison(
    campaign: RecoveryCampaign,
    submission: RecoveryComparisonSubmission,
    *,
    reservation_id: str,
    updated_at: str | None = None,
) -> RecoveryCampaign:
    verify_recovery_campaign(campaign)
    if campaign.status != "qualified_seen_case":
        raise RuntimeError("comparison requires a qualified recovery round")
    if campaign.comparison is not None:
        raise RuntimeError("recovery comparison is one-shot")
    if submission.freeze_sha256 != campaign.freeze.freeze_sha256:
        return invalidate_recovery_campaign(
            campaign,
            "comparison_freeze_mismatch",
            invalidated_at=updated_at,
        )
    _validate_recovery_receipt_binding(
        submission.receipts,
        campaign_id=campaign.campaign_id,
        round_id="comparison-01",
    )
    _reject_duplicate_call_ids(campaign, submission.receipts)
    comparison_payload = {
        **submission.model_dump(mode="json"),
        "comparison_id": "comparison-01",
        "reservation_id": reservation_id,
    }
    comparison = RecoveryComparison(
        **comparison_payload,
        comparison_sha256=canonical_sha256(comparison_payload),
    )
    updated = _updated_campaign(
        campaign,
        comparison=comparison,
        active_comparison_reservation=None,
        updated_at=updated_at or _utc_now(),
    )
    verify_recovery_campaign(updated)
    return updated


def verify_recovery_campaign(campaign: RecoveryCampaign) -> None:
    verify_recovery_freeze(campaign.freeze)
    verify_prior_usage_import(campaign.prior_usage)
    if campaign.campaign_id != campaign_id_for_freeze(campaign.freeze):
        raise ValueError("recovery campaign id mismatch")
    if (
        campaign.freeze.source_official_holdout_lock_id
        != campaign.prior_usage.source_official_holdout_lock_id
    ):
        raise ValueError("recovery campaign prior usage lock mismatch")
    binding = campaign.freeze.predecessor_binding
    carryover = campaign.predecessor_carryover
    if binding is None:
        if carryover is not None:
            raise ValueError("legacy campaign cannot carry predecessor usage")
    else:
        prior_content_sha256 = _prior_usage_content_sha256(campaign.prior_usage)
        if (
            binding.predecessor_prior_usage_content_sha256 is not None
            and prior_content_sha256
            != binding.predecessor_prior_usage_content_sha256
        ):
            raise ValueError("replacement prior usage provenance differs from predecessor")
        predecessor_prior_calls = (
            binding.predecessor_cumulative_llm_calls
            - binding.predecessor_incremental_llm_calls
        )
        if campaign.prior_usage.usage.llm_calls != predecessor_prior_calls:
            raise ValueError("replacement campaign prior usage differs from predecessor")
        if binding.predecessor_incremental_llm_calls == 0:
            if carryover is not None:
                raise ValueError("zero-call predecessor cannot carry usage")
        else:
            expected_carryover = RecoveryPredecessorCarryover(
                predecessor_campaign_id=binding.predecessor_campaign_id,
                predecessor_campaign_sha256=binding.predecessor_campaign_sha256,
                predecessor_invalidation_sha256=(
                    binding.predecessor_invalidation_sha256
                ),
                conservative_llm_calls=(
                    binding.predecessor_incremental_llm_calls
                ),
                started_round_count=(
                    binding.predecessor_started_round_count or 0
                ),
                known_usage=binding.predecessor_known_usage,
                unknown_llm_calls=binding.predecessor_unknown_llm_calls,
            )
            if carryover != expected_carryover:
                raise ValueError("replacement predecessor carryover mismatch")

    previous_hash: str | None = None
    qualified_seen = False
    invalidated_round_seen = False
    call_ids: set[str] = set()
    used_before = 0
    capacity = recovery_pool_capacity(campaign)
    reservation = campaign.active_round_reservation
    comparison_reservation = campaign.active_comparison_reservation
    if reservation is not None and comparison_reservation is not None:
        raise ValueError("recovery campaign has overlapping reservations")
    if reservation is not None:
        reservation_unsigned = reservation.model_dump(
            mode="json",
            exclude={"reservation_sha256"},
        )
        if canonical_sha256(reservation_unsigned) != reservation.reservation_sha256:
            raise ValueError("recovery round reservation sha256 mismatch")
        expected_index = len(campaign.rounds) + 1
        if (
            reservation.round_index != expected_index
            or reservation.round_id != f"round-{expected_index:02d}"
            or reservation.freeze_sha256 != campaign.freeze.freeze_sha256
            or reservation.call_limit > _committed_recovery_pool_remaining(campaign)
        ):
            raise ValueError("recovery round reservation binding mismatch")
    if comparison_reservation is not None:
        unsigned = comparison_reservation.model_dump(
            mode="json",
            exclude={"reservation_sha256"},
        )
        if canonical_sha256(unsigned) != comparison_reservation.reservation_sha256:
            raise ValueError("recovery comparison reservation sha256 mismatch")
        if (
            campaign.status != "qualified_seen_case"
            or campaign.comparison is not None
            or comparison_reservation.freeze_sha256
            != campaign.freeze.freeze_sha256
        ):
            raise ValueError("recovery comparison reservation binding mismatch")
    for expected_index, round_record in enumerate(campaign.rounds, start=1):
        if round_record.round_index != expected_index:
            raise ValueError("recovery round index is not append-only")
        if round_record.round_id != f"round-{expected_index:02d}":
            raise ValueError("recovery round id mismatch")
        if round_record.previous_round_sha256 != previous_hash:
            raise ValueError("recovery round hash chain mismatch")
        if round_record.freeze_sha256 != campaign.freeze.freeze_sha256:
            raise ValueError("recovery round freeze mismatch")
        unsigned = round_record.model_dump(mode="json", exclude={"round_sha256"})
        if canonical_sha256(unsigned) != round_record.round_sha256:
            raise ValueError("recovery round sha256 mismatch")
        submission = RecoveryRoundSubmission.model_validate(
            round_record.model_dump(
                mode="json",
                exclude={
                    "round_id",
                    "round_index",
                    "reservation_id",
                    "status",
                    "previous_round_sha256",
                    "round_sha256",
                },
            )
        )
        if round_record.status != _round_status(submission):
            raise ValueError("recovery round terminal status mismatch")
        if qualified_seen or invalidated_round_seen:
            raise ValueError("terminal recovery round must be the final round")
        qualified_seen = round_record.status == "hard_gate_qualified"
        invalidated_round_seen = round_record.status == "invalidated"
        if round_record.call_limit > capacity - used_before:
            raise ValueError("recovery round call_limit exceeded remaining pool")
        used_before += round_record.usage.llm_calls
        _validate_recovery_receipt_binding(
            round_record.receipts,
            campaign_id=campaign.campaign_id,
            round_id=round_record.round_id,
        )
        _validate_round_usage_evidence(
            round_record.receipts,
            round_record.usage,
            require_complete=(
                round_record.status
                in {"hard_gate_qualified", "hard_gate_failed"}
            ),
        )
        for receipt in round_record.receipts:
            if receipt.call_id in call_ids:
                raise ValueError("recovery receipt call_id was reused")
            call_ids.add(receipt.call_id)
        previous_hash = round_record.round_sha256

    if used_before > capacity:
        raise ValueError("recovery rounds exceeded the recovery pool")

    if campaign.comparison is not None:
        if not qualified_seen:
            raise ValueError("comparison exists without a qualified recovery round")
        comparison_unsigned = campaign.comparison.model_dump(
            mode="json",
            exclude={"comparison_sha256"},
        )
        if canonical_sha256(comparison_unsigned) != campaign.comparison.comparison_sha256:
            raise ValueError("recovery comparison sha256 mismatch")
        comparison_submission = RecoveryComparisonSubmission.model_validate(
            campaign.comparison.model_dump(
                mode="json",
                exclude={"comparison_id", "reservation_id", "comparison_sha256"},
            )
        )
        if _comparison_calls(comparison_submission) > campaign.comparison_call_reserve:
            raise ValueError("recovery comparison exceeded its reserve")
        _validate_recovery_receipt_binding(
            campaign.comparison.receipts,
            campaign_id=campaign.campaign_id,
            round_id="comparison-01",
        )
        for receipt in campaign.comparison.receipts:
            if receipt.call_id in call_ids:
                raise ValueError("recovery receipt call_id was reused")
            call_ids.add(receipt.call_id)

    if campaign.invalidation is not None:
        unsigned_invalidation = campaign.invalidation.model_dump(
            mode="json",
            exclude={"invalidation_sha256"},
        )
        if (
            canonical_sha256(unsigned_invalidation)
            != campaign.invalidation.invalidation_sha256
        ):
            raise ValueError("recovery invalidation sha256 mismatch")

    expected_status, expected_reason = _project_campaign_status(campaign)
    if campaign.status != expected_status:
        raise ValueError("recovery campaign status mismatch")
    if campaign.status_reason != expected_reason:
        raise ValueError("recovery campaign status reason mismatch")
    if cumulative_llm_calls(campaign) > campaign.total_call_ceiling:
        raise ValueError("recovery campaign exceeded cumulative call ceiling")
    unsigned_campaign = campaign.model_dump(mode="json", exclude={"campaign_sha256"})
    if canonical_sha256(unsigned_campaign) != campaign.campaign_sha256:
        raise ValueError("recovery campaign sha256 mismatch")


def create_recovery_call_receipt(
    *,
    campaign_id: str,
    round_id: str,
    phase: str,
    provider: str,
    model: str,
    call_started_at: str,
    call_completed_at: str,
    raw_response: Any | None = None,
    raw_response_sha256: str | None = None,
    call_id: str | None = None,
    logical_slot_id: str | None = None,
    logical_call_id: str | None = None,
    call_group: str | None = None,
    prompt_key: str | None = None,
    prompt_version: str | None = None,
    attempt_type: str | None = None,
    attempt_index: int = 1,
    max_attempts: int = 3,
    outcome: str = "succeeded",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_type: str | None = None,
    error_category: ModelCallErrorCategory | None = None,
    input_sha256: str | None = None,
    output_schema_sha256: str | None = None,
    provider_response_id_sha256: str | None = None,
    source_receipt_sha256: str | None = None,
) -> RecoveryCallReceipt:
    if (raw_response is None) == (raw_response_sha256 is None):
        raise ValueError("provide exactly one of raw_response or raw_response_sha256")
    if raw_response_sha256 is None:
        if isinstance(raw_response, bytes):
            response_sha256 = hashlib.sha256(raw_response).hexdigest()
        elif isinstance(raw_response, str):
            response_sha256 = hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
        else:
            response_sha256 = canonical_sha256(raw_response)
    else:
        response_sha256 = raw_response_sha256
    return RecoveryCallReceipt(
        call_id=call_id or str(uuid4()),
        campaign_id=campaign_id,
        round_id=round_id,
        phase=phase,
        logical_slot_id=logical_slot_id,
        logical_call_id=logical_call_id,
        call_group=call_group,
        prompt_key=prompt_key,
        prompt_version=prompt_version,
        attempt_type=attempt_type,
        attempt_index=attempt_index,
        max_attempts=max_attempts,
        outcome=outcome,
        provider=provider,
        model=model,
        response_sha256=response_sha256,
        input_sha256=input_sha256,
        output_schema_sha256=output_schema_sha256,
        provider_response_id_sha256=provider_response_id_sha256,
        source_receipt_sha256=source_receipt_sha256,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error_type=error_type,
        error_category=error_category,
        call_started_at=call_started_at,
        call_completed_at=call_completed_at,
    )


def map_model_call_receipts(
    receipts: list[ModelCallReceipt],
    *,
    campaign_id: str,
    round_id: str,
    require_complete: bool = True,
) -> tuple[RecoveryCallReceipt, ...]:
    """Map workflow receipts onto frozen slots in code-owned launch order."""

    by_logical_id: dict[str, list[ModelCallReceipt]] = {}
    for receipt in receipts:
        by_logical_id.setdefault(receipt.logical_call_id, []).append(receipt)
    slots_by_prompt: dict[tuple[str, str], list[str]] = {}
    for slot in FIRST_ROUND_LOGICAL_SLOTS:
        call_group, prompt_key, _index = slot.split(":")
        slots_by_prompt.setdefault((call_group, prompt_key), []).append(slot)
    logical_ids_by_prompt: dict[tuple[str, str], list[str]] = {}
    for logical_call_id, attempts in by_logical_id.items():
        identities = {(item.call_group, item.prompt_key) for item in attempts}
        if len(identities) != 1:
            raise ValueError("logical call receipts disagree on group or prompt")
        logical_ids_by_prompt.setdefault(next(iter(identities)), []).append(
            logical_call_id
        )
    unknown_identities = set(logical_ids_by_prompt) - set(slots_by_prompt)
    if unknown_identities:
        raise ValueError("workflow receipts contain an unknown frozen prompt")
    if require_complete and set(logical_ids_by_prompt) != set(slots_by_prompt):
        raise ValueError("workflow receipts do not cover the frozen prompt registry")
    slot_for_logical_id: dict[str, str] = {}
    for identity, logical_ids in logical_ids_by_prompt.items():
        slots = slots_by_prompt[identity]
        started_by_logical_id = {
            logical_id: min(
                datetime.fromisoformat(item.started_at)
                for item in by_logical_id[logical_id]
            )
            for logical_id in logical_ids
        }
        if len(set(started_by_logical_id.values())) != len(started_by_logical_id):
            raise ValueError(
                "same-prompt logical calls require unique code-owned start times"
            )
        logical_ids = sorted(
            logical_ids,
            key=lambda logical_id: started_by_logical_id[logical_id],
        )
        ordered_slots = sorted(slots, key=lambda value: int(value.rsplit(":", 1)[1]))
        if len(logical_ids) > len(ordered_slots) or (
            require_complete and len(logical_ids) != len(ordered_slots)
        ):
            raise ValueError("workflow receipts do not cover all nine logical slots")
        slot_for_logical_id.update(zip(logical_ids, ordered_slots))

    mapped = tuple(
        RecoveryCallReceipt(
            call_id=receipt.call_id,
            campaign_id=campaign_id,
            round_id=round_id,
            phase="recovery_round",
            logical_slot_id=slot_for_logical_id[receipt.logical_call_id],
            logical_call_id=receipt.logical_call_id,
            call_group=receipt.call_group,
            prompt_key=receipt.prompt_key,
            prompt_version=receipt.prompt_version,
            attempt_type=receipt.attempt_type,
            attempt_index=receipt.attempt_index,
            max_attempts=receipt.max_attempts,
            outcome=receipt.outcome,
            provider=receipt.provider,
            model=receipt.model,
            response_sha256=receipt.response_sha256,
            input_sha256=receipt.input_sha256,
            output_schema_sha256=receipt.output_schema_sha256,
            provider_response_id_sha256=receipt.provider_response_id_sha256,
            source_receipt_sha256=canonical_sha256(receipt.model_dump(mode="json")),
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            error_type=receipt.error_type,
            error_category=receipt.error_category,
            call_started_at=receipt.started_at,
            call_completed_at=receipt.completed_at,
        )
        for receipt in receipts
    )
    _validate_slot_receipts(mapped, require_complete=require_complete)
    return mapped


def _validate_required_slot_receipts(
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    _validate_slot_receipts(receipts, require_complete=True)


def _validate_slot_receipts(
    receipts: tuple[RecoveryCallReceipt, ...],
    *,
    require_complete: bool,
) -> None:
    by_slot: dict[str, list[RecoveryCallReceipt]] = {}
    for receipt in receipts:
        if receipt.logical_slot_id is None:
            raise ValueError("evaluated recovery receipt is missing logical_slot_id")
        by_slot.setdefault(receipt.logical_slot_id, []).append(receipt)
    if set(by_slot) - set(FIRST_ROUND_LOGICAL_SLOTS):
        raise ValueError("recovery round contains an unknown logical slot")
    if require_complete and set(by_slot) != set(FIRST_ROUND_LOGICAL_SLOTS):
        raise ValueError("evaluated recovery round must cover all nine logical slots")
    for slot, attempts in by_slot.items():
        call_group, prompt_key, _index = slot.split(":")
        if any(
            item.provider != "qwen"
            or item.phase != "recovery_round"
            or item.call_group != call_group
            or item.prompt_key != prompt_key
            for item in attempts
        ):
            raise ValueError(f"logical slot receipt identity mismatch: {slot}")
        logical_ids = {item.logical_call_id for item in attempts}
        if None in logical_ids or len(logical_ids) != 1:
            raise ValueError(f"logical slot must bind one logical_call_id: {slot}")
        if len({item.max_attempts for item in attempts}) != 1:
            raise ValueError(f"logical slot max_attempts changed: {slot}")
        ordered = sorted(attempts, key=lambda item: item.attempt_index)
        if [item.attempt_index for item in ordered] != list(
            range(1, len(ordered) + 1)
        ):
            raise ValueError(f"logical slot attempts must be contiguous: {slot}")
        for index, item in enumerate(ordered):
            if item.attempt_type == "content_repair" and (
                index == 0 or ordered[index - 1].outcome != "succeeded"
            ):
                raise ValueError(
                    f"logical slot content repair must follow success: {slot}"
                )
            if (
                index < len(ordered) - 1
                and item.outcome == "succeeded"
                and ordered[index + 1].attempt_type != "content_repair"
            ):
                raise ValueError(
                    "successful logical slot may continue only with content repair: "
                    f"{slot}"
                )
        if require_complete and ordered[-1].outcome != "succeeded":
            raise ValueError(f"completed logical slot must end in success: {slot}")


def _recompute_evaluated_submission(
    *,
    campaign: RecoveryCampaign,
    round_id: str,
    call_limit: int,
    packet: BenchmarkPacket,
    supplied_fault_replay: FaultReplayReport,
    supplied_hard_report: HardMetricReport,
    reference: BenchmarkReference,
    usage: RecoveryUsage,
    receipts: tuple[RecoveryCallReceipt, ...],
    started_at: str,
    completed_at: str,
) -> RecoveryRoundSubmission:
    """Rebuild every admission fact from sealed artifacts and code registries."""

    freeze = campaign.freeze
    current_identities = {
        "evaluator": evaluator_identity_sha256(),
        "fault_matrix": fault_matrix_identity_sha256(),
        "prompt_registry": prompt_registry_identity_sha256(),
        "research_runtime": research_runtime_identity_sha256(),
        "hypoweaver_source": hypoweaver_source_sha256(),
    }
    frozen_identities = {
        "evaluator": freeze.evaluator_sha256,
        "fault_matrix": freeze.fault_matrix_sha256,
        "prompt_registry": freeze.prompt_registry_sha256,
        "research_runtime": freeze.research_runtime_identity_sha256,
        "hypoweaver_source": freeze.hypoweaver_source_sha256,
    }
    drifted = sorted(
        name
        for name, frozen in frozen_identities.items()
        if current_identities[name] != frozen
    )
    if drifted:
        raise ValueError("recovery code identity drift: " + ", ".join(drifted))

    verify_benchmark_packet(packet)
    if packet.system_id != "hypoweaver" or packet.official_receipts:
        raise ValueError("recovery qualification requires a non-official packet")
    if (
        packet.case_id != freeze.case_id
        or packet.visible_input_sha256 != freeze.visible_input_sha256
        or tuple(packet.data_sha256) != freeze.data_sha256
    ):
        raise ValueError("recovery packet input identity mismatch")
    if canonical_sha256(reference.model_dump(mode="json")) != freeze.reference_sha256:
        raise ValueError("recovery reference hash mismatch")
    if (
        reference.case_id != freeze.case_id
        or reference.visible_input_sha256 != freeze.visible_input_sha256
        or tuple(reference.data_sha256) != freeze.data_sha256
    ):
        raise ValueError("recovery reference identity mismatch")
    if _usage_from_packet(packet) != usage:
        raise ValueError("recovery packet usage mismatch")

    _validate_recovery_receipt_binding(
        receipts,
        campaign_id=campaign.campaign_id,
        round_id=round_id,
    )
    _validate_round_usage_evidence(receipts, usage, require_complete=True)

    _validate_fault_replay_shape(supplied_fault_replay)
    recomputed_replay = replay_ablations(packet)
    _validate_fault_replay_shape(recomputed_replay)
    replay_payload = recomputed_replay.model_dump(mode="json")
    if canonical_sha256(supplied_fault_replay.model_dump(mode="json")) != canonical_sha256(
        replay_payload
    ):
        raise ValueError("supplied fault replay differs from code recomputation")

    recomputed_hard = evaluate_hard_metrics(
        packet,
        reference,
        fault_outcomes=recomputed_replay.full_system_outcomes,
        clean_false_block_count=recomputed_replay.clean_false_block_count,
    ).model_copy(update={"created_at": supplied_hard_report.created_at})
    _validate_hard_report_shape(recomputed_hard)
    if canonical_sha256(supplied_hard_report.model_dump(mode="json")) != canonical_sha256(
        recomputed_hard.model_dump(mode="json")
    ):
        raise ValueError("supplied hard report differs from code recomputation")
    hard_results = {
        metric.metric_id: metric.passed for metric in recomputed_hard.metrics
    }
    return RecoveryRoundSubmission(
        freeze_sha256=str(freeze.freeze_sha256),
        call_limit=call_limit,
        implementation_sha256=current_identities["hypoweaver_source"],
        started_at=started_at,
        completed_at=completed_at,
        usage=usage,
        receipts=receipts,
        benchmark_packet_sha256=str(packet.packet_sha256),
        hard_metric_report_sha256=canonical_sha256(
            recomputed_hard.model_dump(mode="json")
        ),
        fault_replay_sha256=canonical_sha256(replay_payload),
        hard_metric_results=hard_results,
        ablation_target_degradation_results={
            item.ablation_id: item.target_fault_degraded
            for item in recomputed_replay.ablations
        },
    )


def verify_recovery_round_artifacts(
    campaign: RecoveryCampaign,
    round_record: RecoveryRound,
    *,
    packet: BenchmarkPacket,
    fault_replay: FaultReplayReport,
    hard_metric_report: HardMetricReport,
    reference: BenchmarkReference,
) -> None:
    """Recompute a persisted qualified round before resuming comparison."""

    recomputed = _recompute_evaluated_submission(
        campaign=campaign,
        round_id=round_record.round_id,
        call_limit=round_record.call_limit,
        packet=packet,
        supplied_fault_replay=fault_replay,
        supplied_hard_report=hard_metric_report,
        reference=reference,
        usage=round_record.usage,
        receipts=round_record.receipts,
        started_at=round_record.started_at,
        completed_at=round_record.completed_at,
    )
    persisted = RecoveryRoundSubmission.model_validate(
        round_record.model_dump(
            mode="json",
            exclude={
                "reservation_id",
                "round_id",
                "round_index",
                "status",
                "previous_round_sha256",
                "round_sha256",
            },
        )
    )
    if recomputed != persisted:
        raise ValueError("persisted recovery round differs from code recomputation")


def _validate_comparison_usage_evidence(
    *,
    campaign: RecoveryCampaign,
    qwen_single_pass: RecoveryUsage,
    agent_laboratory: RecoveryUsage,
    blind_reviews: RecoveryUsage,
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    _validate_recovery_receipt_binding(
        receipts,
        campaign_id=campaign.campaign_id,
        round_id="comparison-01",
    )
    if len({item.call_id for item in receipts}) != len(receipts):
        raise ValueError("comparison receipt call_ids must be unique")
    source_hashes = [item.source_receipt_sha256 for item in receipts]
    if any(value in (None, "0" * 64) for value in source_hashes):
        raise ValueError("comparison receipt is missing its source hash")
    if len(set(source_hashes)) != len(source_hashes):
        raise ValueError("comparison source receipt hashes must be unique")
    usage_by_phase = {
        "qwen_single_pass": qwen_single_pass,
        "agent_laboratory": agent_laboratory,
        "blind_review": blind_reviews,
    }
    for phase, usage in usage_by_phase.items():
        phase_receipts = tuple(item for item in receipts if item.phase == phase)
        if len(phase_receipts) != usage.llm_calls:
            raise ValueError(f"comparison receipt count mismatch for {phase}")
        if sum(item.input_tokens for item in phase_receipts) != usage.input_tokens:
            raise ValueError(f"comparison input-token mismatch for {phase}")
        if sum(item.output_tokens for item in phase_receipts) != usage.output_tokens:
            raise ValueError(f"comparison output-token mismatch for {phase}")
        receipt_failures = sorted(
            item.error_type
            for item in phase_receipts
            if item.error_type is not None
        )
        if receipt_failures != sorted(usage.technical_failures):
            raise ValueError(f"comparison technical-failure mismatch for {phase}")


def _validate_blind_comparison_binding(
    *,
    campaign: RecoveryCampaign,
    qualified_packet: BenchmarkPacket,
    agent_packet: BenchmarkPacket,
    blind_summary: PairedReviewSummary,
    blind_usage: RecoveryUsage,
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    reviews_by_sample = {item.sample_index: item for item in blind_summary.reviews}
    if (
        blind_summary.case_id != campaign.freeze.case_id
        or blind_summary.packet_a_id != qualified_packet.packet_id
        or blind_summary.packet_b_id != agent_packet.packet_id
        or len(blind_summary.reviews) != 5
        or set(reviews_by_sample) != {1, 2, 3, 4, 5}
        or any(item.official_receipt is not None for item in blind_summary.reviews)
    ):
        raise ValueError("recovery blind summary provenance mismatch")
    mapped_by_call = {
        item.call_id: item for item in receipts if item.phase == "blind_review"
    }
    if len(mapped_by_call) != 5:
        raise ValueError("recovery blind receipts must contain five unique calls")
    for sample_index in range(1, 6):
        review = reviews_by_sample[sample_index]
        source = review.call_receipt
        if (
            review.label_order
            != campaign.freeze.sealed_label_orders[sample_index - 1]
            or review.system_assignment
            != campaign.freeze.sealed_system_assignments[sample_index - 1]
            or source is None
            or source.outcome != "succeeded"
            or source.provider != "qwen"
        ):
            raise ValueError("recovery blind schedule or receipt mismatch")
        mapped = mapped_by_call.get(source.call_id)
        if mapped is None or (
            mapped.provider != source.provider
            or mapped.model != source.model
            or mapped.outcome != "succeeded"
            or mapped.response_sha256 != source.response_sha256
            or mapped.input_tokens != source.input_tokens
            or mapped.output_tokens != source.output_tokens
            or mapped.call_started_at != source.call_started_at
            or mapped.call_completed_at != source.call_completed_at
            or mapped.source_receipt_sha256
            != canonical_sha256(source.model_dump(mode="json"))
        ):
            raise ValueError("recovery blind receipt source binding mismatch")
    recomputed_usage = RecoveryUsage(
        llm_calls=sum(item.resource_usage.llm_calls for item in blind_summary.reviews),
        input_tokens=sum(
            item.resource_usage.input_tokens for item in blind_summary.reviews
        ),
        output_tokens=sum(
            item.resource_usage.output_tokens for item in blind_summary.reviews
        ),
        wall_time_seconds=sum(
            item.resource_usage.wall_time_seconds for item in blind_summary.reviews
        ),
        technical_failures=tuple(
            failure
            for item in blind_summary.reviews
            for failure in item.resource_usage.technical_failures
        ),
    )
    if recomputed_usage != blind_usage:
        raise ValueError("recovery blind review usage mismatch")


def _validate_delivery_manifest_binding(
    *,
    campaign: RecoveryCampaign,
    manifest: BenchmarkDeliveryManifest,
    qwen_packet: BenchmarkPacket,
    agent_packet: BenchmarkPacket,
    qualified_packet: BenchmarkPacket,
    blind_summary: PairedReviewSummary,
) -> None:
    unsigned = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    if (
        manifest.manifest_sha256 is None
        or canonical_sha256(unsigned) != manifest.manifest_sha256
        or manifest.official
        or manifest.protocol_sha256
        != campaign.freeze.source_official_protocol_sha256
        or manifest.case_id != campaign.freeze.case_id
        or not manifest.all_hard_gates_passed
    ):
        raise ValueError("recovery delivery manifest binding mismatch")
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
    if set(manifest.file_sha256) != expected_paths or any(
        not _is_nonzero_sha256(value) for value in manifest.file_sha256.values()
    ):
        raise ValueError("recovery delivery manifest file registry mismatch")
    expected_json_files = {
        "neutral_packets/qwen_single_pass.json": qwen_packet.model_dump(mode="json"),
        "neutral_packets/agent_laboratory.json": agent_packet.model_dump(mode="json"),
        "neutral_packets/hypoweaver.json": qualified_packet.model_dump(mode="json"),
        "blind_reviews.json": blind_summary.model_dump(mode="json"),
        **{
            f"blind_reviews/review-{item.sample_index}.json": item.model_dump(
                mode="json"
            )
            for item in blind_summary.reviews
        },
    }
    for relative_path, payload in expected_json_files.items():
        expected = hashlib.sha256(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
        ).hexdigest()
        if manifest.file_sha256.get(relative_path) != expected:
            raise ValueError(f"recovery delivery file binding mismatch: {relative_path}")


def _verify_delivery_files_at_root(
    *,
    delivery_root: Path,
    manifest: BenchmarkDeliveryManifest,
) -> None:
    root = delivery_root.resolve()
    persisted_manifest = BenchmarkDeliveryManifest.model_validate_json(
        (root / "delivery_manifest.json").read_text(encoding="utf-8")
    )
    if persisted_manifest != manifest:
        raise ValueError("persisted recovery delivery manifest differs from submission")
    for relative_path, expected_hash in manifest.file_sha256.items():
        artifact = (root / relative_path).resolve()
        if not artifact.is_relative_to(root) or not artifact.is_file():
            raise ValueError("recovery delivery artifact is missing or escapes its root")
        if _file_sha256(artifact) != expected_hash:
            raise ValueError(f"recovery delivery file hash mismatch: {relative_path}")


def _recompute_comparison_submission(
    *,
    campaign: RecoveryCampaign,
    status: str,
    qwen_single_pass: RecoveryUsage,
    agent_laboratory: RecoveryUsage,
    blind_reviews: RecoveryUsage,
    receipts: tuple[RecoveryCallReceipt, ...],
    started_at: str,
    completed_at: str,
    technical_failure: str | None,
    qwen_packet: BenchmarkPacket | None,
    agent_packet: BenchmarkPacket | None,
    qualified_packet: BenchmarkPacket | None,
    blind_summary: PairedReviewSummary | None,
    delivery_manifest: BenchmarkDeliveryManifest | None,
) -> RecoveryComparisonSubmission:
    _validate_comparison_usage_evidence(
        campaign=campaign,
        qwen_single_pass=qwen_single_pass,
        agent_laboratory=agent_laboratory,
        blind_reviews=blind_reviews,
        receipts=receipts,
    )
    common = {
        "freeze_sha256": str(campaign.freeze.freeze_sha256),
        "status": status,
        "qwen_single_pass": qwen_single_pass,
        "agent_laboratory": agent_laboratory,
        "blind_reviews": blind_reviews,
        "receipts": receipts,
        "started_at": started_at,
        "completed_at": completed_at,
        "technical_failure": technical_failure,
    }
    if status == "technical_failed":
        if any(
            value is not None
            for value in (
                qwen_packet,
                agent_packet,
                qualified_packet,
                blind_summary,
                delivery_manifest,
            )
        ):
            raise ValueError("failed comparison cannot bind completed artifacts")
        return RecoveryComparisonSubmission(**common)
    if status != "completed" or any(
        value is None
        for value in (
            qwen_packet,
            agent_packet,
            qualified_packet,
            blind_summary,
            delivery_manifest,
        )
    ):
        raise ValueError("completed comparison requires all source artifacts")
    assert qwen_packet is not None
    assert agent_packet is not None
    assert qualified_packet is not None
    assert blind_summary is not None
    assert delivery_manifest is not None
    qualified = next(
        (item for item in campaign.rounds if item.status == "hard_gate_qualified"),
        None,
    )
    if qualified is None or qualified.benchmark_packet_sha256 != qualified_packet.packet_sha256:
        raise ValueError("comparison qualified packet does not match the first qualified round")
    for expected_system, packet, usage in (
        ("qwen_single_pass", qwen_packet, qwen_single_pass),
        ("agent_laboratory", agent_packet, agent_laboratory),
    ):
        verify_benchmark_packet(packet)
        if packet.system_id != expected_system or packet.official_receipts:
            raise ValueError("recovery comparison packet provenance mismatch")
        if (
            packet.case_id != campaign.freeze.case_id
            or packet.visible_input_sha256 != campaign.freeze.visible_input_sha256
            or tuple(packet.data_sha256) != campaign.freeze.data_sha256
            or _usage_from_packet(packet) != usage
        ):
            raise ValueError("recovery comparison packet identity or usage mismatch")
    verify_benchmark_packet(qualified_packet)
    if (
        qualified_packet.system_id != "hypoweaver"
        or qualified_packet.official_receipts
        or qualified_packet.case_id != campaign.freeze.case_id
        or qualified_packet.visible_input_sha256 != campaign.freeze.visible_input_sha256
        or tuple(qualified_packet.data_sha256) != campaign.freeze.data_sha256
    ):
        raise ValueError("recovery qualified packet provenance mismatch")
    qwen_receipts = tuple(
        item for item in receipts if item.phase == "qwen_single_pass"
    )
    qwen_native = qwen_packet.native_artifact_sha256
    required_qwen_native = {
        "visible_input",
        "single_pass_prompt",
        "single_pass_config",
        "single_pass_raw_response",
    }
    if (
        len(qwen_receipts) != 1
        or qwen_receipts[0].provider != "qwen"
        or qwen_receipts[0].model != qwen_packet.model_id
        or qwen_receipts[0].outcome != "succeeded"
        or not required_qwen_native.issubset(qwen_native)
        or any(
            not _is_nonzero_sha256(qwen_native[key])
            for key in required_qwen_native
        )
        or qwen_native["visible_input"] != campaign.freeze.visible_input_sha256
        or qwen_receipts[0].input_sha256
        != campaign.freeze.visible_input_sha256
        or qwen_receipts[0].response_sha256
        != qwen_native["single_pass_raw_response"]
    ):
        raise ValueError("recovery Qwen baseline receipt binding mismatch")
    agent_native = agent_packet.native_artifact_sha256
    if (
        "benchmark_output" not in agent_native
        or not agent_native
        or any(not _is_nonzero_sha256(value) for value in agent_native.values())
    ):
        raise ValueError("recovery Agent Laboratory artifact binding mismatch")
    _validate_blind_comparison_binding(
        campaign=campaign,
        qualified_packet=qualified_packet,
        agent_packet=agent_packet,
        blind_summary=blind_summary,
        blind_usage=blind_reviews,
        receipts=receipts,
    )
    _validate_delivery_manifest_binding(
        campaign=campaign,
        manifest=delivery_manifest,
        qwen_packet=qwen_packet,
        agent_packet=agent_packet,
        qualified_packet=qualified_packet,
        blind_summary=blind_summary,
    )
    return RecoveryComparisonSubmission(
        qwen_packet_sha256=str(qwen_packet.packet_sha256),
        agent_laboratory_packet_sha256=str(agent_packet.packet_sha256),
        blind_summary_sha256=canonical_sha256(blind_summary.model_dump(mode="json")),
        delivery_manifest_sha256=str(delivery_manifest.manifest_sha256),
        **common,
    )


def verify_recovery_comparison_artifacts(
    campaign: RecoveryCampaign,
    *,
    qwen_packet: BenchmarkPacket | None,
    agent_packet: BenchmarkPacket | None,
    qualified_packet: BenchmarkPacket | None,
    blind_summary: PairedReviewSummary | None,
    delivery_manifest: BenchmarkDeliveryManifest | None,
) -> None:
    """Rebuild a persisted comparison from its packets, review, and delivery."""

    comparison = campaign.comparison
    if comparison is None:
        raise ValueError("recovery campaign has no comparison to verify")
    recomputed = _recompute_comparison_submission(
        campaign=campaign,
        status=comparison.status,
        qwen_single_pass=comparison.qwen_single_pass,
        agent_laboratory=comparison.agent_laboratory,
        blind_reviews=comparison.blind_reviews,
        receipts=comparison.receipts,
        started_at=comparison.started_at,
        completed_at=comparison.completed_at,
        technical_failure=comparison.technical_failure,
        qwen_packet=qwen_packet,
        agent_packet=agent_packet,
        qualified_packet=qualified_packet,
        blind_summary=blind_summary,
        delivery_manifest=delivery_manifest,
    )
    persisted = RecoveryComparisonSubmission.model_validate(
        comparison.model_dump(
            mode="json",
            exclude={"comparison_id", "reservation_id", "comparison_sha256"},
        )
    )
    if recomputed != persisted:
        raise ValueError("persisted recovery comparison differs from code recomputation")


def _validate_fault_replay_shape(report: FaultReplayReport) -> None:
    if (
        len(report.full_system_outcomes) != len(FAULT_IDS)
        or tuple(item.fault_id for item in report.full_system_outcomes) != FAULT_IDS
    ):
        raise ValueError("fault replay must contain the nine ordered faults exactly once")
    if (
        len(report.ablations) != len(ABLATION_IDS)
        or tuple(item.ablation_id for item in report.ablations) != ABLATION_IDS
    ):
        raise ValueError("fault replay must contain the six ordered ablations exactly once")


def _validate_hard_report_shape(report: HardMetricReport) -> None:
    metric_ids = tuple(metric.metric_id for metric in report.metrics)
    if len(metric_ids) != len(set(metric_ids)) or set(metric_ids) != {
        "contract_execution_fidelity",
        "required_step_terminal_rate",
        "required_evidence_completion",
        "fatal_fault_detection_rate",
        "clean_false_block_count",
        "protected_numeric_consistency",
        "statement_traceability",
        "causal_overreach_escape_count",
        "independent_replication_rate",
    }:
        raise ValueError("hard report must contain the fixed metrics exactly once")
    if report.all_hard_gates_passed != all(metric.passed for metric in report.metrics):
        raise ValueError("hard report aggregate flag mismatch")


def _validate_source_receipt_binding(
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    for receipt in receipts:
        if any(
            value is None
            for value in (
                receipt.logical_call_id,
                receipt.call_group,
                receipt.prompt_key,
                receipt.prompt_version,
                receipt.attempt_type,
                receipt.input_sha256,
                receipt.output_schema_sha256,
                receipt.source_receipt_sha256,
            )
        ):
            raise ValueError("evaluated receipt is missing its source binding")
        if (
            receipt.attempt_type == "legacy"
            or receipt.prompt_version == "legacy"
            or receipt.input_sha256 == "0" * 64
            or receipt.output_schema_sha256 == "0" * 64
            or receipt.source_receipt_sha256 == "0" * 64
        ):
            raise ValueError("evaluated receipt cannot use legacy or zero hash bindings")
        source = ModelCallReceipt(
            call_id=receipt.call_id,
            logical_call_id=receipt.logical_call_id,
            call_group=receipt.call_group,
            prompt_key=receipt.prompt_key,
            prompt_version=receipt.prompt_version,
            attempt_index=receipt.attempt_index,
            max_attempts=receipt.max_attempts,
            attempt_type=receipt.attempt_type,
            outcome=receipt.outcome,
            provider=receipt.provider,
            model=receipt.model,
            started_at=receipt.call_started_at,
            completed_at=receipt.call_completed_at,
            response_sha256=receipt.response_sha256,
            input_sha256=receipt.input_sha256,
            output_schema_sha256=receipt.output_schema_sha256,
            provider_response_id_sha256=receipt.provider_response_id_sha256,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            error_type=receipt.error_type,
            error_category=receipt.error_category,
        )
        if canonical_sha256(source.model_dump(mode="json")) != receipt.source_receipt_sha256:
            raise ValueError("evaluated receipt source hash mismatch")


def _validate_round_usage_evidence(
    receipts: tuple[RecoveryCallReceipt, ...],
    usage: RecoveryUsage,
    *,
    require_complete: bool,
) -> None:
    _validate_slot_receipts(receipts, require_complete=require_complete)
    _validate_source_receipt_binding(receipts)
    if len(receipts) != usage.llm_calls:
        raise ValueError("recovery receipt count differs from usage")
    if sum(item.input_tokens for item in receipts) != usage.input_tokens:
        raise ValueError("recovery receipt input tokens differ from usage")
    if sum(item.output_tokens for item in receipts) != usage.output_tokens:
        raise ValueError("recovery receipt output tokens differ from usage")
    receipt_failures = sorted(
        item.error_type for item in receipts if item.error_type is not None
    )
    if receipt_failures != sorted(usage.technical_failures):
        raise ValueError("recovery receipt failures differ from usage")


def _usage_from_packet(packet: BenchmarkPacket) -> RecoveryUsage:
    usage = packet.resource_usage
    return RecoveryUsage(
        llm_calls=usage.llm_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        wall_time_seconds=usage.wall_time_seconds,
        technical_failures=tuple(usage.technical_failures),
    )


class RecoveryCampaignStore:
    """Atomic JSON store whose round history can only grow by one suffix item."""

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")

    def create(self, campaign: RecoveryCampaign) -> RecoveryCampaign:
        verify_recovery_campaign(campaign)
        _write_json(
            self.path,
            campaign.model_dump(mode="json", by_alias=True),
            replace=False,
        )
        return campaign

    def load(self) -> RecoveryCampaign:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("recovery campaign is unreadable") from error
        campaign = RecoveryCampaign.model_validate(payload)
        verify_recovery_campaign(campaign)
        return campaign

    def append_round(self, submission: RecoveryRoundSubmission) -> RecoveryCampaign:
        raise RuntimeError("recovery rounds require atomic reserve/finalize")

    def reserve_round(
        self,
        *,
        owner_id: str,
        lease_seconds: int = 600,
        now: str | None = None,
    ) -> RecoveryRoundReservation | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with _exclusive_lock(self.lock_path):
            campaign = self.load()
            if campaign.status != "open":
                return None
            timestamp = now or _utc_now()
            current_time = datetime.fromisoformat(timestamp)
            active = campaign.active_round_reservation
            if active is not None:
                if datetime.fromisoformat(active.lease_expires_at) > current_time:
                    return None
                invalidated = invalidate_recovery_campaign(
                    campaign,
                    "round_reservation_expired_without_terminal_evidence",
                    invalidated_at=timestamp,
                    conservative_llm_call_charge=active.call_limit,
                )
                self._write(invalidated)
                return None
            remaining = _committed_recovery_pool_remaining(campaign)
            if remaining < campaign.recovery_round_min_calls:
                return None
            if _committed_started_rounds(campaign) >= campaign.max_rounds:
                exhausted = _updated_campaign(
                    campaign,
                    status="exhausted",
                    status_reason="max_rounds_reached",
                    updated_at=timestamp,
                )
                self._write(exhausted)
                return None
            round_index = len(campaign.rounds) + 1
            reservation_payload = {
                "reservation_version": 1,
                "reservation_id": str(uuid4()),
                "round_id": f"round-{round_index:02d}",
                "round_index": round_index,
                "owner_id": owner_id,
                "freeze_sha256": campaign.freeze.freeze_sha256,
                "call_limit": min(campaign.recovery_round_max_calls, remaining),
                "reserved_at": timestamp,
                "lease_expires_at": (
                    current_time + timedelta(seconds=lease_seconds)
                ).isoformat(),
            }
            reservation = RecoveryRoundReservation(
                **reservation_payload,
                reservation_sha256=canonical_sha256(reservation_payload),
            )
            updated = _updated_campaign(
                campaign,
                active_round_reservation=reservation,
                updated_at=timestamp,
            )
            self._write(updated)
            return reservation

    def finalize_terminal_round(
        self,
        *,
        owner_id: str,
        reservation_id: str,
        submission: RecoveryRoundSubmission,
        now: str | None = None,
    ) -> RecoveryCampaign:
        if submission.technical_failure is None and submission.invalidation_reason is None:
            raise ValueError("terminal finalize requires a failure or invalidation reason")
        with _exclusive_lock(self.lock_path):
            campaign = self.load()
            reservation = self._require_live_reservation(
                campaign,
                owner_id=owner_id,
                reservation_id=reservation_id,
                now=now,
            )
            if reservation is None:
                return self.load()
            if submission.call_limit != reservation.call_limit:
                return self._invalidate_under_lock(
                    campaign,
                    "round_terminal_call_limit_mismatch",
                    now,
                )
            try:
                _validate_recovery_receipt_binding(
                    submission.receipts,
                    campaign_id=campaign.campaign_id,
                    round_id=reservation.round_id,
                )
                _validate_round_usage_evidence(
                    submission.receipts,
                    submission.usage,
                    require_complete=False,
                )
            except Exception:
                return self._invalidate_under_lock(
                    campaign,
                    "round_terminal_usage_evidence_invalid",
                    now,
                )
            updated = _append_finalized_round(
                campaign,
                submission,
                reservation_id=reservation.reservation_id,
                updated_at=now,
            )
            self._write(updated)
            return updated

    def reserve_comparison(
        self,
        *,
        owner_id: str,
        lease_seconds: int = 900,
        now: str | None = None,
    ) -> RecoveryComparisonReservation | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with _exclusive_lock(self.lock_path):
            campaign = self.load()
            if campaign.status != "qualified_seen_case" or campaign.comparison is not None:
                return None
            timestamp = now or _utc_now()
            current_time = datetime.fromisoformat(timestamp)
            active = campaign.active_comparison_reservation
            if active is not None:
                if datetime.fromisoformat(active.lease_expires_at) > current_time:
                    return None
                invalidated = invalidate_recovery_campaign(
                    campaign,
                    "comparison_reservation_expired_without_terminal_evidence",
                    invalidated_at=timestamp,
                    conservative_llm_call_charge=active.call_limit,
                )
                self._write(invalidated)
                return None
            if campaign.active_round_reservation is not None:
                return None
            payload = {
                "reservation_version": 1,
                "reservation_id": str(uuid4()),
                "comparison_id": "comparison-01",
                "owner_id": owner_id,
                "freeze_sha256": campaign.freeze.freeze_sha256,
                "call_limit": campaign.comparison_call_reserve,
                "reserved_at": timestamp,
                "lease_expires_at": (
                    current_time + timedelta(seconds=lease_seconds)
                ).isoformat(),
            }
            reservation = RecoveryComparisonReservation(
                **payload,
                reservation_sha256=canonical_sha256(payload),
            )
            updated = _updated_campaign(
                campaign,
                active_comparison_reservation=reservation,
                updated_at=timestamp,
            )
            self._write(updated)
            return reservation

    def finalize_comparison(
        self,
        *,
        owner_id: str,
        reservation_id: str,
        status: str,
        qwen_single_pass: RecoveryUsage,
        agent_laboratory: RecoveryUsage,
        blind_reviews: RecoveryUsage,
        receipts: tuple[RecoveryCallReceipt, ...],
        started_at: str,
        completed_at: str,
        technical_failure: str | None = None,
        qwen_packet: BenchmarkPacket | None = None,
        agent_packet: BenchmarkPacket | None = None,
        qualified_packet: BenchmarkPacket | None = None,
        blind_summary: PairedReviewSummary | None = None,
        delivery_manifest: BenchmarkDeliveryManifest | None = None,
        delivery_root: Path | None = None,
        now: str | None = None,
    ) -> RecoveryCampaign:
        with _exclusive_lock(self.lock_path):
            campaign = self.load()
            reservation = self._require_live_comparison_reservation(
                campaign,
                owner_id=owner_id,
                reservation_id=reservation_id,
                now=now,
            )
            if reservation is None:
                return self.load()
            try:
                if status == "completed":
                    if delivery_manifest is None or delivery_root is None:
                        raise ValueError(
                            "completed comparison requires a persisted delivery root"
                        )
                    _verify_delivery_files_at_root(
                        delivery_root=delivery_root,
                        manifest=delivery_manifest,
                    )
                elif delivery_root is not None:
                    raise ValueError(
                        "failed comparison cannot claim a completed delivery root"
                    )
                submission = _recompute_comparison_submission(
                    campaign=campaign,
                    status=status,
                    qwen_single_pass=qwen_single_pass,
                    agent_laboratory=agent_laboratory,
                    blind_reviews=blind_reviews,
                    receipts=receipts,
                    started_at=started_at,
                    completed_at=completed_at,
                    technical_failure=technical_failure,
                    qwen_packet=qwen_packet,
                    agent_packet=agent_packet,
                    qualified_packet=qualified_packet,
                    blind_summary=blind_summary,
                    delivery_manifest=delivery_manifest,
                )
                updated = _record_finalized_comparison(
                    campaign,
                    submission,
                    reservation_id=reservation.reservation_id,
                    updated_at=now,
                )
            except Exception:
                return self._invalidate_under_lock(
                    campaign,
                    "comparison_terminal_evidence_invalid",
                    now,
                )
            self._write(updated)
            return updated

    def finalize_evaluated_round(
        self,
        *,
        owner_id: str,
        reservation_id: str,
        packet: BenchmarkPacket,
        fault_replay: FaultReplayReport,
        hard_metric_report: HardMetricReport,
        reference: BenchmarkReference,
        usage: RecoveryUsage,
        receipts: tuple[RecoveryCallReceipt, ...],
        started_at: str,
        completed_at: str,
        now: str | None = None,
    ) -> RecoveryCampaign:
        with _exclusive_lock(self.lock_path):
            campaign = self.load()
            reservation = self._require_live_reservation(
                campaign,
                owner_id=owner_id,
                reservation_id=reservation_id,
                now=now,
            )
            if reservation is None:
                return self.load()
            try:
                submission = _recompute_evaluated_submission(
                    campaign=campaign,
                    round_id=reservation.round_id,
                    call_limit=reservation.call_limit,
                    packet=packet,
                    supplied_fault_replay=fault_replay,
                    supplied_hard_report=hard_metric_report,
                    reference=reference,
                    usage=usage,
                    receipts=receipts,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            except Exception:
                return self._invalidate_evaluated_round_under_lock(
                    campaign,
                    reservation,
                    usage=usage,
                    receipts=receipts,
                    started_at=started_at,
                    completed_at=completed_at,
                    now=now,
                )
            updated = _append_finalized_round(
                campaign,
                submission,
                reservation_id=reservation.reservation_id,
                updated_at=now,
            )
            self._write(updated)
            return updated

    def invalidate(
        self,
        reason: str,
        *,
        charge_active_reservation: bool = False,
    ) -> RecoveryCampaign:
        # Retained for caller compatibility. Active reservations are now always
        # conservatively charged, so callers cannot accidentally clear one at zero.
        del charge_active_reservation
        return self._mutate(
            lambda campaign: invalidate_recovery_campaign(
                campaign,
                reason,
            )
        )

    def record_comparison(
        self,
        submission: RecoveryComparisonSubmission,
    ) -> RecoveryCampaign:
        raise RuntimeError("recovery comparison requires atomic reserve/finalize")

    def _mutate(self, operation: Any) -> RecoveryCampaign:
        with _exclusive_lock(self.lock_path):
            current = self.load()
            updated = operation(current)
            if tuple(item.round_sha256 for item in updated.rounds[: len(current.rounds)]) != tuple(
                item.round_sha256 for item in current.rounds
            ):
                raise RuntimeError("recovery mutation attempted to rewrite round history")
            self._write(updated)
            return updated

    def _require_live_reservation(
        self,
        campaign: RecoveryCampaign,
        *,
        owner_id: str,
        reservation_id: str,
        now: str | None,
    ) -> RecoveryRoundReservation | None:
        reservation = campaign.active_round_reservation
        if (
            reservation is None
            or reservation.owner_id != owner_id
            or reservation.reservation_id != reservation_id
        ):
            return None
        timestamp = now or _utc_now()
        if datetime.fromisoformat(reservation.lease_expires_at) <= datetime.fromisoformat(
            timestamp
        ):
            invalidated = invalidate_recovery_campaign(
                campaign,
                "round_reservation_expired_without_terminal_evidence",
                invalidated_at=timestamp,
                conservative_llm_call_charge=reservation.call_limit,
            )
            self._write(invalidated)
            return None
        return reservation

    def _require_live_comparison_reservation(
        self,
        campaign: RecoveryCampaign,
        *,
        owner_id: str,
        reservation_id: str,
        now: str | None,
    ) -> RecoveryComparisonReservation | None:
        reservation = campaign.active_comparison_reservation
        if (
            reservation is None
            or reservation.owner_id != owner_id
            or reservation.reservation_id != reservation_id
        ):
            return None
        timestamp = now or _utc_now()
        if datetime.fromisoformat(reservation.lease_expires_at) <= datetime.fromisoformat(
            timestamp
        ):
            invalidated = invalidate_recovery_campaign(
                campaign,
                "comparison_reservation_expired_without_terminal_evidence",
                invalidated_at=timestamp,
                conservative_llm_call_charge=reservation.call_limit,
            )
            self._write(invalidated)
            return None
        return reservation

    def _invalidate_evaluated_round_under_lock(
        self,
        campaign: RecoveryCampaign,
        reservation: RecoveryRoundReservation,
        *,
        usage: RecoveryUsage,
        receipts: tuple[RecoveryCallReceipt, ...],
        started_at: str,
        completed_at: str,
        now: str | None,
    ) -> RecoveryCampaign:
        try:
            _validate_round_usage_evidence(
                receipts,
                usage,
                require_complete=True,
            )
            invalid_submission = RecoveryRoundSubmission(
                freeze_sha256=str(campaign.freeze.freeze_sha256),
                call_limit=reservation.call_limit,
                implementation_sha256=hypoweaver_source_sha256(),
                started_at=started_at,
                completed_at=completed_at,
                usage=usage,
                receipts=receipts,
                invalidation_reason="round_content_recomputation_failed",
            )
            _validate_recovery_receipt_binding(
                receipts,
                campaign_id=campaign.campaign_id,
                round_id=reservation.round_id,
            )
            updated = _append_finalized_round(
                campaign,
                invalid_submission,
                reservation_id=reservation.reservation_id,
                updated_at=now,
            )
        except Exception:
            updated = invalidate_recovery_campaign(
                campaign,
                "round_evidence_invalid_and_usage_unverifiable",
                invalidated_at=now,
                conservative_llm_call_charge=reservation.call_limit,
            )
        self._write(updated)
        return updated

    def _invalidate_under_lock(
        self,
        campaign: RecoveryCampaign,
        reason: str,
        now: str | None,
    ) -> RecoveryCampaign:
        updated = invalidate_recovery_campaign(
            campaign,
            reason,
            invalidated_at=now,
            conservative_llm_call_charge=(
                campaign.active_round_reservation.call_limit
                if campaign.active_round_reservation is not None
                else campaign.active_comparison_reservation.call_limit
                if campaign.active_comparison_reservation is not None
                else 0
            ),
        )
        self._write(updated)
        return updated

    def _write(self, campaign: RecoveryCampaign) -> None:
        _write_json(
            self.path,
            campaign.model_dump(mode="json", by_alias=True),
            replace=True,
        )


def _round_status(submission: RecoveryRoundSubmission) -> RecoveryRoundStatus:
    if submission.invalidation_reason is not None:
        return "invalidated"
    if submission.technical_failure is not None:
        return "technical_failed"
    if all(submission.hard_metric_results.values()) and all(
        submission.ablation_target_degradation_results.values()
    ):
        return "hard_gate_qualified"
    return "hard_gate_failed"


def _project_campaign_status(campaign: RecoveryCampaign) -> tuple[str, str | None]:
    if campaign.invalidation is not None:
        return "invalidated", campaign.invalidation.reason
    if campaign.rounds:
        last = campaign.rounds[-1]
        if last.status == "hard_gate_qualified":
            return "qualified_seen_case", "first_hard_gate_qualified"
        if last.status == "invalidated":
            return "invalidated", last.invalidation_reason
    if _committed_started_rounds(campaign) >= campaign.max_rounds:
        return "exhausted", "max_rounds_reached"
    if (
        campaign.active_round_reservation is None
        and _committed_recovery_pool_remaining(campaign)
        < campaign.recovery_round_min_calls
    ):
        reason = (
            "insufficient_recovery_pool"
            if not campaign.rounds
            else "recovery_pool_exhausted"
        )
        return "exhausted", reason
    return "open", None


def _updated_campaign(campaign: RecoveryCampaign, **updates: Any) -> RecoveryCampaign:
    payload = campaign.model_dump(mode="json", exclude={"campaign_sha256"})
    payload.update({key: _json_value(value) for key, value in updates.items()})
    payload["protocol_status"] = _protocol_status(payload["status"])
    payload["campaign_sha256"] = canonical_sha256(payload)
    return RecoveryCampaign.model_validate(payload)


def _protocol_status(status: str) -> str:
    return {
        "open": "open",
        "qualified_seen_case": "hard-gate-qualified-on-seen-case",
        "invalidated": "invalidated",
        "exhausted": "exhausted-not-qualified",
    }[status]


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _comparison_calls(comparison: RecoveryComparisonSubmission) -> int:
    return (
        comparison.qwen_single_pass.llm_calls
        + comparison.agent_laboratory.llm_calls
        + comparison.blind_reviews.llm_calls
    )


def _is_nonzero_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and value != "0" * 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_recovery_receipt_binding(
    receipts: tuple[RecoveryCallReceipt, ...],
    *,
    campaign_id: str,
    round_id: str,
) -> None:
    if any(receipt.campaign_id != campaign_id for receipt in receipts):
        raise ValueError("recovery receipt campaign binding mismatch")
    if any(receipt.round_id != round_id for receipt in receipts):
        raise ValueError("recovery receipt round binding mismatch")


def _reject_duplicate_call_ids(
    campaign: RecoveryCampaign,
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    existing = {
        receipt.call_id
        for round_record in campaign.rounds
        for receipt in round_record.receipts
    }
    if campaign.comparison is not None:
        existing.update(receipt.call_id for receipt in campaign.comparison.receipts)
    if existing.intersection(receipt.call_id for receipt in receipts):
        raise ValueError("recovery receipt call_id was reused")


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_json(path: Path, payload: Any, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not replace:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise FileExistsError(path) from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"JSON input is unreadable: {path}") from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ValueError(f"frozen input is unreadable: {path}") from error
    return digest.hexdigest()


def _verify_reference_identity(
    reference: BenchmarkReference,
    protocol: FrozenBenchmarkProtocol,
) -> None:
    if (
        reference.case_id != protocol.case_id
        or reference.visible_input_sha256 != protocol.visible_input_sha256
        or tuple(reference.data_sha256) != tuple(protocol.data_sha256)
    ):
        raise ValueError("recovery reference input identity mismatch")


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append-only non-official recovery campaign controller"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--campaign", type=Path, required=True)
    initialize.add_argument("--freeze", type=Path, required=True)
    initialize.add_argument("--prior-usage", type=Path, required=True)
    reserve_round = commands.add_parser("reserve-round")
    reserve_round.add_argument("--campaign", type=Path, required=True)
    reserve_round.add_argument("--owner", required=True)
    reserve_round.add_argument("--lease-seconds", type=int, default=7200)
    finalize_round = commands.add_parser("finalize-round")
    finalize_round.add_argument("--campaign", type=Path, required=True)
    finalize_round.add_argument("--owner", required=True)
    finalize_round.add_argument("--reservation", required=True)
    finalize_round.add_argument("--submission", type=Path, required=True)
    finalize_round.add_argument("--packet", type=Path)
    finalize_round.add_argument("--fault-replay", type=Path)
    finalize_round.add_argument("--hard-report", type=Path)
    finalize_round.add_argument("--reference", type=Path)
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("--campaign", type=Path, required=True)
    invalidate.add_argument("--reason", required=True)
    reserve_comparison = commands.add_parser("reserve-comparison")
    reserve_comparison.add_argument("--campaign", type=Path, required=True)
    reserve_comparison.add_argument("--owner", required=True)
    reserve_comparison.add_argument("--lease-seconds", type=int, default=7200)
    finalize_comparison = commands.add_parser("finalize-comparison")
    finalize_comparison.add_argument("--campaign", type=Path, required=True)
    finalize_comparison.add_argument("--owner", required=True)
    finalize_comparison.add_argument("--reservation", required=True)
    finalize_comparison.add_argument("--submission", type=Path, required=True)
    show = commands.add_parser("show")
    show.add_argument("--campaign", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)
    store = RecoveryCampaignStore(args.campaign)
    result: BaseModel
    if args.command == "init":
        freeze = seal_recovery_freeze(
            RecoveryFreeze.model_validate(_load_json(args.freeze))
        )
        prior = seal_prior_usage_import(
            PriorUsageImport.model_validate(_load_json(args.prior_usage))
        )
        result = store.create(create_recovery_campaign(freeze, prior))
    elif args.command == "reserve-round":
        reservation = store.reserve_round(
            owner_id=args.owner,
            lease_seconds=args.lease_seconds,
        )
        if reservation is None:
            raise RuntimeError("recovery round reservation is busy or unavailable")
        result = reservation
    elif args.command == "finalize-round":
        submission = RecoveryRoundSubmission.model_validate(
            _load_json(args.submission)
        )
        if submission.technical_failure is not None or submission.invalidation_reason is not None:
            result = store.finalize_terminal_round(
                owner_id=args.owner,
                reservation_id=args.reservation,
                submission=submission,
            )
        else:
            artifact_paths = (
                args.packet,
                args.fault_replay,
                args.hard_report,
                args.reference,
            )
            if any(path is None for path in artifact_paths):
                raise ValueError("evaluated finalize requires all four artifact paths")
            result = store.finalize_evaluated_round(
                owner_id=args.owner,
                reservation_id=args.reservation,
                packet=BenchmarkPacket.model_validate(_load_json(args.packet)),
                fault_replay=FaultReplayReport.model_validate(
                    _load_json(args.fault_replay)
                ),
                hard_metric_report=HardMetricReport.model_validate(
                    _load_json(args.hard_report)
                ),
                reference=BenchmarkReference.model_validate(
                    _load_json(args.reference)
                ),
                usage=submission.usage,
                receipts=submission.receipts,
                started_at=submission.started_at,
                completed_at=submission.completed_at,
            )
    elif args.command == "invalidate":
        result = store.invalidate(args.reason)
    elif args.command == "reserve-comparison":
        reservation = store.reserve_comparison(
            owner_id=args.owner,
            lease_seconds=args.lease_seconds,
        )
        if reservation is None:
            raise RuntimeError("recovery comparison reservation is busy or unavailable")
        result = reservation
    elif args.command == "finalize-comparison":
        submission = RecoveryComparisonSubmission.model_validate(
            _load_json(args.submission)
        )
        result = store.finalize_comparison(
            owner_id=args.owner,
            reservation_id=args.reservation,
            submission=submission,
        )
    else:
        result = store.load()
    print(
        json.dumps(
            result.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
