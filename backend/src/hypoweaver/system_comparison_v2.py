from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import AliasChoices, Field, model_validator

from .models import StrictModel, utc_now
from .seal import canonical_sha256


PROTOCOL_VERSION = "system-comparison-v2"
SYSTEM_IDS = ("hypoweaver", "agent_laboratory")
CASE_SPLITS = ("dev", "validation", "quasi_holdout")
SCORE_DIMENSION_WEIGHTS = {
    "method_selection_and_design": 20,
    "execution_correctness_and_reproducibility": 20,
    "identification_and_diagnostics": 20,
    "robustness_falsification_and_sensitivity": 15,
    "claim_calibration_and_traceability": 15,
    "reporting_and_failure_disclosure": 10,
}
RETRYABLE_TECHNICAL_CATEGORIES = (
    "dns",
    "tls",
    "connect_timeout",
    "read_timeout",
    "proxy",
    "connection_reset",
    "http_429",
    "http_5xx",
)

SystemIdV2 = Literal["hypoweaver", "agent_laboratory"]
CaseSplitV2 = Literal["dev", "validation", "quasi_holdout"]
InputViewV2 = Literal["discovery_blind", "reproduction_aligned"]


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


class AgentLaboratoryScheduleV2(StrictModel):
    max_steps: Literal[5] = 5
    num_papers_lit_review: Literal[0] = 0
    mlesolver_max_steps: Literal[1] = 1
    papersolver_max_steps: Literal[0] = 0


class SystemComparisonBudgetV2(StrictModel):
    provider_attempts_per_system: Literal[40] = 40
    hypoweaver_logical_calls: Literal[20] = 20
    max_attempts_per_logical_call: Literal[3] = 3
    failed_provider_attempts_count_toward_budget: Literal[True] = True


class TechnicalRetryPolicyV2(StrictModel):
    same_request_sha256_required: Literal[True] = True
    prompt_or_context_mutation_prohibited: Literal[True] = True
    fresh_run_retry_prohibited_for_quasi_holdout: Literal[True] = True
    retryable_categories: tuple[str, ...] = RETRYABLE_TECHNICAL_CATEGORIES

    @model_validator(mode="after")
    def validate_fixed_categories(self) -> "TechnicalRetryPolicyV2":
        if self.retryable_categories != RETRYABLE_TECHNICAL_CATEGORIES:
            raise ValueError("technical retry categories must use the frozen v2 order")
        return self


class ComparisonCaseSpecV2(StrictModel):
    case_id: str = Field(min_length=1)
    split: CaseSplitV2
    input_view: str = Field(min_length=1)
    semantic_input_sha256: str = Field(min_length=64, max_length=64)
    system_visible_input_sha256: dict[SystemIdV2, str]
    data_sha256: list[str] = Field(min_length=1)
    hidden_reference_sha256: str = Field(min_length=64, max_length=64)
    system_order: tuple[SystemIdV2, SystemIdV2]
    include_in_primary_score: bool = False
    one_shot: bool = False

    @model_validator(mode="after")
    def validate_case_role(self) -> "ComparisonCaseSpecV2":
        if tuple(self.system_order) not in (
            ("hypoweaver", "agent_laboratory"),
            ("agent_laboratory", "hypoweaver"),
        ):
            raise ValueError("system_order must contain each comparison system once")
        if set(self.system_visible_input_sha256) != set(SYSTEM_IDS):
            raise ValueError("system_visible_input_sha256 must cover both systems")
        hashes = [
            self.semantic_input_sha256,
            self.hidden_reference_sha256,
            *self.system_visible_input_sha256.values(),
            *self.data_sha256,
        ]
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("case identity fields must be lowercase SHA256 values")
        if self.include_in_primary_score and self.split != "quasi_holdout":
            raise ValueError("only quasi_holdout cases may enter the primary score")
        if self.include_in_primary_score and self.input_view != "discovery_blind":
            raise ValueError("primary scoring requires the discovery_blind input view")
        if self.split == "quasi_holdout" and not self.include_in_primary_score:
            raise ValueError("all quasi_holdout cases enter the preregistered primary score")
        if self.split == "quasi_holdout" and not self.one_shot:
            raise ValueError("quasi_holdout cases must be one-shot")
        if self.split != "quasi_holdout" and self.one_shot:
            raise ValueError("only quasi_holdout cases use the formal one-shot lock")
        return self


