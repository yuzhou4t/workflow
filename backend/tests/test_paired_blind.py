from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hypoweaver.benchmark_evaluator import seal_benchmark_packet
from hypoweaver.benchmark_models import (
    BenchmarkPacket,
    BenchmarkResourceUsage,
    NeurIPSRatings,
    NeurIPSReview,
    NormalizedClaim,
    NormalizedDesign,
    OfficialAttemptBinding,
    PairedEvaluationRequest,
    PairedEvaluationView,
)
from hypoweaver.paired_blind import (
    PAIRED_BLIND_CONNECT_TIMEOUT_SECONDS,
    PAIRED_BLIND_TIMEOUT_SECONDS,
    PairedBlindCallError,
    PairedBlindEngine,
    QwenPairedBlindGateway,
    anonymize_packet,
)
from hypoweaver.paired_blind_repository import PairedBlindRepository


class PairedBlindTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = PairedBlindRepository(Path(self.tempdir.name) / "paired.db")
        self.engine = PairedBlindEngine(self.repository)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_fixture_evaluation_runs_exactly_five_anonymous_reviews(self) -> None:
        request = PairedEvaluationRequest(
            packet_a=_packet("packet-a", "hypoweaver", "HypoWeaver 结论仅为关联。"),
            packet_b=_packet("packet-b", "agent_laboratory", "Agent Laboratory 结论仅为关联。"),
            reference_summary="隐藏参考只用于终态后核验。",
        )

        view = await self.engine.evaluate(request)

        self.assertEqual(view.status, "completed")
        self.assertEqual(len(view.result.reviews), 5)
        self.assertEqual(view.result.preference_counts["tie"], 5)
        self.assertEqual(len(view.sealed_label_orders), 5)
        self.assertEqual(set(view.sealed_label_orders), {"A_B", "B_A"})
        self.assertEqual(len(view.sealed_system_assignments), 5)
        self.assertEqual(
            set(view.sealed_system_assignments), {"A_B", "B_A"}
        )
        self.assertEqual(
            [review.label_order for review in view.result.reviews],
            view.sealed_label_orders,
        )
        self.assertEqual(
            [review.system_assignment for review in view.result.reviews],
            view.sealed_system_assignments,
        )
        self.assertEqual(len(view.review_resource_usage), 5)
        self.assertEqual(
            sum(item.llm_calls for item in view.review_resource_usage),
            0,
        )
        self.assertEqual(view.review_call_receipts, [])
        self.assertEqual(view.receipt_count, 0)
        self.assertTrue(
            all(review.call_receipt is None for review in view.result.reviews)
        )
        self.assertEqual(self.engine.get(view.id).id, view.id)

    async def test_legacy_view_defaults_to_no_generic_receipts(self) -> None:
        view = PairedEvaluationView.model_validate(
            {
                "id": "legacy-view",
                "case_id": "case-1",
                "packet_a_id": "packet-a",
                "packet_b_id": "packet-b",
                "status": "completed",
            }
        )

        self.assertEqual(view.review_call_receipts, [])
        self.assertEqual(view.receipt_count, 0)
        self.assertEqual(view.partial_reviews, [])

    async def test_prefrozen_orders_are_used_without_redraw(self) -> None:
        label_orders = ["A_B", "B_A", "A_B", "B_A", "A_B"]
        assignments = ["B_A", "A_B", "B_A", "A_B", "B_A"]
        request = PairedEvaluationRequest(
            packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
            packet_b=_packet("packet-b", "agent_laboratory", "关联结论。"),
            reference_summary="隐藏参考。",
            sealed_label_orders=label_orders,
            sealed_system_assignments=assignments,
        )

        view = await self.engine.evaluate(request)

        self.assertEqual(view.sealed_label_orders, label_orders)
        self.assertEqual(view.sealed_system_assignments, assignments)

    async def test_prefrozen_orders_must_include_both_presentations(self) -> None:
        with self.assertRaisesRegex(ValueError, "represent both A/B orders"):
            PairedEvaluationRequest(
                packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
                packet_b=_packet(
                    "packet-b", "agent_laboratory", "关联结论。"
                ),
                reference_summary="隐藏参考。",
                sealed_label_orders=["A_B"] * 5,
                sealed_system_assignments=["A_B", "B_A", "A_B", "B_A", "A_B"],
            )

    async def test_prefrozen_orders_must_be_supplied_together(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be provided together"):
            PairedEvaluationRequest(
                packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
                packet_b=_packet(
                    "packet-b", "agent_laboratory", "关联结论。"
                ),
                reference_summary="隐藏参考。",
                sealed_label_orders=["A_B", "B_A", "A_B", "B_A", "A_B"],
            )

    async def test_anonymous_payload_removes_system_names_and_native_ids(self) -> None:
        packet = _packet(
            "packet-secret",
            "hypoweaver",
            "HypoWeaver-Qwen 比 Agent Laboratory 更谨慎。",
        )

        payload = anonymize_packet(packet)
        rendered = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("HypoWeaver", rendered)
        self.assertNotIn("Agent Laboratory", rendered)
        self.assertNotIn("claim-native", rendered)
        self.assertIn("[SYSTEM]", rendered)

    async def test_pair_rejects_different_visible_inputs(self) -> None:
        packet_b = _packet("packet-b", "agent_laboratory", "关联结论。").model_copy(
            update={"visible_input_sha256": "c" * 64, "packet_sha256": None}
        )
        packet_b = seal_benchmark_packet(packet_b)
        with self.assertRaisesRegex(ValueError, "same visible input"):
            PairedEvaluationRequest(
                packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
                packet_b=packet_b,
                reference_summary="reference",
            )

    async def test_official_pair_rejects_fixture_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "qwen provider"):
            PairedEvaluationRequest(
                packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
                packet_b=_packet(
                    "packet-b", "agent_laboratory", "关联结论。"
                ),
                reference_summary="reference",
                model_provider="fixture",
                official_attempt=OfficialAttemptBinding(
                    attempt_id="1" * 64,
                    run_manifest_sha256="2" * 64,
                    begun_at="2026-07-16T00:00:00+00:00",
                ),
            )

    async def test_qwen_gateway_binds_raw_response_to_official_attempt(self) -> None:
        binding = OfficialAttemptBinding(
            attempt_id="1" * 64,
            run_manifest_sha256="2" * 64,
            begun_at="2026-07-16T00:00:00+00:00",
        )
        ratings = NeurIPSRatings(
            quality=3,
            significance=3,
            clarity=3,
            soundness=3,
            presentation=3,
            contribution=3,
            overall=6,
            confidence=4,
            recommendation="accept",
        )
        content = NeurIPSReview(
            review_id="model-supplied-id",
            sample_index=1,
            label_order="A_B",
            ratings_a=ratings,
            ratings_b=ratings,
            preferred_label="tie",
        ).model_dump_json()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=response))
            )
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "test-key",
                    "QWEN_REVIEW_MODEL": "qwen3.7-max",
                },
            ),
            patch(
                "hypoweaver.paired_blind.AsyncOpenAI",
                return_value=client,
            ),
        ):
            gateway = QwenPairedBlindGateway(binding)
            review = await gateway.review(
                sample_index=1,
                label_order="A_B",
                payload={"test": True},
            )

        self.assertIsNotNone(review.official_receipt)
        self.assertEqual(review.official_receipt.attempt_id, binding.attempt_id)
        self.assertEqual(
            review.official_receipt.run_manifest_sha256,
            binding.run_manifest_sha256,
        )
        self.assertEqual(review.official_receipt.provider, "qwen")
        self.assertEqual(review.official_receipt.model, "qwen3.7-max")
        self.assertEqual(review.resource_usage.llm_calls, 1)
        self.assertIsNotNone(review.call_receipt)
        self.assertEqual(review.call_receipt.sample_index, 1)
        self.assertEqual(review.call_receipt.provider, "qwen")
        self.assertEqual(review.call_receipt.model, "qwen3.7-max")
        self.assertEqual(review.call_receipt.outcome, "succeeded")
        self.assertEqual(
            review.call_receipt.response_sha256,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(review.call_receipt.input_tokens, 12)
        self.assertEqual(review.call_receipt.output_tokens, 7)
        self.assertEqual(
            client.chat.completions.create.await_args.kwargs["timeout"],
            PAIRED_BLIND_TIMEOUT_SECONDS,
        )

    async def test_qwen_gateway_failure_has_sanitized_nonofficial_receipt(self) -> None:
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=RuntimeError("synthetic timeout"))
                )
            )
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "test-key",
                    "QWEN_REVIEW_MODEL": "qwen3.7-max",
                },
            ),
            patch("hypoweaver.paired_blind.AsyncOpenAI", return_value=client),
        ):
            gateway = QwenPairedBlindGateway()
            with self.assertRaises(PairedBlindCallError) as raised:
                await gateway.review(
                    sample_index=2,
                    label_order="B_A",
                    payload={"test": True},
                )

        receipt = raised.exception.call_receipt
        self.assertEqual(receipt.sample_index, 2)
        self.assertEqual(receipt.provider, "qwen")
        self.assertEqual(receipt.model, "qwen3.7-max")
        self.assertEqual(receipt.outcome, "technical_failure")
        self.assertIsNone(receipt.response_sha256)
        self.assertIsNotNone(receipt.failure_package_sha256)
        self.assertEqual(receipt.failure_type, "RuntimeError")
        self.assertNotIn("synthetic timeout", receipt.model_dump_json())
        self.assertEqual(raised.exception.resource_usage.llm_calls, 1)

    async def test_qwen_gateway_uses_explicit_long_http_timeout(self) -> None:
        client = SimpleNamespace()
        with (
            patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}),
            patch("hypoweaver.paired_blind.httpx.AsyncClient") as http_client,
            patch("hypoweaver.paired_blind.AsyncOpenAI", return_value=client),
        ):
            gateway = QwenPairedBlindGateway()

        timeout = http_client.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, PAIRED_BLIND_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(timeout.read, PAIRED_BLIND_TIMEOUT_SECONDS)
        self.assertEqual(timeout.write, PAIRED_BLIND_TIMEOUT_SECONDS)
        self.assertEqual(timeout.pool, PAIRED_BLIND_TIMEOUT_SECONDS)

    async def test_default_qwen_engine_records_five_verified_real_receipts(self) -> None:
        ratings = NeurIPSRatings(
            quality=3,
            significance=3,
            clarity=3,
            soundness=3,
            presentation=3,
            contribution=3,
            overall=6,
            confidence=4,
            recommendation="accept",
        )
        content = NeurIPSReview(
            review_id="model-supplied-id",
            sample_index=1,
            label_order="A_B",
            ratings_a=ratings,
            ratings_b=ratings,
            preferred_label="tie",
        ).model_dump_json()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "test-key",
                    "QWEN_REVIEW_MODEL": "qwen3.7-max",
                },
            ),
            patch("hypoweaver.paired_blind.AsyncOpenAI", return_value=client),
        ):
            engine = PairedBlindEngine(self.repository)
            view = await engine.evaluate(
                PairedEvaluationRequest(
                    packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
                    packet_b=_packet(
                        "packet-b", "agent_laboratory", "关联结论。"
                    ),
                    reference_summary="隐藏参考。",
                    model_provider="qwen",
                )
            )

        self.assertEqual(create.await_count, 5)
        self.assertEqual(view.receipt_count, 5)
        self.assertEqual(
            sum(item.llm_calls for item in view.review_resource_usage), 5
        )
        self.assertEqual(
            {item.sample_index for item in view.review_call_receipts},
            {1, 2, 3, 4, 5},
        )
        self.assertEqual(
            len({item.call_id for item in view.review_call_receipts}), 5
        )
        view.verify_runtime_receipts(
            expect_real_qwen=True,
            expected_model="qwen3.7-max",
        )

        duplicate = view.model_copy(deep=True)
        duplicate.review_call_receipts[1] = (
            duplicate.review_call_receipts[1].model_copy(
                update={"call_id": duplicate.review_call_receipts[0].call_id}
            )
        )
        with self.assertRaisesRegex(ValueError, "call ids must be unique"):
            duplicate.verify_runtime_receipts(
                expect_real_qwen=True,
                expected_model="qwen3.7-max",
            )

        token_mismatch = view.model_copy(deep=True)
        token_mismatch.review_call_receipts[0] = (
            token_mismatch.review_call_receipts[0].model_copy(
                update={"input_tokens": 13}
            )
        )
        with self.assertRaisesRegex(ValueError, "tokens do not match"):
            token_mismatch.verify_runtime_receipts(
                expect_real_qwen=True,
                expected_model="qwen3.7-max",
            )

    async def test_default_qwen_failure_persists_only_sanitized_receipts(self) -> None:
        create = AsyncMock(side_effect=RuntimeError("secret provider detail"))
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "test-key",
                    "QWEN_REVIEW_MODEL": "qwen3.7-max",
                },
            ),
            patch("hypoweaver.paired_blind.AsyncOpenAI", return_value=client),
        ):
            engine = PairedBlindEngine(self.repository)
            with self.assertRaises(PairedBlindCallError):
                await engine.evaluate(
                    PairedEvaluationRequest(
                        packet_a=_packet(
                            "packet-a", "hypoweaver", "关联结论。"
                        ),
                        packet_b=_packet(
                            "packet-b", "agent_laboratory", "关联结论。"
                        ),
                        reference_summary="隐藏参考。",
                        model_provider="qwen",
                    )
                )

        failed = self.repository.list()[0]
        self.assertEqual(create.await_count, 5)
        self.assertEqual(failed.error, "PairedBlindCallError")
        self.assertEqual(failed.receipt_count, 5)
        self.assertEqual(
            {item.outcome for item in failed.review_call_receipts},
            {"technical_failure"},
        )
        self.assertNotIn("secret provider detail", failed.model_dump_json())
        failed.verify_runtime_receipts(
            expect_real_qwen=True,
            expected_model="qwen3.7-max",
        )

    async def test_qwen_reviews_run_serially_and_preserve_partial_successes(self) -> None:
        ratings = NeurIPSRatings(
            quality=3,
            significance=3,
            clarity=3,
            soundness=3,
            presentation=3,
            contribution=3,
            overall=6,
            confidence=4,
            recommendation="accept",
        )
        content = NeurIPSReview(
            review_id="model-id",
            sample_index=1,
            label_order="A_B",
            ratings_a=ratings,
            ratings_b=ratings,
            preferred_label="tie",
        ).model_dump_json()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )
        active = 0
        maximum_active = 0
        call_index = 0

        async def create(**_kwargs):
            nonlocal active, maximum_active, call_index
            call_index += 1
            current_index = call_index
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1
            if current_index == 3:
                raise RuntimeError("private connection detail")
            return response

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=create))
            )
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "test-key",
                    "QWEN_REVIEW_MODEL": "qwen3.7-max",
                },
            ),
            patch("hypoweaver.paired_blind.AsyncOpenAI", return_value=client),
        ):
            engine = PairedBlindEngine(self.repository)
            with self.assertRaises(PairedBlindCallError):
                await engine.evaluate(
                    PairedEvaluationRequest(
                        packet_a=_packet(
                            "packet-a", "hypoweaver", "关联结论。"
                        ),
                        packet_b=_packet(
                            "packet-b", "agent_laboratory", "关联结论。"
                        ),
                        reference_summary="隐藏参考。",
                        model_provider="qwen",
                    )
                )

        failed = self.repository.list()[0]
        self.assertEqual(maximum_active, 1)
        self.assertEqual(call_index, 5)
        self.assertEqual(
            {item.sample_index for item in failed.partial_reviews},
            {1, 2, 4, 5},
        )
        self.assertNotIn("private connection detail", failed.model_dump_json())
        failed.verify_runtime_receipts(
            expect_real_qwen=True,
            expected_model="qwen3.7-max",
        )

    async def test_qwen_resume_retries_only_missing_samples_across_rounds(self) -> None:
        ratings = NeurIPSRatings(
            quality=3,
            significance=3,
            clarity=3,
            soundness=3,
            presentation=3,
            contribution=3,
            overall=6,
            confidence=4,
            recommendation="accept",
        )
        content = NeurIPSReview(
            review_id="model-id",
            sample_index=1,
            label_order="A_B",
            ratings_a=ratings,
            ratings_b=ratings,
            preferred_label="tie",
        ).model_dump_json()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )
        label_orders = ["A_B", "B_A", "A_B", "B_A", "A_B"]
        assignments = ["B_A", "A_B", "B_A", "A_B", "B_A"]
        request = PairedEvaluationRequest(
            packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
            packet_b=_packet(
                "packet-b", "agent_laboratory", "关联结论。"
            ),
            reference_summary="隐藏参考。",
            model_provider="qwen",
            sealed_label_orders=label_orders,
            sealed_system_assignments=assignments,
        )
        initial_call = 0

        async def initial_create(**_kwargs):
            nonlocal initial_call
            initial_call += 1
            if initial_call in {2, 5}:
                raise RuntimeError("private initial failure")
            return response

        initial_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=initial_create)
                )
            )
        )
        environment = {
            "DASHSCOPE_API_KEY": "test-key",
            "QWEN_REVIEW_MODEL": "qwen3.7-max",
        }
        with (
            patch.dict("os.environ", environment),
            patch(
                "hypoweaver.paired_blind.AsyncOpenAI",
                return_value=initial_client,
            ),
        ):
            with self.assertRaises(PairedBlindCallError):
                await self.engine.evaluate(request)

        predecessor = self.repository.list()[-1]
        self.assertEqual(
            {item.sample_index for item in predecessor.partial_reviews},
            {1, 3, 4},
        )
        changed_schedule = request.model_copy(
            update={
                "sealed_label_orders": ["B_A", "A_B", "B_A", "A_B", "B_A"]
            }
        )
        with self.assertRaisesRegex(ValueError, "schedule mismatch"):
            await self.engine.resume_failed(changed_schedule, predecessor)

        second_call = 0

        async def second_create(**_kwargs):
            nonlocal second_call
            second_call += 1
            if second_call == 2:
                raise RuntimeError("private second failure")
            return response

        second_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=second_create)
                )
            )
        )
        with (
            patch.dict("os.environ", environment),
            patch(
                "hypoweaver.paired_blind.AsyncOpenAI",
                return_value=second_client,
            ),
        ):
            with self.assertRaises(PairedBlindCallError):
                await self.engine.resume_failed(request, predecessor)

        second_predecessor = self.repository.list()[0]
        self.assertEqual(second_call, 2)
        self.assertEqual(
            {item.sample_index for item in second_predecessor.partial_reviews},
            {1, 2, 3, 4},
        )
        self.assertEqual(
            [item.outcome for item in second_predecessor.review_call_receipts],
            [
                "succeeded",
                "succeeded",
                "succeeded",
                "succeeded",
                "technical_failure",
            ],
        )
        second_predecessor.verify_runtime_receipts(
            expect_real_qwen=True,
            expected_model="qwen3.7-max",
        )

        final_create = AsyncMock(return_value=response)
        final_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=final_create)
            )
        )
        with (
            patch.dict("os.environ", environment),
            patch(
                "hypoweaver.paired_blind.AsyncOpenAI",
                return_value=final_client,
            ),
        ):
            completed = await self.engine.resume_failed(
                request,
                second_predecessor,
            )

        self.assertEqual(final_create.await_count, 1)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.partial_reviews, [])
        self.assertEqual(
            [item.sample_index for item in completed.result.reviews],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(
            {item.outcome for item in completed.review_call_receipts},
            {"succeeded"},
        )
        completed.verify_runtime_receipts(
            expect_real_qwen=True,
            expected_model="qwen3.7-max",
        )

    async def test_default_qwen_missing_receipt_is_not_backfilled(self) -> None:
        ratings = NeurIPSRatings(
            quality=3,
            significance=3,
            clarity=3,
            soundness=3,
            presentation=3,
            contribution=3,
            overall=6,
            confidence=4,
            recommendation="accept",
        )
        review_without_receipt = NeurIPSReview(
            review_id="no-receipt",
            sample_index=1,
            label_order="A_B",
            ratings_a=ratings,
            ratings_b=ratings,
            preferred_label="tie",
            resource_usage=BenchmarkResourceUsage(input_tokens=3, output_tokens=2),
        )
        client = SimpleNamespace()
        with (
            patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}),
            patch("hypoweaver.paired_blind.AsyncOpenAI", return_value=client),
            patch.object(
                QwenPairedBlindGateway,
                "review",
                new=AsyncMock(return_value=review_without_receipt),
            ),
        ):
            engine = PairedBlindEngine(self.repository)
            with self.assertRaisesRegex(ValueError, "exactly five calls and receipts"):
                await engine.evaluate(
                    PairedEvaluationRequest(
                        packet_a=_packet(
                            "packet-a", "hypoweaver", "关联结论。"
                        ),
                        packet_b=_packet(
                            "packet-b", "agent_laboratory", "关联结论。"
                        ),
                        reference_summary="隐藏参考。",
                        model_provider="qwen",
                    )
                )

        failed = self.repository.list()[0]
        self.assertEqual(failed.receipt_count, 0)
        self.assertEqual(
            sum(item.llm_calls for item in failed.review_resource_usage), 5
        )

    async def test_injected_qwen_named_gateway_cannot_record_real_usage(self) -> None:
        gateway = _RecordingGateway()
        engine = PairedBlindEngine(
            self.repository,
            gateway_factory=lambda _provider: gateway,
        )
        request = PairedEvaluationRequest(
            packet_a=_packet("packet-a", "hypoweaver", "HypoWeaver 关联结论。"),
            packet_b=_packet(
                "packet-b",
                "agent_laboratory",
                "Agent Laboratory 关联结论。",
            ),
            reference_summary="HypoWeaver 和 Agent Laboratory 共用隐藏参考。",
            model_provider="qwen",
        )

        view = await engine.evaluate(request)

        self.assertEqual(gateway.calls, 5)
        self.assertEqual(
            sum(item.llm_calls for item in view.review_resource_usage),
            0,
        )
        self.assertEqual(
            sum(item.input_tokens for item in view.review_resource_usage),
            150,
        )
        self.assertEqual(view.receipt_count, 0)
        self.assertEqual(view.review_call_receipts, [])
        self.assertTrue(
            all(review.call_receipt is None for review in view.result.reviews)
        )
        view.verify_runtime_receipts(expect_real_qwen=False)
        self.assertTrue(
            all(item.wall_time_seconds >= 0 for item in view.review_resource_usage)
        )
        self.assertTrue(
            all(not item.technical_failures for item in view.review_resource_usage)
        )
        self.assertEqual(
            [call["label_order"] for call in gateway.payloads],
            view.sealed_label_orders,
        )
        self.assertEqual(
            [review.system_assignment for review in view.result.reviews],
            view.sealed_system_assignments,
        )
        rendered_payloads = json.dumps(gateway.payloads, ensure_ascii=False)
        self.assertNotIn("HypoWeaver", rendered_payloads)
        self.assertNotIn("Agent Laboratory", rendered_payloads)
        self.assertNotIn("packet-a", rendered_payloads)
        self.assertNotIn("packet-b", rendered_payloads)

    async def test_failed_review_records_all_attempts_without_redrawing_orders(self) -> None:
        gateway = _RecordingGateway(fail_sample=3)
        engine = PairedBlindEngine(
            self.repository,
            gateway_factory=lambda _provider: gateway,
        )
        request = PairedEvaluationRequest(
            packet_a=_packet("packet-a", "hypoweaver", "关联结论。"),
            packet_b=_packet("packet-b", "agent_laboratory", "关联结论。"),
            reference_summary="隐藏参考。",
            model_provider="qwen",
        )

        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            await engine.evaluate(request)

        failed = self.repository.list()[0]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(gateway.calls, 5)
        self.assertEqual(len(failed.review_resource_usage), 5)
        self.assertEqual(
            sum(item.llm_calls for item in failed.review_resource_usage),
            0,
        )
        self.assertEqual(
            sum(bool(item.technical_failures) for item in failed.review_resource_usage),
            1,
        )
        self.assertEqual(failed.receipt_count, 0)
        self.assertEqual(failed.review_call_receipts, [])
        self.assertEqual(failed.error, "RuntimeError")
        self.assertNotIn("synthetic failure", failed.model_dump_json())
        failed.verify_runtime_receipts(expect_real_qwen=False)
        self.assertEqual(
            [call["label_order"] for call in gateway.payloads],
            failed.sealed_label_orders,
        )
        self.assertEqual(
            set(failed.sealed_system_assignments), {"A_B", "B_A"}
        )


