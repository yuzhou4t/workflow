from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .models import StrictModel, utc_now


TERMINAL_EXECUTION_STATUSES = {
    "succeeded",
    "failed",
    "cancelled",
    "not_executed",
    "fixture_only",
}

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

ABLATION_IDS = (
    "without_reviewer",
    "without_probe",
    "without_h2",
    "without_independent_replication",
    "without_claim_gate",
    "without_manuscript_ir",
)

FaultId = Literal[
    "duplicate_merge_inflation",
    "time_leakage_or_lead_misuse",
    "unit_amplification",
    "variable_timing_shift",
    "wrong_clustering",
    "significant_subgroup_cherry_pick",
    "table_text_mismatch",
    "association_to_causation",
    "deleted_null_or_failure_branch",
]

AblationId = Literal[
    "without_reviewer",
    "without_probe",
    "without_h2",
    "without_independent_replication",
    "without_claim_gate",
    "without_manuscript_ir",
]

ABLATION_NATIVE_ARTIFACTS: dict[AblationId, str] = {
    "without_reviewer": "design_arena",
    "without_probe": "candidate_design_set",
    "without_h2": "formal_research_contract",
    "without_independent_replication": "reproduction_audit",
    "without_claim_gate": "claim_gate_report",
    "without_manuscript_ir": "manuscript_statement_registry",
}


class BenchmarkResourceUsage(StrictModel):
    llm_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0, ge=0)
    technical_failures: list[str] = Field(default_factory=list)


class OfficialAttemptBinding(StrictModel):
    """Unforgeable-at-begin identity shared by every official model call."""

    attempt_id: str = Field(min_length=64, max_length=64)
    run_manifest_sha256: str = Field(min_length=64, max_length=64)
    begun_at: str

    @model_validator(mode="after")
    def validate_binding(self) -> "OfficialAttemptBinding":
        for name, value in (
            ("attempt_id", self.attempt_id),
            ("run_manifest_sha256", self.run_manifest_sha256),
        ):
            if any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase hexadecimal value")
        _aware_datetime(self.begun_at, "begun_at")
        return self


class OfficialCallReceipt(StrictModel):
    """Code-owned provenance for one provider response in an official attempt."""

    receipt_version: Literal[1] = 1
    call_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    attempt_id: str = Field(min_length=64, max_length=64)
    run_manifest_sha256: str = Field(min_length=64, max_length=64)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    response_sha256: str = Field(min_length=64, max_length=64)
    call_started_at: str
    call_completed_at: str

    @model_validator(mode="after")
    def validate_receipt(self) -> "OfficialCallReceipt":
        for name, value in (
            ("attempt_id", self.attempt_id),
            ("run_manifest_sha256", self.run_manifest_sha256),
            ("response_sha256", self.response_sha256),
        ):
            if any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase hexadecimal value")
        started = _aware_datetime(self.call_started_at, "call_started_at")
        completed = _aware_datetime(self.call_completed_at, "call_completed_at")
        if completed < started:
            raise ValueError("call_completed_at cannot precede call_started_at")
        return self


def _aware_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


class NormalizedDesign(StrictModel):
    method_family: str | None = None
    outcomes: list[str] = Field(default_factory=list)
    treatments_or_exposures: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    fixed_effects: list[str] = Field(default_factory=list)
    standard_error_strategy: str | None = None
    planned_check_ids: list[str] = Field(default_factory=list)
    required_check_ids: list[str] = Field(default_factory=list)
    check_threat_ids: dict[str, str] = Field(default_factory=dict)
    frozen_before_execution: bool = False
    source_artifact_sha256: str | None = None
    contract_sha256: str | None = None


class NormalizedExecution(StrictModel):
    execution_id: str
    check_id: str
    execution_status: str
    run_type: str
    estimates: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    not_executed_reason_code: str | None = None
    implementation_id: str | None = None
    implementation_version: str | None = None
    code_sha256: str | None = None
    environment_sha256: str | None = None
    fixed_effects: list[str] = Field(default_factory=list)
    standard_error_strategy: str | None = None
    contract_sha256: str | None = None
    data_sha256: list[str] = Field(default_factory=list)
    source_artifact_sha256: str | None = None


class NormalizedClaim(StrictModel):
    claim_id: str
    text: str
    strength: str
    admission_status: str = "unassessed"
    check_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    gate_reasons: list[str] = Field(default_factory=list)


class NormalizedStatement(StrictModel):
    statement_id: str
    text: str
    statement_kind: str
    section_id: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    protected_values: list[dict[str, Any]] = Field(default_factory=list)


