from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


RunStatus = Literal[
    "created",
    "running",
    "waiting_human",
    "blocked",
    "failed",
    "stopped",
    "completed",
]
StepStatus = Literal[
    "pending",
    "running",
    "waiting_human",
    "succeeded",
    "failed",
    "blocked",
    "skipped",
]
ExecutionStatus = Literal[
    "planned",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "not_executed",
    "fixture_only",
]
ScientificStatus = Literal[
    "not_evaluated",
    "pending_review",
    "valid",
    "limited",
    "invalid",
]
MethodFamily = Literal[
    "policy_causal",
    "panel_association",
    "mechanism_boundary",
    "market_event",
    "spatial",
    "measurement_efficiency",
    "structural_macro",
]
ClaimStrength = Literal[
    "causal_strong",
    "causal_cautious",
    "associational",
    "preliminary",
    "mixed",
    "insufficient",
    "prohibited",
]
ClaimType = Literal[
    "causal",
    "associational",
    "descriptive",
    "mechanism",
    "heterogeneity",
    "unspecified",
]
AdmissionStatus = Literal[
    "unassessed",
    "admitted",
    "downgrade_required",
    "prohibited",
    "rejected",
]
TestRole = Literal[
    "diagnostic",
    "robustness",
    "falsification",
    "replication",
    "exploratory",
]
ModelCallGroup = Literal["h1_h2", "h3", "h4"]
ModelCallOutcome = Literal[
    "succeeded",
    "schema_failure",
    "transport_failure",
    "provider_failure",
]
ModelCallAttemptType = Literal[
    "primary",
    "transport_retry",
    "schema_repair",
    "content_repair",
]
ModelCallErrorCategory = Literal[
    "dns",
    "tls",
    "connect_timeout",
    "read_timeout",
    "proxy",
    "connection_reset",
    "cancelled",
    "http_status",
    "schema",
    "unknown_transport",
    "unknown_provider",
]


class ModelCallContext(StrictModel):
    """Code-owned identity and retry ceiling for one logical model call."""

    logical_call_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    call_group: ModelCallGroup = "h1_h2"
    prompt_key: str = Field(default="legacy", min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=3)
    attempt_type: ModelCallAttemptType = "primary"


