from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from hypoweaver.benchmark_models import BenchmarkCallBudget
from hypoweaver.system_comparison_v2 import (
    SCORE_DIMENSION_WEIGHTS,
    ComparisonCaseSpecV2,
    FrozenSystemComparisonProtocolV2,
    ScientificDimensionScoreV2,
    SystemCaseScoreV2,
    SystemComparisonRunOutputV2,
    SystemComparisonRunConfigurationV2,
    SystemRuntimeEnvelopeV2,
    agent_laboratory_runner_kwargs_v2,
    derive_system_resource_usage_v2,
    freeze_system_comparison_protocol_v2,
    seal_system_comparison_protocol_v2,
    verify_system_comparison_protocol_v2,
)


HASHES = {
    "hypoweaver": "a" * 64,
    "agent_laboratory": "b" * 64,
    "benchmark_harness": "c" * 64,
}


def _case(
    case_id: str,
    split: str,
    *,
    order: tuple[str, str] = ("hypoweaver", "agent_laboratory"),
    input_view: str = "discovery_blind",
    primary: bool = False,
) -> ComparisonCaseSpecV2:
    return ComparisonCaseSpecV2(
        case_id=case_id,
        split=split,
        input_view=input_view,
        semantic_input_sha256="d" * 64,
        system_visible_input_sha256={
            "hypoweaver": "e" * 64,
            "agent_laboratory": "f" * 64,
        },
        data_sha256=["1" * 64],
        hidden_reference_sha256="2" * 64,
        system_order=order,
        include_in_primary_score=primary,
        one_shot=split == "quasi_holdout",
    )


def _protocol() -> FrozenSystemComparisonProtocolV2:
    return FrozenSystemComparisonProtocolV2(
        suite_id="pilot-v2",
        cases=[
            _case("case-dev", "dev"),
            _case("case-validation", "validation"),
            _case("case-holdout-a", "quasi_holdout", primary=True),
            _case(
                "case-holdout-b",
                "quasi_holdout",
                order=("agent_laboratory", "hypoweaver"),
                primary=True,
            ),
        ],
        model_id_by_system={
            "hypoweaver": "qwen-test",
            "agent_laboratory": "qwen-test",
        },
        source_sha256=HASHES,
        configuration_sha256="3" * 64,
    )


