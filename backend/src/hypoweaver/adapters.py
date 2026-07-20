from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .models import (
    AnalysisPlan,
    CaseSubmission,
    ClaimLedger,
    ClaimRecord,
    CriticReport,
    CandidateReview,
    DataProfile,
    DesignReviewerReport,
    EvidenceAssessment,
    ExecutionRecord,
    FormalResearchContract,
    ManuscriptPackage,
    ManuscriptSection,
    MethodRoute,
    ModelCallContext,
    ModelCallReceipt,
    ModelSpec,
    PlannedStep,
    ResearchPackage,
    ResearchRun,
    SchemaValidationIssue,
    ScientificAudit,
    TestableHypotheses,
    TestableHypothesis,
    utc_now,
)
from .prompts import get_prompt
from .runtime_config import RuntimeConfigStore
from .seal import canonical_sha256


OutputModel = TypeVar("OutputModel", bound=BaseModel)
DEFAULT_MODEL_CALL_BUDGET = 20
V2_PROVIDER_ATTEMPT_BUDGET = 40
V2_LOGICAL_CALL_BUDGET = 20
V3_PROVIDER_ATTEMPT_BUDGET = 80
V3_LOGICAL_CALL_BUDGET = 20
ModelCallBudgetMode = Literal["legacy", "v2", "v3"]
MODEL_CALL_GROUP_LIMITS = {
    "h1_h2": 10,
    "h3": 4,
    "h4": 6,
}
# Mandatory first calls retain their slots inside both the global budget and the
# enforced per-stage ceilings.
MODEL_CALL_GROUP_REQUIRED_FIRST_CALLS = {
    "h1_h2": 5,
    "h3": 2,
    "h4": 2,
}
MODEL_CALL_REQUIRED_FIRST_CALLS = sum(
    MODEL_CALL_GROUP_REQUIRED_FIRST_CALLS.values()
)
MODEL_CALL_SHARED_RETRY_POLICY_VERSION = "shared-retry-v1"
MODEL_CALL_RETRY_BACKOFF_SECONDS = {2: 2.0, 3: 8.0}
SCHEMA_ERROR_SUMMARY_LIMIT = 20
SCHEMA_ERROR_LOCATION_LIMIT = 12


def _safe_token_count(value: Any) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(count, 0)


def _provider_response_text(response: Any) -> str | None:
    """Return provider text without allowing malformed response objects to escape."""

    try:
        direct_content = getattr(response, "content", None)
    except Exception:
        direct_content = None
    if isinstance(direct_content, str) and direct_content.strip():
        return direct_content
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
    except Exception:
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    return content


@dataclass(frozen=True)
class _AggregatedStreamUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class _AggregatedStreamResponse:
    id: str
    content: str
    usage: _AggregatedStreamUsage


async def _aggregate_qwen_stream(stream: Any) -> _AggregatedStreamResponse:
    """Collect one JSON-mode stream without persisting partial provider text."""

    response_id = ""
    content_parts: list[str] = []
    usage_values: tuple[int, int] | None = None
    try:
        iterator = stream.__aiter__()
    except (AttributeError, TypeError) as error:
        raise ValueError("qwen streaming response is not async iterable") from error
    async for chunk in iterator:
        chunk_id = str(getattr(chunk, "id", "") or "")
        if chunk_id:
            if response_id and chunk_id != response_id:
                raise ValueError("qwen streaming response id changed")
            response_id = chunk_id
        choices = getattr(chunk, "choices", None) or []
        if len(choices) > 1:
            raise ValueError("qwen streaming response returned multiple choices")
        if choices:
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("qwen streaming content chunk is not text")
                content_parts.append(content)
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            observed_usage = (
                _safe_token_count(getattr(usage, "prompt_tokens", 0)),
                _safe_token_count(getattr(usage, "completion_tokens", 0)),
            )
            if usage_values is not None and observed_usage != usage_values:
                raise ValueError("qwen streaming usage changed")
            usage_values = observed_usage
    if not response_id:
        raise ValueError("qwen streaming response omitted response id")
    if usage_values is None:
        raise ValueError("qwen streaming response omitted usage")
    return _AggregatedStreamResponse(
        id=response_id,
        content="".join(content_parts),
        usage=_AggregatedStreamUsage(
            prompt_tokens=usage_values[0],
            completion_tokens=usage_values[1],
        ),
    )


def _schema_property_names(schema: dict[str, Any]) -> frozenset[str]:
    """Collect only code-owned property names that are safe to expose."""

    names: set[str] = set()
    pending: list[Any] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                names.update(str(name) for name in properties)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return frozenset(names)


def _safe_schema_location_part(
    value: Any,
    *,
    property_names: frozenset[str],
) -> str:
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789_-"
    )
    if isinstance(value, int):
        candidate = str(value)
    elif isinstance(value, str) and value in property_names:
        candidate = value
    else:
        candidate = ""
    if (
        candidate
        and len(candidate) <= 64
        and all(character in allowed for character in candidate)
    ):
        return candidate
    if isinstance(value, (str, int)):
        digest_source = str(value)
    else:
        digest_source = type(value).__name__
    return f"redacted-{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()[:12]}"


def _safe_schema_error_details(
    error: Exception,
    *,
    output_schema: dict[str, Any],
) -> tuple[list[SchemaValidationIssue], int]:
    """Return bounded loc/type evidence without response values or messages."""

    if not isinstance(error, ValidationError):
        return [SchemaValidationIssue(type="malformed_response")], 1
    try:
        details = error.errors(include_url=False, include_input=False)
    except Exception:
        return [SchemaValidationIssue(type="validation_failed")], 1
    if not details:
        return [SchemaValidationIssue(type="validation_failed")], 1
    property_names = _schema_property_names(output_schema)
    summary: list[SchemaValidationIssue] = []
    for detail in details[:SCHEMA_ERROR_SUMMARY_LIMIT]:
        raw_location = detail.get("loc", ())
        location = [
            _safe_schema_location_part(item, property_names=property_names)
            for item in raw_location[:SCHEMA_ERROR_LOCATION_LIMIT]
        ]
        if len(raw_location) > SCHEMA_ERROR_LOCATION_LIMIT:
            location[-1:] = ["truncated"]
        raw_type = detail.get("type", "invalid")
        error_type = str(raw_type)
        if (
            not error_type
            or len(error_type) > 64
            or not error_type[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in error_type
            )
        ):
            error_type = "invalid"
        summary.append(
            SchemaValidationIssue(loc=tuple(location), type=error_type)
        )
    return summary, len(details)


def _safe_schema_error_summary(
    details: list[SchemaValidationIssue],
    total_count: int,
) -> str:
    rendered = "; ".join(
        f"{'.'.join(detail.loc) or 'root'}: {detail.type}"
        for detail in details
    )
    if total_count > len(details):
        rendered += f"; truncated: {total_count - len(details)}"
    return rendered or "validation failed"


