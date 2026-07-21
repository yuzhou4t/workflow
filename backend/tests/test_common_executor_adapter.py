from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from hypoweaver.adapters import FixtureModelGateway
from hypoweaver.case_import import DatasetRegistry
from hypoweaver.common_executor_adapter import (
    CommonExecutorAdapterError,
    assert_h2_pre_result_boundary,
    build_pre_result_binding,
    claim_decision_from_h3,
    run_to_h2_stop,
    validate_sealed_common_result,
)
from hypoweaver.engine import WorkflowEngine, WorkflowTransitionError
from hypoweaver.models import (
    CaseSubmission,
    ClaimLedger,
    CreateRunRequest,
    DatasetRef,
    GateDecisionRequest,
)
from hypoweaver.repository import RunRepository
from hypoweaver.seal import canonical_sha256


class _FixtureQwenWorkflowEngine(WorkflowEngine):
    """Exercise research-mode state transitions without provider calls."""

    def _gateway(self, state):
        return FixtureModelGateway()

    def _reviewer_gateway(self, state):
        return FixtureModelGateway()


class CommonExecutorAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.repository = RunRepository(root / "runs.db")
        registry = DatasetRegistry(root / "datasets.json")
        data_path = root / "panel.csv"
        rows = []
        for firm in range(6):
            for year in range(2018, 2022):
                exposure = firm / 5 + (year - 2018) * 0.2
                rows.append(
                    {
                        "firm_id": f"F{firm}",
                        "year": year,
                        "outcome": 0.4 * exposure + firm * 0.1,
                        "exposure": exposure,
                    }
                )
        pd.DataFrame(rows).to_csv(data_path, index=False)
        data_sha = hashlib.sha256(data_path.read_bytes()).hexdigest()
        ref = DatasetRef(
            dataset_id="common-panel",
            filename=data_path.name,
            sha256=data_sha,
            size_bytes=data_path.stat().st_size,
        )
        registry.register(ref, data_path)
        self.data_sha = data_sha
        self.engine = _FixtureQwenWorkflowEngine(
            self.repository,
            dataset_registry=registry,
            model_call_budget_mode="v3",
        )
        case = CaseSubmission(
            case_id="common_panel_discovery_blind",
            title="Panel association fixture",
            research_question="Is exposure conditionally associated with the outcome?",
            hypotheses=[
                {
                    "hypothesis_id": "H1",
                    "statement": "Exposure is conditionally associated with the outcome.",
                    "expected_direction": "positive",
                }
            ],
            unit_of_analysis="firm-year",
            sample_period="2018-2021",
            data_structure_hint="panel",
            variables=[
                {"name": "firm_id", "role": "id"},
                {"name": "year", "role": "time"},
                {"name": "outcome", "role": "outcome"},
                {"name": "exposure", "role": "exposure"},
            ],
            dataset_refs=[ref],
            constraints=["Associational language only."],
        )
        self.case = case
        run = await self.engine.create_run(
            CreateRunRequest(
                mode="research",
                case=case,
                model_provider="qwen",
                execution_mode="external",
            )
        )
        run = await self.engine.decide_gate(
            run.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="common-h1"),
        )
        selected = run.artifacts["design_arena"]["payload"][
            "provisional_candidate_id"
        ]
        # Refresh the existing deterministic H2 Test-DAG without approving or
        # executing the plan. Tests use the private migration helper so setup
        # can never cross the pre-result boundary by accident.
        if self.engine._refresh_h2_test_dag_if_needed(run, selected):
            run = self.repository.save(run, expected_version=run.version)
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H2"))
        self.assertNotIn("formal_research_contract", run.artifacts)
        usage = {
            "budget_mode": "v3",
            "provider_attempt_ceiling": 80,
            "logical_call_ceiling": 20,
            "provider_attempts": 5,
            "logical_calls": 5,
            "llm_calls": 5,
            "group_limits": {"h1_h2": 10, "h3": 4, "h4": 6},
            "group_usage": {"h1_h2": 5, "h3": 0, "h4": 0},
            "call_receipts": [
                {
                    "call_group": "h1_h2",
                    "prompt_key": prompt,
                    "logical_call_id": f"logical-{index}",
                    "input_sha256": str(index) * 64,
                    "response_sha256": str(index + 1) * 64,
                    "output_schema_sha256": str(index + 2) * 64,
                }
                for index, prompt in enumerate(
                    [
                        "hypothesis_decomposition",
                        "candidate_plan_batch",
                        "candidate_plan_batch",
                        "reviewer_report_batch",
                        "reviewer_report_batch",
                    ],
                    start=1,
                )
            ],
        }
        current = self.engine.get_run(run.id)
        self.engine._put_artifact(current, "model_usage", usage)
        self.run = self.repository.save(current, expected_version=current.version)
        self.selected = self.run.artifacts["design_arena"]["payload"][
            "provisional_candidate_id"
        ]
        plan = next(
            item["plan"]
            for item in self.run.artifacts["design_arena"]["payload"]["candidates"]
            if item["candidate_id"] == self.selected
        )
        model = plan["baseline_models"][0]
        self.request = {
            "schema_version": "sixbench-analysis-request-v1",
            "run_id": "common-run-1",
            "case_id": "common_panel",
            "input_view": "discovery_blind",
            "system_id": "hypoweaver",
            "seed": 20260720,
            "method_selection": {
                "method": "panel_twfe",
                "rationale": "The native H2 design selected firm and year fixed effects.",
            },
            "outcome": model["outcome"],
            "treatments": model["treatments_or_exposures"],
            "controls": model["controls"],
            "fixed_effects": model["fixed_effects"],
            "standard_error": {"strategy": "cluster_entity", "cluster": "firm_id"},
            "diagnostics": [
                "sample_attrition",
                "panel_key_uniqueness",
                "cluster_count",
                "wild_cluster_bootstrap",
            ],
            "claim_plan": {
                "target_terms": ["exposure"],
                "maximum_strength": "associational",
                "rationale": "The visible design does not identify a causal effect.",
            },
        }

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def _result(self, *, data_sha: str | None = None, term: str = "exposure") -> dict:
        return {
            "schema_version": "sixbench-common-execution-result-v1",
            "run_id": self.request["run_id"],
            "case_id": self.request["case_id"],
            "input_view": self.request["input_view"],
            "system_id": "hypoweaver",
            "seed": self.request["seed"],
            "execution_mode": "common_executor_reasoning_control",
            "native_system_execution": False,
            "analysis_request": self.request,
            "executions": [
                {
                    "execution_id": "primary",
                    "status": "completed",
                    "method": "panel_twfe",
                    "estimates": [{"term": term, "coefficient": 0.25}],
                    "diagnostics": {
                        "rows_used": 24,
                        "rows_dropped_missing": 0,
                        "entity_count": 6,
                        "entity_time_key_unique": True,
                        "wild_cluster_bootstrap": {"p_value_two_sided": 0.2},
                    },
                    "requested_diagnostics": self.request["diagnostics"],
                    "claim_plan": self.request["claim_plan"],
                    "implementation_id": "common-panel-fixture-v1",
                    "independence_scope": "benchmark-owned implementation",
                    "shared_components": ["frozen input bytes"],
                }
            ],
            "provenance": {
                "executor_kind": "benchmark_owned_common_executor",
                "execution_mode": "common_executor_reasoning_control",
                "native_system_execution": False,
                "reasoning_source_system_id": "hypoweaver",
                "implementation_id": "common-panel-fixture-v1",
                "reference_executor_functions_reused": False,
                "shared_components": ["frozen input bytes"],
                "request_sha256": canonical_sha256(self.request),
                "execution_contract_sha256": "a" * 64,
                "case_manifest_sha256": "b" * 64,
                "data_sha256": data_sha or self.data_sha,
                "weights_sha256": None,
                "hidden_reference_accessed": False,
                "network_access": "denied",
                "selected_case_package_only": True,
                "contract_paths_used_for_execution": False,
            },
        }

    def _bytes(self, payload: dict) -> bytes:
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()

    def test_h2_boundary_and_request_binding_are_result_free(self) -> None:
        assert_h2_pre_result_boundary(self.run)
        request, binding = build_pre_result_binding(
            self.run,
            self.request,
            selected_candidate_id=self.selected,
        )
        self.assertEqual(request, self.request)
        self.assertEqual(binding["data_sha256"], self.data_sha)
        self.assertNotIn("formal_research_contract", self.run.artifacts)

    async def test_public_pre_hook_stops_before_h2_and_refreshes_test_dag(self) -> None:
        original_decide_gate = self.engine.decide_gate

        async def decide_with_test_receipts(run_id, gate, request):
            state = await original_decide_gate(run_id, gate, request)
            if gate == "H1":
                current = self.engine.get_run(state.id)
                self.engine._put_artifact(
                    current,
                    "model_usage",
                    self.run.artifacts["model_usage"]["payload"],
                )
                state = self.repository.save(
                    current, expected_version=current.version
                )
            return state

        with patch.object(
            self.engine, "decide_gate", side_effect=decide_with_test_receipts
        ):
            state = await run_to_h2_stop(
                self.engine,
                CreateRunRequest(
                    mode="research",
                    case=self.case,
                    model_provider="qwen",
                    execution_mode="external",
                ),
            )
        self.assertEqual((state.status, state.current_gate), ("waiting_human", "H2"))
        self.assertNotIn("formal_research_contract", state.artifacts)
        selected = state.artifacts["design_arena"]["payload"][
            "provisional_candidate_id"
        ]
        self.assertFalse(
            self.engine._refresh_h2_test_dag_if_needed(state, selected)
        )

    def test_h2_boundary_rejects_post_result_artifacts(self) -> None:
        leaked = self.run.model_copy(deep=True)
        self.engine._put_artifact(leaked, "research_run", {"result": "leak"})
        with self.assertRaisesRegex(CommonExecutorAdapterError, "post-H2 artifacts"):
            assert_h2_pre_result_boundary(leaked)

    def test_h2_boundary_rejects_budget_identity_drift(self) -> None:
        drifted = self.run.model_copy(deep=True)
        usage = dict(drifted.artifacts["model_usage"]["payload"])
        usage["provider_attempt_ceiling"] = 40
        self.engine._put_artifact(drifted, "model_usage", usage)
        with self.assertRaisesRegex(CommonExecutorAdapterError, "frozen v3 budget"):
            assert_h2_pre_result_boundary(drifted)

    def test_sealed_result_rejects_wrong_data_hash_and_unregistered_term(self) -> None:
        _, binding = build_pre_result_binding(self.run, self.request)
        with self.assertRaisesRegex(CommonExecutorAdapterError, "provenance"):
            validate_sealed_common_result(
                self._bytes(self._result(data_sha="0" * 64)),
                pre_result_binding=binding,
            )
        with self.assertRaisesRegex(CommonExecutorAdapterError, "unregistered estimate term"):
            validate_sealed_common_result(
                self._bytes(self._result(term="invented_result")),
                pre_result_binding=binding,
            )

    def test_sealed_result_rejects_identity_and_hidden_reference_drift(self) -> None:
        _, binding = build_pre_result_binding(self.run, self.request)
        wrong_identity = self._result()
        wrong_identity["run_id"] = "different-run"
        with self.assertRaisesRegex(CommonExecutorAdapterError, "binding mismatch"):
            validate_sealed_common_result(
                self._bytes(wrong_identity), pre_result_binding=binding
            )
        hidden_reference = self._result()
        hidden_reference["provenance"]["hidden_reference_accessed"] = True
        with self.assertRaisesRegex(CommonExecutorAdapterError, "provenance"):
            validate_sealed_common_result(
                self._bytes(hidden_reference), pre_result_binding=binding
            )

    async def test_ingest_skips_native_executors_and_reaches_claim_gate(self) -> None:
        result_bytes = self._bytes(self._result())
        result_sha = hashlib.sha256(result_bytes).hexdigest()
        with patch.object(
            self.engine.dataset_registry,
            "resolve",
            side_effect=AssertionError("post-result stage reopened raw case data"),
        ):
            run = await self.engine.ingest_external_research_run(
                self.run.id,
                selected_candidate_id=self.selected,
                analysis_request=self.request,
                execution_result_bytes=result_bytes,
                execution_result_sha256=result_sha,
                expected_run_version=self.run.version,
                idempotency_key="common-ingest",
            )
        self.assertEqual((run.status, run.current_gate), ("waiting_human", "H3"))
        statuses = {(step.node_id, step.status) for step in run.steps}
        self.assertIn(("common_executor", "succeeded"), statuses)
        self.assertIn(("fixture_executor", "skipped"), statuses)
        self.assertIn(("external_executor", "skipped"), statuses)
        self.assertEqual(
            run.artifacts["common_executor_result_binding"]["payload"][
                "execution_result_sha256"
            ],
            result_sha,
        )
        self.assertEqual(
            run.artifacts["reproduction_audit"]["payload"]["status"],
            "not_applicable",
        )
        self.assertTrue(run.claims)
        self.assertTrue(
            all(claim.admission_status != "admitted" for claim in run.claims)
        )
        result, result_binding = validate_sealed_common_result(
            result_bytes,
            pre_result_binding=run.artifacts["common_executor_request_binding"]["payload"],
        )
        decision = claim_decision_from_h3(
            result=result,
            result_binding=result_binding,
            claim_ledger=ClaimLedger.model_validate(
                run.artifacts["claim_ledger"]["payload"]
            ),
        )
        self.assertEqual(decision["schema_version"], "sixbench-common-claim-decision-v1")
        self.assertEqual(decision["execution_result_sha256"], result_sha)
        self.assertEqual(decision["claims"][0]["admission_status"], "rejected")

    async def test_ingest_rejects_byte_hash_mismatch_before_contract_freeze(self) -> None:
        result_bytes = self._bytes(self._result())
        with self.assertRaisesRegex(WorkflowTransitionError, "bytes do not match"):
            await self.engine.ingest_external_research_run(
                self.run.id,
                selected_candidate_id=self.selected,
                analysis_request=self.request,
                execution_result_bytes=result_bytes + b" ",
                execution_result_sha256=hashlib.sha256(result_bytes).hexdigest(),
                idempotency_key="bad-hash",
            )
        unchanged = self.engine.get_run(self.run.id)
        self.assertEqual((unchanged.status, unchanged.current_gate), ("waiting_human", "H2"))
        self.assertNotIn("formal_research_contract", unchanged.artifacts)


if __name__ == "__main__":
    unittest.main()
