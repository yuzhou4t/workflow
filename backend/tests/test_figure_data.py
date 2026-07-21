from __future__ import annotations

import hashlib
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any

from hypoweaver.figure_data import DerivedFigureInput, derive_dataset_figure_inputs
from hypoweaver.models import (
    AnalysisPlan,
    DatasetRef,
    ExecutionRecord,
    FormalResearchContract,
    ModelSpec,
    ResearchRun,
)
from hypoweaver.plot_agent.recipe_contracts import validate_recipe_data
from hypoweaver.seal import canonical_sha256
from hypoweaver.visualization import (
    FigureSource,
    build_figure_requests,
)


def _write_panel_csv(path: Path) -> None:
    rows = ["row_id,firm,year,group,x,y,control"]
    for firm_index in range(4):
        for period_index, year in enumerate(range(2012, 2022)):
            group = int(firm_index >= 2)
            x = period_index + firm_index * 0.17 + group * 0.3
            control = (period_index % 3) + firm_index * 0.2
            y = 1.5 + 0.4 * x + 0.1 * control + group * 0.25
            rows.append(
                f"row-{firm_index}-{year},F{firm_index},{year},{group},"
                f"{x:.8f},{y:.8f},{control:.8f}"
            )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _contract_for(path: Path) -> FormalResearchContract:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    dataset_ref = DatasetRef(
        dataset_id=f"dataset-{digest[:16]}",
        filename=path.name,
        sha256=digest,
        size_bytes=path.stat().st_size,
    )
    baseline = ModelSpec(
        step_id="baseline-model",
        name="Frozen panel baseline",
        rationale="Exercise deterministic plot-data derivation.",
        estimator="two-way fixed effects",
        formula="y ~ x + control",
        outcome="y",
        treatments_or_exposures=["x"],
        controls=["control"],
        fixed_effects=["firm", "year"],
        standard_error_strategy="clustered_by_entity",
        parameters={
            "policy_design": {
                "time_field": "year",
                "group_field": "group",
                "policy_start_year": 2016,
            }
        },
    )
    plan = AnalysisPlan(
        plan_id="plan-figure-data",
        plan_version=1,
        method_family="panel_association",
        design_only=False,
        estimands=[],
        sample_rules=[],
        variable_construction=[],
        baseline_models=[baseline],
        diagnostics=[],
        robustness_tests=[],
        falsification_tests=[],
        mechanism_tests=[],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=["firm", "year", "group", "x", "y", "control"],
        unsupported_requested_analyses=[],
    )
    plan_hash = canonical_sha256(plan.model_dump(mode="json"))
    return FormalResearchContract(
        contract_id="contract-figure-data",
        case_id="case-figure-data",
        approved_at="2026-07-22T00:00:00+00:00",
        approved_by="test",
        decision_record_id="decision-figure-data",
        research_package_hash="c" * 64,
        data_hashes=[digest],
        dataset_refs=[dataset_ref],
        approved_plan_hash=plan_hash,
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(str(key) for key in value)
        for item in value.values():
            keys.update(_all_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_keys(item))
    return keys


class FigureDataDerivationTests(unittest.TestCase):
    def test_derives_only_deterministic_aggregate_plot_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "panel.csv"
            _write_panel_csv(path)
            contract = _contract_for(path)

            first, first_warnings = derive_dataset_figure_inputs(contract, path)
            second, second_warnings = derive_dataset_figure_inputs(contract, path)

            self.assertEqual(first, second)
            self.assertEqual(first_warnings, second_warnings)
            self.assertEqual(first_warnings, [])
            by_recipe: dict[str, list[DerivedFigureInput]] = defaultdict(list)
            for item in first:
                by_recipe[item.recipe_id].append(item)
                self.assertEqual(
                    validate_recipe_data(item.recipe_id, item.data),
                    item.data,
                )

            self.assertTrue(
                {
                    "grouped_time_series",
                    "descriptive_statistics",
                    "correlation_heatmap",
                    "distribution_histogram",
                    "box_plot",
                    "scatter_plot",
                }.issubset(by_recipe)
            )

            descriptive = by_recipe["descriptive_statistics"][0].data
            assert isinstance(descriptive, list)
            self.assertEqual(
                [item["variable"] for item in descriptive],
                ["y", "x", "control"],
            )
            self.assertTrue(
                all(
                    item["n"] == 40
                    and item["missing"] == 0
                    and item["sample_scope"] == "frozen_source_rows"
                    for item in descriptive
                )
            )

            correlation = by_recipe["correlation_heatmap"][0].data
            assert isinstance(correlation, dict)
            self.assertEqual(correlation["variables"], ["y", "x", "control"])
            self.assertEqual(correlation["method"], "pearson")
            self.assertEqual(correlation["sample_policy"], "listwise_complete")
            self.assertEqual(correlation["sample_scope"], "frozen_source_rows")
            self.assertEqual(correlation["n"], 40)

            histograms = {
                item.data["variable"]: item.data
                for item in by_recipe["distribution_histogram"]
                if isinstance(item.data, dict)
            }
            self.assertEqual(set(histograms), {"x", "y"})
            self.assertTrue(
                all(
                    sum(bin_["count"] for bin_ in payload["bins"])
                    == payload["n"]
                    == 40
                    for payload in histograms.values()
                )
            )

            trend = by_recipe["grouped_time_series"][0].data
            assert isinstance(trend, dict)
            self.assertEqual(trend["value_name"], "y")
            self.assertEqual(trend["time_variable"], "year")
            self.assertEqual(trend["series_variable"], "group")
            self.assertEqual(
                trend["series_labels"],
                {"0": "Control (0)", "1": "Treated (1)"},
            )
            self.assertEqual(trend["intervention_period"], 2016.0)
            self.assertEqual(trend["sample_scope"], "frozen_source_rows")
            self.assertEqual(
                {item["series"] for item in trend["records"]},
                {"0", "1"},
            )
            self.assertEqual(
                {item["period"] for item in trend["records"]},
                set(range(2012, 2022)),
            )
            self.assertEqual(len(trend["records"]), 20)

            box = by_recipe["box_plot"][0].data
            assert isinstance(box, dict)
            self.assertEqual(box["variable"], "y")
            self.assertEqual(box["group_variable"], "group")
            self.assertEqual(box["whisker_rule"], "tukey_1_5_iqr")
            self.assertEqual(box["sample_scope"], "frozen_source_rows")
            self.assertEqual(
                [item["group"] for item in box["groups"]],
                ["Control (0)", "Treated (1)"],
            )

            scatter = by_recipe["scatter_plot"][0].data
            assert isinstance(scatter, dict)
            self.assertEqual(scatter["grain"], "bin")
            self.assertEqual(scatter["sample_scope"], "frozen_source_rows")
            self.assertGreaterEqual(len(scatter["points"]), 8)
            self.assertLessEqual(len(scatter["points"]), 500)
            self.assertEqual(sum(item["n"] for item in scatter["points"]), 40)

            all_payloads = [item.data for item in first]
            keys = _all_keys(all_payloads)
            self.assertNotIn("row_id", keys)
            self.assertNotIn("firm", keys)
            self.assertNotIn("execution_id", keys)
            self.assertNotIn("row-0-2012", str(all_payloads))

    def test_rejects_dataset_after_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "panel.csv"
            _write_panel_csv(path)
            contract = _contract_for(path)
            derive_dataset_figure_inputs(contract, path)

            path.write_bytes(path.read_bytes() + b"tampered")

            with self.assertRaisesRegex(ValueError, "SHA256"):
                derive_dataset_figure_inputs(contract, path)

    def test_source_row_aggregates_do_not_impersonate_estimation_sample(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "panel.csv"
            _write_panel_csv(path)
            contract = _contract_for(path)
            run = ResearchRun(
                research_run_id="research-source-scope",
                case_id=contract.case_id,
                contract_hash="frozen-contract-hash",
                plan_version=contract.approved_plan.plan_version,
                execution_status="succeeded",
                scientific_status="valid",
                fixture_only=False,
                executions=[
                    ExecutionRecord(
                        execution_id="execution-baseline",
                        run_type="baseline",
                        plan_step_id="baseline-model",
                        execution_status="succeeded",
                        diagnostic_results={
                            "rows_input": 40,
                            "rows_used": 32,
                            "rows_dropped": 8,
                        },
                    )
                ],
            )
            research_source = FigureSource(
                artifact_id="workflow-run:research_run",
                artifact_key="research_run",
                sha256="a" * 64,
            )
            dataset_ref = contract.dataset_refs[0]
            dataset_source = FigureSource(
                artifact_id=f"dataset:{dataset_ref.dataset_id}",
                artifact_key=dataset_ref.filename,
                sha256=dataset_ref.sha256,
            )

            requests, _ = build_figure_requests(
                run,
                research_source,
                "evidence",
                contract=contract,
                dataset_path=path,
                dataset_source=dataset_source,
            )
            derived = [request for request in requests if request.data_sources]

            self.assertTrue(derived)
            self.assertTrue(
                all(request.execution_ids == [] for request in derived)
            )
            self.assertTrue(
                all(
                    "frozen_source_rows" in str(request.bindings.data)
                    for request in derived
                )
            )


if __name__ == "__main__":
    unittest.main()