class ScientificScoringRubricV2(StrictModel):
    dimension_weights: dict[str, int] = Field(
        default_factory=lambda: dict(SCORE_DIMENSION_WEIGHTS)
    )
    judge_samples_per_case: Literal[5] = 5
    case_is_statistical_unit: Literal[True] = True
    judge_calls_are_not_independent_cases: Literal[True] = True
    infrastructure_failure_is_not_scored_as_scientific_zero: Literal[True] = True

    @model_validator(mode="after")
    def validate_fixed_weights(self) -> "ScientificScoringRubricV2":
        if self.dimension_weights != SCORE_DIMENSION_WEIGHTS:
            raise ValueError("v2 score weights must match the preregistered rubric")
        if sum(self.dimension_weights.values()) != 100:
            raise ValueError("v2 score weights must sum to 100")
        return self


class FrozenSystemComparisonProtocolV2(StrictModel):
    protocol_version: Literal["system-comparison-v2"] = PROTOCOL_VERSION
    suite_id: str = Field(min_length=1)
    comparison_estimand: Literal["system_package_capability"] = (
        "system_package_capability"
    )
    systems: tuple[SystemIdV2, SystemIdV2] = SYSTEM_IDS
    cases: list[ComparisonCaseSpecV2] = Field(min_length=3)
    model_id_by_system: dict[SystemIdV2, str]
    budget: SystemComparisonBudgetV2 = Field(
        default_factory=SystemComparisonBudgetV2
    )
    technical_retry_policy: TechnicalRetryPolicyV2 = Field(
        default_factory=TechnicalRetryPolicyV2
    )
    agent_laboratory_schedule: AgentLaboratoryScheduleV2 = Field(
        default_factory=AgentLaboratoryScheduleV2
    )
    scoring: ScientificScoringRubricV2 = Field(
        default_factory=ScientificScoringRubricV2
    )
    source_sha256: dict[str, str]
    configuration_sha256: str = Field(min_length=64, max_length=64)
    frozen_at: str = Field(default_factory=utc_now)
    protocol_sha256: str | None = None

    @model_validator(mode="after")
    def validate_suite(self) -> "FrozenSystemComparisonProtocolV2":
        if tuple(self.systems) != SYSTEM_IDS:
            raise ValueError("v2 compares HypoWeaver and Agent Laboratory only")
        if set(self.model_id_by_system) != set(SYSTEM_IDS):
            raise ValueError("model_id_by_system must cover both systems")
        if any(not value.strip() for value in self.model_id_by_system.values()):
            raise ValueError("model ids cannot be empty")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case ids must be unique inside a frozen suite")
        if {case.split for case in self.cases} != set(CASE_SPLITS):
            raise ValueError("v2 suite must freeze dev, validation, and quasi_holdout groups")
        primary = [case for case in self.cases if case.include_in_primary_score]
        if not primary:
            raise ValueError("v2 suite requires at least one primary quasi-holdout case")
        if len(primary) >= 2 and len({case.system_order for case in primary}) < 2:
            raise ValueError("primary cases must counterbalance the two system orders")
        if set(self.source_sha256) != {
            "hypoweaver",
            "agent_laboratory",
            "benchmark_harness",
        }:
            raise ValueError("v2 must freeze both systems and the benchmark harness")
        hashes = [self.configuration_sha256, *self.source_sha256.values()]
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("suite source and configuration hashes must be SHA256 values")
        return self