class NormalizedReproduction(StrictModel):
    mode: str = "not_available"
    status: str = "not_available"
    covered_check_ids: list[str] = Field(default_factory=list)
    primary_implementation_id: str | None = None
    replication_implementation_id: str | None = None
    independence_scope: Literal[
        "unspecified",
        "estimator_only",
        "data_preparation_and_estimator",
        "end_to_end",
    ] = "unspecified"
    shared_components: list[str] = Field(default_factory=list)


class BenchmarkPacket(StrictModel):
    packet_version: Literal["enterprise-panel-v1"] = "enterprise-panel-v1"
    packet_id: str
    system_id: Literal[
        "qwen_single_pass",
        "agent_laboratory",
        "hypoweaver",
        "hypoweaver_ablation",
    ]
    case_id: str
    visible_input_sha256: str
    data_sha256: list[str]
    model_id: str
    design: NormalizedDesign
    executions: list[NormalizedExecution] = Field(default_factory=list)
    claims: list[NormalizedClaim] = Field(default_factory=list)
    statements: list[NormalizedStatement] = Field(default_factory=list)
    manuscript_text: str = ""
    manuscript_section_texts: dict[str, str] = Field(default_factory=dict)
    manuscript_sha256: str | None = None
    reproduction: NormalizedReproduction = Field(default_factory=NormalizedReproduction)
    resource_usage: BenchmarkResourceUsage = Field(default_factory=BenchmarkResourceUsage)
    official_receipts: list[OfficialCallReceipt] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    native_artifact_sha256: dict[str, str] = Field(default_factory=dict)
    ablation_id: str | None = None
    sealed_at: str = Field(default_factory=utc_now)
    packet_sha256: str | None = None

    @model_validator(mode="after")
    def validate_ablation_identity(self) -> "BenchmarkPacket":
        if self.system_id == "hypoweaver_ablation" and not self.ablation_id:
            raise ValueError("hypoweaver_ablation requires ablation_id")
        if self.system_id != "hypoweaver_ablation" and self.ablation_id:
            raise ValueError("ablation_id is only valid for hypoweaver_ablation")
        return self


class FaultOutcome(StrictModel):
    fault_id: FaultId
    detected: bool
    action: Literal["block", "downgrade", "disclose", "missed"]
    evidence: list[str] = Field(default_factory=list)


class BenchmarkReference(StrictModel):
    protocol_version: Literal["enterprise-panel-v1"] = "enterprise-panel-v1"
    case_id: str
    visible_input_sha256: str
    data_sha256: list[str]
    expected_design: dict[str, Any]
    expected_contract_sha256: str | None = None
    required_check_ids: list[str]
    required_threat_ids: list[str] = Field(default_factory=list)
    independently_reproducible_check_ids: list[str]
    clean_packet_ids: list[str] = Field(default_factory=list)


class HardMetric(StrictModel):
    metric_id: Literal[
        "contract_execution_fidelity",
        "required_step_terminal_rate",
        "required_evidence_completion",
        "fatal_fault_detection_rate",
        "clean_false_block_count",
        "protected_numeric_consistency",
        "statement_traceability",
        "causal_overreach_escape_count",
        "independent_replication_rate",
    ]
    numerator: int
    denominator: int
    value: float
    target: str
    passed: bool
    evidence: list[str] = Field(default_factory=list)


class HardMetricReport(StrictModel):
    report_id: str
    case_id: str
    packet_id: str
    protocol_version: Literal["enterprise-panel-v1"] = "enterprise-panel-v1"
    metrics: list[HardMetric]
    all_hard_gates_passed: bool
    created_at: str = Field(default_factory=utc_now)


class AblationReplayResult(StrictModel):
    ablation_id: AblationId
    disabled_component: str
    packet_sha256: str
    target_fault_ids: list[FaultId]
    fault_outcomes: list[FaultOutcome]
    detected_fault_count: int = Field(ge=0, le=9)
    target_fault_degraded: bool
    reused_frozen_fixture: Literal[True] = True
    llm_calls: Literal[0] = 0


class FaultReplayReport(StrictModel):
    protocol_version: Literal["enterprise-panel-v1"] = "enterprise-panel-v1"
    case_id: str
    clean_packet_sha256: str
    full_system_outcomes: list[FaultOutcome]
    clean_false_block_count: int = Field(ge=0)
    ablations: list[AblationReplayResult]

    @model_validator(mode="after")
    def validate_matrix(self) -> "FaultReplayReport":
        if {item.fault_id for item in self.full_system_outcomes} != set(FAULT_IDS):
            raise ValueError("fault replay must contain all nine fixed faults")
        if {item.ablation_id for item in self.ablations} != set(ABLATION_IDS):
            raise ValueError("fault replay must contain all six fixed ablations")
        return self


