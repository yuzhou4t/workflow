from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from pydantic import ValidationError

import hypoweaver.api as api_module
from hypoweaver.definition import build_app_a_definition
from hypoweaver.engine import PRESET_CASES, WorkflowEngine
from hypoweaver.models import (
    ClaimLedger,
    ClaimRecord,
    ExecutionRecord,
    ResearchRun,
    RunState,
)
from hypoweaver.plot_agent.recipe_contracts import (
    RECIPE_IDS,
    RecipeId,
    recipe_data_snapshot,
)
from hypoweaver.plot_agent.renderer import resolve_artifact_uri
from hypoweaver.repository import RunRepository
from hypoweaver.visualization import (
    FigureRequest,
    FigureSource,
    LocalFigureRenderer,
    build_figure_requests,
)


def _research_run() -> ResearchRun:
    return ResearchRun(
        research_run_id="research-001",
        case_id="case-001",
        contract_hash="contract-sha256",
        plan_version=1,
        execution_status="succeeded",
        scientific_status="valid",
        fixture_only=False,
        executions=[
            ExecutionRecord(
                execution_id="execution-baseline",
                run_type="baseline",
                plan_step_id="baseline-model",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "green_finance",
                        "coefficient": 0.25,
                        "p_value": 0.04,
                        "confidence_interval_95": [0.02, 0.48],
                        "nobs": 120,
                    }
                ],
                diagnostic_results={
                    "rows_input": 150,
                    "rows_used": 120,
                    "rows_dropped": 30,
                },
            ),
            ExecutionRecord(
                execution_id="execution-robustness",
                run_type="robustness",
                plan_step_id="robustness-model",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "green_finance",
                        "coefficient": 0.2,
                        "confidence_interval_95": [-0.01, 0.41],
                        "nobs": 118,
                    }
                ],
                diagnostic_results={
                    "rows_input": 150,
                    "rows_used": 118,
                    "rows_dropped": 32,
                },
            ),
        ],
    )


def _approved_ledger() -> ClaimLedger:
    return ClaimLedger(
        ledger_id="ledger-001",
        case_id="case-001",
        research_run_id="research-001",
        claims=[
            ClaimRecord(
                claim_id="claim-H1",
                hypothesis_id="H1",
                claim_text="绿色金融变量与结果变量存在条件关联。",
                final_text="绿色金融变量与结果变量存在条件关联。",
                evidence_status="supported",
                allowed_strength="associational",
                supporting_runs=["execution-baseline"],
                opposing_runs=[],
                scope="冻结样本",
                robustness_status="limited",
                unresolved_risks=[],
                approval_status="approved",
                claim_type="associational",
            ),
            ClaimRecord(
                claim_id="claim-H2",
                hypothesis_id="H2",
                claim_text="未获授权结论。",
                evidence_status="inconclusive",
                allowed_strength="prohibited",
                supporting_runs=["execution-robustness"],
                opposing_runs=[],
                scope="冻结样本",
                robustness_status="not_admitted",
                unresolved_risks=[],
                approval_status="rejected",
                claim_type="associational",
            ),
        ],
        excluded_findings=[],
        unresolved_issues=[],
    )