def _packet(packet_id: str, system_id: str, text: str) -> BenchmarkPacket:
    packet = BenchmarkPacket(
        packet_id=packet_id,
        system_id=system_id,
        case_id="case-1",
        visible_input_sha256="a" * 64,
        data_sha256=["b" * 64],
        model_id="qwen3.7-plus",
        design=NormalizedDesign(method_family="panel_association"),
        claims=[
            NormalizedClaim(
                claim_id="claim-native",
                text=text,
                strength="associational",
            )
        ],
    )
    return seal_benchmark_packet(packet)


class _RecordingGateway:
    provider_name = "qwen"
    model = "qwen-test"

    def __init__(self, *, fail_sample: int | None = None) -> None:
        self.fail_sample = fail_sample
        self.calls = 0
        self.payloads: list[dict] = []

    async def review(
        self,
        *,
        sample_index: int,
        label_order: str,
        payload: dict,
    ) -> NeurIPSReview:
        self.calls += 1
        self.payloads.append(
            {"label_order": label_order, "payload": payload}
        )
        if sample_index == self.fail_sample:
            raise RuntimeError("synthetic failure")
        ratings = NeurIPSRatings(
            quality=3,
            significance=3,
            clarity=3,
            soundness=3,
            presentation=3,
            contribution=3,
            overall=6,
            confidence=4,
            recommendation="accept",
        )
        return NeurIPSReview(
            review_id=f"fake-{sample_index}",
            sample_index=sample_index,
            label_order=label_order,
            ratings_a=ratings,
            ratings_b=ratings,
            preferred_label="tie",
            resource_usage=BenchmarkResourceUsage(
                input_tokens=30,
                output_tokens=10,
            ),
        )


if __name__ == "__main__":
    unittest.main()
