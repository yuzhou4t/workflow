from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .models import AnalysisPlan, ClaimLedger, ResearchRun, StrictModel, utc_now


DimensionName = Literal[
    "method_design",
    "execution_reproducibility",
    "main_result_match",
    "robustness_falsification",
    "claim_calibration",
    "failure_disclosure",
]

DIMENSION_WEIGHTS: dict[str, int] = {
    "method_design": 20,
    "execution_reproducibility": 20,
    "main_result_match": 20,
    "robustness_falsification": 15,
    "claim_calibration": 15,
    "failure_disclosure": 10,
}


class SealedOutput(StrictModel):
    run_id: str
    seal_algorithm: Literal["hmac-sha256"]
    contract_sha256: str
    research_run_sha256: str
    claim_ledger_sha256: str
    manuscript_sha256: str
    analysis_plan_sha256: str | None = None
    seal_sha256: str


class BlindEvaluationRequest(StrictModel):
    case_id: str
    sealed_output: SealedOutput
    analysis_plan: AnalysisPlan
    research_run: ResearchRun
    claim_ledger: ClaimLedger
    reference_summary: str = Field(min_length=1)
    reference_paper_text: str = Field(min_length=1)
    model_provider: Literal["fixture", "qwen"] = "fixture"

    @model_validator(mode="after")
    def validate_case_identity(self) -> "BlindEvaluationRequest":
        if self.sealed_output.run_id == "":
            raise ValueError("sealed_output.run_id is required")
        if self.research_run.case_id != self.case_id:
            raise ValueError("research_run.case_id does not match case_id")
        if self.claim_ledger.case_id != self.case_id:
            raise ValueError("claim_ledger.case_id does not match case_id")
        if self.claim_ledger.research_run_id != self.research_run.research_run_id:
            raise ValueError("claim_ledger.research_run_id does not match research_run")
        return self


class DimensionSuggestion(StrictModel):
    dimension: DimensionName
    suggested_points: float | None = None
    applicable: bool = True
    diagnosis: str
    difference_vs_reference: str

    @model_validator(mode="after")
    def validate_points(self) -> "DimensionSuggestion":
        weight = DIMENSION_WEIGHTS[self.dimension]
        if self.applicable:
            if self.suggested_points is None:
                raise ValueError("applicable dimensions require suggested_points")
            if not 0 <= self.suggested_points <= weight:
                raise ValueError(f"suggested_points must be between 0 and {weight}")
        elif self.suggested_points is not None:
            raise ValueError("not_applicable dimensions must use null suggested_points")
        return self


class BlindSuggestionSet(StrictModel):
    dimensions: list[DimensionSuggestion]
    key_agreements: list[str] = Field(default_factory=list)
    key_differences: list[str] = Field(default_factory=list)
    verifiable_difference_explanations: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def validate_fixed_dimensions(self) -> "BlindSuggestionSet":
        names = [item.dimension for item in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError("evaluation dimensions must be unique")
        if set(names) != set(DIMENSION_WEIGHTS):
            raise ValueError("evaluation must contain exactly the six fixed dimensions")
        return self


class DimensionScore(StrictModel):
    dimension: DimensionName
    weight: int
    points: float | None
    status: Literal["scored", "not_applicable"]
    diagnosis: str
    difference_vs_reference: str


class BlindEvaluationResult(StrictModel):
    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    case_id: str
    source_run_id: str
    provider: Literal["fixture", "qwen"]
    overall_score: float
    dimension_scores: list[DimensionScore]
    key_agreements: list[str]
    key_differences: list[str]
    verifiable_difference_explanations: list[str]
    fixture_only: bool
    notes: str
    created_at: str = Field(default_factory=utc_now)


class BlindEvaluationView(StrictModel):
    id: str
    definition_id: Literal["app-b"] = "app-b"
    definition_version: Literal["1.0.0"] = "1.0.0"
    case_id: str
    source_run_id: str
    status: Literal["completed", "failed"]
    seal_sha256: str
    result: BlindEvaluationResult | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class BlindPromptPayload(StrictModel):
    analysis_plan: dict[str, Any]
    research_run: dict[str, Any]
    claim_ledger: dict[str, Any]
    reference_summary: str
    reference_paper_text: str
    fixture_only: bool
