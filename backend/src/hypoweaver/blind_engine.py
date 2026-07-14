from __future__ import annotations

import hmac
import os
from typing import Protocol
from uuid import uuid4

from openai import AsyncOpenAI

from .blind_models import (
    DIMENSION_WEIGHTS,
    BlindEvaluationRequest,
    BlindEvaluationResult,
    BlindEvaluationView,
    BlindPromptPayload,
    BlindSuggestionSet,
    DimensionScore,
    DimensionSuggestion,
)
from .blind_prompts import BLIND_SYSTEM_PROMPT, BLIND_USER_TEMPLATE
from .blind_repository import BlindRepository
from .models import utc_now
from .seal import canonical_sha256, verify_manifest


class SealValidationError(ValueError):
    pass


class BlindGateway(Protocol):
    provider_name: str

    async def suggest(self, payload: BlindPromptPayload) -> BlindSuggestionSet: ...


class FixtureBlindGateway:
    provider_name = "fixture"

    async def suggest(self, payload: BlindPromptPayload) -> BlindSuggestionSet:
        dimensions = []
        for name in DIMENSION_WEIGHTS:
            not_applicable = payload.fixture_only and name == "main_result_match"
            dimensions.append(
                DimensionSuggestion(
                    dimension=name,
                    applicable=not not_applicable,
                    suggested_points=None if not_applicable else 0,
                    diagnosis="Fixture 仅验证盲测代码链，不提供科研质量判断。",
                    difference_vs_reference=(
                        "not_applicable：主系统未真实执行。"
                        if not_applicable
                        else "Fixture 未进行内容比较。"
                    ),
                )
            )
        return BlindSuggestionSet(
            dimensions=dimensions,
            notes="fixture_only：所有可评分维度为零分干跑，不代表科研质量。",
        )


class QwenBlindGateway:
    provider_name = "qwen"

    def __init__(self) -> None:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for qwen blind evaluation")
        self.model = os.getenv("QWEN_MODEL", "qwen-plus")
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv(
                "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
        )

    async def suggest(self, payload: BlindPromptPayload) -> BlindSuggestionSet:
        input_json = payload.model_dump_json(indent=2)
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": BLIND_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": BLIND_USER_TEMPLATE.replace("{{input_json}}", input_json),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Qwen returned an empty blind evaluation")
        return BlindSuggestionSet.model_validate_json(content)


class BlindEngine:
    def __init__(self, repository: BlindRepository) -> None:
        self.repository = repository

    @staticmethod
    def _validate_seal(request: BlindEvaluationRequest) -> None:
        manifest = request.sealed_output.model_dump(
            mode="json", exclude={"seal_sha256", "analysis_plan_sha256"}
        )
        if request.sealed_output.analysis_plan_sha256 is not None:
            manifest["analysis_plan_sha256"] = request.sealed_output.analysis_plan_sha256
        if not verify_manifest(manifest, request.sealed_output.seal_sha256):
            raise SealValidationError("seal_sha256 mismatch")

        research_run_hash = canonical_sha256(request.research_run.model_dump(mode="json"))
        if not hmac.compare_digest(
            research_run_hash, request.sealed_output.research_run_sha256
        ):
            raise SealValidationError("research_run_sha256 mismatch")

        ledger_hash = canonical_sha256(request.claim_ledger.model_dump(mode="json"))
        if not hmac.compare_digest(ledger_hash, request.sealed_output.claim_ledger_sha256):
            raise SealValidationError("claim_ledger_sha256 mismatch")

        if request.sealed_output.analysis_plan_sha256:
            plan_hash = canonical_sha256(request.analysis_plan.model_dump(mode="json"))
            if not hmac.compare_digest(
                plan_hash, request.sealed_output.analysis_plan_sha256
            ):
                raise SealValidationError("analysis_plan_sha256 mismatch")

    @staticmethod
    def _validate_suggestions(
        suggestions: BlindSuggestionSet, *, fixture_only: bool
    ) -> None:
        by_name = {item.dimension: item for item in suggestions.dimensions}
        main = by_name["main_result_match"]
        if fixture_only and (main.applicable or main.suggested_points is not None):
            raise ValueError("fixture_only main_result_match must be not_applicable")
        if not fixture_only and not main.applicable:
            raise ValueError("executed research requires main_result_match")

    @staticmethod
    def _score(
        request: BlindEvaluationRequest,
        suggestions: BlindSuggestionSet,
        provider: str,
    ) -> BlindEvaluationResult:
        ordered = {item.dimension: item for item in suggestions.dimensions}
        scores: list[DimensionScore] = []
        earned = 0.0
        applicable_weight = 0
        for name, weight in DIMENSION_WEIGHTS.items():
            item = ordered[name]
            if item.applicable:
                points = float(item.suggested_points or 0)
                earned += points
                applicable_weight += weight
                status = "scored"
            else:
                points = None
                status = "not_applicable"
            scores.append(
                DimensionScore(
                    dimension=name,
                    weight=weight,
                    points=points,
                    status=status,
                    diagnosis=item.diagnosis,
                    difference_vs_reference=item.difference_vs_reference,
                )
            )
        overall = round(earned / applicable_weight * 100, 2) if applicable_weight else 0.0
        return BlindEvaluationResult(
            case_id=request.case_id,
            source_run_id=request.sealed_output.run_id,
            provider=provider,
            overall_score=overall,
            dimension_scores=scores,
            key_agreements=suggestions.key_agreements,
            key_differences=suggestions.key_differences,
            verifiable_difference_explanations=suggestions.verifiable_difference_explanations,
            fixture_only=request.research_run.fixture_only,
            notes=suggestions.notes,
        )

    async def evaluate(self, request: BlindEvaluationRequest) -> BlindEvaluationView:
        self._validate_seal(request)
        gateway: BlindGateway = (
            QwenBlindGateway() if request.model_provider == "qwen" else FixtureBlindGateway()
        )
        now = utc_now()
        view = BlindEvaluationView(
            id=str(uuid4()),
            case_id=request.case_id,
            source_run_id=request.sealed_output.run_id,
            status="failed",
            seal_sha256=request.sealed_output.seal_sha256,
            created_at=now,
            updated_at=now,
        )
        self.repository.create(request, view)
        try:
            payload = BlindPromptPayload(
                analysis_plan=request.analysis_plan.model_dump(mode="json"),
                research_run=request.research_run.model_dump(mode="json"),
                claim_ledger=request.claim_ledger.model_dump(mode="json"),
                reference_summary=request.reference_summary,
                reference_paper_text=request.reference_paper_text,
                fixture_only=request.research_run.fixture_only,
            )
            suggestions = await gateway.suggest(payload)
            self._validate_suggestions(
                suggestions, fixture_only=request.research_run.fixture_only
            )
            view.result = self._score(request, suggestions, gateway.provider_name)
            view.status = "completed"
            view.error = None
        except Exception as error:
            view.status = "failed"
            view.error = str(error)
            self.repository.update(view)
            raise
        return self.repository.update(view)

    def get(self, evaluation_id: str) -> BlindEvaluationView:
        return self.repository.get(evaluation_id)

    def list(self) -> list[BlindEvaluationView]:
        return self.repository.list()