class BenchmarkCallBudget(StrictModel):
    qwen_single_pass_max_calls: Literal[1] = 1
    hypoweaver_max_calls: int = Field(default=20, ge=1, le=20)
    agent_laboratory_max_calls: int = Field(default=20, ge=1, le=20)
    blind_review_calls: Literal[5] = 5
    total_max_calls: int = Field(default=46, ge=1, le=59)

    @model_validator(mode="after")
    def validate_total(self) -> "BenchmarkCallBudget":
        calculated = (
            self.qwen_single_pass_max_calls
            + self.hypoweaver_max_calls
            + self.agent_laboratory_max_calls
            + self.blind_review_calls
        )
        if self.total_max_calls != calculated:
            raise ValueError("total_max_calls must equal the sum of component limits")
        return self


class FrozenBenchmarkProtocol(StrictModel):
    protocol_version: Literal["enterprise-panel-v1"] = "enterprise-panel-v1"
    case_id: str
    visible_input_sha256: str
    data_sha256: list[str]
    reference_sha256: str
    source_sha256: dict[str, str]
    configuration_sha256: str
    source_artifact_paths: dict[str, list[str]] = Field(default_factory=dict)
    configuration_artifact_paths: list[str] = Field(default_factory=list)
    fault_ids: list[FaultId] = Field(default_factory=lambda: list(FAULT_IDS))
    ablation_ids: list[AblationId] = Field(default_factory=lambda: list(ABLATION_IDS))
    call_budget: BenchmarkCallBudget = Field(default_factory=BenchmarkCallBudget)
    official_hidden_run_is_one_shot: Literal[True] = True
    official_hidden_run_count: Literal[1] = 1
    ablations_reuse_frozen_fixture: Literal[True] = True
    frozen_at: str = Field(default_factory=utc_now)
    protocol_sha256: str | None = None

    @model_validator(mode="after")
    def validate_fixed_matrix(self) -> "FrozenBenchmarkProtocol":
        if tuple(self.fault_ids) != FAULT_IDS:
            raise ValueError("protocol must freeze the nine faults in registry order")
        if tuple(self.ablation_ids) != ABLATION_IDS:
            raise ValueError("protocol must freeze the six ablations in registry order")
        required_sources = {
            "hypoweaver",
            "agent_laboratory",
            "benchmark_harness",
        }
        if set(self.source_sha256) != required_sources:
            raise ValueError(
                "source_sha256 must freeze HypoWeaver, Agent Laboratory, and the benchmark harness"
            )
        if (
            self.source_artifact_paths
            and set(self.source_artifact_paths) != required_sources
        ):
            raise ValueError(
                "source_artifact_paths must identify HypoWeaver, Agent Laboratory, "
                "and the benchmark harness"
            )
        artifact_paths = [
            path
            for paths in self.source_artifact_paths.values()
            for path in paths
        ] + self.configuration_artifact_paths
        for path in artifact_paths:
            parts = path.split("/")
            if (
                not path
                or path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError(
                    "benchmark artifact paths must be normalized relative POSIX paths"
                )
        if any(
            len(paths) != len(set(paths))
            for paths in self.source_artifact_paths.values()
        ):
            raise ValueError("source_artifact_paths cannot contain duplicates")
        if len(self.configuration_artifact_paths) != len(
            set(self.configuration_artifact_paths)
        ):
            raise ValueError("configuration_artifact_paths cannot contain duplicates")
        hashes = [
            self.visible_input_sha256,
            self.reference_sha256,
            self.configuration_sha256,
            *self.data_sha256,
            *self.source_sha256.values(),
        ]
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise ValueError("frozen protocol hashes must be lowercase SHA256 values")
        return self


class BenchmarkUsageReport(StrictModel):
    qwen_single_pass: BenchmarkResourceUsage
    hypoweaver: BenchmarkResourceUsage
    agent_laboratory: BenchmarkResourceUsage
    blind_reviews: BenchmarkResourceUsage
    blind_review_calls: int = Field(ge=0)
    ablation_llm_calls: Literal[0] = 0
    total_llm_calls: int = Field(ge=0)
    within_budget: bool
    technical_failures: list[str] = Field(default_factory=list)


class BenchmarkDeliveryManifest(StrictModel):
    protocol_version: Literal["enterprise-panel-v1"] = "enterprise-panel-v1"
    protocol_sha256: str
    case_id: str
    official: bool
    file_sha256: dict[str, str]
    all_hard_gates_passed: bool
    claim_condition_met: bool
    completed_at: str = Field(default_factory=utc_now)
    manifest_sha256: str | None = None


class NeurIPSRatings(StrictModel):
    quality: int = Field(ge=1, le=4)
    significance: int = Field(ge=1, le=4)
    clarity: int = Field(ge=1, le=4)
    soundness: int = Field(ge=1, le=4)
    presentation: int = Field(ge=1, le=4)
    contribution: int = Field(ge=1, le=4)
    overall: int = Field(ge=1, le=10)
    confidence: int = Field(ge=1, le=5)
    recommendation: Literal["accept", "reject"]


class PairedBlindCallReceipt(StrictModel):
    """Sanitized provenance for one non-fixture paired-review provider call."""

    receipt_version: Literal[1] = 1
    call_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    sample_index: int = Field(ge=1, le=5)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    outcome: Literal["succeeded", "technical_failure"]
    response_sha256: str | None = None
    failure_package_sha256: str | None = None
    failure_type: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    call_started_at: str
    call_completed_at: str

    @model_validator(mode="after")
    def validate_receipt(self) -> "PairedBlindCallReceipt":
        if self.provider == "fixture":
            raise ValueError("fixture reviews cannot produce provider call receipts")
        hashes = [
            value
            for value in (self.response_sha256, self.failure_package_sha256)
            if value is not None
        ]
        if len(hashes) != 1 or any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise ValueError("paired blind receipt requires exactly one SHA256 hash")
        if self.outcome == "succeeded":
            if (
                self.response_sha256 is None
                or self.failure_package_sha256 is not None
                or self.failure_type is not None
            ):
                raise ValueError("successful receipt requires response_sha256 only")
        elif (
            self.failure_package_sha256 is None
            or self.response_sha256 is not None
        ):
            raise ValueError(
                "technical failure receipt requires failure_package_sha256 only"
            )
        started = _aware_datetime(self.call_started_at, "call_started_at")
        completed = _aware_datetime(self.call_completed_at, "call_completed_at")
        if completed < started:
            raise ValueError("call_completed_at cannot precede call_started_at")
        return self


class NeurIPSReview(StrictModel):
    review_id: str
    sample_index: int = Field(ge=1, le=5)
    label_order: Literal["A_B", "B_A"]
    system_assignment: Literal["A_B", "B_A"] = "A_B"
    ratings_a: NeurIPSRatings
    ratings_b: NeurIPSRatings
    preferred_label: Literal["A", "B", "tie"]
    diagnosis: list[str] = Field(default_factory=list)
    resource_usage: BenchmarkResourceUsage = Field(
        default_factory=BenchmarkResourceUsage
    )
    official_receipt: OfficialCallReceipt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    call_receipt: PairedBlindCallReceipt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PairedReviewSummary(StrictModel):
    case_id: str
    packet_a_id: str
    packet_b_id: str
    reviews: list[NeurIPSReview] = Field(min_length=5, max_length=5)
    median_scores: dict[str, dict[str, float]]
    interquartile_ranges: dict[str, dict[str, float]]
    preference_counts: dict[str, int]
    model_only: Literal[True] = True


class PairedEvaluationRequest(StrictModel):
    packet_a: BenchmarkPacket
    packet_b: BenchmarkPacket
    reference_summary: str = Field(min_length=1)
    model_provider: Literal["fixture", "qwen"] = "fixture"
    review_samples: Literal[5] = 5
    official_attempt: OfficialAttemptBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    sealed_label_orders: list[Literal["A_B", "B_A"]] | None = Field(
        default=None,
        min_length=5,
        max_length=5,
        exclude_if=lambda value: value is None,
    )
    sealed_system_assignments: list[Literal["A_B", "B_A"]] | None = Field(
        default=None,
        min_length=5,
        max_length=5,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_pair(self) -> "PairedEvaluationRequest":
        if self.packet_a.packet_id == self.packet_b.packet_id:
            raise ValueError("paired evaluation requires distinct packet ids")
        if self.packet_a.case_id != self.packet_b.case_id:
            raise ValueError("paired packets must use the same case")
        if self.packet_a.visible_input_sha256 != self.packet_b.visible_input_sha256:
            raise ValueError("paired packets must use the same visible input")
        if self.packet_a.data_sha256 != self.packet_b.data_sha256:
            raise ValueError("paired packets must use the same data hashes")
        if self.official_attempt is not None and self.model_provider != "qwen":
            raise ValueError("official paired evaluation requires the qwen provider")
        if (self.sealed_label_orders is None) != (
            self.sealed_system_assignments is None
        ):
            raise ValueError(
                "sealed label orders and system assignments must be provided together"
            )
        for field_name, orders in (
            ("sealed_label_orders", self.sealed_label_orders),
            ("sealed_system_assignments", self.sealed_system_assignments),
        ):
            if orders is not None and set(orders) != {"A_B", "B_A"}:
                raise ValueError(f"{field_name} must represent both A/B orders")
        return self


class PairedEvaluationView(StrictModel):
    id: str
    definition_id: Literal["app-b-paired"] = "app-b-paired"
    definition_version: Literal["2.0.0"] = "2.0.0"
    case_id: str
    packet_a_id: str
    packet_b_id: str
    status: Literal["completed", "failed"]
    sealed_label_orders: list[Literal["A_B", "B_A"]] = Field(
        default_factory=list
    )
    sealed_system_assignments: list[Literal["A_B", "B_A"]] = Field(
        default_factory=list
    )
    review_resource_usage: list[BenchmarkResourceUsage] = Field(
        default_factory=list
    )
    review_call_receipts: list[PairedBlindCallReceipt] = Field(
        default_factory=list
    )
    partial_reviews: list[NeurIPSReview] = Field(default_factory=list)
    receipt_count: int = Field(default=0, ge=0)
    result: PairedReviewSummary | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    def verify_runtime_receipts(
        self,
        *,
        expect_real_qwen: bool,
        expected_model: str | None = None,
    ) -> None:
        """Recompute new-run receipt invariants without rejecting legacy views."""

        recorded_calls = sum(item.llm_calls for item in self.review_resource_usage)
        if self.receipt_count != len(self.review_call_receipts):
            raise ValueError("paired blind receipt_count does not match receipt list")
        if expect_real_qwen:
            if recorded_calls != 5 or self.receipt_count != 5:
                raise ValueError(
                    "real paired blind evaluation requires exactly five calls and receipts"
                )
            receipts_by_sample = {
                receipt.sample_index: receipt for receipt in self.review_call_receipts
            }
            if set(receipts_by_sample) != {1, 2, 3, 4, 5}:
                raise ValueError(
                    "real paired blind receipts require five distinct sample indexes"
                )
            if len({receipt.call_id for receipt in self.review_call_receipts}) != 5:
                raise ValueError("real paired blind receipt call ids must be unique")
            receipt_models = {
                receipt.model for receipt in self.review_call_receipts
            }
            if len(receipt_models) != 1 or (
                expected_model is not None and receipt_models != {expected_model}
            ):
                raise ValueError("real paired blind receipt model mismatch")
            if len(self.review_resource_usage) != 5:
                raise ValueError(
                    "real paired blind evaluation requires five usage records"
                )
            for sample_index, usage in enumerate(self.review_resource_usage, start=1):
                receipt = receipts_by_sample[sample_index]
                if receipt.provider != "qwen":
                    raise ValueError("real paired blind receipt provider must be qwen")
                if (
                    receipt.input_tokens != usage.input_tokens
                    or receipt.output_tokens != usage.output_tokens
                ):
                    raise ValueError(
                        "paired blind receipt tokens do not match resource usage"
                    )
            if self.status == "completed" and any(
                receipt.outcome != "succeeded"
                for receipt in self.review_call_receipts
            ):
                raise ValueError(
                    "completed paired blind evaluation requires successful receipts"
                )
        elif recorded_calls != 0 or self.review_call_receipts or self.receipt_count:
            raise ValueError(
                "fixture or injected paired blind evaluation cannot record real calls"
            )
        partial_by_sample = {
            review.sample_index: review for review in self.partial_reviews
        }
        if len(partial_by_sample) != len(self.partial_reviews):
            raise ValueError("partial paired reviews require unique sample indexes")
        if self.status == "completed" and self.partial_reviews:
            raise ValueError("completed paired evaluation cannot retain partial reviews")
        receipts_by_sample = {
            receipt.sample_index: receipt for receipt in self.review_call_receipts
        }
        for sample_index, review in partial_by_sample.items():
            receipt = receipts_by_sample.get(sample_index)
            if expect_real_qwen and (
                review.call_receipt is None
                or receipt is None
                or review.call_receipt != receipt
                or receipt.outcome != "succeeded"
            ):
                raise ValueError("partial review must bind a successful call receipt")
            if not expect_real_qwen and review.call_receipt is not None:
                raise ValueError("injected partial review cannot bind a real receipt")