class SystemComparisonRunConfigurationV2(StrictModel):
    artifact_root: str = Field(min_length=1)
    protocol_path: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    case_root: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    working_dir: str = Field(min_length=1)
    official_state_root: str = Field(min_length=1)
    agent_laboratory_root: str = Field(min_length=1)
    runtime_public_path: str = Field(min_length=1)
    budget: SystemComparisonBudgetV2 = Field(
        default_factory=SystemComparisonBudgetV2
    )
    agent_laboratory_schedule: AgentLaboratoryScheduleV2 = Field(
        default_factory=AgentLaboratoryScheduleV2
    )
    agent_timeout_seconds: int = Field(default=1800, ge=1)
    hypoweaver_timeout_seconds: int = Field(default=1800, ge=1)

    @model_validator(mode="after")
    def validate_relative_artifacts(self) -> "SystemComparisonRunConfigurationV2":
        for value in (
            self.protocol_path,
            self.case_root,
            self.agent_laboratory_root,
            self.runtime_public_path,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("v2 repository artifacts must be relative paths")
        return self


class SystemResourceUsageV2(StrictModel):
    system_id: SystemIdV2
    logical_calls: int = Field(ge=0)
    provider_attempts: int = Field(ge=0)
    successful_provider_attempts: int = Field(ge=0)
    failed_provider_attempts: int = Field(ge=0)
    technical_retry_attempts: int = Field(ge=0)
    schema_repair_attempts: int = Field(ge=0)
    content_repair_attempts: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_provider_wall_time_seconds: float = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "model_provider_wall_time_seconds",
            "wall_time_seconds",
        ),
        description=(
            "Cumulative provider-request latency reported by the native model "
            "ledger; it is not the benchmark cell's end-to-end elapsed time."
        ),
    )
    technical_failures: list[str] = Field(default_factory=list)
    receipt_count: int = Field(ge=0)
    retry_sequences_valid: bool
    retry_request_identity_verified: bool
    within_provider_attempt_budget: bool
    within_logical_call_budget: bool | None
    within_budget: bool

    @model_validator(mode="after")
    def validate_budget_status(self) -> "SystemResourceUsageV2":
        calculated = (
            self.within_provider_attempt_budget
            and self.within_logical_call_budget is not False
            and self.retry_sequences_valid
            and self.retry_request_identity_verified
        )
        if self.within_budget != calculated:
            raise ValueError("within_budget does not match the v2 receipt checks")
        if self.provider_attempts != self.receipt_count:
            raise ValueError("provider_attempts must equal receipt_count")
        if (
            self.successful_provider_attempts + self.failed_provider_attempts
            != self.provider_attempts
        ):
            raise ValueError("provider attempt outcomes must sum to provider_attempts")
        return self


class SystemRuntimeEnvelopeV2(StrictModel):
    system_id: SystemIdV2
    provider_attempt_limit: Literal[40] = 40
    logical_call_limit: int | None = None
    max_attempts_per_logical_call: Literal[3] = 3
    cell_wall_time_limit_seconds: Literal[2700] = 2700
    statistical_phase_wall_time_limit_seconds: Literal[1800] = 1800
    frozen_dag_step_limit_per_implementation: Literal[12] = 12

    @model_validator(mode="after")
    def validate_system_limit(self) -> "SystemRuntimeEnvelopeV2":
        expected = 20 if self.system_id == "hypoweaver" else None
        if self.logical_call_limit != expected:
            raise ValueError(
                "HypoWeaver must declare 20 logical calls; Agent Laboratory has no "
                "cross-architecture logical-call ceiling"
            )
        return self