def _research_run_with_specialized_results() -> ResearchRun:
    run = _research_run().model_copy(deep=True)
    run.executions.extend(
        [
            ExecutionRecord(
                execution_id="execution-event-study",
                run_type="falsification",
                plan_step_id="event-study",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "event_2018",
                        "coefficient": -0.08,
                        "confidence_interval_95": [-0.18, 0.02],
                        "event_year": 2018,
                        "relative_year": -2,
                    },
                    {
                        "term": "event_2020",
                        "coefficient": 0.12,
                        "confidence_interval_95": [0.01, 0.23],
                        "event_year": 2020,
                        "relative_year": 0,
                    },
                    {
                        "term": "event_2021",
                        "coefficient": 0.16,
                        "confidence_interval_95": [0.03, 0.29],
                        "event_year": 2021,
                        "relative_year": 1,
                    },
                ],
                diagnostic_results={
                    "reference_year": 2019,
                    "joint_pretrend_p_value": 0.42,
                },
            ),
            ExecutionRecord(
                execution_id="execution-heterogeneity-state",
                run_type="heterogeneity",
                plan_step_id="heterogeneity-state",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "green_finance",
                        "coefficient": 0.31,
                        "confidence_interval_95": [0.05, 0.57],
                        "nobs": 64,
                    }
                ],
                diagnostic_results={
                    "subgroup_variable": "ownership",
                    "subgroup_value": "state",
                },
            ),
            ExecutionRecord(
                execution_id="execution-heterogeneity-private",
                run_type="heterogeneity",
                plan_step_id="heterogeneity-private",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "green_finance",
                        "coefficient": 0.11,
                        "confidence_interval_95": [-0.09, 0.31],
                        "nobs": 56,
                    }
                ],
                diagnostic_results={
                    "subgroup_variable": "ownership",
                    "subgroup_value": "private",
                },
            ),
        ]
    )
    return run


