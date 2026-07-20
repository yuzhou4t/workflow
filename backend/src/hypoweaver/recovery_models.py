from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .models import ModelCallErrorCategory


HARD_METRIC_IDS = (
    "contract_execution_fidelity",
    "required_step_terminal_rate",
    "required_evidence_completion",
    "fatal_fault_detection_rate",
    "clean_false_block_count",
    "protected_numeric_consistency",
    "statement_traceability",
    "causal_overreach_escape_count",
    "independent_replication_rate",
)

RecoveryCampaignStatus = Literal[
    "open",
    "qualified_seen_case",
    "invalidated",
    "exhausted",
]
RecoveryRoundStatus = Literal[
    "hard_gate_qualified",
    "hard_gate_failed",
    "technical_failed",
    "invalidated",
]
RecoveryReceiptPhase = Literal[
    "recovery_round",
    "qwen_single_pass",
    "agent_laboratory",
    "blind_review",
]
RecoveryProtocolStatus = Literal[
    "open",
    "hard-gate-qualified-on-seen-case",
    "invalidated",
    "exhausted-not-qualified",
]


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _aware_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


class RecoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecoveryUsage(RecoveryModel):
    llm_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0, ge=0)
    technical_failures: tuple[str, ...] = ()


class PriorUsageEvidence(RecoveryModel):
    evidence_version: Literal[1] = 1
    evidence_status: Literal[
        "complete_receipts",
        "partial_receipts",
        "ledger_only",
    ]
    resource_ledger_sha256: str
    ledger_llm_calls: int = Field(ge=0)
    verified_receipt_sha256: tuple[str, ...] = ()
    missing_receipt_count: int = Field(ge=0)
    token_usage_status: Literal["exact", "lower_bound"] = "exact"
    limitation_codes: tuple[
        Literal[
            "legacy_official_receipts_unavailable",
            "legacy_single_pass_tokens_unavailable",
        ],
        ...,
    ] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> "PriorUsageEvidence":
        hashes = (self.resource_ledger_sha256, *self.verified_receipt_sha256)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("prior evidence hashes must be lowercase SHA256 values")
        if len(set(self.verified_receipt_sha256)) != len(
            self.verified_receipt_sha256
        ):
            raise ValueError("verified prior receipt hashes must be unique")
        if (
            len(self.verified_receipt_sha256) + self.missing_receipt_count
            != self.ledger_llm_calls
        ):
            raise ValueError("prior evidence coverage must equal ledger_llm_calls")
        expected_status = "complete_receipts"
        if self.missing_receipt_count:
            expected_status = (
                "ledger_only"
                if not self.verified_receipt_sha256
                else "partial_receipts"
            )
        if self.evidence_status != expected_status:
            raise ValueError("prior evidence_status does not match receipt coverage")
        if self.missing_receipt_count and not self.limitation_codes:
            raise ValueError("receipt gaps require an explicit limitation code")
        if not self.missing_receipt_count and self.limitation_codes:
            unrelated = set(self.limitation_codes) - {
                "legacy_single_pass_tokens_unavailable"
            }
            if unrelated:
                raise ValueError(
                    "complete receipts cannot carry a receipt-gap limitation"
                )
        if self.token_usage_status == "lower_bound":
            if "legacy_single_pass_tokens_unavailable" not in self.limitation_codes:
                raise ValueError("lower-bound token usage requires an explicit limitation")
        elif "legacy_single_pass_tokens_unavailable" in self.limitation_codes:
            raise ValueError("exact token usage cannot carry a token-gap limitation")
        if len(set(self.limitation_codes)) != len(self.limitation_codes):
            raise ValueError("prior limitation codes must be unique")
        return self