class SystemComparisonV2ProtocolTests(unittest.TestCase):
    def test_v1_budget_remains_unchanged(self) -> None:
        legacy = BenchmarkCallBudget()

        self.assertEqual(legacy.hypoweaver_max_calls, 20)
        self.assertEqual(legacy.agent_laboratory_max_calls, 20)
        self.assertEqual(legacy.total_max_calls, 46)

    def test_v2_protocol_freezes_partitions_budgets_and_schedule(self) -> None:
        frozen = seal_system_comparison_protocol_v2(_protocol())

        verify_system_comparison_protocol_v2(frozen)
        self.assertEqual(frozen.budget.provider_attempts_per_system, 40)
        self.assertEqual(frozen.budget.hypoweaver_logical_calls, 20)
        self.assertEqual(frozen.agent_laboratory_schedule.max_steps, 5)
        self.assertEqual(frozen.agent_laboratory_schedule.num_papers_lit_review, 0)
        self.assertEqual(frozen.agent_laboratory_schedule.mlesolver_max_steps, 1)
        self.assertEqual(frozen.agent_laboratory_schedule.papersolver_max_steps, 0)
        self.assertEqual(sum(frozen.scoring.dimension_weights.values()), 100)

    def test_freeze_writes_once_and_hash_detects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "protocol.json"
            frozen = freeze_system_comparison_protocol_v2(_protocol(), target)

            verify_system_comparison_protocol_v2(frozen)
            self.assertTrue(target.is_file())
            with self.assertRaises(FileExistsError):
                freeze_system_comparison_protocol_v2(_protocol(), target)
            changed = frozen.model_copy(update={"suite_id": "changed"})
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                verify_system_comparison_protocol_v2(changed)

    def test_primary_score_rejects_seen_or_method_aligned_cases(self) -> None:
        with self.assertRaises(ValidationError):
            _case("seen", "dev", primary=True)
        with self.assertRaises(ValidationError):
            _case(
                "aligned",
                "quasi_holdout",
                input_view="reproduction_aligned",
                primary=True,
            )

    def test_primary_case_order_must_be_counterbalanced(self) -> None:
        with self.assertRaisesRegex(ValidationError, "counterbalance"):
            FrozenSystemComparisonProtocolV2(
                suite_id="uncounterbalanced",
                cases=[
                    _case("dev", "dev"),
                    _case("validation", "validation"),
                    _case("holdout-a", "quasi_holdout", primary=True),
                    _case("holdout-b", "quasi_holdout", primary=True),
                ],
                model_id_by_system={
                    "hypoweaver": "qwen-test",
                    "agent_laboratory": "qwen-test",
                },
                source_sha256=HASHES,
                configuration_sha256="3" * 64,
            )

    def test_v2_run_config_keeps_repository_artifacts_relative(self) -> None:
        config = SystemComparisonRunConfigurationV2(
            artifact_root="/tmp/artifacts",
            protocol_path="protocols/v2.json",
            case_id="case-holdout-a",
            case_root="benchmark-cases/case-holdout-a",
            output_dir="/tmp/results",
            working_dir="/tmp/work",
            official_state_root="/tmp/states",
            agent_laboratory_root="Agent Laboratory",
            runtime_public_path="runtime/v2.json",
        )
        self.assertEqual(config.agent_timeout_seconds, 1800)
        self.assertEqual(config.budget.provider_attempts_per_system, 40)
        self.assertEqual(config.budget.hypoweaver_logical_calls, 20)
        self.assertEqual(config.agent_laboratory_schedule.max_steps, 5)
        with self.assertRaises(ValidationError):
            SystemComparisonRunConfigurationV2.model_validate(
                {
                    **config.model_dump(mode="json"),
                    "protocol_path": "../v2.json",
                }
            )

    def test_agent_runner_kwargs_are_the_frozen_v2_values(self) -> None:
        self.assertEqual(
            agent_laboratory_runner_kwargs_v2(),
            {
                "max_llm_calls": 40,
                "max_steps": 5,
                "num_papers_lit_review": 0,
                "mlesolver_max_steps": 1,
                "papersolver_max_steps": 0,
            },
        )


