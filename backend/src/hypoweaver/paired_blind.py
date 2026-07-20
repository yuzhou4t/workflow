from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from openai import AsyncOpenAI

from .benchmark_evaluator import summarize_paired_reviews, verify_benchmark_packet
from .benchmark_models import (
    BenchmarkPacket,
    BenchmarkResourceUsage,
    NeurIPSRatings,
    NeurIPSReview,
    OfficialAttemptBinding,
    PairedBlindCallReceipt,
    PairedEvaluationRequest,
    PairedEvaluationView,
)
from .benchmark_protocol import create_official_call_receipt
from .models import utc_now
from .paired_blind_repository import PairedBlindRepository
from .runtime_config import RuntimeConfigStore


PAIRED_BLIND_SYSTEM_PROMPT = """你是独立的匿名科研稿件评审者。两份产物来自同一研究题、同一可见输入和同一数据，但系统身份已经隐藏。
隐藏参考只是核验材料，不是绝对真值；与参考一致不自动正确，不一致也不自动错误。
分别按 NeurIPS 风格评价 A 与 B：quality、significance、clarity、soundness、presentation、contribution 均为 1-4 分；overall 为 1-10 分；confidence 为 1-5 分；给出 accept/reject。
重点检查方法是否适合、执行是否真实、数字是否一致、反证和失败是否保留、结论是否超出证据。不得猜测系统身份，不得修改任何产物。
只输出符合给定 JSON Schema 的对象。"""

PAIRED_BLIND_TIMEOUT_SECONDS = 360.0
PAIRED_BLIND_CONNECT_TIMEOUT_SECONDS = 30.0


class PairedBlindGateway(Protocol):
    provider_name: str

    async def review(
        self,
        *,
        sample_index: int,
        label_order: str,
        payload: dict[str, Any],
    ) -> NeurIPSReview: ...


def _ratings(score: int = 3, overall: int = 6) -> NeurIPSRatings:
    return NeurIPSRatings(
        quality=score,
        significance=score,
        clarity=score,
        soundness=score,
        presentation=score,
        contribution=score,
        overall=overall,
        confidence=3,
        recommendation="accept" if overall >= 6 else "reject",
    )


class FixturePairedBlindGateway:
    provider_name = "fixture"

    async def review(
        self,
        *,
        sample_index: int,
        label_order: str,
        payload: dict[str, Any],
    ) -> NeurIPSReview:
        return NeurIPSReview(
            review_id=f"fixture-review-{sample_index}",
            sample_index=sample_index,
            label_order=label_order,
            ratings_a=_ratings(),
            ratings_b=_ratings(),
            preferred_label="tie",
            diagnosis=["Fixture 只验证匿名配对评测链路，不代表科研质量。"],
        )


def _sealed_label_orders() -> list[Literal["A_B", "B_A"]]:
    """Draw the five presentation orders once, with both orders represented."""

    random_source = secrets.SystemRandom()
    orders: list[Literal["A_B", "B_A"]] = [
        "A_B",
        "A_B",
        "B_A",
        "B_A",
        random_source.choice(("A_B", "B_A")),
    ]
    random_source.shuffle(orders)
    return orders


class _ReviewCallBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = asyncio.Lock()

    async def reserve(self) -> None:
        async with self._lock:
            if self.used >= self.limit:
                raise RuntimeError("paired blind review call budget exhausted")
            self.used += 1


class PairedBlindCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        resource_usage: BenchmarkResourceUsage,
        call_receipt: PairedBlindCallReceipt,
    ) -> None:
        super().__init__(message)
        self.resource_usage = resource_usage
        self.call_receipt = call_receipt


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _failure_package_sha256(
    *,
    sample_index: int,
    error: Exception,
    raw_response: str | None = None,
) -> str:
    package = {
        "sample_index": sample_index,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "raw_response_sha256": (
            _sha256_text(raw_response) if raw_response is not None else None
        ),
    }
    return _sha256_text(
        json.dumps(
            package,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


class QwenPairedBlindGateway:
    provider_name = "qwen"

    def __init__(
        self,
        official_attempt: OfficialAttemptBinding | None = None,
        config_store: RuntimeConfigStore | None = None,
        model_override: str | None = None,
    ) -> None:
        config = (config_store or RuntimeConfigStore()).resolve()
        if not config.qwen_api_key:
            raise RuntimeError(
                "Qwen API Key is required for paired blind evaluation"
            )
        self.model = (
            model_override
            or os.getenv("QWEN_REVIEW_MODEL")
            or config.qwen_model
        )
        self.official_attempt = official_attempt
        self.http_client = httpx.AsyncClient(
            trust_env=urlsplit(config.qwen_base_url).hostname
            != "dashscope.aliyuncs.com",
            timeout=httpx.Timeout(
                PAIRED_BLIND_TIMEOUT_SECONDS,
                connect=PAIRED_BLIND_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        self.client = AsyncOpenAI(
            api_key=config.qwen_api_key,
            base_url=config.qwen_base_url,
            http_client=self.http_client,
            max_retries=0,
        )

    async def aclose(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            closed = close()
            if hasattr(closed, "__await__"):
                await closed
        if not self.http_client.is_closed:
            await self.http_client.aclose()

    async def review(
        self,
        *,
        sample_index: int,
        label_order: str,
        payload: dict[str, Any],
    ) -> NeurIPSReview:
        started = time.monotonic()
        call_started_at = utc_now()
        response: Any | None = None
        content: str | None = None
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PAIRED_BLIND_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, indent=2),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                timeout=PAIRED_BLIND_TIMEOUT_SECONDS,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Qwen returned an empty paired review")
            parsed_payload = json.loads(content)
            if not isinstance(parsed_payload, dict):
                raise ValueError("Qwen paired review must be a JSON object")
            parsed_payload.pop("official_receipt", None)
            parsed_payload.pop("call_receipt", None)
            parsed_payload.pop("system_assignment", None)
            parsed = NeurIPSReview.model_validate(parsed_payload)
        except Exception as error:
            response_usage = getattr(response, "usage", None)
            call_completed_at = utc_now()
            resource_usage = BenchmarkResourceUsage(
                llm_calls=1,
                input_tokens=int(
                    getattr(response_usage, "prompt_tokens", 0) or 0
                ),
                output_tokens=int(
                    getattr(response_usage, "completion_tokens", 0) or 0
                ),
                wall_time_seconds=time.monotonic() - started,
                technical_failures=[type(error).__name__],
            )
            raise PairedBlindCallError(
                str(error),
                resource_usage,
                PairedBlindCallReceipt(
                    sample_index=sample_index,
                    provider=self.provider_name,
                    model=self.model,
                    outcome="technical_failure",
                    failure_package_sha256=_failure_package_sha256(
                        sample_index=sample_index,
                        error=error,
                        raw_response=content,
                    ),
                    failure_type=type(error).__name__,
                    input_tokens=resource_usage.input_tokens,
                    output_tokens=resource_usage.output_tokens,
                    call_started_at=call_started_at,
                    call_completed_at=call_completed_at,
                ),
            ) from error
        usage = getattr(response, "usage", None)
        call_completed_at = utc_now()
        official_receipt = (
            create_official_call_receipt(
                self.official_attempt,
                provider=self.provider_name,
                model=self.model,
                raw_response=content,
                call_started_at=call_started_at,
                call_completed_at=call_completed_at,
            )
            if self.official_attempt is not None
            else None
        )
        return parsed.model_copy(
            update={
                "review_id": f"review-{uuid4()}",
                "sample_index": sample_index,
                "label_order": label_order,
                "resource_usage": BenchmarkResourceUsage(
                    llm_calls=1,
                    input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(
                        getattr(usage, "completion_tokens", 0) or 0
                    ),
                    wall_time_seconds=time.monotonic() - started,
                ),
                "official_receipt": official_receipt,
                "call_receipt": PairedBlindCallReceipt(
                    sample_index=sample_index,
                    provider=self.provider_name,
                    model=self.model,
                    outcome="succeeded",
                    response_sha256=_sha256_text(content),
                    input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(
                        getattr(usage, "completion_tokens", 0) or 0
                    ),
                    call_started_at=call_started_at,
                    call_completed_at=call_completed_at,
                ),
            }
        )


def _require_real_qwen_receipt(
    receipt: PairedBlindCallReceipt | None,
    *,
    gateway: QwenPairedBlindGateway,
    sample_index: int,
    expected_outcome: Literal["succeeded", "technical_failure"],
    usage: BenchmarkResourceUsage,
) -> PairedBlindCallReceipt:
    if receipt is None:
        raise ValueError("real Qwen paired review did not return a call receipt")
    if (
        receipt.sample_index != sample_index
        or receipt.provider != gateway.provider_name
        or receipt.model != gateway.model
        or receipt.outcome != expected_outcome
    ):
        raise ValueError("real Qwen paired review receipt provenance mismatch")
    if (
        receipt.input_tokens != usage.input_tokens
        or receipt.output_tokens != usage.output_tokens
    ):
        raise ValueError("real Qwen paired review receipt usage mismatch")
    return receipt


def _neutralize_text(text: str) -> str:
    return re.sub(
        r"(?i)HypoWeaver(?:-Qwen)?|Agent\s+Laboratory",
        "[SYSTEM]",
        text,
    )


def anonymize_packet(packet: BenchmarkPacket) -> dict[str, Any]:
    check_ids = {
        check_id: f"K{index}"
        for index, check_id in enumerate(
            dict.fromkeys(
                [
                    *packet.design.planned_check_ids,
                    *[item.check_id for item in packet.executions],
                ]
            ),
            start=1,
        )
    }
    execution_ids = {
        item.execution_id: f"E{index}"
        for index, item in enumerate(packet.executions, start=1)
    }
    claim_ids = {
        item.claim_id: f"C{index}"
        for index, item in enumerate(packet.claims, start=1)
    }
    return {
        "design": {
            "method_family": packet.design.method_family,
            "outcomes": packet.design.outcomes,
            "treatments_or_exposures": packet.design.treatments_or_exposures,
            "controls": packet.design.controls,
            "fixed_effects": packet.design.fixed_effects,
            "standard_error_strategy": packet.design.standard_error_strategy,
            "planned_check_ids": [
                check_ids[item] for item in packet.design.planned_check_ids
            ],
            "required_check_ids": [
                check_ids[item]
                for item in packet.design.required_check_ids
                if item in check_ids
            ],
            "frozen_before_execution": packet.design.frozen_before_execution,
        },
        "executions": [
            {
                "execution_id": execution_ids[item.execution_id],
                "check_id": check_ids[item.check_id],
                "execution_status": item.execution_status,
                "run_type": item.run_type,
                "estimates": item.estimates,
                "diagnostics": item.diagnostics,
                "not_executed_reason_code": item.not_executed_reason_code,
                "implementation_available": bool(item.implementation_id),
            }
            for item in packet.executions
        ],
        "claims": [
            {
                "claim_id": claim_ids[item.claim_id],
                "text": _neutralize_text(item.text),
                "strength": item.strength,
                "admission_status": item.admission_status,
                "check_ids": [
                    check_ids[check_id]
                    for check_id in item.check_ids
                    if check_id in check_ids
                ],
                "execution_ids": [
                    execution_ids[execution_id]
                    for execution_id in item.execution_ids
                    if execution_id in execution_ids
                ],
                "gate_reasons": item.gate_reasons,
            }
            for item in packet.claims
        ],
        "statements": [
            {
                "statement_id": f"S{index}",
                "text": _neutralize_text(item.text),
                "statement_kind": item.statement_kind,
                "claim_ids": [
                    claim_ids[claim_id]
                    for claim_id in item.claim_ids
                    if claim_id in claim_ids
                ],
                "execution_ids": [
                    execution_ids[execution_id]
                    for execution_id in item.execution_ids
                    if execution_id in execution_ids
                ],
                "protected_value_count": len(item.protected_values),
            }
            for index, item in enumerate(packet.statements, start=1)
        ],
        "manuscript_text": _neutralize_text(packet.manuscript_text),
        "reproduction": {
            "mode": packet.reproduction.mode,
            "status": packet.reproduction.status,
            "covered_check_count": len(packet.reproduction.covered_check_ids),
            "implementations_are_distinct": bool(
                packet.reproduction.primary_implementation_id
                and packet.reproduction.replication_implementation_id
                and packet.reproduction.primary_implementation_id
                != packet.reproduction.replication_implementation_id
            ),
        },
    }


def _review_output_schema() -> dict[str, Any]:
    schema = NeurIPSReview.model_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("official_receipt", None)
        properties.pop("call_receipt", None)
        properties.pop("system_assignment", None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            field
            for field in required
            if field
            not in {"official_receipt", "call_receipt", "system_assignment"}
        ]
    return schema


class PairedBlindEngine:
    def __init__(
        self,
        repository: PairedBlindRepository,
        *,
        gateway_factory: Callable[[str], PairedBlindGateway] | None = None,
        config_store: RuntimeConfigStore | None = None,
        review_model_override: str | None = None,
    ) -> None:
        self.repository = repository
        self._gateway_factory = gateway_factory
        self._config_store = config_store
        self._review_model_override = review_model_override

    async def evaluate(self, request: PairedEvaluationRequest) -> PairedEvaluationView:
        return await self._evaluate(request, predecessor=None)

    async def resume_failed(
        self,
        request: PairedEvaluationRequest,
        predecessor: PairedEvaluationView,
    ) -> PairedEvaluationView:
        """Retry only samples without a successful review in a failed Qwen view."""

        self._validate_resume_request(request, predecessor)
        return await self._evaluate(request, predecessor=predecessor)

    @staticmethod
    def _validate_resume_request(
        request: PairedEvaluationRequest,
        predecessor: PairedEvaluationView,
    ) -> None:
        if request.model_provider != "qwen":
            raise ValueError("paired blind resume requires the qwen provider")
        if predecessor.status != "failed" or predecessor.result is not None:
            raise ValueError("paired blind resume requires a failed predecessor")
        if (
            predecessor.case_id != request.packet_a.case_id
            or predecessor.packet_a_id != request.packet_a.packet_id
            or predecessor.packet_b_id != request.packet_b.packet_id
        ):
            raise ValueError("paired blind resume packet identity mismatch")
        if (
            request.sealed_label_orders is None
            or request.sealed_system_assignments is None
            or request.sealed_label_orders != predecessor.sealed_label_orders
            or request.sealed_system_assignments
            != predecessor.sealed_system_assignments
        ):
            raise ValueError("paired blind resume schedule mismatch")
        predecessor.verify_runtime_receipts(expect_real_qwen=True)
        receipts_by_sample = {
            receipt.sample_index: receipt
            for receipt in predecessor.review_call_receipts
        }
        usage_by_sample = {
            sample_index: usage
            for sample_index, usage in enumerate(
                predecessor.review_resource_usage,
                start=1,
            )
        }
        reviews_by_sample = {
            review.sample_index: review for review in predecessor.partial_reviews
        }
        successful_samples = {
            sample_index
            for sample_index, receipt in receipts_by_sample.items()
            if receipt.outcome == "succeeded"
        }
        if set(reviews_by_sample) != successful_samples:
            raise ValueError(
                "paired blind resume requires one partial review per successful receipt"
            )
        for sample_index, review in reviews_by_sample.items():
            if (
                review.label_order
                != predecessor.sealed_label_orders[sample_index - 1]
                or review.system_assignment
                != predecessor.sealed_system_assignments[sample_index - 1]
                or review.resource_usage != usage_by_sample[sample_index]
                or review.call_receipt != receipts_by_sample[sample_index]
            ):
                raise ValueError("paired blind resume predecessor provenance mismatch")

    async def _evaluate(
        self,
        request: PairedEvaluationRequest,
        *,
        predecessor: PairedEvaluationView | None,
    ) -> PairedEvaluationView:
        verify_benchmark_packet(request.packet_a)
        verify_benchmark_packet(request.packet_b)
        gateway: PairedBlindGateway
        if self._gateway_factory is not None:
            gateway = self._gateway_factory(request.model_provider)
        else:
            gateway = (
                QwenPairedBlindGateway(
                    request.official_attempt,
                    config_store=self._config_store,
                    model_override=self._review_model_override,
                )
                if request.model_provider == "qwen"
                else FixturePairedBlindGateway()
            )
        real_qwen = self._gateway_factory is None and isinstance(
            gateway, QwenPairedBlindGateway
        )
        if predecessor is not None:
            if not real_qwen:
                raise ValueError("paired blind resume requires the default Qwen gateway")
            predecessor.verify_runtime_receipts(
                expect_real_qwen=True,
                expected_model=gateway.model,
            )
        label_orders = (
            list(request.sealed_label_orders)
            if request.sealed_label_orders is not None
            else _sealed_label_orders()
        )
        system_assignments = (
            list(request.sealed_system_assignments)
            if request.sealed_system_assignments is not None
            else _sealed_label_orders()
        )
        view = PairedEvaluationView(
            id=str(uuid4()),
            case_id=request.packet_a.case_id,
            packet_a_id=request.packet_a.packet_id,
            packet_b_id=request.packet_b.packet_id,
            status="failed",
            sealed_label_orders=label_orders,
            sealed_system_assignments=system_assignments,
        )
        self.repository.create(request, view)
        anonymous_a = anonymize_packet(request.packet_a)
        anonymous_b = anonymize_packet(request.packet_b)
        usage_by_sample: dict[int, BenchmarkResourceUsage] = {}
        receipt_by_sample: dict[int, PairedBlindCallReceipt] = {}
        reviews_by_sample: dict[int, NeurIPSReview] = {}
        if predecessor is None:
            pending_samples = list(range(1, 6))
        else:
            predecessor_usage = {
                sample_index: usage
                for sample_index, usage in enumerate(
                    predecessor.review_resource_usage,
                    start=1,
                )
            }
            predecessor_receipts = {
                receipt.sample_index: receipt
                for receipt in predecessor.review_call_receipts
            }
            reviews_by_sample = {
                review.sample_index: review
                for review in predecessor.partial_reviews
            }
            for sample_index in reviews_by_sample:
                usage_by_sample[sample_index] = predecessor_usage[sample_index]
                receipt_by_sample[sample_index] = predecessor_receipts[sample_index]
            pending_samples = [
                sample_index
                for sample_index in range(1, 6)
                if sample_index not in reviews_by_sample
            ]
        call_budget = _ReviewCallBudget(len(pending_samples))

        async def run_sample(sample_index: int) -> NeurIPSReview:
            await call_budget.reserve()
            label_order = label_orders[sample_index - 1]
            system_assignment = system_assignments[sample_index - 1]
            labeled_outputs = (
                {"A": anonymous_a, "B": anonymous_b}
                if system_assignment == "A_B"
                else {"A": anonymous_b, "B": anonymous_a}
            )
            presentation_labels = (
                ("A", "B") if label_order == "A_B" else ("B", "A")
            )
            ordered = [
                {"label": label, "output": labeled_outputs[label]}
                for label in presentation_labels
            ]
            started = time.monotonic()
            try:
                review = await gateway.review(
                    sample_index=sample_index,
                    label_order=label_order,
                    payload={
                        "reference_summary": _neutralize_text(
                            request.reference_summary
                        ),
                        "outputs_in_review_order": ordered,
                        "required_output_schema": _review_output_schema(),
                    },
                )
            except Exception as error:
                reported_failure = getattr(error, "resource_usage", None)
                usage = BenchmarkResourceUsage(
                    llm_calls=1 if real_qwen else 0,
                    input_tokens=(
                        reported_failure.input_tokens if reported_failure else 0
                    ),
                    output_tokens=(
                        reported_failure.output_tokens if reported_failure else 0
                    ),
                    wall_time_seconds=max(
                        (
                            reported_failure.wall_time_seconds
                            if reported_failure
                            else 0
                        ),
                        time.monotonic() - started,
                    ),
                    technical_failures=[
                        f"sample-{sample_index}:{type(error).__name__}"
                    ],
                )
                usage_by_sample[sample_index] = usage
                if real_qwen:
                    receipt_by_sample[sample_index] = _require_real_qwen_receipt(
                        getattr(error, "call_receipt", None),
                        gateway=gateway,
                        sample_index=sample_index,
                        expected_outcome="technical_failure",
                        usage=usage,
                    )
                raise
            reported = review.resource_usage
            usage = BenchmarkResourceUsage(
                llm_calls=1 if real_qwen else 0,
                input_tokens=reported.input_tokens,
                output_tokens=reported.output_tokens,
                wall_time_seconds=max(
                    reported.wall_time_seconds,
                    time.monotonic() - started,
                ),
                technical_failures=[
                    f"sample-{sample_index}:reported_technical_failure"
                    for _failure in reported.technical_failures
                ],
            )
            usage_by_sample[sample_index] = usage
            call_receipt: PairedBlindCallReceipt | None = None
            if real_qwen:
                call_receipt = _require_real_qwen_receipt(
                    review.call_receipt,
                    gateway=gateway,
                    sample_index=sample_index,
                    expected_outcome="succeeded",
                    usage=usage,
                )
                receipt_by_sample[sample_index] = call_receipt
            return review.model_copy(
                update={
                    "sample_index": sample_index,
                    "label_order": label_order,
                    "system_assignment": system_assignment,
                    "resource_usage": usage,
                    "call_receipt": call_receipt,
                }
            )

        try:
            errors: list[BaseException] = []
            for sample_index in pending_samples:
                try:
                    reviews_by_sample[sample_index] = await run_sample(sample_index)
                except BaseException as error:
                    errors.append(error)
            view.review_resource_usage = [
                usage_by_sample[index]
                for index in range(1, 6)
                if index in usage_by_sample
            ]
            view.review_call_receipts = [
                receipt_by_sample[index]
                for index in range(1, 6)
                if index in receipt_by_sample
            ]
            view.receipt_count = len(view.review_call_receipts)
            view.verify_runtime_receipts(
                expect_real_qwen=real_qwen,
                expected_model=gateway.model if real_qwen else None,
            )
            reviews = [
                reviews_by_sample[sample_index]
                for sample_index in range(1, 6)
                if sample_index in reviews_by_sample
            ]
            view.partial_reviews = reviews
            if errors:
                raise errors[0]
            view.result = summarize_paired_reviews(
                request.packet_a.case_id,
                request.packet_a.packet_id,
                request.packet_b.packet_id,
                reviews,
            )
            view.status = "completed"
            view.error = None
            view.partial_reviews = []
            view.verify_runtime_receipts(
                expect_real_qwen=real_qwen,
                expected_model=gateway.model if real_qwen else None,
            )
        except Exception as error:
            view.status = "failed"
            view.error = type(error).__name__
            self.repository.update(view)
            raise
        finally:
            close = getattr(gateway, "aclose", None)
            if close is not None:
                await close()
        return self.repository.update(view)

    def get(self, evaluation_id: str) -> PairedEvaluationView:
        return self.repository.get(evaluation_id)

    def list(self) -> list[PairedEvaluationView]:
        return self.repository.list()


def build_paired_definition() -> dict[str, Any]:
    return {
        "id": "app-b-paired",
        "version": "2.0.0",
        "title": "HypoWeaver 匿名配对科研评测",
        "description": "验证中立封存包，执行五次 NeurIPS 风格模型盲评并由代码聚合。",
        "review_scale": {
            "quality": "1-4",
            "significance": "1-4",
            "clarity": "1-4",
            "soundness": "1-4",
            "presentation": "1-4",
            "contribution": "1-4",
            "overall": "1-10",
            "confidence": "1-5",
        },
        "samples": 5,
        "model_only": True,
        "can_mutate_app_a": False,
    }