class SystemComparisonRunOutputV2(StrictModel):
    schema_version: Literal["system-comparison-run-v2"] = (
        "system-comparison-run-v2"
    )
    protocol_sha256: str = Field(min_length=64, max_length=64)
    case_id: str = Field(min_length=1)
    suite_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    input_view: InputViewV2 | None = None
    independent_case_id: str | None = Field(default=None, min_length=1)
    hidden_reference_access: Literal["denied"] | None = None
    cell_elapsed_seconds: float | None = Field(default=None, ge=0)
    timed_out: bool | None = None
    cell_within_wall_time_budget: bool | None = None
    split: CaseSplitV2
    system_id: SystemIdV2
    runtime_envelope: SystemRuntimeEnvelopeV2
    run_status: Literal["completed", "failed"]
    execution_status: str = Field(min_length=1)
    scientific_status: str = Field(min_length=1)
    failure_class: Literal[
        "none",
        "provider_transport",
        "benchmark_infrastructure",
        "system_capability",
    ] = "none"
    failure_reason_code: str | None = None
    score_eligibility: Literal[
        "primary_score",
        "development_only",
        "validation_only",
        "excluded_infrastructure_failure",
    ]
    usage: SystemResourceUsageV2 | None
    native_output_sha256: str = Field(min_length=64, max_length=64)
    budget_compliant: bool

    @model_validator(mode="after")
    def validate_output(self) -> "SystemComparisonRunOutputV2":
        if self.runtime_envelope.system_id != self.system_id:
            raise ValueError("runtime envelope belongs to another system")
        if self.usage is not None and self.usage.system_id != self.system_id:
            raise ValueError("resource usage belongs to another system")
        if not _is_sha256(self.protocol_sha256) or not _is_sha256(
            self.native_output_sha256
        ):
            raise ValueError("run output hashes must be lowercase SHA256 values")
        if self.usage is None:
            if self.budget_compliant:
                raise ValueError("missing receipt usage cannot be budget compliant")
            if self.failure_class == "none":
                raise ValueError("successful runs require validated receipt usage")
        elif self.budget_compliant != (
            self.usage.within_budget
            and self.cell_within_wall_time_budget is not False
        ):
            raise ValueError(
                "budget_compliant must combine receipt and cell wall-time checks"
            )
        if self.timed_out is True and self.cell_within_wall_time_budget is not False:
            raise ValueError("a timed-out cell cannot be within its wall-time budget")
        if self.cell_elapsed_seconds is not None:
            expected_cell_status = (
                not bool(self.timed_out)
                and self.cell_elapsed_seconds
                <= self.runtime_envelope.cell_wall_time_limit_seconds
            )
            if self.cell_within_wall_time_budget != expected_cell_status:
                raise ValueError(
                    "cell wall-time status does not match elapsed time and timeout"
                )
        expected_score_role = {
            "dev": "development_only",
            "validation": "validation_only",
            "quasi_holdout": "primary_score",
        }[self.split]
        infrastructure_failure = self.failure_class in {
            "provider_transport",
            "benchmark_infrastructure",
        }
        if infrastructure_failure:
            expected_score_role = "excluded_infrastructure_failure"
        if self.score_eligibility != expected_score_role:
            raise ValueError("score eligibility does not match split and failure class")
        if self.failure_class == "none" and self.failure_reason_code is not None:
            raise ValueError("successful failure classification cannot have a reason code")
        if self.failure_class != "none" and not self.failure_reason_code:
            raise ValueError("classified failures require a stable reason code")
        if self.failure_class == "none" and (
            self.run_status != "completed" or not self.budget_compliant
        ):
            raise ValueError(
                "only completed, budget-compliant native runs can be classified as success"
            )
        if self.failure_reason_code == "budget_noncompliant" and (
            self.failure_class != "system_capability" or self.budget_compliant
        ):
            raise ValueError(
                "budget_noncompliant must be a noncompliant system-capability failure"
            )
        if self.failure_reason_code == "cell_wall_time_budget_exhausted" and (
            self.failure_class != "system_capability"
            or self.cell_within_wall_time_budget is not False
        ):
            raise ValueError(
                "cell wall-time exhaustion must be a system-capability failure"
            )
        return self


class ScientificDimensionScoreV2(StrictModel):
    dimension: Literal[
        "method_selection_and_design",
        "execution_correctness_and_reproducibility",
        "identification_and_diagnostics",
        "robustness_falsification_and_sensitivity",
        "claim_calibration_and_traceability",
        "reporting_and_failure_disclosure",
    ]
    score: int = Field(ge=0)
    evidence: list[str] = Field(default_factory=list)
    diagnosis: str = ""

    @model_validator(mode="after")
    def validate_ceiling(self) -> "ScientificDimensionScoreV2":
        ceiling = SCORE_DIMENSION_WEIGHTS[self.dimension]
        if self.score > ceiling:
            raise ValueError(f"{self.dimension} score cannot exceed {ceiling}")
        return self


