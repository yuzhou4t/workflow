from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ConfigDict, ValidationError

from hypoweaver.adapters import (
    MODEL_CALL_GROUP_LIMITS,
    V2_LOGICAL_CALL_BUDGET,
    V2_PROVIDER_ATTEMPT_BUDGET,
    ModelCallBudget,
    QwenModelGateway,
)
from hypoweaver.engine import _gather_llm_batches_to_terminal
from hypoweaver.models import (
    AnalysisPlan,
    CandidatePlanBatch,
    CandidateReview,
    ClaimLedger,
    ClaimRecord,
    DesignReviewerReport,
    EvidenceAssessment,
    EvidenceClaimBundle,
    ManuscriptSectionDraftBatch,
    ModelCallContext,
    ModelCallReceipt,
    ReviewerReportBatch,
)
from hypoweaver.prompts import get_prompt
from hypoweaver.seal import canonical_sha256


class _TinyOutput(BaseModel):
    value: int


class _StrictTinyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _AsyncChunkStream:
    def __init__(
        self,
        chunks: list[SimpleNamespace | BaseException],
    ) -> None:
        self._chunks = chunks
        self._index = 0

    def __aiter__(self) -> _AsyncChunkStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


def _stream_response(
    content: str | None,
    *,
    response_id: str = "response-valid",
    prompt_tokens: int = 6,
    completion_tokens: int = 3,
) -> _AsyncChunkStream:
    chunks: list[SimpleNamespace] = []
    if content is not None:
        midpoint = max(len(content) // 2, 1)
        for part in (content[:midpoint], content[midpoint:]):
            if part:
                chunks.append(
                    SimpleNamespace(
                        id=response_id,
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=part)
                            )
                        ],
                        usage=None,
                    )
                )
    chunks.append(
        SimpleNamespace(
            id=response_id,
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )
    )
    return _AsyncChunkStream(chunks)


def _mock_qwen_gateway(
    create_completion: AsyncMock,
    *,
    budget: ModelCallBudget | None = None,
    retry_sleep: AsyncMock | None = None,
) -> QwenModelGateway:
    gateway = QwenModelGateway.__new__(QwenModelGateway)
    gateway.model = "qwen-test"
    gateway.budget = budget if budget is not None else ModelCallBudget()
    gateway.retry_sleep = retry_sleep if retry_sleep is not None else AsyncMock()
    gateway.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_completion)
        )
    )
    return gateway


def _plan(strategy: str) -> dict:
    return AnalysisPlan(
        plan_id=f"plan-{strategy}",
        plan_version=1,
        method_family="panel_association",
        design_only=False,
        estimands=[],
        sample_rules=[],
        variable_construction=[],
        baseline_models=[],
        diagnostics=[],
        robustness_tests=[],
        falsification_tests=[],
        mechanism_tests=[],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=[],
        unsupported_requested_analyses=[],
    ).model_dump(mode="json")


def _review(dimension: str, candidate_ids: tuple[str, ...]) -> DesignReviewerReport:
    return DesignReviewerReport(
        report_id=f"review-{dimension}",
        dimension=dimension,
        reviewer_policy="isolated-context",
        candidate_reviews=[
            CandidateReview(candidate_id=candidate_id, verdict="pass")
            for candidate_id in candidate_ids
        ],
    )


def _claim(claim_id: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        hypothesis_id="H1",
        claim_text="x 与 y 呈条件关联。",
        evidence_status="inconclusive",
        allowed_strength="insufficient",
        supporting_runs=[],
        opposing_runs=[],
        scope="frozen scope",
        robustness_status="pending_review",
        unresolved_risks=[],
    )


