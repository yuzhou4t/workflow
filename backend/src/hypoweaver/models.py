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


class CriticReport(StrictModel):
    report_id: str
    review_round: int = Field(ge=1, le=2)
    verdict: Literal["pass", "revise", "blocked"]
    issues: list[CriticIssue]
    approved_elements: list[str]
    remaining_risks: list[str]


class ContractBudget(StrictModel):
    max_executions: int = Field(default=12, ge=1)
    max_llm_calls: int = Field(default=20, ge=1)
    max_wall_time_seconds: int = Field(default=1800, ge=60)


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
    allowed_strength: Literal[
        "causal_strong",
        "causal_cautious",
        "associational",
        "preliminary",
        "mixed",
        "insufficient",
        "prohibited",
    ]
    supporting_runs: list[str]
    opposing_runs: list[str]
    scope: str
    robustness_status: str
    unresolved_risks: list[str]
    approval_status: Literal[
        "pending", "approved", "downgraded", "revise", "hold", "rejected"
    ] = "pending"
    human_decision_reason: str | None = None


class ClaimLedger(StrictModel):
    ledger_id: str
    case_id: str
    research_run_id: str
    claims: list[ClaimRecord]
    excluded_findings: list[str]
    unresolved_issues: list[str]


class ManuscriptSection(StrictModel):
    section_id: str
    title: str
    content_markdown: str
    status: Literal["generated", "not_generated"]
    claim_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)


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
    mode: Literal["research_plan_only", "full_manuscript"]
    status: Literal["draft", "needs_revision", "ready_for_human_review", "not_generated"]
    research_plan_markdown: str
    manuscript_sections: list[ManuscriptSection]
    empirical_findings_status: Literal["included", "not_executed", "prohibited_fixture"]
    disclosures: list[str]
    unresolved_issues: list[str]
    audit_result: Literal["not_run", "pass_with_no_critical_issues", "revise"] = "not_run"

    @model_validator(mode="after")
    def validate_plan_only_boundary(self) -> "ManuscriptPackage":
        if self.mode == "research_plan_only" and self.empirical_findings_status == "included":
            raise ValueError("research_plan_only cannot include empirical findings")
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
    gate: Literal["H1", "H2", "H3"]
    action: str
    actor: str
    comment: str = ""
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
    current_gate: Literal["H1", "H2", "H3"] | None = None
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
    action: Literal["approve", "revise", "reject", "generate_plan_only"]
    comment: str = ""
    actor: str = "local_researcher"
    expected_run_version: int | None = None
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))
    reviewed_artifact_hashes: dict[str, str] = Field(default_factory=dict)
    claims: list[ClaimDecisionInput] = Field(default_factory=list)

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