class RecoveryPredecessorBinding(RecoveryModel):
    binding_version: Literal[1] = 1
    predecessor_campaign_id: str = Field(min_length=1)
    predecessor_campaign_sha256: str
    predecessor_freeze_sha256: str
    predecessor_invalidation_sha256: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_status: Literal["invalidated"] = "invalidated"
    predecessor_cumulative_llm_calls: int = Field(ge=0)
    predecessor_incremental_llm_calls: int = Field(default=0, ge=0, le=120)
    predecessor_started_round_count: int | None = Field(
        default=None,
        ge=0,
        le=6,
        exclude_if=lambda value: value is None,
    )
    predecessor_prior_usage_content_sha256: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_known_usage: RecoveryUsage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_unknown_llm_calls: int | None = Field(
        default=None,
        ge=0,
        le=120,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_binding(self) -> "RecoveryPredecessorBinding":
        if any(
            not _is_sha256(value)
            for value in (
                self.predecessor_campaign_sha256,
                self.predecessor_freeze_sha256,
            )
        ):
            raise ValueError("predecessor binding hashes must be lowercase SHA256 values")
        if (
            self.predecessor_invalidation_sha256 is not None
            and not _is_sha256(self.predecessor_invalidation_sha256)
        ):
            raise ValueError("predecessor invalidation hash must be lowercase SHA256")
        if (
            self.predecessor_prior_usage_content_sha256 is not None
            and not _is_sha256(self.predecessor_prior_usage_content_sha256)
        ):
            raise ValueError("predecessor prior usage hash must be lowercase SHA256")
        extended_fields_present = (
            self.predecessor_invalidation_sha256 is not None,
            self.predecessor_started_round_count is not None,
            self.predecessor_prior_usage_content_sha256 is not None,
        )
        if any(extended_fields_present) and not all(extended_fields_present):
            raise ValueError("predecessor accounting fields must appear together")
        if self.predecessor_incremental_llm_calls and not all(
            extended_fields_present
        ):
            raise ValueError("incremental predecessor calls require sealed accounting")
        usage_fields_present = (
            self.predecessor_known_usage is not None,
            self.predecessor_unknown_llm_calls is not None,
        )
        if any(usage_fields_present) and not all(usage_fields_present):
            raise ValueError("predecessor usage accounting fields must appear together")
        if all(usage_fields_present):
            if not all(extended_fields_present):
                raise ValueError("predecessor usage requires sealed accounting")
            assert self.predecessor_known_usage is not None
            assert self.predecessor_unknown_llm_calls is not None
            if (
                self.predecessor_known_usage.llm_calls
                + self.predecessor_unknown_llm_calls
                != self.predecessor_incremental_llm_calls
            ):
                raise ValueError("predecessor known and unknown calls must be additive")
        return self


class RecoveryPredecessorCarryover(RecoveryModel):
    carryover_version: Literal[1] = 1
    predecessor_campaign_id: str = Field(min_length=1)
    predecessor_campaign_sha256: str
    predecessor_invalidation_sha256: str
    conservative_llm_calls: int = Field(ge=1, le=120)
    started_round_count: int = Field(ge=1, le=6)
    accounting_status: Literal["conservative_unknown_usage"] = (
        "conservative_unknown_usage"
    )
    known_usage: RecoveryUsage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    unknown_llm_calls: int | None = Field(
        default=None,
        ge=0,
        le=120,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_carryover(self) -> "RecoveryPredecessorCarryover":
        if any(
            not _is_sha256(value)
            for value in (
                self.predecessor_campaign_sha256,
                self.predecessor_invalidation_sha256,
            )
        ):
            raise ValueError("predecessor carryover hashes must be lowercase SHA256 values")
        usage_fields_present = (
            self.known_usage is not None,
            self.unknown_llm_calls is not None,
        )
        if any(usage_fields_present) and not all(usage_fields_present):
            raise ValueError("carryover usage accounting fields must appear together")
        if all(usage_fields_present):
            assert self.known_usage is not None
            assert self.unknown_llm_calls is not None
            if (
                self.known_usage.llm_calls + self.unknown_llm_calls
                != self.conservative_llm_calls
            ):
                raise ValueError("carryover known and unknown calls must be additive")
        return self


class RecoveryFreeze(RecoveryModel):
    freeze_version: Literal[1] = 1
    case_id: str = Field(min_length=1)
    visible_input_sha256: str
    data_sha256: tuple[str, ...] = Field(min_length=1)
    reference_sha256: str
    reference_summary_sha256: str
    evaluator_sha256: str
    fault_matrix_sha256: str
    prompt_registry_sha256: str
    research_runtime_identity_sha256: str
    configuration_sha256: str
    benchmark_harness_sha256: str
    hypoweaver_source_sha256: str
    agent_laboratory_sha256: str
    source_official_protocol_sha256: str
    source_official_holdout_lock_id: str
    sealed_label_orders: tuple[Literal["A_B", "B_A"], ...] = Field(
        min_length=5,
        max_length=5,
    )
    sealed_system_assignments: tuple[Literal["A_B", "B_A"], ...] = Field(
        min_length=5,
        max_length=5,
    )
    predecessor_binding: RecoveryPredecessorBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    frozen_at: str
    freeze_sha256: str | None = None

    @model_validator(mode="after")
    def validate_freeze(self) -> "RecoveryFreeze":
        hashes = (
            self.visible_input_sha256,
            *self.data_sha256,
            self.reference_sha256,
            self.reference_summary_sha256,
            self.evaluator_sha256,
            self.fault_matrix_sha256,
            self.prompt_registry_sha256,
            self.research_runtime_identity_sha256,
            self.configuration_sha256,
            self.benchmark_harness_sha256,
            self.hypoweaver_source_sha256,
            self.agent_laboratory_sha256,
            self.source_official_protocol_sha256,
            self.source_official_holdout_lock_id,
        )
        if self.freeze_sha256 is not None:
            hashes = (*hashes, self.freeze_sha256)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("recovery freeze hashes must be lowercase SHA256 values")
        if set(self.sealed_label_orders) != {"A_B", "B_A"}:
            raise ValueError("sealed_label_orders must contain both A/B orders")
        if set(self.sealed_system_assignments) != {"A_B", "B_A"}:
            raise ValueError("sealed_system_assignments must contain both A/B orders")
        _aware_datetime(self.frozen_at, "frozen_at")
        return self


class PriorUsageImport(RecoveryModel):
    import_version: Literal[1] = 1
    source_official_attempt_id: str
    source_official_run_manifest_sha256: str
    source_official_holdout_lock_id: str
    usage: RecoveryUsage
    evidence: PriorUsageEvidence
    imported_at: str
    import_sha256: str | None = None

    @model_validator(mode="after")
    def validate_import(self) -> "PriorUsageImport":
        hashes = (
            self.source_official_attempt_id,
            self.source_official_run_manifest_sha256,
            self.source_official_holdout_lock_id,
        )
        if self.import_sha256 is not None:
            hashes = (*hashes, self.import_sha256)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("prior usage import hashes must be lowercase SHA256 values")
        if self.evidence.ledger_llm_calls != self.usage.llm_calls:
            raise ValueError("prior evidence ledger must match imported llm_calls")
        _aware_datetime(self.imported_at, "imported_at")
        return self


class RecoveryCallReceipt(RecoveryModel):
    receipt_version: Literal[1] = 1
    provenance_scope: Literal["seen_case_recovery_non_official"] = (
        "seen_case_recovery_non_official"
    )
    call_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    phase: RecoveryReceiptPhase
    logical_slot_id: str | None = None
    logical_call_id: str | None = None
    call_group: Literal["h1_h2", "h3", "h4"] | None = None
    prompt_key: str | None = None
    prompt_version: str | None = None
    attempt_type: Literal[
        "primary",
        "transport_retry",
        "schema_repair",
        "content_repair",
        "legacy",
    ] | None = None
    attempt_index: int = Field(default=1, ge=1, le=3)
    max_attempts: int = Field(default=3, ge=1, le=3)
    outcome: Literal[
        "succeeded",
        "schema_failure",
        "transport_failure",
        "provider_failure",
    ] = "succeeded"
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    response_sha256: str
    input_sha256: str | None = None
    output_schema_sha256: str | None = None
    provider_response_id_sha256: str | None = None
    source_receipt_sha256: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    error_type: str | None = None
    error_category: ModelCallErrorCategory | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    call_started_at: str
    call_completed_at: str

    @model_validator(mode="after")
    def validate_receipt(self) -> "RecoveryCallReceipt":
        hashes = (
            self.response_sha256,
            self.input_sha256,
            self.output_schema_sha256,
            self.provider_response_id_sha256,
            self.source_receipt_sha256,
        )
        if any(value is not None and not _is_sha256(value) for value in hashes):
            raise ValueError("receipt hashes must be lowercase SHA256 values")
        started = _aware_datetime(self.call_started_at, "call_started_at")
        completed = _aware_datetime(self.call_completed_at, "call_completed_at")
        if completed < started:
            raise ValueError("call_completed_at cannot precede call_started_at")
        if self.attempt_index > self.max_attempts:
            raise ValueError("attempt_index cannot exceed max_attempts")
        if self.outcome == "succeeded" and self.error_type is not None:
            raise ValueError("successful receipt cannot carry error_type")
        if self.outcome == "succeeded" and self.error_category is not None:
            raise ValueError("successful receipt cannot carry error_category")
        if self.outcome != "succeeded" and not self.error_type:
            raise ValueError("failed receipt requires error_type")
        return self


class RecoveryRoundReservation(RecoveryModel):
    reservation_version: Literal[1] = 1
    reservation_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    round_index: int = Field(ge=1, le=6)
    owner_id: str = Field(min_length=1)
    freeze_sha256: str
    call_limit: int = Field(ge=9, le=20)
    reserved_at: str
    lease_expires_at: str
    reservation_sha256: str

    @model_validator(mode="after")
    def validate_reservation(self) -> "RecoveryRoundReservation":
        for value in (self.freeze_sha256, self.reservation_sha256):
            if not _is_sha256(value):
                raise ValueError("reservation hashes must be lowercase SHA256 values")
        reserved = _aware_datetime(self.reserved_at, "reserved_at")
        expires = _aware_datetime(self.lease_expires_at, "lease_expires_at")
        if expires <= reserved:
            raise ValueError("lease_expires_at must follow reserved_at")
        return self


class RecoveryComparisonReservation(RecoveryModel):
    reservation_version: Literal[1] = 1
    reservation_id: str = Field(min_length=1)
    comparison_id: Literal["comparison-01"] = "comparison-01"
    owner_id: str = Field(min_length=1)
    freeze_sha256: str
    call_limit: Literal[26] = 26
    reserved_at: str
    lease_expires_at: str
    reservation_sha256: str

    @model_validator(mode="after")
    def validate_reservation(self) -> "RecoveryComparisonReservation":
        for value in (self.freeze_sha256, self.reservation_sha256):
            if not _is_sha256(value):
                raise ValueError("comparison reservation hashes must be SHA256 values")
        reserved = _aware_datetime(self.reserved_at, "reserved_at")
        expires = _aware_datetime(self.lease_expires_at, "lease_expires_at")
        if expires <= reserved:
            raise ValueError("lease_expires_at must follow reserved_at")
        return self


class RecoveryRoundSubmission(RecoveryModel):
    freeze_sha256: str
    call_limit: int = Field(ge=9, le=20)
    implementation_sha256: str
    started_at: str
    completed_at: str
    usage: RecoveryUsage
    receipts: tuple[RecoveryCallReceipt, ...] = ()
    benchmark_packet_sha256: str | None = None
    hard_metric_report_sha256: str | None = None
    fault_replay_sha256: str | None = None
    hard_metric_results: dict[str, bool] = Field(default_factory=dict)
    ablation_target_degradation_results: dict[str, bool] = Field(
        default_factory=dict
    )
    technical_failure: str | None = None
    invalidation_reason: str | None = None

    @model_validator(mode="after")
    def validate_submission(self) -> "RecoveryRoundSubmission":
        hashes = (self.freeze_sha256, self.implementation_sha256)
        for optional_hash in (
            self.benchmark_packet_sha256,
            self.hard_metric_report_sha256,
            self.fault_replay_sha256,
        ):
            if optional_hash is not None:
                hashes = (*hashes, optional_hash)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("round hashes must be lowercase SHA256 values")
        started = _aware_datetime(self.started_at, "started_at")
        completed = _aware_datetime(self.completed_at, "completed_at")
        if completed < started:
            raise ValueError("completed_at cannot precede started_at")
        if self.usage.llm_calls > self.call_limit:
            raise ValueError("round llm_calls cannot exceed call_limit")
        if len(self.receipts) != self.usage.llm_calls:
            raise ValueError("round receipt count must equal llm_calls")
        if len({receipt.call_id for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("round receipt call_ids must be unique")
        terminal_reasons = sum(
            value is not None
            for value in (self.technical_failure, self.invalidation_reason)
        )
        if terminal_reasons > 1:
            raise ValueError("round cannot be both technical_failed and invalidated")
        if terminal_reasons:
            if any(
                value is not None
                for value in (
                    self.benchmark_packet_sha256,
                    self.hard_metric_report_sha256,
                    self.fault_replay_sha256,
                )
            ) or self.hard_metric_results or self.ablation_target_degradation_results:
                raise ValueError("failed or invalidated round cannot carry hard metrics")
        else:
            if any(
                value is None
                for value in (
                    self.benchmark_packet_sha256,
                    self.hard_metric_report_sha256,
                    self.fault_replay_sha256,
                )
            ):
                raise ValueError(
                    "evaluated round requires packet, hard metric, and fault replay hashes"
                )
            if set(self.hard_metric_results) != set(HARD_METRIC_IDS):
                raise ValueError("evaluated round must contain all fixed hard metrics")
            if set(self.ablation_target_degradation_results) != {
                "without_reviewer",
                "without_probe",
                "without_h2",
                "without_independent_replication",
                "without_claim_gate",
                "without_manuscript_ir",
            }:
                raise ValueError(
                    "evaluated round must contain all six ablation degradations"
                )
            if self.usage.llm_calls < 9:
                raise ValueError("evaluated recovery round must use at least nine calls")
        for receipt in self.receipts:
            receipt_started = _aware_datetime(receipt.call_started_at, "call_started_at")
            receipt_completed = _aware_datetime(
                receipt.call_completed_at,
                "call_completed_at",
            )
            if receipt.phase != "recovery_round":
                raise ValueError("recovery round receipts must use recovery_round phase")
            if receipt_started < started or receipt_completed > completed:
                raise ValueError("round receipt timestamps must fall within the round")
        return self


class RecoveryRound(RecoveryModel):
    reservation_id: str | None = None
    round_id: str = Field(min_length=1)
    round_index: int = Field(ge=1, le=6)
    status: RecoveryRoundStatus
    freeze_sha256: str
    call_limit: int = Field(ge=9, le=20)
    implementation_sha256: str
    started_at: str
    completed_at: str
    usage: RecoveryUsage
    receipts: tuple[RecoveryCallReceipt, ...] = ()
    benchmark_packet_sha256: str | None = None
    hard_metric_report_sha256: str | None = None
    fault_replay_sha256: str | None = None
    hard_metric_results: dict[str, bool] = Field(default_factory=dict)
    ablation_target_degradation_results: dict[str, bool] = Field(
        default_factory=dict
    )
    technical_failure: str | None = None
    invalidation_reason: str | None = None
    previous_round_sha256: str | None = None
    round_sha256: str

    @model_validator(mode="after")
    def validate_round(self) -> "RecoveryRound":
        hashes = (self.freeze_sha256, self.implementation_sha256, self.round_sha256)
        for optional_hash in (
            self.benchmark_packet_sha256,
            self.hard_metric_report_sha256,
            self.fault_replay_sha256,
            self.previous_round_sha256,
        ):
            if optional_hash is not None:
                hashes = (*hashes, optional_hash)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("recovery round hashes must be lowercase SHA256 values")
        return self


class RecoveryComparisonSubmission(RecoveryModel):
    freeze_sha256: str
    status: Literal["completed", "technical_failed"]
    qwen_single_pass: RecoveryUsage
    agent_laboratory: RecoveryUsage
    blind_reviews: RecoveryUsage
    receipts: tuple[RecoveryCallReceipt, ...] = ()
    qwen_packet_sha256: str | None = None
    agent_laboratory_packet_sha256: str | None = None
    blind_summary_sha256: str | None = None
    delivery_manifest_sha256: str | None = None
    started_at: str
    completed_at: str
    technical_failure: str | None = None

    @model_validator(mode="after")
    def validate_submission(self) -> "RecoveryComparisonSubmission":
        hashes = [self.freeze_sha256]
        artifact_hashes = (
            self.qwen_packet_sha256,
            self.agent_laboratory_packet_sha256,
            self.blind_summary_sha256,
            self.delivery_manifest_sha256,
        )
        hashes.extend(value for value in artifact_hashes if value is not None)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("comparison hashes must be lowercase SHA256 values")
        started = _aware_datetime(self.started_at, "started_at")
        completed = _aware_datetime(self.completed_at, "completed_at")
        if completed < started:
            raise ValueError("completed_at cannot precede started_at")
        usage_by_phase = {
            "qwen_single_pass": self.qwen_single_pass,
            "agent_laboratory": self.agent_laboratory,
            "blind_review": self.blind_reviews,
        }
        if self.status == "completed":
            if self.technical_failure is not None:
                raise ValueError("completed comparison cannot carry technical_failure")
            if self.qwen_single_pass.llm_calls != 1:
                raise ValueError("completed comparison requires one Qwen baseline call")
            if self.blind_reviews.llm_calls != 5:
                raise ValueError("completed comparison requires five blind-review calls")
            if any(value is None for value in artifact_hashes):
                raise ValueError("completed comparison requires all artifact hashes")
        elif not self.technical_failure:
            raise ValueError("technical_failed comparison requires technical_failure")
        if self.agent_laboratory.llm_calls > 20:
            raise ValueError("Agent Laboratory comparison cannot exceed 20 calls")
        total_calls = sum(usage.llm_calls for usage in usage_by_phase.values())
        if total_calls > 26:
            raise ValueError("comparison calls cannot exceed the reserved 26 calls")
        if len(self.receipts) != total_calls:
            raise ValueError("comparison receipt count must equal llm_calls")
        if len({receipt.call_id for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("comparison receipt call_ids must be unique")
        for phase, usage in usage_by_phase.items():
            if sum(receipt.phase == phase for receipt in self.receipts) != usage.llm_calls:
                raise ValueError(f"comparison receipt count mismatch for {phase}")
        for receipt in self.receipts:
            receipt_started = _aware_datetime(receipt.call_started_at, "call_started_at")
            receipt_completed = _aware_datetime(
                receipt.call_completed_at,
                "call_completed_at",
            )
            if receipt_started < started or receipt_completed > completed:
                raise ValueError("comparison receipt timestamps must fall within comparison")
        return self


class RecoveryComparison(RecoveryComparisonSubmission):
    comparison_id: Literal["comparison-01"] = "comparison-01"
    reservation_id: str | None = None
    comparison_sha256: str

    @model_validator(mode="after")
    def validate_comparison_hash(self) -> "RecoveryComparison":
        if not _is_sha256(self.comparison_sha256):
            raise ValueError("comparison_sha256 must be a lowercase SHA256 value")
        return self


class RecoveryInvalidation(RecoveryModel):
    reason: str = Field(min_length=1)
    reservation_id: str | None = None
    reservation_scope: Literal["round", "comparison"] | None = None
    unknown_call_evidence: bool = False
    conservative_llm_call_charge: int = Field(default=0, ge=0, le=26)
    invalidated_at: str
    invalidation_sha256: str

    @model_validator(mode="after")
    def validate_invalidation(self) -> "RecoveryInvalidation":
        _aware_datetime(self.invalidated_at, "invalidated_at")
        if not _is_sha256(self.invalidation_sha256):
            raise ValueError("invalidation_sha256 must be a lowercase SHA256 value")
        if self.unknown_call_evidence != bool(self.conservative_llm_call_charge):
            raise ValueError(
                "unknown_call_evidence must match conservative call charging"
            )
        if (self.reservation_id is None) != (self.reservation_scope is None):
            raise ValueError("invalidation reservation id and scope must appear together")
        if self.reservation_scope == "comparison":
            if self.conservative_llm_call_charge != 26:
                raise ValueError(
                    "comparison reservation invalidation must charge all 26 calls"
                )
        elif self.reservation_scope == "round":
            if not 9 <= self.conservative_llm_call_charge <= 20:
                raise ValueError(
                    "round reservation invalidation must charge its full call_limit"
                )
        elif self.conservative_llm_call_charge != 0:
            raise ValueError("invalidation without a reservation cannot charge calls")
        return self


class RecoveryCampaign(RecoveryModel):
    campaign_version: Literal[1] = 1
    provenance_scope: Literal["seen_case_recovery_non_official"] = (
        "seen_case_recovery_non_official"
    )
    official: Literal[False] = False
    campaign_id: str = Field(min_length=1)
    freeze: RecoveryFreeze = Field(
        validation_alias=AliasChoices("freeze", "protocol"),
        serialization_alias="protocol",
    )
    prior_usage: PriorUsageImport
    predecessor_carryover: RecoveryPredecessorCarryover | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    total_call_ceiling: Literal[120] = 120
    comparison_call_reserve: Literal[26] = 26
    recovery_round_min_calls: Literal[9] = 9
    recovery_round_max_calls: Literal[20] = 20
    max_rounds: Literal[6] = 6
    rounds: tuple[RecoveryRound, ...] = Field(
        default=(),
        validation_alias=AliasChoices("rounds", "round_manifests"),
        serialization_alias="round_manifests",
    )
    active_round_reservation: RecoveryRoundReservation | None = None
    active_comparison_reservation: RecoveryComparisonReservation | None = None
    comparison: RecoveryComparison | None = Field(
        default=None,
        validation_alias=AliasChoices("comparison", "comparison_manifest"),
        serialization_alias="comparison_manifest",
    )
    invalidation: RecoveryInvalidation | None = None
    status: RecoveryCampaignStatus = "open"
    protocol_status: RecoveryProtocolStatus = "open"
    status_reason: str | None = None
    cumulative_token_usage_status: Literal["exact", "lower_bound"] = "exact"
    created_at: str
    updated_at: str
    campaign_sha256: str

    @model_validator(mode="after")
    def validate_campaign_shape(self) -> "RecoveryCampaign":
        created = _aware_datetime(self.created_at, "created_at")
        updated = _aware_datetime(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        if not _is_sha256(self.campaign_sha256):
            raise ValueError("campaign_sha256 must be a lowercase SHA256 value")
        if self.campaign_id == self.freeze.source_official_holdout_lock_id:
            raise ValueError("recovery campaign cannot reuse the official holdout lock id")
        inherited_rounds = (
            self.predecessor_carryover.started_round_count
            if self.predecessor_carryover is not None
            else 0
        )
        started_rounds = (
            inherited_rounds
            + len(self.rounds)
            + int(self.active_round_reservation is not None)
        )
        if started_rounds > self.max_rounds:
            raise ValueError("recovery campaign exceeds max_rounds")
        expected_protocol_status = {
            "open": "open",
            "qualified_seen_case": "hard-gate-qualified-on-seen-case",
            "invalidated": "invalidated",
            "exhausted": "exhausted-not-qualified",
        }[self.status]
        if self.protocol_status != expected_protocol_status:
            raise ValueError("recovery protocol_status does not match status")
        expected_token_status = (
            "lower_bound"
            if self.predecessor_carryover is not None
            else self.prior_usage.evidence.token_usage_status
        )
        if self.cumulative_token_usage_status != expected_token_status:
            raise ValueError("cumulative token usage status mismatch")
        return self


# Public protocol names used by recovery delivery artifacts. The aliases retain
# compatibility with the initial internal names without creating duplicate schemas.
RecoveryCampaignProtocol = RecoveryFreeze
RecoveryRoundManifest = RecoveryRound