class SystemComparisonV2UsageTests(unittest.TestCase):
    def test_hypoweaver_can_use_twenty_logical_calls_and_forty_attempts(self) -> None:
        receipts = []
        for logical_index in range(20):
            logical_id = f"logical-{logical_index}"
            request_sha256 = f"{logical_index + 10:064x}"
            receipts.extend(
                [
                    {
                        "logical_call_id": logical_id,
                        "attempt_index": 1,
                        "attempt_type": "primary",
                        "outcome": "transport_failure",
                        "input_sha256": request_sha256,
                    },
                    {
                        "logical_call_id": logical_id,
                        "attempt_index": 2,
                        "attempt_type": "transport_retry",
                        "outcome": "succeeded",
                        "input_sha256": request_sha256,
                    },
                ]
            )

        usage = derive_system_resource_usage_v2(
            "hypoweaver",
            {"llm_calls": 40, "call_receipts": receipts},
        )

        self.assertEqual(usage.logical_calls, 20)
        self.assertEqual(usage.provider_attempts, 40)
        self.assertEqual(usage.technical_retry_attempts, 20)
        self.assertTrue(usage.retry_request_identity_verified)
        self.assertTrue(usage.within_budget)

    def test_hypoweaver_twenty_first_logical_call_is_out_of_budget(self) -> None:
        receipts = [
            {
                "logical_call_id": f"logical-{index}",
                "attempt_index": 1,
                "attempt_type": "primary",
                "outcome": "succeeded",
                "input_sha256": f"{index + 1:064x}",
            }
            for index in range(21)
        ]

        usage = derive_system_resource_usage_v2(
            "hypoweaver",
            {"provider_attempts": 21, "call_receipts": receipts},
        )

        self.assertTrue(usage.within_provider_attempt_budget)
        self.assertFalse(usage.within_logical_call_budget)
        self.assertFalse(usage.within_budget)

    def test_agent_logical_budget_is_not_applicable_and_provider_time_is_named(self) -> None:
        usage = derive_system_resource_usage_v2(
            "agent_laboratory",
            {
                "provider_attempts": 1,
                "model_wall_time_seconds": 12.5,
                "call_receipts": [
                    {
                        "logical_call_id": "logical-1",
                        "attempt_index": 1,
                        "attempt_type": "primary",
                        "outcome": "succeeded",
                        "input_sha256": "4" * 64,
                    }
                ],
            },
        )

        self.assertIsNone(usage.within_logical_call_budget)
        self.assertTrue(usage.within_budget)
        self.assertEqual(usage.model_provider_wall_time_seconds, 12.5)
        payload = usage.model_dump(mode="json")
        self.assertEqual(payload["model_provider_wall_time_seconds"], 12.5)
        self.assertNotIn("wall_time_seconds", payload)

        legacy_payload = dict(payload)
        legacy_payload["wall_time_seconds"] = legacy_payload.pop(
            "model_provider_wall_time_seconds"
        )
        restored = type(usage).model_validate(legacy_payload)
        self.assertEqual(restored.model_provider_wall_time_seconds, 12.5)

    def test_retry_without_verifiable_same_request_is_not_v2_eligible(self) -> None:
        usage = derive_system_resource_usage_v2(
            "agent_laboratory",
            {
                "llm_calls": 2,
                "call_receipts": [
                    {
                        "logical_call_id": 1,
                        "attempt_index": 1,
                        "status": "failed",
                    },
                    {
                        "logical_call_id": 1,
                        "attempt_index": 2,
                        "status": "completed",
                    },
                ],
            },
        )

        self.assertFalse(usage.retry_request_identity_verified)
        self.assertFalse(usage.within_budget)

    def test_declared_attempts_must_equal_receipts(self) -> None:
        with self.assertRaisesRegex(ValueError, "receipt count"):
            derive_system_resource_usage_v2(
                "agent_laboratory",
                {"llm_calls": 2, "call_receipts": []},
            )

    def test_run_output_binds_usage_to_the_v2_runtime_envelope(self) -> None:
        usage = derive_system_resource_usage_v2(
            "hypoweaver",
            {
                "llm_calls": 1,
                "call_receipts": [
                    {
                        "logical_call_id": "logical-1",
                        "attempt_index": 1,
                        "attempt_type": "primary",
                        "outcome": "succeeded",
                        "input_sha256": "4" * 64,
                    }
                ],
            },
        )

        output = SystemComparisonRunOutputV2(
            protocol_sha256="5" * 64,
            case_id="case-holdout-a",
            split="quasi_holdout",
            system_id="hypoweaver",
            runtime_envelope=SystemRuntimeEnvelopeV2(
                system_id="hypoweaver",
                logical_call_limit=20,
            ),
            run_status="completed",
            execution_status="succeeded",
            scientific_status="limited",
            score_eligibility="primary_score",
            usage=usage,
            native_output_sha256="6" * 64,
            budget_compliant=True,
        )

        self.assertEqual(output.usage.provider_attempts, 1)
        with self.assertRaises(ValidationError):
            SystemRuntimeEnvelopeV2(
                system_id="hypoweaver",
                logical_call_limit=None,
            )


class SystemComparisonV2ScoreTests(unittest.TestCase):
    def test_six_dimensions_sum_to_total(self) -> None:
        dimensions = [
            ScientificDimensionScoreV2(
                dimension=dimension,
                score=weight,
                evidence=["frozen evidence"],
            )
            for dimension, weight in SCORE_DIMENSION_WEIGHTS.items()
        ]

        score = SystemCaseScoreV2(
            case_id="case-holdout-a",
            system_id="hypoweaver",
            score_status="scoreable",
            dimensions=dimensions,
            total_score=100,
        )

        self.assertEqual(score.total_score, 100)

    def test_infrastructure_failure_has_no_scientific_zero(self) -> None:
        score = SystemCaseScoreV2(
            case_id="case-holdout-a",
            system_id="hypoweaver",
            score_status="excluded_infrastructure_failure",
            failure_class="provider_transport",
        )

        self.assertIsNone(score.total_score)
        with self.assertRaises(ValidationError):
            SystemCaseScoreV2(
                case_id="case-holdout-a",
                system_id="hypoweaver",
                score_status="excluded_infrastructure_failure",
                failure_class="provider_transport",
                total_score=0,
            )


if __name__ == "__main__":
    unittest.main()