class BatchModelTests(unittest.TestCase):
    def test_candidate_batches_support_one_or_two_unique_fixed_strategies(self) -> None:
        one = CandidatePlanBatch.model_validate(
            {
                "plans": [
                    {
                        "strategy": "direct_baseline",
                        "plan": _plan("direct"),
                    }
                ]
            }
        )
        self.assertEqual(len(one.plans), 1)

        two = CandidatePlanBatch.model_validate(
            {
                "plans": [
                    {
                        "strategy": "identification_first",
                        "plan": _plan("identification"),
                    },
                    {
                        "strategy": "measurement_robustness",
                        "plan": _plan("measurement"),
                    },
                ]
            }
        )
        self.assertEqual(len(two.plans), 2)

        with self.assertRaisesRegex(ValidationError, "duplicate strategies"):
            CandidatePlanBatch.model_validate(
                {
                    "plans": [
                        {
                            "strategy": "direct_baseline",
                            "plan": _plan("one"),
                        },
                        {
                            "strategy": "direct_baseline",
                            "plan": _plan("two"),
                        },
                    ]
                }
            )

    def test_reviewer_batches_support_partial_recovery_without_merging_dimensions(self) -> None:
        partial = ReviewerReportBatch(
            reports=[_review("measurement", ("candidate-a", "candidate-b"))]
        )
        self.assertEqual(len(partial.reports), 1)

        paired = ReviewerReportBatch(
            reports=[
                _review("causal", ("candidate-a", "candidate-b")),
                _review("statistical", ("candidate-a", "candidate-b")),
            ]
        )
        self.assertEqual(len(paired.reports), 2)

        with self.assertRaisesRegex(ValidationError, "duplicate dimensions"):
            ReviewerReportBatch(
                reports=[
                    _review("causal", ("candidate-a",)),
                    _review("causal", ("candidate-a",)),
                ]
            )
        with self.assertRaisesRegex(ValidationError, "same non-empty candidate set"):
            ReviewerReportBatch(
                reports=[
                    _review("causal", ("candidate-a",)),
                    _review("statistical", ("candidate-b",)),
                ]
            )

    def test_evidence_claim_and_manuscript_batches_reject_duplicate_ids(self) -> None:
        assessment = EvidenceAssessment(
            evidence_status="inconclusive",
            execution_status="succeeded",
            scientific_status="limited",
            supporting_run_ids=[],
            opposing_run_ids=[],
            limitations=[],
        )
        bundle = EvidenceClaimBundle(
            evidence_assessment=assessment,
            candidate_claim_ledger=ClaimLedger(
                ledger_id="ledger",
                case_id="case",
                research_run_id="run",
                claims=[_claim("claim-H1")],
                excluded_findings=[],
                unresolved_issues=[],
            ),
        )
        self.assertEqual(bundle.candidate_claim_ledger.claims[0].claim_id, "claim-H1")

        with self.assertRaisesRegex(ValidationError, "duplicate claim ids"):
            EvidenceClaimBundle(
                evidence_assessment=assessment,
                candidate_claim_ledger=ClaimLedger(
                    ledger_id="ledger",
                    case_id="case",
                    research_run_id="run",
                    claims=[_claim("claim-H1"), _claim("claim-H1")],
                    excluded_findings=[],
                    unresolved_issues=[],
                ),
            )

        drafts = ManuscriptSectionDraftBatch(
            sections=[
                {"section_id": "abstract", "content_template": "模板"},
                {"section_id": "introduction", "content_template": "模板"},
            ]
        )
        self.assertEqual(len(drafts.sections), 2)
        with self.assertRaisesRegex(ValidationError, "duplicate section ids"):
            ManuscriptSectionDraftBatch(
                sections=[
                    {"section_id": "abstract", "content_template": "模板一"},
                    {"section_id": "abstract", "content_template": "模板二"},
                ]
            )

    def test_prompt_first_render_contains_compact_full_schema_and_call_policy(self) -> None:
        prompt = get_prompt("evidence_claim_bundle")
        rendered = prompt.render({"input": "safe"})[0]["rendered"]

        self.assertIn('"call_group":"h3"', rendered)
        self.assertIn('"max_provider_attempts":3', rendered)
        schema = rendered.split("必须完整满足以下压缩 JSON Schema：", 1)[1]
        self.assertNotIn("\n", schema)
        self.assertIn('"candidate_claim_ledger"', schema)
        self.assertIn('"evidence_assessment"', schema)
        self.assertEqual(prompt.call_policy()["call_group"], "h3")