def _valid_recipe_data() -> dict[RecipeId, list[dict[str, Any]] | dict[str, Any]]:
    return {
        "coefficient_forest": [
            {
                "term": "green_finance",
                "coefficient": 0.25,
                "ci_lower": 0.02,
                "ci_upper": 0.48,
                "execution_id": "execution-render",
                "p_value": 0.04,
                "sample_size": 120,
            }
        ],
        "sample_flow": {
            "rows_input": 150,
            "rows_used": 120,
            "rows_dropped": 30,
        },
        "event_study": {
            "points": [
                {
                    "relative_time": -2.0,
                    "event_year": 2018,
                    "coefficient": -0.08,
                    "ci_lower": -0.18,
                    "ci_upper": 0.02,
                    "execution_id": "execution-event",
                },
                {
                    "relative_time": 0.0,
                    "event_year": 2020,
                    "coefficient": 0.12,
                    "ci_lower": 0.01,
                    "ci_upper": 0.23,
                    "execution_id": "execution-event",
                },
                {
                    "relative_time": 1.0,
                    "event_year": 2021,
                    "coefficient": 0.16,
                    "ci_lower": 0.03,
                    "ci_upper": 0.29,
                    "execution_id": "execution-event",
                },
            ],
            "reference_period": -1.0,
            "joint_pretrend_p_value": 0.42,
        },
        "grouped_time_series": {
            "value_name": "outcome",
            "time_variable": "year",
            "series_variable": "treated",
            "series_labels": {
                "0": "Control (0)",
                "1": "Treated (1)",
            },
            "sample_scope": "upstream_aggregate",
            "intervention_period": 2020.0,
            "records": [
                {"period": 2019.0, "period_label": "2019", "series": "0", "value": 1.1, "n": 20},
                {"period": 2020.0, "period_label": "2020", "series": "0", "value": 1.2, "n": 20},
                {"period": 2019.0, "period_label": "2019", "series": "1", "value": 1.0, "n": 20},
                {"period": 2020.0, "period_label": "2020", "series": "1", "value": 1.4, "n": 20},
            ],
        },
        "heterogeneity_forest": [
            {
                "subgroup": "ownership=private",
                "subgroup_variable": "ownership",
                "term": "green_finance",
                "coefficient": 0.11,
                "ci_lower": -0.09,
                "ci_upper": 0.31,
                "execution_id": "execution-heterogeneity-private",
                "sample_size": 56,
            },
            {
                "subgroup": "ownership=state",
                "subgroup_variable": "ownership",
                "term": "green_finance",
                "coefficient": 0.31,
                "ci_lower": 0.05,
                "ci_upper": 0.57,
                "execution_id": "execution-heterogeneity-state",
                "sample_size": 64,
            },
        ],
        "specification_curve": {
            "term": "green_finance",
            "points": [
                {
                    "specification": "baseline",
                    "run_type": "baseline",
                    "coefficient": 0.25,
                    "ci_lower": 0.02,
                    "ci_upper": 0.48,
                    "execution_id": "execution-baseline",
                },
                {
                    "specification": "robustness",
                    "run_type": "robustness",
                    "coefficient": 0.2,
                    "ci_lower": -0.01,
                    "ci_upper": 0.41,
                    "execution_id": "execution-robustness",
                },
            ],
        },
        "descriptive_statistics": [
            {
                "variable": "green_finance",
                "sample_scope": "upstream_aggregate",
                "n": 120,
                "missing": 2,
                "mean": 0.5,
                "std": 0.2,
                "min": 0.0,
                "q1": 0.35,
                "median": 0.5,
                "q3": 0.65,
                "max": 1.0,
            }
        ],
        "correlation_heatmap": {
            "variables": ["green_finance", "outcome"],
            "matrix": [[1.0, 0.25], [0.25, 1.0]],
            "method": "pearson",
            "sample_policy": "listwise_complete",
            "sample_scope": "upstream_aggregate",
            "n": 120,
        },
        "distribution_histogram": {
            "variable": "green_finance",
            "sample_scope": "upstream_aggregate",
            "bins": [
                {"lower": 0.0, "upper": 0.5, "count": 45},
                {"lower": 0.5, "upper": 1.0, "count": 75},
            ],
            "binning_rule": "fixed_width_2",
            "n": 120,
        },
        "box_plot": {
            "variable": "outcome",
            "group_variable": "treated",
            "whisker_rule": "tukey_1_5_iqr",
            "sample_scope": "upstream_aggregate",
            "groups": [
                {
                    "group": "control",
                    "whisker_low": 0.0,
                    "q1": 0.2,
                    "median": 0.4,
                    "q3": 0.6,
                    "whisker_high": 0.9,
                    "n": 60,
                },
                {
                    "group": "treated",
                    "whisker_low": 0.1,
                    "q1": 0.3,
                    "median": 0.5,
                    "q3": 0.75,
                    "whisker_high": 1.0,
                    "n": 60,
                },
            ],
        },
        "scatter_plot": {
            "x_variable": "green_finance",
            "y_variable": "outcome",
            "sample_scope": "upstream_aggregate",
            "grain": "bin",
            "points": [
                {
                    "x": index / 10,
                    "y": 0.2 + index / 20,
                    "n": 15,
                    "label": f"bin-{index + 1}",
                }
                for index in range(8)
            ],
        },
        "spatial_choropleth": {
            "crs": "EPSG:4326",
            "value_name": "green_finance",
            "geometry_source_sha256": "d" * 64,
            "value_source_sha256": "e" * 64,
            "regions": [
                {
                    "region_id": "region-a",
                    "label": "Region A",
                    "value": 0.4,
                    "polygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]],
                },
                {
                    "region_id": "region-b",
                    "label": "Region B",
                    "value": 0.7,
                    "polygon": [[1.0, 0.0], [2.0, 0.0], [2.0, 1.0], [1.0, 0.0]],
                },
            ],
        },
        "mechanism_evidence_graph": {
            "nodes": [
                {"node_id": "green_finance", "label": "Green finance"},
                {"node_id": "mediator", "label": "Mediator"},
                {"node_id": "outcome", "label": "Outcome"},
            ],
            "edges": [
                {
                    "edge_id": "edge-theory",
                    "source": "green_finance",
                    "target": "mediator",
                    "edge_kind": "hypothesized",
                    "label": "pre-specified path",
                },
                {
                    "edge_id": "edge-estimated",
                    "source": "mediator",
                    "target": "outcome",
                    "edge_kind": "hypothesized",
                    "label": "pre-specified second path",
                },
            ],
        },
    }