def _model_call_error_category(error: BaseException) -> str:
    """Classify a provider failure without persisting messages or endpoints."""

    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, APIStatusError):
        return "http_status"

    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None

    if any(isinstance(item, socket.gaierror) for item in chain):
        return "dns"
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return "tls"
    if any(isinstance(item, httpx.ProxyError) for item in chain):
        return "proxy"
    if any(isinstance(item, ConnectionResetError) for item in chain):
        return "connection_reset"
    if any(isinstance(item, httpx.ConnectTimeout) for item in chain):
        return "connect_timeout"
    if any(isinstance(item, httpx.ReadTimeout) for item in chain):
        return "read_timeout"
    if isinstance(error, (APIConnectionError, APITimeoutError)) or any(
        isinstance(item, httpx.TransportError) for item in chain
    ):
        return "unknown_transport"
    return "unknown_provider"


@dataclass
class ModelCallBudget:
    max_calls: int = DEFAULT_MODEL_CALL_BUDGET
    budget_mode: ModelCallBudgetMode = "legacy"
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_time_seconds: float = 0.0
    technical_failures: list[str] = field(default_factory=list)
    call_receipts: list[dict[str, Any]] = field(default_factory=list)
    _group_usage: dict[str, int] = field(init=False, repr=False)
    _logical_attempts: dict[str, int] = field(init=False, repr=False)
    _logical_groups: dict[str, str] = field(init=False, repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.budget_mode in {"v2", "v3"}:
            # Frozen comparison envelopes. ``max_calls`` remains a
            # constructor field for backward-compatible restoration, but its
            # provider-attempt ceiling is not caller-tunable in this mode.
            self.max_calls = (
                V2_PROVIDER_ATTEMPT_BUDGET
                if self.budget_mode == "v2"
                else V3_PROVIDER_ATTEMPT_BUDGET
            )
        self._group_usage = {key: 0 for key in MODEL_CALL_GROUP_LIMITS}
        self._logical_attempts = {}
        self._logical_groups = {}
        for receipt in self.call_receipts:
            if not isinstance(receipt, dict):
                continue
            group = receipt.get("call_group")
            logical_call_id = receipt.get("logical_call_id")
            if isinstance(logical_call_id, str) and logical_call_id:
                is_new_logical_call = logical_call_id not in self._logical_groups
                self._logical_attempts[logical_call_id] = (
                    self._logical_attempts.get(logical_call_id, 0) + 1
                )
                if group in self._group_usage:
                    self._logical_groups[logical_call_id] = group
                    if self.budget_mode in {"v2", "v3"}:
                        if is_new_logical_call:
                            self._group_usage[group] += 1
                    else:
                        self._group_usage[group] += 1
            elif group in self._group_usage and self.budget_mode == "legacy":
                self._group_usage[group] += 1

    def _required_first_calls_remaining(
        self,
        *,
        starting_context: ModelCallContext | None = None,
    ) -> dict[str, int]:
        started_by_group = {key: 0 for key in MODEL_CALL_GROUP_REQUIRED_FIRST_CALLS}
        for group in self._logical_groups.values():
            if group in started_by_group:
                started_by_group[group] += 1
        if (
            starting_context is not None
            and starting_context.logical_call_id not in self._logical_groups
        ):
            started_by_group[starting_context.call_group] += 1
        return {
            group: max(required - started_by_group[group], 0)
            for group, required in MODEL_CALL_GROUP_REQUIRED_FIRST_CALLS.items()
        }

    def next_attempt(
        self,
        context: ModelCallContext,
    ) -> tuple[int, str]:
        """Return the next durable attempt and its type for an interrupted call."""

        with self._lock:
            existing_group = self._logical_groups.get(context.logical_call_id)
            if existing_group is not None and existing_group != context.call_group:
                raise RuntimeError(
                    "同一逻辑模型调用不能跨调用分组恢复。"
                )
            receipts = [
                receipt
                for receipt in self.call_receipts
                if receipt.get("logical_call_id") == context.logical_call_id
            ]
            used_attempts = self._logical_attempts.get(context.logical_call_id, 0)
            attempt_index = used_attempts + 1
            if attempt_index > context.max_attempts:
                raise RuntimeError(
                    f"逻辑模型调用 {context.logical_call_id} "
                    f"已达最多 {context.max_attempts} 次尝试。"
                )
            if not receipts:
                return attempt_index, context.attempt_type
            last_outcome = receipts[-1].get("outcome")
            if last_outcome == "succeeded":
                if context.attempt_type == "content_repair":
                    return attempt_index, "content_repair"
                raise RuntimeError(
                    f"逻辑模型调用 {context.logical_call_id} 已成功，不能重复执行。"
                )
            if last_outcome == "schema_failure":
                return attempt_index, "schema_repair"
            if last_outcome == "transport_failure":
                return attempt_index, "transport_retry"
            raise RuntimeError(
                f"逻辑模型调用 {context.logical_call_id} 的既有失败"
                "不具备安全的跨进程重试证据。"
            )

    def reserve(
        self,
        context: ModelCallContext | None = None,
        *,
        attempt_index: int | None = None,
    ) -> int:
        call_context = context or ModelCallContext()
        with self._lock:
            if self.llm_calls >= self.max_calls:
                raise RuntimeError(
                    f"模型调用预算已用完（{self.llm_calls}/{self.max_calls}）。"
                )
            used_attempts = self._logical_attempts.get(
                call_context.logical_call_id,
                0,
            )
            next_attempt = used_attempts + 1
            requested_attempt = attempt_index or next_attempt
            if requested_attempt != next_attempt:
                raise RuntimeError(
                    "同一逻辑模型调用的 attempt_index 必须严格递增。"
                )
            if requested_attempt > call_context.max_attempts:
                raise RuntimeError(
                    f"逻辑模型调用 {call_context.logical_call_id} "
                    f"已达最多 {call_context.max_attempts} 次尝试。"
                )
            existing_group = self._logical_groups.get(
                call_context.logical_call_id
            )
            if (
                existing_group is not None
                and existing_group != call_context.call_group
            ):
                raise RuntimeError(
                    "同一逻辑模型调用不能跨调用分组重试。"
                )
            is_new_logical_call = existing_group is None
            logical_calls = len(self._logical_groups)
            if (
                self.budget_mode in {"v2", "v3"}
                and is_new_logical_call
                and logical_calls >= V2_LOGICAL_CALL_BUDGET
            ):
                raise RuntimeError(
                    "逻辑模型调用预算已用完"
                    f"（{logical_calls}/{V2_LOGICAL_CALL_BUDGET}）。"
                )
            group_used = self._group_usage[call_context.call_group]
            group_limit = MODEL_CALL_GROUP_LIMITS[call_context.call_group]
            group_slot_consumed = (
                self.budget_mode == "legacy" or is_new_logical_call
            )
            if group_slot_consumed and group_used >= group_limit:
                raise RuntimeError(
                    "模型调用分组预算已用完"
                    f"（{call_context.call_group}: {group_used}/{group_limit}）。"
                )
            required_after = self._required_first_calls_remaining(
                starting_context=call_context,
            )
            group_reserved_for_unstarted = required_after[
                call_context.call_group
            ]
            group_remaining_after = (
                group_limit - group_used - int(group_slot_consumed)
            )
            if group_remaining_after < group_reserved_for_unstarted:
                raise RuntimeError(
                    "模型调用重试被拒绝：必须为该分组尚未启动的"
                    "必做首轮调用保留配额"
                    f"（{call_context.call_group} 保留 "
                    f"{group_reserved_for_unstarted}）。"
                )
            globally_reserved_for_unstarted = sum(required_after.values())
            provider_remaining_after = self.max_calls - self.llm_calls - 1
            if provider_remaining_after < globally_reserved_for_unstarted:
                raise RuntimeError(
                    "模型调用重试被拒绝：必须为全部尚未启动的必做首轮调用"
                    f"保留配额（全局保留 {globally_reserved_for_unstarted}）。"
                )
            if self.budget_mode in {"v2", "v3"}:
                logical_remaining_after = (
                    (
                        V2_LOGICAL_CALL_BUDGET
                        if self.budget_mode == "v2"
                        else V3_LOGICAL_CALL_BUDGET
                    )
                    - logical_calls
                    - int(is_new_logical_call)
                )
                if logical_remaining_after < globally_reserved_for_unstarted:
                    raise RuntimeError(
                        "逻辑模型调用被拒绝：必须为全部尚未启动的必做首轮调用"
                        f"保留逻辑槽位（全局保留 {globally_reserved_for_unstarted}）。"
                    )
            self.llm_calls += 1
            self._group_usage[call_context.call_group] = (
                group_used + int(group_slot_consumed)
            )
            self._logical_attempts[call_context.logical_call_id] = requested_attempt
            self._logical_groups[call_context.logical_call_id] = (
                call_context.call_group
            )
            return requested_attempt

    def record_response(
        self,
        response: Any,
        elapsed: float,
        *,
        provider: str | None = None,
        model: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        context: ModelCallContext | None = None,
        attempt_index: int = 1,
        attempt_type: str | None = None,
        outcome: str = "succeeded",
        error_type: str | None = None,
        error_category: str | None = None,
        prompt_version: str = "legacy",
        input_sha256: str = "0" * 64,
        output_schema_sha256: str = "0" * 64,
        schema_error_summary: tuple[SchemaValidationIssue, ...] = (),
        schema_error_count: int = 0,
    ) -> None:
        try:
            usage = getattr(response, "usage", None)
        except Exception:
            usage = None
        try:
            prompt_tokens = getattr(usage, "prompt_tokens", 0)
        except Exception:
            prompt_tokens = 0
        try:
            completion_tokens = getattr(usage, "completion_tokens", 0)
        except Exception:
            completion_tokens = 0
        input_tokens = _safe_token_count(prompt_tokens)
        output_tokens = _safe_token_count(completion_tokens)
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.wall_time_seconds += elapsed
        if provider and model and started_at and completed_at:
            content = _provider_response_text(response)
            try:
                response_id = str(getattr(response, "id", "") or "")
            except Exception:
                response_id = ""
            call_context = context or ModelCallContext()
            receipt = ModelCallReceipt(
                logical_call_id=call_context.logical_call_id,
                call_group=call_context.call_group,
                prompt_key=call_context.prompt_key,
                prompt_version=prompt_version,
                attempt_index=attempt_index,
                max_attempts=call_context.max_attempts,
                attempt_type=attempt_type or call_context.attempt_type,
                outcome=outcome,
                provider=provider,
                model=model,
                started_at=started_at,
                completed_at=completed_at,
                response_sha256=(
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if content is not None
                    else canonical_sha256(
                        {
                            "outcome": outcome,
                            "error_type": error_type
                            or "MalformedProviderResponse",
                        }
                    )
                ),
                input_sha256=input_sha256,
                output_schema_sha256=output_schema_sha256,
                provider_response_id_sha256=(
                    canonical_sha256(response_id) if response_id else None
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_type=error_type,
                error_category=error_category,
                schema_error_summary=list(schema_error_summary),
                schema_error_count=schema_error_count,
            )
            with self._lock:
                self.call_receipts.append(receipt.model_dump(mode="json"))

    def record_failure(
        self,
        error: BaseException,
        elapsed: float,
        *,
        provider: str | None = None,
        model: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        context: ModelCallContext | None = None,
        attempt_index: int = 1,
        attempt_type: str | None = None,
        outcome: str = "transport_failure",
        error_category: str | None = None,
        prompt_version: str = "legacy",
        input_sha256: str = "0" * 64,
        output_schema_sha256: str = "0" * 64,
    ) -> None:
        failure_type = type(error).__name__
        with self._lock:
            self.wall_time_seconds += elapsed
            self.technical_failures.append(failure_type)
        if provider and model and started_at and completed_at:
            call_context = context or ModelCallContext()
            receipt = ModelCallReceipt(
                logical_call_id=call_context.logical_call_id,
                call_group=call_context.call_group,
                prompt_key=call_context.prompt_key,
                prompt_version=prompt_version,
                attempt_index=attempt_index,
                max_attempts=call_context.max_attempts,
                attempt_type=attempt_type or call_context.attempt_type,
                outcome=outcome,
                provider=provider,
                model=model,
                started_at=started_at,
                completed_at=completed_at,
                response_sha256=canonical_sha256(
                    {
                        "outcome": outcome,
                        "error_type": failure_type,
                    }
                ),
                input_sha256=input_sha256,
                output_schema_sha256=output_schema_sha256,
                provider_response_id_sha256=None,
                input_tokens=0,
                output_tokens=0,
                error_type=failure_type,
                error_category=error_category,
            )
            with self._lock:
                self.call_receipts.append(receipt.model_dump(mode="json"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            required_remaining = self._required_first_calls_remaining()
            reserved_first_calls = sum(required_remaining.values())
            remaining_calls = max(self.max_calls - self.llm_calls, 0)
            started_required_first_calls = sum(
                MODEL_CALL_GROUP_REQUIRED_FIRST_CALLS[group] - remaining
                for group, remaining in required_remaining.items()
            )
            logical_calls = len(self._logical_groups)
            logical_call_ceiling = (
                (
                    V2_LOGICAL_CALL_BUDGET
                    if self.budget_mode == "v2"
                    else V3_LOGICAL_CALL_BUDGET
                )
                if self.budget_mode in {"v2", "v3"}
                else None
            )
            group_counting_unit = (
                "logical_call"
                if self.budget_mode in {"v2", "v3"}
                else "provider_attempt"
            )
            retry_attempts_used = sum(
                max(attempts - 1, 0)
                for attempts in self._logical_attempts.values()
            )
            return {
                "budget_mode": self.budget_mode,
                "max_calls": self.max_calls,
                "provider_attempt_ceiling": self.max_calls,
                "logical_call_ceiling": logical_call_ceiling,
                "provider_attempt_counting_unit": "provider_request",
                "logical_call_counting_unit": "distinct_logical_call_id",
                "provider_attempts": self.llm_calls,
                "logical_calls": logical_calls,
                "llm_calls": self.llm_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "wall_time_seconds": round(self.wall_time_seconds, 6),
                "technical_failures": list(self.technical_failures),
                "call_receipts": list(self.call_receipts),
                "group_limits": dict(MODEL_CALL_GROUP_LIMITS),
                "group_usage": dict(self._group_usage),
                "group_counting_unit": group_counting_unit,
                "logical_call_attempts": dict(self._logical_attempts),
                "logical_call_groups": dict(self._logical_groups),
                "shared_retry_policy": {
                    "version": MODEL_CALL_SHARED_RETRY_POLICY_VERSION,
                    "mode": "global_shared_retry_pool_with_group_caps",
                    "legacy_group_limits_enforced": self.budget_mode == "legacy",
                    "required_first_calls": MODEL_CALL_REQUIRED_FIRST_CALLS,
                    "required_first_calls_by_group": dict(
                        MODEL_CALL_GROUP_REQUIRED_FIRST_CALLS
                    ),
                    "reserved_for_unstarted_required_first_calls": (
                        reserved_first_calls
                    ),
                    "reserved_for_unstarted_by_group": required_remaining,
                    "shared_retry_capacity": max(
                        self.max_calls - MODEL_CALL_REQUIRED_FIRST_CALLS,
                        0,
                    ),
                    "shared_retry_used": max(
                        (
                            retry_attempts_used
                            if self.budget_mode in {"v2", "v3"}
                            else self.llm_calls - started_required_first_calls
                        ),
                        0,
                    ),
                    "shared_retry_remaining": max(
                        remaining_calls - reserved_first_calls,
                        0,
                    ),
                    "max_attempts_per_logical_call": 3,
                },
            }


class ModelGateway(Protocol):
    provider_name: str

    async def generate(
        self,
        prompt_key: str,
        payload: dict[str, Any],
        output_model: type[OutputModel],
        *,
        call_context: ModelCallContext | None = None,
    ) -> OutputModel: ...


class ResearchExecutor(Protocol):
    executor_name: str

    async def execute(self, contract: FormalResearchContract) -> ResearchRun: ...


class ResearchReproducer(Protocol):
    reproducer_name: str

    async def execute(self, contract: FormalResearchContract) -> ResearchRun: ...


def _variables(package: ResearchPackage, *roles: str) -> list[str]:
    return [variable.name for variable in package.variables if variable.role in roles]


def _planned(step_id: str, name: str, rationale: str, **parameters: Any) -> PlannedStep:
    return PlannedStep(
        step_id=step_id,
        name=name,
        rationale=rationale,
        parameters=parameters,
    )


class FixtureModelGateway:
    """Deterministic adapter for workflow verification; it never fabricates evidence."""

    provider_name = "fixture"

    async def generate(
        self,
        prompt_key: str,
        payload: dict[str, Any],
        output_model: type[OutputModel],
        *,
        call_context: ModelCallContext | None = None,
    ) -> OutputModel:
        handlers = {
            "intake": self._intake,
            "hypothesis_decomposition": self._decompose,
            "method_route": self._route,
            "analysis_design": self._design,
            "candidate_plan_batch": self._candidate_plan_batch,
            "design_reviewer": self._design_reviewer,
            "reviewer_report_batch": self._reviewer_report_batch,
            "method_critic": self._critic,
            "plan_revision": self._revise,
            "evidence_assessment": self._assess,
            "evidence_claim_bundle": self._evidence_claim_bundle,
            "scientific_audit": self._audit,
            "claim_ledger": self._claims,
            "scientific_writer": self._write,
            "manuscript_section_draft_batch": self._write_draft_batch,
        }
        try:
            raw = handlers[prompt_key](payload)
        except KeyError as error:
            raise ValueError(f"fixture has no handler for prompt {prompt_key}") from error
        return output_model.model_validate(raw)

    @staticmethod
    def _intake(payload: dict[str, Any]) -> dict[str, Any]:
        case = CaseSubmission.model_validate(payload["case"])
        return ResearchPackage(
            **case.model_dump(),
            input_conflicts=[],
            missing_required_information=(
                [] if case.dataset_refs else ["尚未接入可执行数据资产；本次只能形成研究设计。"]
            ),
        ).model_dump()

    @staticmethod
    def _decompose(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        outcomes = _variables(package, "outcome")
        exposures = _variables(package, "treatment", "exposure")
        mechanisms = _variables(package, "mediator")
        return TestableHypotheses(
            items=[
                TestableHypothesis(
                    hypothesis_id=hypothesis.hypothesis_id,
                    theoretical_claim=hypothesis.statement,
                    observable_prediction=(
                        f"在预先定义样本与模型中，{', '.join(exposures) or '核心解释变量'}"
                        f"与 {', '.join(outcomes) or '结果变量'} 呈{hypothesis.expected_direction}方向关系。"
                    ),
                    analysis_unit=package.unit_of_analysis,
                    outcome_variables=outcomes,
                    treatment_or_exposure_variables=exposures,
                    mechanism_variables=mechanisms,
                    boundary_conditions=["仅适用于冻结合同中的样本、时期与变量口径。"],
                    competing_explanations=["遗漏变量", "反向因果", "同期政策或共同趋势"],
                    falsification_conditions=["前置趋势或伪处理检验显示同类效应。", "替代口径下方向不稳定。"],
                )
                for hypothesis in package.hypotheses
            ]
        ).model_dump()

    @staticmethod
    def _route(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        profile = DataProfile.model_validate(payload["data_profile"])
        text = " ".join(
            [package.title, package.research_question, *package.known_policy_facts]
        ).lower()
        if profile.data_structure == "event" or any(word in text for word in ("公告日", "发行日", "事件研究")):
            route = "market_event"
            goal = "causal"
            assumptions = ["事件窗口无其他重大混杂事件", "预期收益模型设定合理"]
        elif profile.data_structure == "spatial_panel" or any(word in text for word in ("空间溢出", "邻近地区")):
            route = "spatial"
            goal = "associational"
            assumptions = [
                "空间权重矩阵在查看结果前定义并冻结",
                "空间标识与权重矩阵行列一一对应",
                "没有额外外生识别时只解释为空间关联",
            ]
        elif (
            package.design_envelope is not None
            and package.design_envelope.research_goal == "mechanism"
        ):
            route = "mechanism_boundary"
            goal = "mechanism"
            assumptions = ["机制变量时间顺序合理", "机制结论不超过识别设计"]
        elif any(word in text for word in ("政策", "试验区", "指引", "试点", "did")):
            route = "policy_causal"
            goal = "causal"
            assumptions = ["平行趋势", "无预期效应", "不存在与处理同时发生的差异化冲击"]
        elif any(word in text for word in ("机制", "中介", "调节", "异质性")):
            route = "mechanism_boundary"
            goal = "mechanism"
            assumptions = ["机制变量时间顺序合理", "机制结论不超过识别设计"]
        elif any(word in text for word in ("指数", "效率", "sbm", "熵值")):
            route = "measurement_efficiency"
            goal = "measurement"
            assumptions = ["指标选择与权重规则预先确定"]
        elif any(word in text for word in ("dsge", "结构模型", "宏观模拟")):
            route = "structural_macro"
            goal = "structural"
            assumptions = ["结构参数可识别", "校准目标与数据矩一致"]
        elif profile.data_structure in ("panel", "cross_section"):
            route = "panel_association"
            goal = "associational"
            assumptions = ["固定效应和控制变量足以支持受限关联解释"]
        else:
            return MethodRoute(
                route_status="needs_human_review",
                research_goal="mixed",
                primary_route=None,
                route_reason=["研究目标与数据结构不足以唯一确定方法家族。"],
                required_assumptions=[],
                testable_assumptions=[],
                untestable_assumptions=[],
                alternative_routes=[],
                rejected_routes=[],
                missing_information=["请补充数据结构、分析层级或外生冲击信息。"],
            ).model_dump()
        return MethodRoute(
            route_status="routed",
            research_goal=goal,
            primary_route=route,
            route_reason=[f"研究目标与输入特征匹配 {route} 方法家族。"],
            required_assumptions=assumptions,
            testable_assumptions=assumptions[:2],
            untestable_assumptions=assumptions[2:],
            alternative_routes=(
                ["panel_association"] if route == "policy_causal" else []
            ),
            rejected_routes=[],
            missing_information=([] if package.dataset_refs else ["尚未提供可执行数据资产"]),
        ).model_dump()

    @staticmethod
    def _design(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        route = MethodRoute.model_validate(payload["method_route"])
        profile = DataProfile.model_validate(payload["data_profile"])
        family = route.primary_route
        if family is None:
            raise ValueError("cannot design without a routed method family")
        outcomes = _variables(package, "outcome")
        exposures = _variables(package, "treatment", "exposure")
        controls = _variables(package, "control")
        entity = _variables(package, "id")
        time = _variables(package, "time")
        strategy = str(payload.get("candidate_strategy", "direct_baseline"))
        candidate_id = str(payload.get("candidate_id", strategy))
        estimator = {
            "policy_causal": "DID / staggered-adoption DID（按政策实施方式确定）",
            "panel_association": "双向固定效应面板模型",
            "mechanism_boundary": "固定效应主模型 + 预注册机制/边界检验",
            "market_event": "事件研究",
            "spatial": "空间杜宾面板模型（由直接、间接和总效应目标确定）",
            "measurement_efficiency": "熵值法或 Super-SBM（按指标目标确定）",
            "structural_macro": "结构模型设计（高级分支）",
        }[family]
        if family == "spatial":
            estimator = {
                "direct_baseline": "空间杜宾面板模型（SDM）",
                "identification_first": "空间滞后面板模型（SAR）",
                "measurement_robustness": "空间误差面板模型（SEM）",
            }.get(strategy, estimator)
        diagnostics = [_planned("diag_data", "数据完整性与主键诊断", "确认样本和唯一键可执行")]
        if family == "policy_causal":
            diagnostics.extend(
                [
                    _planned("diag_parallel", "平行趋势与动态效应", "DID 的必要识别诊断"),
                    _planned("diag_anticipation", "预期效应检查", "排除政策前行为调整"),
                ]
            )
        if family == "spatial":
            diagnostics.append(_planned("diag_spatial", "空间相关诊断", "选择空间模型并检验权重矩阵敏感性"))
        formula = None
        if outcomes and exposures:
            formula = f"{outcomes[0]} ~ {' + '.join(exposures + controls)}"
        weights_ref = next(
            (
                item
                for item in package.dataset_refs
                if item.role == "supplementary"
                and item.filename.casefold() == "spatial_weights.csv"
            ),
            None,
        )
        spatial_keys = _variables(package, "spatial_id")
        model_parameters: dict[str, Any] = {}
        if family == "spatial" and weights_ref and spatial_keys:
            spatial_model = {
                "direct_baseline": "sdm",
                "identification_first": "sar",
                "measurement_robustness": "sem",
            }.get(strategy, "sdm")
            model_parameters = {
                "spatial_model": spatial_model,
                "spatial_weights_dataset_id": weights_ref.dataset_id,
                "spatial_weights_sha256": weights_ref.sha256,
                "spatial_id": spatial_keys[0],
            }
            if spatial_model == "sdm":
                model_parameters.update(
                    {
                        "spatially_lagged_covariates": [*exposures, *controls],
                        "effect_decomposition": ["direct", "indirect", "total"],
                    }
                )
        estimands = [
            _planned(
                "estimand_main",
                "核心估计对象",
                "对应 H1 的预先定义效应或关联参数",
            )
        ]
        if family == "spatial":
            estimands = [
                _planned("estimand_direct", "平均直接效应", "估计本地空间关联"),
                _planned("estimand_indirect", "平均间接效应", "估计跨地区空间关联"),
                _planned("estimand_total", "平均总效应", "汇总直接与间接空间关联"),
            ]
        return AnalysisPlan(
            plan_id=f"plan-{package.case_id}-{candidate_id}",
            plan_version=1,
            method_family=family,
            base_method_family="panel_association" if family == "mechanism_boundary" else None,
            design_only=not bool(package.dataset_refs),
            estimands=estimands,
            sample_rules=[_planned("sample_main", "冻结样本边界", "禁止观察结果后调整样本", period=package.sample_period)],
            variable_construction=[_planned("vars_main", "冻结变量口径", "使用案例包定义并记录全部变换")],
            baseline_models=[
                ModelSpec(
                    step_id="model_baseline",
                    name="基准模型",
                    rationale="对应主假设的首要模型",
                    estimator=estimator,
                    formula=formula,
                    outcome=outcomes[0] if outcomes else None,
                    treatments_or_exposures=exposures,
                    controls=controls,
                    fixed_effects=entity + time,
                    standard_error_strategy=(
                        "空间最大似然与 Delta 方法近似；边界解必须单独标记"
                        if family == "spatial"
                        else "按分析层级聚类；具体维度在 H2 前确认"
                    ),
                    parameters=model_parameters,
                )
            ],
            diagnostics=[
                *diagnostics,
                *(
                    [_planned("diag_identification", "识别威胁诊断", "优先检查竞争解释和识别条件")]
                    if strategy == "identification_first"
                    else []
                ),
                *(
                    [_planned("diag_measurement", "测量与缺失敏感性诊断", "优先检查变量口径和样本损失")]
                    if strategy == "measurement_robustness"
                    else []
                ),
            ],
            robustness_tests=[
                _planned("robust_alt_measure", "替代变量口径", "检验结论对测量选择的敏感性"),
                *(
                    [_planned("robust_missingness", "缺失样本敏感性", "检查样本筛选对结果的影响")]
                    if strategy == "measurement_robustness"
                    else []
                ),
            ],
            falsification_tests=[
                _planned("falsification_placebo", "安慰剂或伪处理", "排除机械相关和共同趋势"),
                *(
                    [_planned("falsification_competing", "竞争性解释检验", "优先排查替代识别解释")]
                    if strategy == "identification_first"
                    else []
                ),
            ],
            mechanism_tests=(
                [_planned("mechanism_predefined", "预注册机制检验", "机制证据不得写成已证明因果链")]
                if _variables(package, "mediator")
                else []
            ),
            heterogeneity_tests=[_planned("heterogeneity_predefined", "预定义异质性", "只允许理论事先支持的分组")],
            identification_assumptions=route.required_assumptions,
            alternative_explanations=["反向因果", "遗漏变量", "同期政策"],
            failure_conditions=["必要识别诊断失败", "核心变量无法按冻结口径构造"],
            stop_conditions=["完成预注册模型与诊断后停止，不按显著性追加模型"],
            required_data_fields=[variable.name for variable in package.variables],
            unsupported_requested_analyses=(
                ["当前未接入数据，所有统计分析均未执行"] if not package.dataset_refs else []
            ),
        ).model_dump()

    @staticmethod
    def _candidate_plan_batch(payload: dict[str, Any]) -> dict[str, Any]:
        strategies = payload.get("candidate_strategies") or payload.get("strategies")
        if not isinstance(strategies, list) or not 1 <= len(strategies) <= 2:
            raise ValueError("candidate plan batch requires one or two strategies")
        if len(strategies) != len(set(strategies)):
            raise ValueError("candidate plan batch strategies must be unique")
        return {
            "plans": [
                {
                    "strategy": strategy,
                    "plan": FixtureModelGateway._design(
                        {
                            **payload,
                            "candidate_strategy": strategy,
                            "candidate_id": f"candidate-{strategy}",
                        }
                    ),
                }
                for strategy in strategies
            ]
        }

    @staticmethod
    def _design_reviewer(payload: dict[str, Any]) -> dict[str, Any]:
        dimension = str(payload["dimension"])
        candidate_reviews: list[CandidateReview] = []
        for candidate in payload["candidates"]:
            probe = candidate["probe_report"]
            verdict = "reject" if probe["verdict"] == "fail" else "pass"
            candidate_reviews.append(
                CandidateReview(
                    candidate_id=candidate["candidate_id"],
                    verdict=verdict,
                    strengths=[f"{dimension} Reviewer 已核对该候选的结构化计划与 Probe。"],
                    issues=[],
                    required_follow_ups=(
                        ["先解决 Probe 中的硬失败再进入 H2。"]
                        if verdict == "reject"
                        else []
                    ),
                )
            )
        return DesignReviewerReport(
            report_id=f"design-review-{dimension}",
            dimension=dimension,
            reviewer_policy="fixture-isolated-context",
            candidate_reviews=candidate_reviews,
            remaining_risks=[],
        ).model_dump()

    @staticmethod
    def _reviewer_report_batch(payload: dict[str, Any]) -> dict[str, Any]:
        dimensions = payload.get("dimensions")
        if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 2:
            raise ValueError("reviewer report batch requires one or two dimensions")
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("reviewer report batch dimensions must be unique")
        return {
            "reports": [
                FixtureModelGateway._design_reviewer(
                    {**payload, "dimension": dimension}
                )
                for dimension in dimensions
            ]
        }

    @staticmethod
    def _critic(payload: dict[str, Any]) -> dict[str, Any]:
        plan = AnalysisPlan.model_validate(payload["analysis_plan"])
        dimension = payload.get("dimension", "reproducibility")
        remaining = []
        if plan.design_only:
            remaining.append("尚未接入数据，只能审查设计，不能确认可执行性。")
        return CriticReport(
            report_id=f"critic-{dimension}-{plan.plan_version}",
            review_round=max(1, plan.revision_round + 1),
            verdict="pass",
            issues=[],
            approved_elements=[f"{dimension} 维度未发现阻止 H2 的结构性问题。"],
            remaining_risks=remaining,
        ).model_dump()

    @staticmethod
    def _revise(payload: dict[str, Any]) -> dict[str, Any]:
        plan = AnalysisPlan.model_validate(payload["analysis_plan"])
        revised = plan.model_copy(deep=True)
        revised.plan_version += 1
        revised.revision_round += 1
        return revised.model_dump()

    @staticmethod
    def _assess(payload: dict[str, Any]) -> dict[str, Any]:
        run = ResearchRun.model_validate(payload["research_run"])
        if run.fixture_only or run.execution_status in ("not_executed", "fixture_only"):
            return EvidenceAssessment(
                evidence_status="not_tested",
                execution_status=run.execution_status,
                scientific_status="not_evaluated",
                supporting_run_ids=[],
                opposing_run_ids=[],
                limitations=[run.not_executed_reason or "未执行真实统计分析"],
            ).model_dump()
        return EvidenceAssessment(
            evidence_status="inconclusive",
            execution_status=run.execution_status,
            scientific_status=run.scientific_status,
            supporting_run_ids=[],
            opposing_run_ids=[],
            limitations=["需要由配置的模型网关解释真实执行记录。"],
        ).model_dump()

    @staticmethod
    def _evidence_claim_bundle(payload: dict[str, Any]) -> dict[str, Any]:
        assessment = FixtureModelGateway._assess(payload)
        ledger = FixtureModelGateway._claims(
            {**payload, "evidence_assessment": assessment}
        )
        return {
            "evidence_assessment": assessment,
            "candidate_claim_ledger": ledger,
        }

    @staticmethod
    def _audit(payload: dict[str, Any]) -> dict[str, Any]:
        assessment = EvidenceAssessment.model_validate(payload["evidence_assessment"])
        if assessment.evidence_status == "not_tested":
            return ScientificAudit(
                verdict="not_evaluated",
                contract_compliant=True,
                critical_issues=[],
                unresolved_risks=assessment.limitations,
            ).model_dump()
        return ScientificAudit(
            verdict="limited",
            contract_compliant=True,
            critical_issues=[],
            unresolved_risks=assessment.limitations,
        ).model_dump()

    @staticmethod
    def _claims(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        run = ResearchRun.model_validate(payload["research_run"])
        assessment = EvidenceAssessment.model_validate(payload["evidence_assessment"])
        no_evidence = run.fixture_only or assessment.evidence_status == "not_tested"
        return ClaimLedger(
            ledger_id=f"ledger-{run.research_run_id}",
            case_id=package.case_id,
            research_run_id=run.research_run_id,
            claims=[
                ClaimRecord(
                    claim_id=f"claim-{hypothesis.hypothesis_id}",
                    hypothesis_id=hypothesis.hypothesis_id,
                    claim_text=(
                        f"{hypothesis.statement}（尚未检验）"
                        if no_evidence
                        else hypothesis.statement
                    ),
                    evidence_status="not_tested" if no_evidence else assessment.evidence_status,
                    allowed_strength="prohibited" if no_evidence else "insufficient",
                    supporting_runs=[],
                    opposing_runs=[],
                    scope="冻结合同定义的样本、时期与变量口径",
                    robustness_status="not_executed" if no_evidence else "pending_review",
                    unresolved_risks=assessment.limitations,
                )
                for hypothesis in package.hypotheses
            ],
            excluded_findings=[],
            unresolved_issues=assessment.limitations,
        ).model_dump()

    @staticmethod
    def _write(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        plan = AnalysisPlan.model_validate(payload["analysis_plan"])
        run = ResearchRun.model_validate(payload["research_run"])
        approved_claims = payload.get("approved_claims", [])
        plan_md = "\n".join(
            [
                f"# {package.title}：科学假设与研究计划",
                "",
                f"## 研究问题\n{package.research_question}",
                "",
                "## 待检验假设",
                *[f"- {item.statement}" for item in package.hypotheses],
                "",
                f"## 方法路线\n{plan.method_family}",
                "",
                "## 基准设计",
                *[f"- {model.name}：{model.estimator}" for model in plan.baseline_models],
                "",
                "## 必要诊断与证伪",
                *[f"- {step.name}" for step in [*plan.diagnostics, *plan.falsification_tests]],
                "",
                "## 当前证据边界",
                "本 Run 未执行真实统计分析，不报告任何样本量、系数、显著性或实证结论。",
            ]
        )
        plan_only = run.fixture_only or run.execution_status in ("not_executed", "fixture_only")
        sections = [
            ManuscriptSection(
                section_id="research_plan",
                title="科学假设与研究计划",
                content_markdown=plan_md,
                status="generated",
            )
        ]
        if not plan_only:
            sections.append(
                ManuscriptSection(
                    section_id="approved_findings",
                    title="获批实证发现",
                    content_markdown="\n".join(
                        f"- {claim['final_text'] or claim['claim_text']}" for claim in approved_claims
                    ),
                    status="generated",
                    claim_ids=[claim["claim_id"] for claim in approved_claims],
                    run_ids=[run.research_run_id],
                )
            )
        return ManuscriptPackage(
            package_id=f"manuscript-{package.case_id}",
            case_id=package.case_id,
            mode="research_plan_only" if plan_only else "full_manuscript",
            status="ready_for_human_review",
            research_plan_markdown=plan_md,
            manuscript_sections=sections,
            empirical_findings_status="prohibited_fixture" if run.fixture_only else ("not_executed" if plan_only else "included"),
            disclosures=[
                "该成果由 HypoWeaver-Qwen 代码工作流生成。",
                "Fixture 模式仅验证流程，不构成实证研究结果。",
            ] if plan_only else ["所有实证表述仅来自 H3 授权结论。"],
            unresolved_issues=[] if approved_claims else ["当前没有可写入的获批实证结论。"],
        ).model_dump()

    @staticmethod
    def _write_draft_batch(payload: dict[str, Any]) -> dict[str, Any]:
        specs = payload.get("section_specs")
        if not isinstance(specs, list) or not 1 <= len(specs) <= 4:
            raise ValueError("manuscript draft batch requires one to four sections")
        sections = []
        for spec in specs:
            section_id = str(spec["section_id"])
            focus = str(spec.get("focus") or "忠实呈现冻结设计与证据边界")
            anchors = [
                f"[[STATEMENT:{statement_id}]]"
                for statement_id in spec.get("required_statement_ids", [])
            ]
            content = (
                f"{focus}。本节只使用输入中的安全叙述信息，"
                "区分已执行证据、未执行计划与仍未解决的风险，"
                "并保持获批结论的证据强度与适用范围。"
            ) * 8
            if anchors:
                content += "\n\n" + "\n".join(anchors)
            sections.append(
                {
                    "section_id": section_id,
                    "content_template": content,
                }
            )
        return {"sections": sections}


class QwenModelGateway:
    provider_name = "qwen"

    def __init__(
        self,
        model_override: str | None = None,
        *,
        budget: ModelCallBudget | None = None,
        config_store: RuntimeConfigStore | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        config = (config_store or RuntimeConfigStore()).resolve()
        if not config.qwen_api_key:
            raise RuntimeError(
                "Qwen API Key is required; configure runtime settings or DASHSCOPE_API_KEY"
            )
        self.model = model_override or config.qwen_model
        self.budget = budget or ModelCallBudget()
        self.retry_sleep = retry_sleep or asyncio.sleep
        self.http_client = httpx.AsyncClient(
            trust_env=urlsplit(config.qwen_base_url).hostname
            != "dashscope.aliyuncs.com"
        )
        self.client = AsyncOpenAI(
            api_key=config.qwen_api_key,
            base_url=config.qwen_base_url,
            http_client=self.http_client,
            max_retries=0,
        )

    async def generate(
        self,
        prompt_key: str,
        payload: dict[str, Any],
        output_model: type[OutputModel],
        *,
        call_context: ModelCallContext | None = None,
    ) -> OutputModel:
        prompt = get_prompt(prompt_key)
        context = call_context or ModelCallContext(
            call_group=prompt.call_group,
            prompt_key=prompt_key,
            max_attempts=prompt.max_attempts,
        )
        if context.prompt_key != prompt_key:
            raise ValueError("model call context prompt_key does not match the prompt")
        if context.call_group != prompt.call_group:
            raise ValueError("model call context call_group does not match the prompt")
        if context.max_attempts > prompt.max_attempts:
            raise ValueError("model call context exceeds the frozen prompt retry ceiling")
        messages = [
            {"role": item["role"], "content": item["rendered"]}
            for item in prompt.render(payload, output_model=output_model)
        ]
        input_sha256 = canonical_sha256(payload)
        output_schema = output_model.model_json_schema()
        output_schema_sha256 = canonical_sha256(output_schema)
        budget = getattr(self, "budget", None)
        if budget is None:
            budget = ModelCallBudget()
            self.budget = budget
        last_error: Exception | None = None
        first_attempt_index, next_attempt_type = budget.next_attempt(context)
        for attempt_index in range(first_attempt_index, context.max_attempts + 1):
            if next_attempt_type == "transport_retry":
                delay_seconds = MODEL_CALL_RETRY_BACKOFF_SECONDS.get(
                    attempt_index,
                    0.0,
                )
                if delay_seconds:
                    retry_sleep = getattr(self, "retry_sleep", asyncio.sleep)
                    await retry_sleep(delay_seconds)
            budget.reserve(context, attempt_index=attempt_index)
            started_at = utc_now()
            started = time.monotonic()
            try:
                stream = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body={"enable_thinking": False},
                    temperature=0,
                    max_tokens=prompt.max_tokens,
                    timeout=prompt.timeout_seconds,
                )
                response = await _aggregate_qwen_stream(stream)
            except asyncio.CancelledError as error:
                budget.record_failure(
                    error,
                    time.monotonic() - started,
                    provider=self.provider_name,
                    model=self.model,
                    started_at=started_at,
                    completed_at=utc_now(),
                    context=context,
                    attempt_index=attempt_index,
                    attempt_type=next_attempt_type,
                    outcome="transport_failure",
                    error_category=_model_call_error_category(error),
                    prompt_version=prompt.version,
                    input_sha256=input_sha256,
                    output_schema_sha256=output_schema_sha256,
                )
                raise
            except (
                APIConnectionError,
                APITimeoutError,
                httpx.TransportError,
            ) as error:
                budget.record_failure(
                    error,
                    time.monotonic() - started,
                    provider=self.provider_name,
                    model=self.model,
                    started_at=started_at,
                    completed_at=utc_now(),
                    context=context,
                    attempt_index=attempt_index,
                    attempt_type=next_attempt_type,
                    outcome="transport_failure",
                    error_category=_model_call_error_category(error),
                    prompt_version=prompt.version,
                    input_sha256=input_sha256,
                    output_schema_sha256=output_schema_sha256,
                )
                if attempt_index < context.max_attempts:
                    next_attempt_type = "transport_retry"
                    continue
                raise RuntimeError(
                    f"千问调用期间连接中断或超时，连续 "
                    f"{context.max_attempts} 次有界尝试均未恢复。"
                    "请检查网络/代理后重新启动本次研究；案例数据无需重新整理。"
                ) from error
            except APIStatusError as error:
                budget.record_failure(
                    error,
                    time.monotonic() - started,
                    provider=self.provider_name,
                    model=self.model,
                    started_at=started_at,
                    completed_at=utc_now(),
                    context=context,
                    attempt_index=attempt_index,
                    attempt_type=next_attempt_type,
                    outcome="provider_failure",
                    error_category=_model_call_error_category(error),
                    prompt_version=prompt.version,
                    input_sha256=input_sha256,
                    output_schema_sha256=output_schema_sha256,
                )
                retryable_status = error.status_code == 429 or (
                    500 <= error.status_code < 600
                )
                if retryable_status and attempt_index < context.max_attempts:
                    next_attempt_type = "transport_retry"
                    continue
                raise RuntimeError(
                    f"千问调用返回 HTTP {error.status_code}。请在配置页重新测试当前模型与 API 地址。"
                ) from error
            except Exception as error:
                budget.record_failure(
                    error,
                    time.monotonic() - started,
                    provider=self.provider_name,
                    model=self.model,
                    started_at=started_at,
                    completed_at=utc_now(),
                    context=context,
                    attempt_index=attempt_index,
                    attempt_type=next_attempt_type,
                    outcome="provider_failure",
                    error_category=_model_call_error_category(error),
                    prompt_version=prompt.version,
                    input_sha256=input_sha256,
                    output_schema_sha256=output_schema_sha256,
                )
                raise
            content = _provider_response_text(response)
            try:
                if content is None:
                    raise ValueError("malformed provider response")
                output = output_model.model_validate_json(content)
            except Exception as error:
                last_error = error
                schema_error_summary, schema_error_count = (
                    _safe_schema_error_details(
                        error,
                        output_schema=output_schema,
                    )
                )
                budget.record_response(
                    response,
                    time.monotonic() - started,
                    provider=self.provider_name,
                    model=self.model,
                    started_at=started_at,
                    completed_at=utc_now(),
                    context=context,
                    attempt_index=attempt_index,
                    attempt_type=next_attempt_type,
                    outcome="schema_failure",
                    error_type=type(error).__name__,
                    error_category="schema",
                    prompt_version=prompt.version,
                    input_sha256=input_sha256,
                    output_schema_sha256=output_schema_sha256,
                    schema_error_summary=tuple(schema_error_summary),
                    schema_error_count=schema_error_count,
                )
                if attempt_index < context.max_attempts:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一输出未通过已提供的 JSON Schema。"
                                "只修复结构，不改变研究判断。"
                                "\n错误: "
                                f"{_safe_schema_error_summary(schema_error_summary, schema_error_count)}"
                            ),
                        }
                    )
                    next_attempt_type = "schema_repair"
                    continue
                break
            budget.record_response(
                response,
                time.monotonic() - started,
                provider=self.provider_name,
                model=self.model,
                started_at=started_at,
                completed_at=utc_now(),
                context=context,
                attempt_index=attempt_index,
                attempt_type=next_attempt_type,
                outcome="succeeded",
                prompt_version=prompt.version,
                input_sha256=input_sha256,
                output_schema_sha256=output_schema_sha256,
            )
            return output
        error_type = type(last_error).__name__ if last_error else "UnknownError"
        raise ValueError(
            f"qwen output failed schema validation ({error_type})"
        )


class FixtureExecutor:
    executor_name = "fixture"

    async def execute(self, contract: FormalResearchContract) -> ResearchRun:
        plan = contract.approved_plan
        planned = [
            *[("baseline", item) for item in plan.baseline_models],
            *[("diagnostic", item) for item in plan.diagnostics],
            *[("robustness", item) for item in plan.robustness_tests],
            *[("falsification", item) for item in plan.falsification_tests],
            *[("mechanism", item) for item in plan.mechanism_tests],
            *[("heterogeneity", item) for item in plan.heterogeneity_tests],
        ]
        if not planned:
            planned = [
                (
                    "baseline",
                    PlannedStep(
                        step_id="model_baseline",
                        name="未冻结基准模型",
                        rationale="Fixture 仅验证工作流边界。",
                    ),
                )
            ]
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=contract.approved_plan.plan_version,
            execution_status="fixture_only",
            scientific_status="not_evaluated",
            fixture_only=True,
            not_executed_reason="Fixture Executor 只验证工作流状态与接口，未执行任何统计模型。",
            executions=[
                ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type=run_type,
                    plan_step_id=step.step_id,
                    execution_status="not_executed",
                    estimates=[],
                    diagnostic_results={},
                    warnings=["Fixture 未执行任何统计步骤。"],
                    check_id=step.step_id,
                    not_executed_reason_code="fixture_only",
                )
                for run_type, step in planned
            ],
            warnings=["Fixture 结果不得进入实证论文结论。"],
        )


class HttpResearchExecutor:
    executor_name = "external"

    def __init__(self, config_store: RuntimeConfigStore | None = None) -> None:
        config = (config_store or RuntimeConfigStore()).resolve()
        if not config.research_engine_url:
            raise RuntimeError(
                "Python Research Engine URL is required; configure runtime settings or RESEARCH_ENGINE_URL"
            )
        self.url = config.research_engine_url
        self.token = config.research_engine_token

    async def execute(self, contract: FormalResearchContract) -> ResearchRun:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        trust_env = urlsplit(self.url).hostname not in {"127.0.0.1", "localhost", "::1"}
        wall_time = float(contract.budget.max_wall_time_seconds)
        async with asyncio.timeout(wall_time):
            async with httpx.AsyncClient(
                timeout=_research_http_timeout(wall_time),
                trust_env=trust_env,
            ) as client:
                response = await client.post(
                    f"{self.url.rstrip('/')}/v1/runs",
                    json={"contract": contract.model_dump(mode="json")},
                    headers=headers,
                )
                response.raise_for_status()
            return ResearchRun.model_validate(response.json())


class HttpResearchReproducer:
    reproducer_name = "external-independent"

    def __init__(self, config_store: RuntimeConfigStore | None = None) -> None:
        config = (config_store or RuntimeConfigStore()).resolve()
        if not config.research_engine_url:
            raise RuntimeError(
                "Python Research Engine URL is required; configure runtime settings or RESEARCH_ENGINE_URL"
            )
        self.url = config.research_engine_url
        self.token = config.research_engine_token

    async def execute(self, contract: FormalResearchContract) -> ResearchRun:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        trust_env = urlsplit(self.url).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        wall_time = float(contract.budget.max_wall_time_seconds)
        async with asyncio.timeout(wall_time):
            async with httpx.AsyncClient(
                timeout=_research_http_timeout(wall_time),
                trust_env=trust_env,
            ) as client:
                response = await client.post(
                    f"{self.url.rstrip('/')}/v1/reproductions",
                    json={"contract": contract.model_dump(mode="json")},
                    headers=headers,
                )
                response.raise_for_status()
            return ResearchRun.model_validate(response.json())


def _research_http_timeout(wall_time_seconds: float) -> httpx.Timeout:
    """Let the frozen contract own read/write/overall time; cap only connect."""

    return httpx.Timeout(
        connect=min(10.0, wall_time_seconds),
        read=wall_time_seconds,
        write=wall_time_seconds,
        pool=wall_time_seconds,
    )