class ModelCallBudgetTests(unittest.TestCase):
    @staticmethod
    def _required_contexts() -> list[ModelCallContext]:
        return [
            ModelCallContext(
                logical_call_id=f"required-{group}-{index}",
                call_group=group,
                prompt_key="legacy",
            )
            for group, count in (("h1_h2", 5), ("h3", 2), ("h4", 2))
            for index in range(count)
        ]

    def test_group_limits_are_enforced_with_shared_retry_capacity(self) -> None:
        self.assertEqual(MODEL_CALL_GROUP_LIMITS, {"h1_h2": 10, "h3": 4, "h4": 6})
        budget = ModelCallBudget()
        contexts = self._required_contexts()
        for context in contexts:
            budget.reserve(context, attempt_index=1)
        for context in contexts:
            budget.reserve(context, attempt_index=2)
        for context in contexts[-2:]:
            budget.reserve(context, attempt_index=3)

        snapshot = budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 20)
        self.assertEqual(snapshot["provider_attempts"], 20)
        self.assertEqual(snapshot["logical_calls"], 9)
        self.assertEqual(snapshot["group_limits"], MODEL_CALL_GROUP_LIMITS)
        self.assertEqual(snapshot["group_usage"], MODEL_CALL_GROUP_LIMITS)
        policy = snapshot["shared_retry_policy"]
        self.assertEqual(policy["version"], "shared-retry-v1")
        self.assertEqual(
            policy["mode"],
            "global_shared_retry_pool_with_group_caps",
        )
        self.assertTrue(policy["legacy_group_limits_enforced"])
        self.assertEqual(policy["required_first_calls"], 9)
        self.assertEqual(policy["shared_retry_capacity"], 11)
        self.assertEqual(policy["shared_retry_used"], 11)
        self.assertEqual(policy["shared_retry_remaining"], 0)
        with self.assertRaisesRegex(RuntimeError, "20/20"):
            budget.reserve(contexts[-1], attempt_index=4)

    def test_v2_separates_provider_attempts_from_logical_group_slots(self) -> None:
        budget = ModelCallBudget(budget_mode="v2")
        contexts = [
            ModelCallContext(
                logical_call_id=f"v2-{group}-{index}",
                call_group=group,
                prompt_key="legacy",
            )
            for group, count in (("h1_h2", 10), ("h3", 4), ("h4", 6))
            for index in range(count)
        ]
        for context in contexts:
            budget.reserve(context, attempt_index=1)
        for context in contexts:
            budget.reserve(context, attempt_index=2)

        snapshot = budget.snapshot()
        self.assertEqual(snapshot["budget_mode"], "v2")
        self.assertEqual(
            snapshot["provider_attempt_ceiling"],
            V2_PROVIDER_ATTEMPT_BUDGET,
        )
        self.assertEqual(
            snapshot["logical_call_ceiling"],
            V2_LOGICAL_CALL_BUDGET,
        )
        self.assertEqual(snapshot["provider_attempts"], 40)
        self.assertEqual(snapshot["logical_calls"], 20)
        self.assertEqual(snapshot["group_usage"], MODEL_CALL_GROUP_LIMITS)
        self.assertEqual(snapshot["group_counting_unit"], "logical_call")
        with self.assertRaisesRegex(RuntimeError, "40/40"):
            budget.reserve(contexts[0], attempt_index=3)

    def test_v2_retry_does_not_consume_another_logical_group_slot(self) -> None:
        budget = ModelCallBudget(budget_mode="v2")
        contexts = self._required_contexts()
        for context in contexts:
            budget.reserve(context, attempt_index=1)
        budget.reserve(contexts[0], attempt_index=2)
        budget.reserve(contexts[0], attempt_index=3)

        snapshot = budget.snapshot()
        self.assertEqual(snapshot["provider_attempts"], 11)
        self.assertEqual(snapshot["logical_calls"], 9)
        self.assertEqual(
            snapshot["group_usage"],
            {"h1_h2": 5, "h3": 2, "h4": 2},
        )
        self.assertEqual(snapshot["logical_call_attempts"][contexts[0].logical_call_id], 3)

    def test_group_limit_blocks_further_attempts(self) -> None:
        budget = ModelCallBudget()
        required = self._required_contexts()
        for item in required:
            budget.reserve(item, attempt_index=1)
        for item in required[5:7]:
            budget.reserve(item, attempt_index=2)
        with self.assertRaisesRegex(RuntimeError, "h3: 4/4"):
            budget.reserve(required[5], attempt_index=3)
        self.assertEqual(budget.snapshot()["group_usage"]["h3"], 4)

    def test_group_retries_preserve_its_unstarted_required_first_calls(self) -> None:
        budget = ModelCallBudget()
        required = self._required_contexts()
        for context in required[:2]:
            for attempt_index in range(1, 4):
                budget.reserve(context, attempt_index=attempt_index)
        budget.reserve(required[2], attempt_index=1)
        budget.reserve(required[2], attempt_index=2)

        with self.assertRaisesRegex(RuntimeError, "h1_h2 保留 2"):
            budget.reserve(required[2], attempt_index=3)
        self.assertEqual(budget.snapshot()["group_usage"]["h1_h2"], 8)

        for context in required[3:5]:
            budget.reserve(context, attempt_index=1)
        self.assertEqual(budget.snapshot()["group_usage"]["h1_h2"], 10)

    def test_logical_attempt_limit_still_blocks_within_group_ceiling(self) -> None:
        budget = ModelCallBudget()
        required = self._required_contexts()
        for item in required:
            budget.reserve(item, attempt_index=1)
        context = required[-1]
        self.assertEqual(budget.reserve(context, attempt_index=2), 2)
        self.assertEqual(budget.reserve(context, attempt_index=3), 3)
        with self.assertRaisesRegex(RuntimeError, "最多 3 次"):
            budget.reserve(context, attempt_index=4)

    def test_concurrent_reservations_cannot_exceed_group_budget(self) -> None:
        budget = ModelCallBudget()
        for context in self._required_contexts():
            budget.reserve(context)

        def reserve(index: int) -> bool:
            try:
                budget.reserve(
                    ModelCallContext(
                        logical_call_id=f"concurrent-{index}",
                        call_group="h1_h2",
                        prompt_key="candidate_plan_batch",
                    )
                )
            except RuntimeError:
                return False
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(reserve, range(32)))

        self.assertEqual(sum(results), 5)
        self.assertEqual(budget.llm_calls, 14)
        self.assertEqual(budget.snapshot()["group_usage"]["h1_h2"], 10)

    def test_concurrent_group_caps_remain_compatible_with_global_budget(self) -> None:
        budget = ModelCallBudget()
        for context in self._required_contexts():
            budget.reserve(context)
        candidates = [
            ModelCallContext(
                logical_call_id=f"concurrent-{group}-{index}",
                call_group=group,
                prompt_key="legacy",
            )
            for group, count in (("h1_h2", 12), ("h3", 12), ("h4", 12))
            for index in range(count)
        ]

        def reserve(context: ModelCallContext) -> bool:
            try:
                budget.reserve(context)
            except RuntimeError:
                return False
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(reserve, candidates))

        self.assertEqual(sum(results), 11)
        self.assertEqual(budget.llm_calls, 20)
        self.assertEqual(budget.snapshot()["group_usage"], MODEL_CALL_GROUP_LIMITS)

    def test_replacement03_trajectory_cannot_borrow_h3_h4_group_slots(self) -> None:
        budget = ModelCallBudget()
        contexts = [
            ModelCallContext(
                logical_call_id=f"required-{index}",
                call_group="h1_h2",
                prompt_key="legacy",
            )
            for index in range(5)
        ]
        budget.reserve(contexts[0], attempt_index=1)
        budget.reserve(contexts[1], attempt_index=1)
        for attempt in range(1, 4):
            budget.reserve(contexts[2], attempt_index=attempt)
        for attempt in range(1, 4):
            budget.reserve(contexts[3], attempt_index=attempt)
        for attempt in range(1, 3):
            budget.reserve(contexts[4], attempt_index=attempt)

        before = budget.snapshot()
        self.assertEqual(before["llm_calls"], 10)
        self.assertEqual(
            before["shared_retry_policy"][
                "reserved_for_unstarted_by_group"
            ],
            {"h1_h2": 0, "h3": 2, "h4": 2},
        )
        self.assertEqual(
            before["group_usage"]["h1_h2"],
            MODEL_CALL_GROUP_LIMITS["h1_h2"],
        )
        with self.assertRaisesRegex(RuntimeError, "h1_h2: 10/10"):
            budget.reserve(contexts[4], attempt_index=3)

        future = self._required_contexts()[5:]
        for context in future:
            budget.reserve(context, attempt_index=1)
        self.assertEqual(budget.llm_calls, 14)
        self.assertEqual(
            budget.snapshot()["shared_retry_policy"][
                "reserved_for_unstarted_required_first_calls"
            ],
            0,
        )

    def test_extra_h1_logical_ids_do_not_cover_future_h3_h4_first_calls(self) -> None:
        budget = ModelCallBudget(max_calls=10)
        h1_contexts = self._required_contexts()[:5]
        for context in h1_contexts:
            budget.reserve(context)
        budget.reserve(
            ModelCallContext(
                logical_call_id="extra-h1-call",
                call_group="h1_h2",
                prompt_key="legacy",
            )
        )
        policy = budget.snapshot()["shared_retry_policy"]
        self.assertEqual(
            policy["reserved_for_unstarted_by_group"],
            {"h1_h2": 0, "h3": 2, "h4": 2},
        )
        with self.assertRaisesRegex(RuntimeError, "全局保留 4"):
            budget.reserve(
                ModelCallContext(
                    logical_call_id="another-extra-h1-call",
                    call_group="h1_h2",
                    prompt_key="legacy",
                )
            )

    def test_reduced_round_limit_preserves_all_nine_first_calls(self) -> None:
        budget = ModelCallBudget(max_calls=9)
        first = ModelCallContext(
            logical_call_id="first-required",
            call_group="h1_h2",
            prompt_key="hypothesis_decomposition",
        )
        budget.reserve(first, attempt_index=1)
        with self.assertRaisesRegex(RuntimeError, "全部尚未启动"):
            budget.reserve(first, attempt_index=2)

        for index in range(1, 9):
            group = "h1_h2" if index < 5 else "h3" if index < 7 else "h4"
            budget.reserve(
                ModelCallContext(
                    logical_call_id=f"required-slot-{index}",
                    call_group=group,
                    prompt_key="legacy",
                )
            )
        self.assertEqual(budget.llm_calls, 9)

    def test_content_repair_can_follow_success_but_still_stops_at_attempt_three(
        self,
    ) -> None:
        budget = ModelCallBudget()
        initial = ModelCallContext(
            logical_call_id="writer-batch-1",
            call_group="h4",
            prompt_key="manuscript_section_draft_batch",
        )
        repair = initial.model_copy(update={"attempt_type": "content_repair"})

        def record_success(context: ModelCallContext, attempt_index: int) -> None:
            budget.record_response(
                SimpleNamespace(
                    id=f"response-{attempt_index}",
                    content='{"sections":[]}',
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                    ),
                ),
                0.0,
                provider="qwen",
                model="qwen-test",
                started_at="2026-07-17T00:00:00+00:00",
                completed_at="2026-07-17T00:00:01+00:00",
                context=context,
                attempt_index=attempt_index,
                attempt_type=context.attempt_type,
            )

        budget.reserve(initial, attempt_index=1)
        record_success(initial, 1)
        with self.assertRaisesRegex(RuntimeError, "已成功"):
            budget.next_attempt(initial)

        self.assertEqual(budget.next_attempt(repair), (2, "content_repair"))
        budget.reserve(repair, attempt_index=2)
        record_success(repair, 2)
        self.assertEqual(budget.next_attempt(repair), (3, "content_repair"))
        budget.reserve(repair, attempt_index=3)
        record_success(repair, 3)

        with self.assertRaisesRegex(RuntimeError, "最多 3 次"):
            budget.next_attempt(repair)
        self.assertEqual(
            [item["attempt_type"] for item in budget.call_receipts],
            ["primary", "content_repair", "content_repair"],
        )

    def test_legacy_receipt_defaults_are_safe(self) -> None:
        receipt = ModelCallReceipt.model_validate(
            {
                "provider": "qwen",
                "model": "legacy-model",
                "started_at": "2026-07-16T00:00:00+00:00",
                "completed_at": "2026-07-16T00:00:01+00:00",
                "response_sha256": "a" * 64,
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        self.assertEqual(receipt.prompt_version, "legacy")
        self.assertEqual(receipt.attempt_type, "legacy")
        self.assertEqual(receipt.input_sha256, "0" * 64)
        self.assertEqual(receipt.output_schema_sha256, "0" * 64)
        self.assertIsNone(receipt.error_category)
        self.assertEqual(receipt.schema_error_summary, [])
        self.assertEqual(receipt.schema_error_count, 0)


class ParallelBatchDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_global_slot_waits_for_inflight_sibling_receipt(self) -> None:
        budget = ModelCallBudget()
        contexts = ModelCallBudgetTests._required_contexts()

        def record_attempt(context: ModelCallContext, attempt_index: int) -> None:
            budget.reserve(context, attempt_index=attempt_index)
            budget.record_failure(
                RuntimeError("fixture transport failure"),
                0.0,
                provider="qwen",
                model="fixture-no-network",
                started_at="2026-07-17T00:00:00+00:00",
                completed_at="2026-07-17T00:00:00+00:00",
                context=context,
                attempt_index=attempt_index,
                attempt_type=(
                    "primary" if attempt_index == 1 else "transport_retry"
                ),
            )

        for context in contexts:
            record_attempt(context, 1)
            record_attempt(context, 2)
        record_attempt(contexts[7], 3)

        last_slot_reserved = asyncio.Event()
        blocked_attempt_observed = asyncio.Event()

        async def inflight_last_slot() -> None:
            budget.reserve(contexts[8], attempt_index=3)
            last_slot_reserved.set()
            await blocked_attempt_observed.wait()
            budget.record_failure(
                RuntimeError("fixture final transport failure"),
                0.0,
                provider="qwen",
                model="fixture-no-network",
                started_at="2026-07-17T00:00:00+00:00",
                completed_at="2026-07-17T00:00:00+00:00",
                context=contexts[8],
                attempt_index=3,
                attempt_type="transport_retry",
            )

        async def blocked_sibling() -> None:
            await last_slot_reserved.wait()
            try:
                budget.reserve(contexts[0], attempt_index=3)
            finally:
                blocked_attempt_observed.set()

        inflight = asyncio.create_task(inflight_last_slot())
        blocked = asyncio.create_task(blocked_sibling())
        with self.assertRaisesRegex(RuntimeError, "20/20"):
            await _gather_llm_batches_to_terminal(inflight, blocked)

        snapshot = budget.snapshot()
        self.assertTrue(inflight.done())
        self.assertTrue(blocked.done())
        self.assertEqual(snapshot["llm_calls"], 20)
        self.assertEqual(len(snapshot["call_receipts"]), 20)


class QwenGatewayReceiptTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _status_error(status_code: int) -> APIStatusError:
        request = httpx.Request(
            "POST",
            "https://qwen.example.test/v1/chat/completions",
        )
        return APIStatusError(
            f"status {status_code}",
            response=httpx.Response(status_code, request=request),
            body=None,
        )

    async def test_retryable_provider_status_counts_every_attempt(self) -> None:
        retry_sleep = AsyncMock()
        create_completion = AsyncMock(
            side_effect=[
                self._status_error(429),
                self._status_error(503),
                _stream_response('{"value":7}'),
            ]
        )
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )

        output = await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        self.assertEqual(output.value, 7)
        receipts = gateway.budget.snapshot()["call_receipts"]
        self.assertEqual(len(receipts), gateway.budget.llm_calls)
        self.assertEqual(
            [item["outcome"] for item in receipts],
            ["provider_failure", "provider_failure", "succeeded"],
        )
        self.assertEqual(
            [item["error_category"] for item in receipts],
            ["http_status", "http_status", None],
        )
        self.assertEqual(
            retry_sleep.await_args_list,
            [call(2.0), call(8.0)],
        )
        first_call = create_completion.await_args_list[0].kwargs
        self.assertTrue(first_call["stream"])
        self.assertEqual(first_call["stream_options"], {"include_usage": True})

    async def test_two_transport_failures_use_third_attempt_and_succeed(self) -> None:
        transport_error = APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://qwen.example.test/v1/chat/completions",
            )
        )
        retry_sleep = AsyncMock()
        create_completion = AsyncMock(
            side_effect=[
                transport_error,
                transport_error,
                _stream_response('{"value":7}'),
            ]
        )
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )

        output = await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        self.assertEqual(output.value, 7)
        self.assertEqual(create_completion.await_count, 3)
        receipts = gateway.budget.snapshot()["call_receipts"]
        self.assertEqual(
            [item["attempt_type"] for item in receipts],
            ["primary", "transport_retry", "transport_retry"],
        )
        self.assertEqual(
            [item["outcome"] for item in receipts],
            ["transport_failure", "transport_failure", "succeeded"],
        )
        self.assertEqual(
            [item["error_category"] for item in receipts],
            ["unknown_transport", "unknown_transport", None],
        )
        self.assertEqual(retry_sleep.await_args_list, [call(2.0), call(8.0)])

    async def test_streaming_json_aggregates_content_usage_and_response_id(self) -> None:
        create_completion = AsyncMock(
            return_value=_stream_response(
                '{"value":7}',
                response_id="stream-response-id",
                prompt_tokens=11,
                completion_tokens=4,
            )
        )
        gateway = _mock_qwen_gateway(create_completion)

        output = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
        )

        self.assertEqual(output.value, 7)
        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["input_tokens"], 11)
        self.assertEqual(snapshot["output_tokens"], 4)
        receipt = snapshot["call_receipts"][0]
        self.assertEqual(
            receipt["response_sha256"],
            hashlib.sha256(b'{"value":7}').hexdigest(),
        )
        self.assertEqual(
            receipt["provider_response_id_sha256"],
            canonical_sha256("stream-response-id"),
        )
        request = create_completion.await_args.kwargs
        self.assertTrue(request["stream"])
        self.assertEqual(request["stream_options"], {"include_usage": True})

    async def test_midstream_connection_error_discards_partial_text_and_retries(self) -> None:
        raw_secret = "PARTIAL-CONTENT-MUST-NOT-PERSIST"
        stream_error = APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://qwen.example.test/v1/chat/completions",
            )
        )
        interrupted = _AsyncChunkStream(
            [
                SimpleNamespace(
                    id="interrupted-response",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=raw_secret)
                        )
                    ],
                    usage=None,
                ),
                stream_error,
            ]
        )
        create_completion = AsyncMock(
            side_effect=[interrupted, _stream_response('{"value":7}')]
        )
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )

        output = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
        )

        self.assertEqual(output.value, 7)
        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 2)
        self.assertEqual(
            [item["outcome"] for item in snapshot["call_receipts"]],
            ["transport_failure", "succeeded"],
        )
        first = snapshot["call_receipts"][0]
        self.assertEqual(first["input_tokens"], 0)
        self.assertEqual(first["output_tokens"], 0)
        self.assertIsNone(first["provider_response_id_sha256"])
        self.assertNotIn(raw_secret, str(snapshot))
        retry_sleep.assert_awaited_once_with(2.0)

    async def test_midstream_remote_protocol_error_uses_transport_retry(self) -> None:
        interrupted = _AsyncChunkStream(
            [httpx.RemoteProtocolError("peer closed stream")]
        )
        create_completion = AsyncMock(
            side_effect=[interrupted, _stream_response('{"value":7}')]
        )
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )

        output = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
        )

        self.assertEqual(output.value, 7)
        receipts = gateway.budget.snapshot()["call_receipts"]
        self.assertEqual(receipts[0]["outcome"], "transport_failure")
        self.assertEqual(receipts[0]["error_category"], "unknown_transport")
        retry_sleep.assert_awaited_once_with(2.0)

    async def test_stream_without_final_usage_does_not_invent_exact_tokens(self) -> None:
        raw_secret = "PARTIAL-WITHOUT-USAGE-MUST-NOT-PERSIST"
        incomplete = _AsyncChunkStream(
            [
                SimpleNamespace(
                    id="incomplete-response",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=raw_secret)
                        )
                    ],
                    usage=None,
                )
            ]
        )
        gateway = _mock_qwen_gateway(
            AsyncMock(return_value=incomplete)
        )

        with self.assertRaisesRegex(ValueError, "omitted usage"):
            await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(snapshot["input_tokens"], 0)
        self.assertEqual(snapshot["output_tokens"], 0)
        receipt = snapshot["call_receipts"][0]
        self.assertEqual(receipt["input_tokens"], 0)
        self.assertEqual(receipt["output_tokens"], 0)
        self.assertIsNone(receipt["provider_response_id_sha256"])
        self.assertNotIn(raw_secret, str(snapshot))

    async def test_interrupted_logical_call_resumes_at_next_attempt(self) -> None:
        context = ModelCallContext(
            logical_call_id="durable-interrupted-call",
            call_group="h1_h2",
            prompt_key="intake",
        )
        budget = ModelCallBudget()
        budget.reserve(context, attempt_index=1)
        budget.record_failure(
            APIConnectionError(
                request=httpx.Request(
                    "POST",
                    "https://qwen.example.test/v1/chat/completions",
                )
            ),
            0.0,
            provider="qwen",
            model="qwen-test",
            started_at="2026-07-17T00:00:00+00:00",
            completed_at="2026-07-17T00:00:00+00:00",
            context=context,
            attempt_index=1,
            error_category="unknown_transport",
        )
        create_completion = AsyncMock(
            return_value=_stream_response('{"value":7}')
        )
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            create_completion,
            budget=budget,
            retry_sleep=retry_sleep,
        )

        output = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
            call_context=context,
        )

        self.assertEqual(output.value, 7)
        receipts = budget.snapshot()["call_receipts"]
        self.assertEqual([item["attempt_index"] for item in receipts], [1, 2])
        self.assertEqual(
            [item["attempt_type"] for item in receipts],
            ["primary", "transport_retry"],
        )
        retry_sleep.assert_awaited_once_with(2.0)

    async def test_successful_call_reuses_logical_id_for_bounded_content_repairs(
        self,
    ) -> None:
        budget = ModelCallBudget()
        create_completion = AsyncMock(
            side_effect=[
                _stream_response('{"value":1}', response_id="initial"),
                _stream_response('{"value":2}', response_id="repair-1"),
                _stream_response('{"value":3}', response_id="repair-2"),
            ]
        )
        gateway = _mock_qwen_gateway(create_completion, budget=budget)
        initial = ModelCallContext(
            logical_call_id="stable-writer-batch",
            call_group="h1_h2",
            prompt_key="intake",
        )
        repair = initial.model_copy(update={"attempt_type": "content_repair"})

        first = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
            call_context=initial,
        )
        second = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
            call_context=repair,
        )
        third = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
            call_context=repair,
        )

        self.assertEqual([first.value, second.value, third.value], [1, 2, 3])
        receipts = budget.snapshot()["call_receipts"]
        self.assertEqual(
            [item["logical_call_id"] for item in receipts],
            ["stable-writer-batch"] * 3,
        )
        self.assertEqual(
            [item["attempt_index"] for item in receipts],
            [1, 2, 3],
        )
        self.assertEqual(
            [item["attempt_type"] for item in receipts],
            ["primary", "content_repair", "content_repair"],
        )
        with self.assertRaisesRegex(RuntimeError, "最多 3 次"):
            await gateway.generate(
                "intake",
                {"safe": "input"},
                _TinyOutput,
                call_context=repair,
            )
        self.assertEqual(create_completion.await_count, 3)

    async def test_dns_category_is_derived_without_persisting_error_text(self) -> None:
        request = httpx.Request(
            "POST",
            "https://secret.example.test/v1/chat/completions?api_key=redacted",
        )
        try:
            raise APIConnectionError(request=request) from socket.gaierror(
                "sensitive-hostname"
            )
        except APIConnectionError as error:
            dns_error = error
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            AsyncMock(
                side_effect=[
                    dns_error,
                    _stream_response(
                        '{"value":7}',
                        prompt_tokens=1,
                        completion_tokens=1,
                    ),
                ]
            ),
            retry_sleep=retry_sleep,
        )

        await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        receipt = gateway.budget.snapshot()["call_receipts"][0]
        self.assertEqual(receipt["error_category"], "dns")
        self.assertNotIn("sensitive-hostname", str(receipt))
        self.assertNotIn("api_key", str(receipt))
        retry_sleep.assert_awaited_once_with(2.0)

    async def test_three_transport_failures_exhaust_logical_attempt_ceiling(self) -> None:
        transport_error = APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://qwen.example.test/v1/chat/completions",
            )
        )
        create_completion = AsyncMock(side_effect=transport_error)
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )

        with self.assertRaisesRegex(RuntimeError, "3 次有界尝试"):
            await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        self.assertEqual(create_completion.await_count, 3)
        receipts = gateway.budget.snapshot()["call_receipts"]
        self.assertEqual(len(receipts), 3)
        self.assertTrue(
            all(item["outcome"] == "transport_failure" for item in receipts)
        )
        self.assertEqual(retry_sleep.await_args_list, [call(2.0), call(8.0)])

    async def test_cancelled_provider_attempt_records_terminal_receipt(self) -> None:
        provider_started = asyncio.Event()

        async def create_completion(**_kwargs):
            provider_started.set()
            await asyncio.Event().wait()

        gateway = _mock_qwen_gateway(
            AsyncMock(side_effect=create_completion),
        )
        task = asyncio.create_task(
            gateway.generate("intake", {"safe": "input"}, _TinyOutput)
        )
        await provider_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(len(snapshot["call_receipts"]), 1)
        self.assertEqual(snapshot["technical_failures"], ["CancelledError"])
        self.assertEqual(
            snapshot["call_receipts"][0]["outcome"],
            "transport_failure",
        )
        self.assertEqual(
            snapshot["call_receipts"][0]["error_type"],
            "CancelledError",
        )
        self.assertEqual(
            snapshot["call_receipts"][0]["error_category"],
            "cancelled",
        )

    async def test_cancelled_during_backoff_does_not_reserve_next_attempt(self) -> None:
        transport_error = APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://qwen.example.test/v1/chat/completions",
            )
        )
        create_completion = AsyncMock(side_effect=transport_error)
        retry_sleep = AsyncMock(side_effect=asyncio.CancelledError())
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )

        with self.assertRaises(asyncio.CancelledError):
            await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(len(snapshot["call_receipts"]), 1)
        self.assertEqual(create_completion.await_count, 1)
        retry_sleep.assert_awaited_once_with(2.0)

    async def test_transport_retry_cannot_consume_unstarted_first_call_slots(self) -> None:
        transport_error = APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://qwen.example.test/v1/chat/completions",
            )
        )
        create_completion = AsyncMock(side_effect=transport_error)
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            create_completion,
            budget=ModelCallBudget(max_calls=9),
            retry_sleep=retry_sleep,
        )
        first = ModelCallContext(
            logical_call_id="required-first",
            call_group="h1_h2",
            prompt_key="intake",
        )

        with self.assertRaisesRegex(RuntimeError, "全部尚未启动"):
            await gateway.generate(
                "intake",
                {"safe": "input"},
                _TinyOutput,
                call_context=first,
            )

        self.assertEqual(create_completion.await_count, 1)
        self.assertEqual(gateway.budget.llm_calls, 1)
        retry_sleep.assert_awaited_once_with(2.0)
        for group, count in (("h1_h2", 4), ("h3", 2), ("h4", 2)):
            for index in range(count):
                gateway.budget.reserve(
                    ModelCallContext(
                        logical_call_id=f"remaining-{group}-{index}",
                        call_group=group,
                        prompt_key="legacy",
                    )
                )
        self.assertEqual(gateway.budget.llm_calls, 9)

    async def test_schema_error_summary_survives_repair_budget_rejection(self) -> None:
        sensitive_value = "PRIVATE-RESPONSE-TEXT-MUST-NOT-PERSIST"
        sensitive_key = "PRIVATE-RESPONSE-KEY-MUST-NOT-PERSIST"
        gateway = _mock_qwen_gateway(
            AsyncMock(
                return_value=_stream_response(
                    (
                        f'{{"value":"{sensitive_value}",'
                        f'"{sensitive_key}":"redacted"}}'
                    ),
                    prompt_tokens=3,
                    completion_tokens=2,
                )
            ),
            budget=ModelCallBudget(max_calls=9),
        )
        context = ModelCallContext(
            logical_call_id="schema-repair-budget-blocked",
            call_group="h1_h2",
            prompt_key="intake",
        )

        with self.assertRaisesRegex(RuntimeError, "全部尚未启动"):
            await gateway.generate(
                "intake",
                {"safe": "input"},
                _StrictTinyOutput,
                call_context=context,
            )

        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(len(snapshot["call_receipts"]), 1)
        receipt = snapshot["call_receipts"][0]
        self.assertEqual(receipt["outcome"], "schema_failure")
        self.assertEqual(receipt["schema_error_count"], 2)
        self.assertEqual(
            receipt["schema_error_summary"][0],
            {"loc": ["value"], "type": "int_parsing"},
        )
        self.assertEqual(
            receipt["schema_error_summary"][1]["type"],
            "extra_forbidden",
        )
        self.assertRegex(
            receipt["schema_error_summary"][1]["loc"][0],
            r"^redacted-[0-9a-f]{12}$",
        )
        self.assertNotIn(sensitive_value, str(snapshot))
        self.assertNotIn(sensitive_key, str(snapshot))

    async def test_non_retryable_provider_status_fails_after_one_attempt(self) -> None:
        create_completion = AsyncMock(side_effect=self._status_error(400))
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(create_completion, retry_sleep=retry_sleep)

        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        self.assertEqual(create_completion.await_count, 1)
        self.assertEqual(len(gateway.budget.snapshot()["call_receipts"]), 1)
        retry_sleep.assert_not_awaited()

    async def test_transport_then_schema_retry_share_one_logical_sequence(self) -> None:
        transport_error = APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://qwen.example.test/v1/chat/completions",
            )
        )
        invalid = _stream_response(
            "{}",
            response_id="response-invalid",
            prompt_tokens=5,
            completion_tokens=2,
        )
        valid = _stream_response('{"value":7}')
        create_completion = AsyncMock(
            side_effect=[transport_error, invalid, valid]
        )
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(
            create_completion,
            retry_sleep=retry_sleep,
        )
        context = ModelCallContext(
            logical_call_id="logical-mixed-retry",
            call_group="h1_h2",
            prompt_key="intake",
        )

        output = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
            call_context=context,
        )

        self.assertEqual(output.value, 7)
        receipts = gateway.budget.snapshot()["call_receipts"]
        self.assertEqual([item["attempt_index"] for item in receipts], [1, 2, 3])
        self.assertEqual(
            [item["attempt_type"] for item in receipts],
            ["primary", "transport_retry", "schema_repair"],
        )
        self.assertEqual(
            [item["outcome"] for item in receipts],
            ["transport_failure", "schema_failure", "succeeded"],
        )
        self.assertEqual(
            [item["error_category"] for item in receipts],
            ["unknown_transport", "schema", None],
        )
        self.assertEqual(
            {item["logical_call_id"] for item in receipts},
            {"logical-mixed-retry"},
        )
        self.assertEqual(gateway.budget.llm_calls, 3)
        retry_sleep.assert_awaited_once_with(2.0)

    async def test_schema_repair_uses_one_logical_call_and_detailed_receipts(self) -> None:
        invalid = _stream_response(
            "{}",
            response_id="response-invalid",
            prompt_tokens=5,
            completion_tokens=2,
        )
        valid = _stream_response('{"value":7}')
        create_completion = AsyncMock(side_effect=[invalid, valid])
        retry_sleep = AsyncMock()
        gateway = _mock_qwen_gateway(create_completion, retry_sleep=retry_sleep)
        context = ModelCallContext(
            logical_call_id="logical-schema-repair",
            call_group="h1_h2",
            prompt_key="intake",
        )

        output = await gateway.generate(
            "intake",
            {"safe": "input"},
            _TinyOutput,
            call_context=context,
        )

        self.assertEqual(output.value, 7)
        self.assertEqual(create_completion.await_count, 2)
        receipts = gateway.budget.snapshot()["call_receipts"]
        self.assertEqual([item["attempt_index"] for item in receipts], [1, 2])
        self.assertEqual(
            [item["attempt_type"] for item in receipts],
            ["primary", "schema_repair"],
        )
        self.assertEqual(
            [item["outcome"] for item in receipts],
            ["schema_failure", "succeeded"],
        )
        self.assertEqual(
            [item["error_category"] for item in receipts],
            ["schema", None],
        )
        self.assertEqual(receipts[0]["schema_error_count"], 1)
        self.assertEqual(
            receipts[0]["schema_error_summary"],
            [{"loc": ["value"], "type": "missing"}],
        )
        self.assertNotIn("schema_error_summary", receipts[1])
        self.assertNotIn("schema_error_count", receipts[1])
        self.assertEqual(
            {item["logical_call_id"] for item in receipts},
            {"logical-schema-repair"},
        )
        self.assertTrue(all(item["input_sha256"] != "0" * 64 for item in receipts))
        self.assertTrue(
            all(item["output_schema_sha256"] != "0" * 64 for item in receipts)
        )
        self.assertTrue(all(item["prompt_version"] == "1.0.0" for item in receipts))
        first_call = create_completion.await_args_list[0].kwargs
        self.assertEqual(first_call["max_tokens"], 8192)
        self.assertEqual(first_call["timeout"], 120)
        self.assertTrue(first_call["stream"])
        self.assertEqual(first_call["stream_options"], {"include_usage": True})
        retry_sleep.assert_not_awaited()

    async def test_malformed_choices_still_emit_one_receipt_per_attempt(self) -> None:
        empty_choices = _stream_response(
            None,
            response_id="empty-response",
            prompt_tokens=2,
            completion_tokens=0,
        )
        missing_content = _stream_response(
            None,
            response_id="missing-content",
            prompt_tokens=3,
            completion_tokens=1,
        )
        valid = _stream_response(
            '{"value":7}',
            prompt_tokens=5,
            completion_tokens=2,
        )
        create_completion = AsyncMock(
            side_effect=[empty_choices, missing_content, valid]
        )
        gateway = _mock_qwen_gateway(create_completion)

        output = await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        self.assertEqual(output.value, 7)
        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 3)
        self.assertEqual(len(snapshot["call_receipts"]), snapshot["llm_calls"])
        self.assertEqual(
            [receipt["outcome"] for receipt in snapshot["call_receipts"]],
            ["schema_failure", "schema_failure", "succeeded"],
        )
        self.assertEqual(snapshot["input_tokens"], 10)
        self.assertEqual(snapshot["output_tokens"], 3)

    async def test_inconsistent_stream_id_is_rejected_without_partial_text(self) -> None:
        raw_secret = "PARTIAL-STREAM-SECRET"
        malformed_stream = _AsyncChunkStream(
            [
                SimpleNamespace(
                    id="response-one",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=raw_secret)
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    id="response-two",
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                    ),
                ),
            ]
        )
        gateway = _mock_qwen_gateway(
            AsyncMock(return_value=malformed_stream)
        )

        with self.assertRaisesRegex(ValueError, "response id changed"):
            await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(snapshot["call_receipts"][0]["outcome"], "provider_failure")
        self.assertNotIn(raw_secret, str(snapshot))

    async def test_invalid_provider_text_is_hashed_and_never_persisted(self) -> None:
        raw_secret = "RAW-PROVIDER-ERROR-DO-NOT-PERSIST"
        invalid_responses = [
            _stream_response(
                f"{{not-json:{raw_secret}}}",
                response_id=f"{raw_secret}-{index}",
                prompt_tokens=1,
                completion_tokens=1,
            )
            for index in range(3)
        ]
        create_completion = AsyncMock(side_effect=invalid_responses)
        gateway = _mock_qwen_gateway(create_completion)

        with self.assertRaisesRegex(ValueError, "failed schema validation") as raised:
            await gateway.generate("intake", {"safe": "input"}, _TinyOutput)

        snapshot = gateway.budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 3)
        self.assertEqual(len(snapshot["call_receipts"]), snapshot["llm_calls"])
        self.assertNotIn(raw_secret, str(snapshot))
        self.assertNotIn(raw_secret, str(raised.exception))
        self.assertNotIn(raw_secret, str(create_completion.await_args_list))


if __name__ == "__main__":
    unittest.main()