class SchemaValidationIssue(StrictModel):
    """Redacted location and stable error type for one Schema failure."""

    loc: tuple[str, ...] = Field(default_factory=tuple, max_length=12)
    type: str = Field(
        default="invalid",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_safe_location(self) -> "SchemaValidationIssue":
        allowed = frozenset(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789_-"
        )
        if any(
            not item
            or len(item) > 64
            or any(character not in allowed for character in item)
            for item in self.loc
        ):
            raise ValueError("schema error locations must be redacted identifiers")
        return self


class ModelCallReceipt(StrictModel):
    """Redacted provenance for one actual provider attempt."""

    receipt_version: Literal[1] = 1
    call_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    logical_call_id: str = Field(
        default_factory=lambda: f"legacy-{uuid4()}",
        min_length=1,
    )
    call_group: ModelCallGroup = "h1_h2"
    prompt_key: str = Field(default="legacy", min_length=1)
    prompt_version: str = Field(default="legacy", min_length=1)
    attempt_index: int = Field(default=1, ge=1, le=3)
    max_attempts: int = Field(default=3, ge=1, le=3)
    attempt_type: Literal[
        "primary",
        "transport_retry",
        "schema_repair",
        "content_repair",
        "legacy",
    ] = "legacy"
    outcome: ModelCallOutcome = "succeeded"
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    started_at: str
    completed_at: str
    response_sha256: str = Field(min_length=64, max_length=64)
    input_sha256: str = Field(default="0" * 64, min_length=64, max_length=64)
    output_schema_sha256: str = Field(
        default="0" * 64,
        min_length=64,
        max_length=64,
    )
    provider_response_id_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    error_type: str | None = None
    error_category: ModelCallErrorCategory | None = None
    schema_error_summary: list[SchemaValidationIssue] = Field(
        default_factory=list,
        max_length=20,
        exclude_if=lambda value: not value,
    )
    schema_error_count: int = Field(
        default=0,
        ge=0,
        exclude_if=lambda value: value == 0,
    )

    @model_validator(mode="after")
    def validate_receipt(self) -> "ModelCallReceipt":
        if self.attempt_index > self.max_attempts:
            raise ValueError("attempt_index cannot exceed max_attempts")
        for name, value in (
            ("response_sha256", self.response_sha256),
            ("input_sha256", self.input_sha256),
            ("output_schema_sha256", self.output_schema_sha256),
            ("provider_response_id_sha256", self.provider_response_id_sha256),
        ):
            if value is not None and any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{name} must be a lowercase hexadecimal value")
        try:
            started = datetime.fromisoformat(self.started_at)
            completed = datetime.fromisoformat(self.completed_at)
        except ValueError as error:
            raise ValueError("model call receipt timestamps must be ISO-8601") from error
        if (
            started.tzinfo is None
            or started.utcoffset() is None
            or completed.tzinfo is None
            or completed.utcoffset() is None
        ):
            raise ValueError("model call receipt timestamps must include a timezone")
        if completed < started:
            raise ValueError("completed_at cannot precede started_at")
        if self.outcome == "succeeded" and self.error_type is not None:
            raise ValueError("a succeeded model call cannot include error_type")
        if self.outcome == "succeeded" and self.error_category is not None:
            raise ValueError("a succeeded model call cannot include error_category")
        if self.outcome != "succeeded" and not self.error_type:
            raise ValueError("a failed model call must include error_type")
        if self.schema_error_count < len(self.schema_error_summary):
            raise ValueError(
                "schema_error_count cannot be smaller than the persisted summary"
            )
        if self.outcome != "schema_failure" and (
            self.schema_error_summary or self.schema_error_count
        ):
            raise ValueError(
                "only a schema_failure receipt can include a schema error summary"
            )
        return self


class Hypothesis(StrictModel):
    hypothesis_id: str
    statement: str
    expected_direction: Literal[
        "positive", "negative", "nonlinear", "heterogeneous", "unspecified"
    ] = "unspecified"
    mechanism: str | None = None


class VariableSpec(StrictModel):
    name: str
    label: str | None = None
    role: Literal[
        "outcome",
        "treatment",
        "exposure",
        "mediator",
        "moderator",
        "control",
        "id",
        "time",
        "spatial_id",
        "event_date",
        "fixed_effect",
        "cluster",
        "unknown",
    ] = "unknown"
    definition: str | None = None
    source: str | None = None


class DatasetRef(StrictModel):
    dataset_id: str
    role: Literal["main", "supplementary"] = "main"
    filename: str
    mime_type: str = "text/csv"
    sha256: str
    size_bytes: int = Field(ge=0)


class DesignEnvelope(StrictModel):
    benchmark_track: Literal["strict_blind", "reproduction_aligned"] = "strict_blind"
    research_goal: Literal[
        "causal",
        "associational",
        "mechanism",
        "prediction",
        "measurement",
        "structural",
        "mixed",
    ] = "mixed"
    target_estimands: list[str] = Field(default_factory=list)
    design_constraints: list[str] = Field(default_factory=list)
    required_diagnostics: list[str] = Field(default_factory=list)
    allowed_claim_strength: Literal[
        "causal", "associational", "descriptive", "not_prespecified"
    ] = "not_prespecified"


class PolicyDesignSpec(StrictModel):
    """Code-owned policy timing and optional reproduction-aligned specification.

    ``policy_date``, ``group_field`` and ``time_field`` are observable case facts.
    The remaining fields are optional so a strict discovery view does not have to
    reveal the reference study's estimator choices.  Before execution the policy
    plan normalizer must resolve them into a complete frozen ModelSpec contract.
    """

    policy_date: str = Field(pattern=r"^\d{4}-\d{2}$")
    group_field: str = Field(min_length=1)
    time_field: str = Field(min_length=1)
    policy_start_weight: float | None = Field(default=None, ge=0, le=1)
    post_start_weight: float = Field(default=1.0, ge=0, le=1)
    exposure_name: str = Field(default="policy_exposure", min_length=1)
    fixed_effects: list[str] = Field(default_factory=list)
    cluster_fields: list[str] = Field(default_factory=list)
    cluster_composition: Literal["interaction"] = "interaction"
    event_reference_year: int | None = None
    event_years: list[int] = Field(default_factory=list)
    event_remote_pre_years: list[int] = Field(default_factory=list)
    event_term_scaling: Literal["binary_group_year_contrast"] = (
        "binary_group_year_contrast"
    )
    placebo_start_year: int | None = None
    placebo_repetitions: int | None = Field(default=None, ge=1, le=500)
    permutation_scheme: Literal[
        "assignment_unit_label",
        "rowwise_exposure",
    ] = "assignment_unit_label"
    permutation_unit_field: str | None = None
    random_seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_policy_design(self) -> "PolicyDesignSpec":
        policy_year, policy_month = (int(value) for value in self.policy_date.split("-"))
        if not 1 <= policy_month <= 12:
            raise ValueError("policy_date month must be between 01 and 12")
        if len(self.fixed_effects) != len(set(self.fixed_effects)):
            raise ValueError("policy fixed_effects must be unique")
        if len(self.cluster_fields) != len(set(self.cluster_fields)):
            raise ValueError("policy cluster_fields must be unique")
        if len(self.event_years) != len(set(self.event_years)):
            raise ValueError("policy event_years must be unique")
        if self.event_years != sorted(self.event_years):
            raise ValueError("policy event_years must be sorted")
        if len(self.event_remote_pre_years) != len(
            set(self.event_remote_pre_years)
        ):
            raise ValueError("policy event_remote_pre_years must be unique")
        if self.event_remote_pre_years != sorted(self.event_remote_pre_years):
            raise ValueError("policy event_remote_pre_years must be sorted")
        if self.event_reference_year in set(self.event_years):
            raise ValueError("event_reference_year cannot also be an estimated event year")
        if self.event_reference_year in set(self.event_remote_pre_years):
            raise ValueError("event_reference_year cannot be inside the remote-pre bin")
        if self.event_reference_year is not None and self.event_reference_year >= policy_year:
            raise ValueError("event_reference_year must precede the policy year")
        if any(year >= policy_year for year in self.event_remote_pre_years):
            raise ValueError("event_remote_pre_years must all precede the policy year")
        if (
            self.event_remote_pre_years
            and self.event_years
            and max(self.event_remote_pre_years) >= min(self.event_years)
        ):
            raise ValueError(
                "event_remote_pre_years must precede every explicit event year"
            )
        if (self.placebo_repetitions is None) != (self.random_seed is None):
            raise ValueError(
                "placebo_repetitions and random_seed must be supplied together"
            )
        return self


class CaseSubmission(StrictModel):
    case_id: str
    title: str
    research_question: str
    hypotheses: list[Hypothesis]
    unit_of_analysis: str | None = None
    sample_period: str | None = None
    data_structure_hint: Literal[
        "cross_section", "panel", "time_series", "spatial_panel", "event", "unknown"
    ] = "unknown"
    variables: list[VariableSpec]
    dataset_refs: list[DatasetRef] = Field(default_factory=list)
    design_envelope: DesignEnvelope | None = None
    policy_design: PolicyDesignSpec | None = None
    known_policy_facts: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_minimum_research_content(self) -> "CaseSubmission":
        if not self.hypotheses:
            raise ValueError("at least one hypothesis is required")
        if not any(variable.role == "outcome" for variable in self.variables):
            raise ValueError("an outcome variable is required")
        return self


class ResearchPackage(CaseSubmission):
    input_conflicts: list[str] = Field(default_factory=list)
    missing_required_information: list[str] = Field(default_factory=list)


class TestableHypothesis(StrictModel):
    hypothesis_id: str
    theoretical_claim: str
    observable_prediction: str
    analysis_unit: str | None
    outcome_variables: list[str]
    treatment_or_exposure_variables: list[str]
    mechanism_variables: list[str]
    boundary_conditions: list[str]
    competing_explanations: list[str]
    falsification_conditions: list[str]


class TestableHypotheses(StrictModel):
    items: list[TestableHypothesis]


class MissingnessRecord(StrictModel):
    variable: str
    missing_count: int | None = None
    missing_rate: float | None = Field(default=None, ge=0, le=1)


class DataProfile(StrictModel):
    profile_execution_status: Literal[
        "succeeded", "partially_succeeded", "not_executed", "failed"
    ]
    data_structure: Literal[
        "cross_section", "panel", "time_series", "spatial_panel", "event", "mixed", "unknown"
    ]
    unit_of_observation: str | None
    entity_key: list[str] = Field(default_factory=list)
    time_key: str | None = None
    spatial_key: str | None = None
    event_date_key: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    column_count: int | None = Field(default=None, ge=0)
    duplicate_key_count: int | None = Field(default=None, ge=0)
    missingness: list[MissingnessRecord] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    measurement_risks: list[str] = Field(default_factory=list)
    merge_risks: list[str] = Field(default_factory=list)
    supported_method_families: list[MethodFamily] = Field(default_factory=list)
    unsupported_method_families: list[MethodFamily] = Field(default_factory=list)
    readiness: Literal["ready", "partially_ready", "blocked"]
    blocking_reasons: list[str] = Field(default_factory=list)


class RejectedRoute(StrictModel):
    route: MethodFamily
    reason: str


class MethodRoute(StrictModel):
    route_status: Literal["routed", "blocked", "needs_human_review"]
    research_goal: Literal[
        "causal", "associational", "mechanism", "prediction", "measurement", "structural", "mixed"
    ]
    primary_route: MethodFamily | None
    route_reason: list[str]
    required_assumptions: list[str]
    testable_assumptions: list[str]
    untestable_assumptions: list[str]
    alternative_routes: list[MethodFamily]
    rejected_routes: list[RejectedRoute]
    missing_information: list[str]

    @model_validator(mode="after")
    def validate_route_result(self) -> "MethodRoute":
        if self.route_status == "routed" and self.primary_route is None:
            raise ValueError("a routed result requires primary_route")
        if self.route_status != "routed" and self.primary_route is not None:
            raise ValueError("a blocked route cannot silently select a method family")
        return self


class PlannedStep(StrictModel):
    step_id: str
    name: str
    priority: Literal["required", "recommended", "optional"] = "required"
    execution_status: Literal["planned"] = "planned"
    rationale: str
    required_data_fields: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    threat_id: str | None = None
    target_claim_ids: list[str] = Field(default_factory=list)
    test_role: TestRole | None = None
    required_for_admission: bool = False
    source_issue_ids: list[str] = Field(default_factory=list)
    not_executable_reason: str | None = None


class ModelSpec(PlannedStep):
    estimator: str
    formula: str | None = None
    outcome: str | None = None
    treatments_or_exposures: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    fixed_effects: list[str] = Field(default_factory=list)
    standard_error_strategy: str | None = None


class DeviationLog(StrictModel):
    issue_id: str
    change: str
    reason: str


class AnalysisPlan(StrictModel):
    plan_id: str
    plan_version: int = Field(ge=1)
    method_family: MethodFamily
    base_method_family: MethodFamily | None = None
    design_only: bool
    estimands: list[PlannedStep]
    sample_rules: list[PlannedStep]
    variable_construction: list[PlannedStep]
    baseline_models: list[ModelSpec]
    diagnostics: list[PlannedStep]
    robustness_tests: list[PlannedStep]
    falsification_tests: list[PlannedStep]
    mechanism_tests: list[PlannedStep]
    heterogeneity_tests: list[PlannedStep]
    identification_assumptions: list[str]
    alternative_explanations: list[str]
    failure_conditions: list[str]
    stop_conditions: list[str]
    required_data_fields: list[str]
    unsupported_requested_analyses: list[str]
    revision_round: int = Field(default=0, ge=0, le=2)
    deviation_log: list[DeviationLog] = Field(default_factory=list)
    check_registry_version: str | None = None


class CandidatePlanDraft(StrictModel):
    strategy: Literal[
        "direct_baseline", "identification_first", "measurement_robustness"
    ]
    plan: AnalysisPlan


class CandidatePlanBatch(StrictModel):
    plans: list[CandidatePlanDraft] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_unique_strategies(self) -> "CandidatePlanBatch":
        strategies = [item.strategy for item in self.plans]
        if len(strategies) != len(set(strategies)):
            raise ValueError("candidate plan batch contains duplicate strategies")
        return self


class CriticIssue(StrictModel):
    issue_id: str
    dimension: Literal["measurement", "causal", "statistical", "reproducibility"]
    severity: Literal["critical", "major", "minor"]
    evidence: str
    why_it_matters: str
    required_fix: str
    return_stage: Literal["intake", "data_profile", "method_route", "analysis_plan", "human"]
    repair_type: Literal["technical", "scientific", "human_required"]
    status: Literal["open", "resolved", "accepted_risk"] = "open"
    threat_id: str | None = None


class CriticReport(StrictModel):
    report_id: str
    review_round: int = Field(ge=1, le=2)
    verdict: Literal["pass", "revise", "blocked"]
    issues: list[CriticIssue]
    approved_elements: list[str]
    remaining_risks: list[str]


class ProbeCheck(StrictModel):
    check_id: str
    status: Literal["pass", "warn", "fail"]
    evidence: str
    required_follow_up: str | None = None


class ProbeReport(StrictModel):
    report_id: str
    candidate_id: str
    verdict: Literal["pass", "warn", "fail"]
    checks: list[ProbeCheck]
    executor_ready: bool
    used_outcome_results: Literal[False] = False


class DesignCandidate(StrictModel):
    candidate_id: str
    strategy: Literal[
        "direct_baseline", "identification_first", "measurement_robustness"
    ]
    rationale: str
    plan: AnalysisPlan
    probe_report: ProbeReport


class CandidateDesignSet(StrictModel):
    candidate_set_id: str
    candidates: list[DesignCandidate] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def validate_candidates(self) -> "CandidateDesignSet":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate design ids must be unique")
        return self


class CandidateReview(StrictModel):
    candidate_id: str
    verdict: Literal["pass", "revise", "reject"]
    strengths: list[str] = Field(default_factory=list)
    issues: list[CriticIssue] = Field(default_factory=list)
    required_follow_ups: list[str] = Field(default_factory=list)


class DesignReviewerReport(StrictModel):
    report_id: str
    dimension: Literal["measurement", "causal", "statistical", "reproducibility"]
    reviewer_policy: str
    candidate_reviews: list[CandidateReview]
    remaining_risks: list[str] = Field(default_factory=list)


class ReviewerReportBatch(StrictModel):
    reports: list[DesignReviewerReport] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_independent_dimensions(self) -> "ReviewerReportBatch":
        dimensions = [item.dimension for item in self.reports]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("reviewer batch contains duplicate dimensions")
        candidate_sets = [
            {review.candidate_id for review in report.candidate_reviews}
            for report in self.reports
        ]
        if not candidate_sets[0] or any(
            candidate_ids != candidate_sets[0] for candidate_ids in candidate_sets[1:]
        ):
            raise ValueError("every reviewer must cover the same non-empty candidate set")
        return self


class DesignArena(StrictModel):
    arena_id: str
    candidates: list[DesignCandidate]
    reviewer_reports: list[DesignReviewerReport]
    recommended_candidate_ids: list[str]
    provisional_candidate_id: str | None = None
    selection_rationale: list[str]

    @model_validator(mode="after")
    def validate_candidate_references(self) -> "DesignArena":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("design candidate ids must be unique")
        known = set(candidate_ids)
        if not set(self.recommended_candidate_ids).issubset(known):
            raise ValueError("recommended candidate id is not in the arena")
        if self.recommended_candidate_ids and self.provisional_candidate_id not in self.recommended_candidate_ids:
            raise ValueError("provisional candidate must be recommended")
        if not self.recommended_candidate_ids and self.provisional_candidate_id is not None:
            raise ValueError("blocked design arena cannot have a provisional candidate")
        for report in self.reviewer_reports:
            reviewed = {item.candidate_id for item in report.candidate_reviews}
            if reviewed != known:
                raise ValueError("every reviewer must assess every design candidate")
        return self


class ContractBudget(StrictModel):
    max_executions: int = Field(
        default=12,
        ge=1,
        description=(
            "Frozen DAG step slots per statistical implementation. The primary "
            "executor and independent reproducer report their work separately."
        ),
    )
    max_wall_time_seconds: int = Field(
        default=1800,
        ge=60,
        description="Wall-time ceiling for each statistical implementation phase.",
    )
    max_end_to_end_wall_time_seconds: int = Field(
        default=2700,
        ge=60,
        description=(
            "End-to-end system-cell ceiling enforced by the benchmark orchestrator."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_llm_call_budget(cls, value: Any) -> Any:
        """Read old contracts without restoring an unenforced budget source."""

        if isinstance(value, dict) and "max_llm_calls" in value:
            value = dict(value)
            value.pop("max_llm_calls")
        return value


class FormalResearchContract(StrictModel):
    contract_id: str
    case_id: str
    status: Literal["frozen", "superseded"] = "frozen"
    approved_at: str
    approved_by: str
    decision_record_id: str
    research_package_hash: str
    data_hashes: list[str]
    dataset_refs: list[DatasetRef] = Field(default_factory=list)
    approved_plan_hash: str
    approved_plan: AnalysisPlan
    prohibited_deviations: list[str]
    allowed_technical_repairs: list[str]
    unresolved_risks: list[str]
    budget: ContractBudget = Field(default_factory=ContractBudget)


class ExecutionProvenance(StrictModel):
    implementation_id: str
    implementation_version: str
    code_sha256: str
    environment_sha256: str
    contract_sha256: str
    data_sha256: list[str] = Field(default_factory=list)


class ExecutionRecord(StrictModel):
    execution_id: str
    run_type: Literal[
        "baseline", "diagnostic", "robustness", "falsification", "mechanism", "heterogeneity", "replication"
    ]
    plan_step_id: str
    execution_status: ExecutionStatus
    estimates: list[dict[str, Any]] = Field(default_factory=list)
    diagnostic_results: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    check_id: str | None = None
    not_executed_reason_code: Literal[
        "budget_exhausted",
        "not_executable",
        "dependency_failed",
        "external_replication_pending",
        "fixture_only",
    ] | None = None
    provenance: ExecutionProvenance | None = None


class ResearchRun(StrictModel):
    research_run_id: str
    case_id: str
    contract_hash: str
    plan_version: int
    execution_status: ExecutionStatus
    scientific_status: ScientificStatus
    fixture_only: bool
    not_executed_reason: str | None = None
    executions: list[ExecutionRecord] = Field(default_factory=list)
    deviations: list[dict[str, Any]] = Field(default_factory=list)
    failed_runs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fixture_boundary(self) -> "ResearchRun":
        if not self.fixture_only:
            return self
        if self.scientific_status in ("valid", "limited"):
            raise ValueError("fixture execution cannot receive a valid scientific status")
        for execution in self.executions:
            if execution.estimates or execution.diagnostic_results:
                raise ValueError("fixture execution cannot contain empirical estimates or diagnostics")
        return self


class ReproductionAudit(StrictModel):
    audit_id: str
    primary_run_id: str
    replication_run_id: str | None = None
    status: Literal["matched", "diverged", "not_applicable", "failed"]
    compared_fields: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    numeric_tolerance: float = Field(default=1e-8, gt=0)
    relative_tolerance: float = Field(default=1e-6, gt=0)
    mode: Literal[
        "independent_implementation", "same_implementation_rerun"
    ] = "same_implementation_rerun"
    independence_scope: Literal[
        "unspecified",
        "estimator_only",
        "data_preparation_and_estimator",
        "end_to_end",
    ] = "unspecified"
    shared_components: list[str] = Field(default_factory=list)
    covered_plan_step_ids: list[str] = Field(default_factory=list)
    primary_implementation_id: str | None = None
    replication_implementation_id: str | None = None
    metric_differences: list[dict[str, Any]] = Field(default_factory=list)


class EvidenceObject(StrictModel):
    evidence_id: str
    claim_id: str
    check_id: str
    execution_id: str | None = None
    source_kind: Literal[
        "execution", "reproduction", "scientific_audit", "contract"
    ]
    status: Literal["supporting", "opposing", "incomplete", "invalid"]
    reason: str


class EvidenceRegistry(StrictModel):
    registry_version: str = "enterprise-panel-v1"
    case_id: str
    research_run_id: str
    required_check_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceObject] = Field(default_factory=list)


class ClaimGateResult(StrictModel):
    claim_id: str
    admission_status: AdmissionStatus
    max_allowed_strength: ClaimStrength
    reasons: list[str] = Field(default_factory=list)


class ClaimGateReport(StrictModel):
    gate_id: str
    case_id: str
    research_run_id: str
    registry_version: str
    results: list[ClaimGateResult] = Field(default_factory=list)


class EvidenceAssessment(StrictModel):
    evidence_status: Literal["supported", "contradicted", "mixed", "inconclusive", "not_tested"]
    execution_status: ExecutionStatus
    scientific_status: ScientificStatus
    supporting_run_ids: list[str]
    opposing_run_ids: list[str]
    limitations: list[str]


class ScientificAudit(StrictModel):
    verdict: Literal["valid", "limited", "invalid", "not_evaluated"]
    contract_compliant: bool
    critical_issues: list[str]
    unresolved_risks: list[str]


class ClaimRecord(StrictModel):
    claim_id: str
    hypothesis_id: str | None
    claim_text: str
    final_text: str | None = None
    evidence_status: Literal["supported", "contradicted", "mixed", "inconclusive", "not_tested"]
    allowed_strength: ClaimStrength
    supporting_runs: list[str]
    opposing_runs: list[str]
    scope: str
    robustness_status: str
    unresolved_risks: list[str]
    approval_status: Literal[
        "pending", "approved", "downgraded", "revise", "hold", "rejected"
    ] = "pending"
    human_decision_reason: str | None = None
    claim_type: ClaimType = "unspecified"
    required_check_ids: list[str] = Field(default_factory=list)
    admission_status: AdmissionStatus = "unassessed"
    max_allowed_strength: ClaimStrength | None = None
    gate_reasons: list[str] = Field(default_factory=list)


class ClaimLedger(StrictModel):
    ledger_id: str
    case_id: str
    research_run_id: str
    claims: list[ClaimRecord]
    excluded_findings: list[str]
    unresolved_issues: list[str]


class EvidenceClaimBundle(StrictModel):
    evidence_assessment: EvidenceAssessment
    candidate_claim_ledger: ClaimLedger

    @model_validator(mode="after")
    def validate_unique_claims(self) -> "EvidenceClaimBundle":
        claim_ids = [
            claim.claim_id for claim in self.candidate_claim_ledger.claims
        ]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("candidate claim ledger contains duplicate claim ids")
        return self


class ProtectedValue(StrictModel):
    value_id: str
    value_kind: Literal[
        "claim_text",
        "count",
        "coefficient",
        "standard_error",
        "interval_bound",
        "p_value",
        "fit_statistic",
        "year",
        "passage_quote",
    ]
    source_kind: Literal["claim", "execution", "passage"]
    source_id: str
    source_path: str
    raw_value: Any
    rendered_value: str


class ManuscriptStatement(StrictModel):
    statement_id: str
    statement_kind: Literal[
        "authorized_claim",
        "estimate_fact",
        "sample_fact",
        "diagnostic_fact",
        "citation",
    ]
    text_template: str
    protected_values: list[ProtectedValue] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    citation_passage_ids: list[str] = Field(default_factory=list)


class ManuscriptSectionDraft(StrictModel):
    section_id: str
    content_template: str


class ManuscriptSectionDraftBatch(StrictModel):
    sections: list[ManuscriptSectionDraft] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_unique_sections(self) -> "ManuscriptSectionDraftBatch":
        section_ids = [section.section_id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("manuscript draft batch contains duplicate section ids")
        return self


class VerifiedPassageRef(StrictModel):
    passage_id: str
    source_id: str
    locator: str
    text_sha256: str
    citation_render: str


class ManuscriptSection(StrictModel):
    section_id: str
    title: str
    content_markdown: str
    status: Literal["generated", "not_generated"]
    claim_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    content_template: str | None = None
    statements: list[ManuscriptStatement] = Field(default_factory=list)


FULL_MANUSCRIPT_SECTION_IDS = (
    "abstract",
    "introduction",
    "theory_hypotheses",
    "data_variables",
    "research_design",
    "empirical_results",
    "discussion_limitations",
    "conclusion",
)
TRACEABLE_MANUSCRIPT_SECTION_IDS = {
    "abstract",
    "empirical_results",
    "discussion_limitations",
    "conclusion",
}
MIN_FULL_MANUSCRIPT_CHARS = 3200


class ManuscriptPackage(StrictModel):
    package_id: str
    case_id: str
    version: int = 1
    mode: Literal[
        "research_plan_only",
        "full_manuscript",
        "identification_failure_report",
    ]
    status: Literal["draft", "needs_revision", "ready_for_human_review", "not_generated"]
    research_plan_markdown: str
    manuscript_sections: list[ManuscriptSection]
    figure_ids: list[str] = Field(default_factory=list)
    empirical_findings_status: Literal[
        "included",
        "not_executed",
        "prohibited_fixture",
        "executed_not_admissible",
    ]
    disclosures: list[str]
    unresolved_issues: list[str]
    audit_scope: Literal["manuscript_claim_consistency_only"] = (
        "manuscript_claim_consistency_only"
    )
    audit_result: Literal["not_run", "pass_with_no_critical_issues", "revise"] = "not_run"
    ir_version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_plan_only_boundary(self) -> "ManuscriptPackage":
        if self.mode == "research_plan_only" and self.empirical_findings_status == "included":
            raise ValueError("research_plan_only cannot include empirical findings")
        if self.mode == "identification_failure_report":
            if self.empirical_findings_status != "executed_not_admissible":
                raise ValueError(
                    "identification_failure_report requires "
                    "empirical_findings_status=executed_not_admissible"
                )
            if self.status == "not_generated":
                raise ValueError(
                    "identification_failure_report cannot have status=not_generated"
                )
            if any(
                section.claim_ids
                for section in self.manuscript_sections
                if section.status == "generated"
            ):
                raise ValueError(
                    "identification_failure_report cannot contain admitted claim ids"
                )
            return self
        if self.mode != "full_manuscript":
            return self
        if self.status == "not_generated":
            raise ValueError("full_manuscript cannot have status=not_generated")
        generated = [
            section for section in self.manuscript_sections
            if section.status == "generated"
        ]
        section_ids = [section.section_id for section in generated]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("full_manuscript section_id values must be unique")
        missing = [
            section_id for section_id in FULL_MANUSCRIPT_SECTION_IDS
            if section_id not in section_ids
        ]
        if missing:
            raise ValueError(
                "full_manuscript is missing required sections: " + ", ".join(missing)
            )
        short_sections = [
            section.section_id for section in generated
            if section.section_id in FULL_MANUSCRIPT_SECTION_IDS
            and len(section.content_markdown.strip()) < 180
        ]
        if short_sections:
            raise ValueError(
                "full_manuscript sections are too short: " + ", ".join(short_sections)
            )
        total_chars = sum(len(section.content_markdown.strip()) for section in generated)
        if total_chars < MIN_FULL_MANUSCRIPT_CHARS:
            raise ValueError(
                f"full_manuscript requires at least {MIN_FULL_MANUSCRIPT_CHARS} content characters; got {total_chars}"
            )
        return self


class PromptContent(StrictModel):
    id: str
    role: Literal["system", "user", "code"]
    template: str
    rendered: str | None = None


class StepAttempt(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    node_id: str
    attempt: int = Field(default=1, ge=1)
    status: StepStatus
    started_at: str | None = None
    ended_at: str | None = None
    prompts: list[PromptContent] = Field(default_factory=list)
    input: Any = None
    output: Any = None
    logs: list[str] = Field(default_factory=list)
    error: str | None = None


class RunEvent(StrictModel):
    seq: int = Field(ge=1)
    type: str
    message: str
    timestamp: str = Field(default_factory=utc_now)
    node_id: str | None = None
    status: StepStatus | None = None


class DecisionRecord(StrictModel):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    gate: Literal["H1", "H2", "H3", "H4"]
    action: str
    actor: str
    comment: str = ""
    selected_candidate_id: str | None = None
    reviewed_hashes: dict[str, str] = Field(default_factory=dict)
    claim_decisions: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class RunState(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    definition_id: str = "app-a"
    definition_version: str = "1.0.0"
    case_id: str
    case_name: str
    mode: Literal["fixture", "research"]
    model_provider: Literal["fixture", "qwen"] = "fixture"
    execution_mode: Literal["fixture", "external"] = "fixture"
    status: RunStatus = "created"
    current_node_id: str | None = None
    current_gate: Literal["H1", "H2", "H3", "H4"] | None = None
    version: int = Field(default=1, ge=1)
    execution_status: str = "not_started"
    scientific_status: str = "not_evaluated"
    plan_only: bool = False
    case_submission: CaseSubmission
    artifacts: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepAttempt] = Field(default_factory=list)
    events: list[RunEvent] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    processed_idempotency_keys: list[str] = Field(default_factory=list)
    last_error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class CreateRunRequest(StrictModel):
    definition_id: Literal["app-a"] = "app-a"
    preset_case_id: str | None = None
    mode: Literal["fixture", "research"] = "fixture"
    case: CaseSubmission | None = None
    model_provider: Literal["fixture", "qwen"] | None = None
    execution_mode: Literal["fixture", "external"] | None = None

    @model_validator(mode="after")
    def require_preset_or_case(self) -> "CreateRunRequest":
        if not self.preset_case_id and not self.case:
            raise ValueError("preset_case_id or case is required")
        if self.preset_case_id and self.case:
            raise ValueError("provide preset_case_id or case, not both")
        if self.mode == "fixture" and self.model_provider not in (None, "fixture"):
            raise ValueError("fixture mode requires model_provider=fixture")
        if self.mode == "fixture" and self.execution_mode not in (None, "fixture"):
            raise ValueError("fixture mode requires execution_mode=fixture")
        if self.mode == "research" and self.model_provider not in (None, "qwen"):
            raise ValueError("research mode requires model_provider=qwen")
        if self.mode == "research" and self.execution_mode not in (None, "external"):
            raise ValueError("research mode requires execution_mode=external")
        return self


class ClaimDecisionInput(StrictModel):
    claim_id: str
    decision: Literal["approve", "downgrade", "reject", "hold"]
    final_text: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def validate_downgrade_wording(self) -> "ClaimDecisionInput":
        if self.decision == "downgrade" and not self.final_text:
            raise ValueError("downgrade requires final_text with calibrated wording")
        return self


class GateDecisionRequest(StrictModel):
    action: Literal[
        "approve",
        "revise",
        "reject",
        "generate_plan_only",
        "generate_identification_failure_report",
    ]
    comment: str = ""
    actor: str = "local_researcher"
    expected_run_version: int | None = None
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))
    reviewed_artifact_hashes: dict[str, str] = Field(default_factory=dict)
    claims: list[ClaimDecisionInput] = Field(default_factory=list)
    selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def unique_claim_decisions(self) -> "GateDecisionRequest":
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim decisions must be unique by claim_id")
        return self


class RevisionRequest(StrictModel):
    gate: Literal["H1", "H2"]
    expected_run_version: int = Field(ge=1)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))
    actor: str = "local_researcher"
    case: CaseSubmission | None = None
    analysis_plan: AnalysisPlan | None = None

    @model_validator(mode="after")
    def validate_revision_payload(self) -> "RevisionRequest":
        if self.gate == "H1" and self.case is None:
            raise ValueError("H1 revision requires case")
        if self.gate == "H2" and self.analysis_plan is None:
            raise ValueError("H2 revision requires analysis_plan")
        if self.gate == "H1" and self.analysis_plan is not None:
            raise ValueError("H1 revision cannot include analysis_plan")
        if self.gate == "H2" and self.case is not None:
            raise ValueError("H2 revision cannot include case")
        return self