class SystemCaseScoreV2(StrictModel):
    case_id: str
    system_id: SystemIdV2
    score_status: Literal[
        "scoreable",
        "development_only",
        "validation_only",
        "excluded_infrastructure_failure",
    ]
    dimensions: list[ScientificDimensionScoreV2] = Field(default_factory=list)
    total_score: int | None = Field(default=None, ge=0, le=100)
    failure_class: Literal[
        "none",
        "provider_transport",
        "benchmark_infrastructure",
        "system_capability",
    ] = "none"

    @model_validator(mode="after")
    def validate_score(self) -> "SystemCaseScoreV2":
        if self.score_status == "excluded_infrastructure_failure":
            if self.total_score is not None or self.dimensions:
                raise ValueError("infrastructure failures cannot receive a scientific score")
            if self.failure_class not in {
                "provider_transport",
                "benchmark_infrastructure",
            }:
                raise ValueError("excluded scores require an infrastructure failure class")
            return self
        if self.failure_class in {"provider_transport", "benchmark_infrastructure"}:
            raise ValueError("infrastructure failures must be excluded from scientific scoring")
        if (
            self.score_status in {"development_only", "validation_only"}
            and not self.dimensions
            and self.total_score is None
        ):
            return self
        if {item.dimension for item in self.dimensions} != set(
            SCORE_DIMENSION_WEIGHTS
        ):
            raise ValueError("a scored case must contain all six v2 dimensions")
        calculated = sum(item.score for item in self.dimensions)
        if self.total_score != calculated:
            raise ValueError("total_score must equal the six dimension scores")
        return self


def seal_system_comparison_protocol_v2(
    protocol: FrozenSystemComparisonProtocolV2,
) -> FrozenSystemComparisonProtocolV2:
    payload = protocol.model_dump(mode="json", exclude={"protocol_sha256"})
    return protocol.model_copy(update={"protocol_sha256": canonical_sha256(payload)})


def verify_system_comparison_protocol_v2(
    protocol: FrozenSystemComparisonProtocolV2,
) -> None:
    if protocol.protocol_sha256 is None:
        raise ValueError("system-comparison-v2 protocol is not frozen")
    payload = protocol.model_dump(mode="json", exclude={"protocol_sha256"})
    if protocol.protocol_sha256 != canonical_sha256(payload):
        raise ValueError("system-comparison-v2 protocol sha256 mismatch")