def _request_for_recipe(
    recipe_id: RecipeId,
    data: list[dict[str, Any]] | dict[str, Any],
    source: FigureSource,
) -> FigureRequest:
    execution_ids: list[str] = []

    def collect_execution_ids(value: Any) -> None:
        if isinstance(value, dict):
            execution_id = value.get("execution_id")
            if isinstance(execution_id, str) and execution_id not in execution_ids:
                execution_ids.append(execution_id)
            for item in value.values():
                collect_execution_ids(item)
        elif isinstance(value, list):
            for item in value:
                collect_execution_ids(item)

    collect_execution_ids(data)
    return FigureRequest(
        request_id=f"request-{recipe_id}",
        stage="evidence",
        case_id="case-render",
        research_run_id="research-render",
        contract_hash="contract-render",
        recipe_id=recipe_id,
        source=source,
        execution_ids=execution_ids or ["execution-render"],
        bindings={"data": data},
    )


class FigureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = FigureSource(
            artifact_id="workflow-run:research_run",
            artifact_key="research_run",
            sha256="a" * 64,
        )

    def test_contract_rejects_unknown_fields(self) -> None:
        request, _ = build_figure_requests(
            _research_run(),
            self.source,
            "evidence",
        )
        payload = request[0].model_dump(mode="json")
        payload["unknown"] = True

        with self.assertRaises(ValidationError):
            FigureRequest.model_validate(payload)

    def test_definition_places_plot_agent_on_both_workflow_boundaries(self) -> None:
        definition = build_app_a_definition()
        edges = {
            (edge["source"], edge["target"])
            for edge in definition["edges"]
        }

        self.assertIn(
            ("reproduction_audit", "evidence_visualization"),
            edges,
        )
        self.assertIn(
            ("evidence_visualization", "evidence_assessment"),
            edges,
        )
        self.assertIn(
            ("h3_gate", "publication_visualization"),
            edges,
        )
        self.assertIn(
            ("publication_visualization", "scientific_writer"),
            edges,
        )

    def test_evidence_requests_use_only_real_execution_values(self) -> None:
        requests, warnings = build_figure_requests(
            _research_run(),
            self.source,
            "evidence",
        )

        self.assertEqual(
            [request.recipe_id for request in requests],
            ["coefficient_forest", "sample_flow", "specification_curve"],
        )
        coefficient = requests[0]
        self.assertEqual(
            coefficient.execution_ids,
            ["execution-baseline", "execution-robustness"],
        )
        self.assertEqual(coefficient.claim_ids, [])
        self.assertEqual(
            coefficient.bindings.data[0]["coefficient"],  # type: ignore[index]
            0.25,
        )
        self.assertEqual(warnings, [])

    def test_publication_requests_exclude_rejected_claim_executions(self) -> None:
        run = _research_run()
        run.executions[0].estimates.append(
            {
                "term": "unapproved_control",
                "coefficient": 0.1,
                "confidence_interval_95": [0.01, 0.19],
                "nobs": 120,
            }
        )
        requests, warnings = build_figure_requests(
            run,
            self.source,
            "publication",
            approved_ledger=_approved_ledger(),
            allowed_estimate_terms={"green_finance"},
        )

        self.assertEqual(len(requests), 2)
        self.assertTrue(
            all(
                request.execution_ids == ["execution-baseline"]
                and request.claim_ids == ["claim-H1"]
                for request in requests
            )
        )
        self.assertNotIn("execution-robustness", str(requests))
        self.assertNotIn("unapproved_control", str(requests))
        self.assertEqual(
            warnings,
            ["论文图已排除未进入 Writer 授权范围的估计项。"],
        )

    def test_specialized_results_route_only_to_matching_recipes(self) -> None:
        requests, warnings = build_figure_requests(
            _research_run_with_specialized_results(),
            self.source,
            "evidence",
        )
        by_recipe = {request.recipe_id: request for request in requests}

        self.assertEqual(
            list(by_recipe),
            [
                "coefficient_forest",
                "sample_flow",
                "event_study",
                "heterogeneity_forest",
                "specification_curve",
            ],
        )
        coefficient = by_recipe["coefficient_forest"]
        self.assertEqual(
            coefficient.execution_ids,
            ["execution-baseline", "execution-robustness"],
        )
        self.assertNotIn("event_", str(coefficient.bindings.data))
        self.assertNotIn("ownership=", str(coefficient.bindings.data))

        event = by_recipe["event_study"]
        self.assertEqual(event.execution_ids, ["execution-event-study"])
        event_data = event.bindings.data
        assert isinstance(event_data, dict)
        self.assertEqual(event_data["reference_period"], -1.0)
        self.assertEqual(event_data["joint_pretrend_p_value"], 0.42)
        self.assertEqual(
            [point["relative_time"] for point in event_data["points"]],
            [-2.0, 0.0, 1.0],
        )
        self.assertEqual(
            event_data["points"][0],
            {
                "relative_time": -2.0,
                "event_year": 2018,
                "coefficient": -0.08,
                "ci_lower": -0.18,
                "ci_upper": 0.02,
                "execution_id": "execution-event-study",
            },
        )

        heterogeneity = by_recipe["heterogeneity_forest"]
        heterogeneity_data = heterogeneity.bindings.data
        assert isinstance(heterogeneity_data, list)
        self.assertEqual(
            [row["subgroup"] for row in heterogeneity_data],
            ["ownership=private", "ownership=state"],
        )
        self.assertEqual(
            [row["coefficient"] for row in heterogeneity_data],
            [0.11, 0.31],
        )
        self.assertEqual(
            [row["sample_size"] for row in heterogeneity_data],
            [56, 64],
        )

        specification = by_recipe["specification_curve"]
        specification_data = specification.bindings.data
        assert isinstance(specification_data, dict)
        self.assertEqual(
            [point["run_type"] for point in specification_data["points"]],
            ["baseline", "robustness"],
        )
        self.assertEqual(
            specification.execution_ids,
            ["execution-baseline", "execution-robustness"],
        )
        self.assertEqual(warnings, [])

    def test_explicit_inputs_reject_unhashable_recipe_and_unfrozen_mechanism(
        self,
    ) -> None:
        run = _research_run()
        run.executions[0].diagnostic_results["figure_inputs"] = [
            {"recipe_id": ["not-a-string"], "data": {}},
            {
                "recipe_id": "mechanism_evidence_graph",
                "data": {
                    "nodes": [
                        {"node_id": "source", "label": "Source"},
                        {"node_id": "target", "label": "Target"},
                    ],
                    "edges": [
                        {
                            "edge_id": "unfrozen-path",
                            "source": "source",
                            "target": "target",
                            "edge_kind": "hypothesized",
                            "label": "proposed path",
                        }
                    ],
                },
            },
        ]

        requests, warnings = build_figure_requests(
            run,
            self.source,
            "evidence",
        )

        self.assertNotIn(
            "mechanism_evidence_graph",
            [request.recipe_id for request in requests],
        )
        self.assertTrue(any("不受支持" in warning for warning in warnings))
        self.assertTrue(
            any("frozen mechanism step" in warning for warning in warnings)
        )

    def test_duplicate_term_skips_only_ambiguous_specification_curve(self) -> None:
        run = _research_run()
        run.executions[0].estimates.append(
            {
                "term": "green_finance",
                "coefficient": 0.23,
                "confidence_interval_95": [0.01, 0.45],
                "nobs": 120,
            }
        )

        requests, warnings = build_figure_requests(
            run,
            self.source,
            "evidence",
        )

        self.assertIn(
            "coefficient_forest",
            [request.recipe_id for request in requests],
        )
        self.assertNotIn(
            "specification_curve",
            [request.recipe_id for request in requests],
        )
        self.assertTrue(any("规格曲线" in warning for warning in warnings))

    def test_invalid_recipe_inputs_fail_closed_at_request_boundary(self) -> None:
        valid = _valid_recipe_data()
        invalid: dict[RecipeId, dict[str, Any]] = {
            "correlation_heatmap": {
                **valid["correlation_heatmap"],  # type: ignore[dict-item]
                "matrix": [[1.0, 0.2], [0.3, 1.0]],
            },
            "box_plot": {
                "variable": "outcome",
                "group_variable": "treated",
                "whisker_rule": "tukey_1_5_iqr",
                "sample_scope": "upstream_aggregate",
                "groups": [
                    {
                        "group": "bad",
                        "whisker_low": 0.0,
                        "q1": 0.8,
                        "median": 0.5,
                        "q3": 0.2,
                        "whisker_high": 1.0,
                        "n": 10,
                    }
                ],
            },
            "spatial_choropleth": {
                "crs": "EPSG:4326",
                "value_name": "outcome",
                "geometry_source_sha256": "d" * 64,
                "value_source_sha256": "e" * 64,
                "regions": [
                    {
                        "region_id": "open-ring",
                        "label": "Open ring",
                        "value": 0.2,
                        "polygon": [
                            [0.0, 0.0],
                            [1.0, 0.0],
                            [1.0, 1.0],
                            [0.0, 1.0],
                        ],
                    }
                ],
            },
            "mechanism_evidence_graph": {
                "nodes": [
                    {"node_id": "same", "label": "Same"},
                    {"node_id": "other", "label": "Other"},
                ],
                "edges": [
                    {
                        "edge_id": "self-loop",
                        "source": "same",
                        "target": "same",
                        "edge_kind": "hypothesized",
                        "label": "invalid",
                    }
                ],
            },
        }

        for recipe_id, data in invalid.items():
            with self.subTest(recipe_id=recipe_id):
                with self.assertRaises(ValidationError):
                    _request_for_recipe(recipe_id, data, self.source)

        with self.assertRaises(ValidationError):
            _request_for_recipe(
                "correlation_heatmap",
                {
                    "variables": ["a", "b", "c"],
                    "matrix": [
                        [1.0, 0.9, 0.9],
                        [0.9, 1.0, -0.9],
                        [0.9, -0.9, 1.0],
                    ],
                    "method": "pearson",
                    "sample_policy": "listwise_complete",
                    "sample_scope": "upstream_aggregate",
                    "n": 50,
                },
                self.source,
            )

        with self.assertRaises(ValidationError):
            _request_for_recipe(
                "correlation_heatmap",
                {
                    "variables": ["a", "b", "c"],
                    "matrix": [
                        [1.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0],
                        [0.0, 0.0, 1.0],
                    ],
                    "method": "pearson",
                    "sample_policy": "listwise_complete",
                    "sample_scope": "upstream_aggregate",
                    "n": 2,
                },
                self.source,
            )

        unbalanced_trend = {
            **valid["grouped_time_series"],  # type: ignore[dict-item]
            "records": [
                {"period": 2018.0, "series": "0", "value": 1.0},
                {"period": 2020.0, "series": "0", "value": 1.2},
                {"period": 2019.0, "series": "1", "value": 1.1},
                {"period": 2020.0, "series": "1", "value": 1.4},
            ],
        }
        with self.assertRaises(ValidationError):
            _request_for_recipe(
                "grouped_time_series",
                unbalanced_trend,
                self.source,
            )

        split_antimeridian_regions = {
            **valid["spatial_choropleth"],  # type: ignore[dict-item]
            "regions": [
                {
                    "region_id": "west",
                    "label": "West",
                    "value": 0.2,
                    "polygon": [
                        [-179.5, 0.0],
                        [-178.5, 0.0],
                        [-178.5, 1.0],
                        [-179.5, 0.0],
                    ],
                },
                {
                    "region_id": "east",
                    "label": "East",
                    "value": 0.3,
                    "polygon": [
                        [178.5, 0.0],
                        [179.5, 0.0],
                        [179.5, 1.0],
                        [178.5, 0.0],
                    ],
                },
            ],
        }
        with self.assertRaises(ValidationError):
            _request_for_recipe(
                "spatial_choropleth",
                split_antimeridian_regions,
                self.source,
            )

        hypothesized_with_empirical_binding = {
            "nodes": [
                {"node_id": "source", "label": "Source"},
                {"node_id": "target", "label": "Target"},
            ],
            "edges": [
                {
                    "edge_id": "hypothesis-with-claim",
                    "source": "source",
                    "target": "target",
                    "edge_kind": "hypothesized",
                    "label": "not estimated",
                    "claim_id": "claim-should-not-bind",
                }
            ],
        }
        with self.assertRaises(ValidationError):
            _request_for_recipe(
                "mechanism_evidence_graph",
                hypothesized_with_empirical_binding,
                self.source,
            )

        for invalid_data in (
            {
                "crs": "EPSG:4326",
                "value_name": "outcome",
                "geometry_source_sha256": "d" * 64,
                "value_source_sha256": "e" * 64,
                "regions": [
                    {
                        "region_id": "bow-tie",
                        "label": "Bow tie",
                        "value": 0.2,
                        "polygon": [
                            [0.0, 0.0],
                            [1.0, 1.0],
                            [0.0, 1.0],
                            [1.0, 0.0],
                            [0.0, 0.0],
                        ],
                    }
                ],
            },
            {
                "nodes": [
                    {"node_id": "source", "label": "Source"},
                    {"node_id": "target", "label": "Target"},
                ],
                "edges": [
                    {
                        "edge_id": "causal-language",
                        "source": "source",
                        "target": "target",
                        "edge_kind": "hypothesized",
                        "label": "promotes outcome",
                    }
                ],
            },
        ):
            with self.assertRaises(ValidationError):
                _request_for_recipe(
                    (
                        "spatial_choropleth"
                        if "regions" in invalid_data
                        else "mechanism_evidence_graph"
                    ),
                    invalid_data,
                    self.source,
                )

    def test_fixture_never_creates_figure_requests(self) -> None:
        fixture = ResearchRun(
            research_run_id="fixture-run",
            case_id="case-001",
            contract_hash="contract-sha256",
            plan_version=1,
            execution_status="fixture_only",
            scientific_status="not_evaluated",
            fixture_only=True,
        )

        requests, warnings = build_figure_requests(
            fixture,
            self.source,
            "evidence",
        )

        self.assertEqual(requests, [])
        self.assertIn("Fixture", warnings[0])


@unittest.skipUnless(
    importlib.util.find_spec("matplotlib") is not None,
    "matplotlib is installed by backend/requirements.txt",
)
class LocalPlotAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_recipe_renderers_write_expected_artifacts_and_snapshots(
        self,
    ) -> None:
        self.assertEqual(tuple(_valid_recipe_data()), RECIPE_IDS)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = FigureSource(
                artifact_id="workflow-run:research_run",
                artifact_key="research_run",
                sha256="b" * 64,
            )
            renderer = LocalFigureRenderer(root)
            rendered = {}

            for recipe_id, data in _valid_recipe_data().items():
                with self.subTest(recipe_id=recipe_id):
                    request = _request_for_recipe(recipe_id, data, source)
                    bundle = await renderer.render(request)
                    figure = bundle.figures[0]
                    rendered[recipe_id] = (request, bundle)

                    self.assertEqual(bundle.status, "succeeded")
                    self.assertEqual(figure.recipe_id, recipe_id)
                    self.assertEqual(
                        {item.format for item in figure.files},
                        {"svg", "png", "pdf", "csv"},
                    )
                    self.assertEqual(
                        figure.data_snapshot,
                        recipe_data_snapshot(recipe_id, request.bindings.data),
                    )
                    for file in figure.files:
                        self.assertTrue(
                            resolve_artifact_uri(
                                file.artifact_uri,
                                artifact_root=root,
                                expected_sha256=file.sha256,
                            ).is_file()
                        )

            repeated_request, first = rendered["event_study"]
            second = await renderer.render(repeated_request)
            self.assertEqual(
                [item.sha256 for item in first.figures[0].files],
                [item.sha256 for item in second.figures[0].files],
            )

    async def test_renderer_writes_all_formats_with_stable_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = FigureSource(
                artifact_id="workflow-run:research_run",
                artifact_key="research_run",
                sha256="a" * 64,
            )
            requests, _ = build_figure_requests(
                _research_run(),
                source,
                "evidence",
            )
            renderer = LocalFigureRenderer(root)

            first = await renderer.render(requests[0])
            second = await renderer.render(requests[0])

            first_files = first.figures[0].files
            second_files = second.figures[0].files
            self.assertEqual(
                [item.sha256 for item in first_files],
                [item.sha256 for item in second_files],
            )
            self.assertEqual(
                {item.format for item in first_files},
                {"svg", "png", "pdf", "csv"},
            )
            for file in first_files:
                path = resolve_artifact_uri(
                    file.artifact_uri,
                    artifact_root=root,
                    expected_sha256=file.sha256,
                )
                self.assertTrue(path.is_file())
            self.assertEqual(
                first.figures[0].data_snapshot,
                {"records": requests[0].bindings.data},
            )
            self.assertNotIn("显著", first.figures[0].caption)

            png = next(file for file in first_files if file.format == "png")
            png_path = resolve_artifact_uri(
                png.artifact_uri,
                artifact_root=root,
            )
            png_path.write_bytes(png_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ValueError, "sha256"):
                resolve_artifact_uri(
                    png.artifact_uri,
                    artifact_root=root,
                    expected_sha256=png.sha256,
                )
            with self.assertRaisesRegex(
                RuntimeError,
                "immutable figure artifact collision",
            ):
                await renderer.render(requests[0])

    async def test_main_api_streams_hash_verified_figure_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine = WorkflowEngine(RunRepository(root / "runs.db"))
            state = RunState(
                case_id="case-001",
                case_name="Figure API Test",
                mode="research",
                case_submission=PRESET_CASES["green-finance-did"],
            )
            run = _research_run()
            envelope = engine._put_artifact(state, "research_run", run)
            requests, _ = build_figure_requests(
                run,
                FigureSource(
                    artifact_id=envelope["artifact_id"],
                    artifact_key="research_run",
                    sha256=envelope["sha256"],
                ),
                "evidence",
            )
            bundle = await LocalFigureRenderer(root / "figures").render(
                requests[0]
            )
            engine._put_artifact(state, "evidence_figure_bundle", bundle)
            engine.repository.create(state)
            figure = bundle.figures[0]
            png = next(file for file in figure.files if file.format == "png")

            def resolve_for_test(
                artifact_uri: str,
                *,
                expected_sha256: str | None = None,
            ) -> Path:
                return resolve_artifact_uri(
                    artifact_uri,
                    artifact_root=root / "figures",
                    expected_sha256=expected_sha256,
                )

            with (
                patch.object(api_module, "engine", engine),
                patch.object(
                    api_module,
                    "resolve_artifact_uri",
                    side_effect=resolve_for_test,
                ),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api_module.app),
                    base_url="http://127.0.0.1",
                ) as client:
                    response = await client.get(
                        f"/api/v1/runs/{state.id}/figures/{figure.figure_id}/png"
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(
                hashlib.sha256(response.content).hexdigest(),
                png.sha256,
            )


if __name__ == "__main__":
    unittest.main()