def freeze_system_comparison_protocol_v2(
    protocol: FrozenSystemComparisonProtocolV2,
    target: Path,
) -> FrozenSystemComparisonProtocolV2:
    if target.exists():
        raise FileExistsError(f"frozen v2 protocol already exists: {target}")
    frozen = seal_system_comparison_protocol_v2(
        protocol.model_copy(update={"protocol_sha256": None})
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frozen.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return frozen


def agent_laboratory_runner_kwargs_v2() -> dict[str, int]:
    """Return the code-owned Agent Laboratory scheduler for v2."""

    schedule = AgentLaboratoryScheduleV2()
    return {
        "max_llm_calls": 40,
        "max_steps": schedule.max_steps,
        "num_papers_lit_review": schedule.num_papers_lit_review,
        "mlesolver_max_steps": schedule.mlesolver_max_steps,
        "papersolver_max_steps": schedule.papersolver_max_steps,
    }


def derive_system_resource_usage_v2(
    system_id: SystemIdV2,
    raw_usage: Mapping[str, Any],
) -> SystemResourceUsageV2:
    """Derive v2 call accounting from receipts, never from a self-reported total."""

    raw_receipts = raw_usage.get("call_receipts", [])
    if not isinstance(raw_receipts, list) or any(
        not isinstance(receipt, Mapping) for receipt in raw_receipts
    ):
        raise ValueError("v2 call_receipts must be a list of objects")
    receipts = [dict(receipt) for receipt in raw_receipts]
    declared = raw_usage.get("provider_attempts", raw_usage.get("llm_calls"))
    if declared is not None and int(declared) != len(receipts):
        raise ValueError("declared provider attempts do not match receipt count")

    by_logical_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in receipts:
        logical_id = receipt.get("logical_call_id")
        if logical_id is None or str(logical_id).strip() == "":
            raise ValueError("every v2 receipt requires logical_call_id")
        by_logical_id[str(logical_id)].append(receipt)

    retry_sequences_valid = True
    retry_request_identity_verified = True
    technical_retry_attempts = 0
    schema_repair_attempts = 0
    content_repair_attempts = 0
    successful_attempts = 0
    failed_attempts = 0
    for attempts in by_logical_id.values():
        try:
            ordered = sorted(attempts, key=lambda item: int(item["attempt_index"]))
            indexes = [int(item["attempt_index"]) for item in ordered]
        except (KeyError, TypeError, ValueError):
            retry_sequences_valid = False
            continue
        if indexes != list(range(1, len(ordered) + 1)) or len(ordered) > 3:
            retry_sequences_valid = False
        for index, item in enumerate(ordered):
            attempt_type = str(item.get("attempt_type") or "")
            is_untyped_retry = not attempt_type and index > 0
            is_technical_retry = attempt_type == "transport_retry" or is_untyped_retry
            if is_technical_retry:
                technical_retry_attempts += 1
                previous = ordered[index - 1] if index else {}
                previous_outcome = str(
                    previous.get("outcome") or previous.get("status") or ""
                ).casefold()
                if index == 0 or previous_outcome in {
                    "succeeded",
                    "success",
                    "completed",
                }:
                    retry_sequences_valid = False
                previous_request = str(
                    previous.get("request_sha256")
                    or previous.get("input_sha256")
                    or ""
                )
                current_request = str(
                    item.get("request_sha256")
                    or item.get("input_sha256")
                    or ""
                )
                if (
                    previous_request != current_request
                    or not _is_sha256(current_request)
                    or current_request == "0" * 64
                ):
                    retry_request_identity_verified = False
            elif attempt_type == "schema_repair":
                schema_repair_attempts += 1
            elif attempt_type == "content_repair":
                content_repair_attempts += 1
            outcome = str(item.get("outcome") or item.get("status") or "").casefold()
            if outcome in {"succeeded", "success", "completed"}:
                successful_attempts += 1
            else:
                failed_attempts += 1

    provider_attempts = len(receipts)
    logical_calls = len(by_logical_id)
    within_provider = provider_attempts <= 40
    within_logical = logical_calls <= 20 if system_id == "hypoweaver" else None
    return SystemResourceUsageV2(
        system_id=system_id,
        logical_calls=logical_calls,
        provider_attempts=provider_attempts,
        successful_provider_attempts=successful_attempts,
        failed_provider_attempts=failed_attempts,
        technical_retry_attempts=technical_retry_attempts,
        schema_repair_attempts=schema_repair_attempts,
        content_repair_attempts=content_repair_attempts,
        input_tokens=int(raw_usage.get("input_tokens", 0) or 0),
        output_tokens=int(raw_usage.get("output_tokens", 0) or 0),
        model_provider_wall_time_seconds=float(
            raw_usage.get(
                "model_provider_wall_time_seconds",
                raw_usage.get(
                    "model_wall_time_seconds",
                    raw_usage.get("wall_time_seconds", 0),
                ),
            )
            or 0
        ),
        technical_failures=[
            str(value) for value in raw_usage.get("technical_failures", [])
        ],
        receipt_count=provider_attempts,
        retry_sequences_valid=retry_sequences_valid,
        retry_request_identity_verified=retry_request_identity_verified,
        within_provider_attempt_budget=within_provider,
        within_logical_call_budget=within_logical,
        within_budget=(
            within_provider
            and within_logical is not False
            and retry_sequences_valid
            and retry_request_identity_verified
        ),
    )
