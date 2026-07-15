from __future__ import annotations

import hashlib
import math
import tempfile
import unittest
from pathlib import Path

import httpx

import hypoweaver.research_api as research_api_module
from hypoweaver.case_import import DatasetRegistry
from hypoweaver.models import (
    AnalysisPlan,
    DatasetRef,
    FormalResearchContract,
    ModelSpec,
    PlannedStep,
)
from hypoweaver.research_engine import PanelResearchEngine


class PanelResearchEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.csv_path = self.root / "panel.csv"
        rows = ["firm,year,y,y_alt,x,x_alt,size,mediator"]
        for firm_index, firm in enumerate(("A", "B", "C", "D"), start=1):
            for year in (2019, 2020, 2021, 2022):
                x = firm_index * (year - 2017) + (firm_index % 2)
                x_alt = x * 0.7 + (year - 2019) * 0.1
                size = 10 + firm_index + (year - 2019) * 0.2
                mediator = firm_index * 0.3 + (year - 2019) * 0.15 + ((firm_index + year) % 2) * 0.2
                y = 0.8 * x + 0.3 * size + 0.4 * x * mediator + firm_index + (year - 2019) * 0.5
                y_alt = 0.6 * y + 0.2 * x_alt
                rows.append(f"{firm},{year},{y},{y_alt},{x},{x_alt},{size},{mediator}")
        rows.append("E,2022,12,8,6,4.4,15,1.2")
        self.csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        digest = hashlib.sha256(self.csv_path.read_bytes()).hexdigest()
        self.dataset_ref = DatasetRef(
            dataset_id=f"ds_{digest[:16]}",
            filename="panel.csv",
            sha256=digest,
            size_bytes=self.csv_path.stat().st_size,
        )
        self.registry = DatasetRegistry(self.root / "datasets.json")
        self.registry.register(self.dataset_ref, self.csv_path)
        self.contract = _contract(self.dataset_ref)
        self.engine = PanelResearchEngine(self.registry)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_executes_frozen_two_way_fixed_effects_model(self) -> None:
        result = self.engine.execute(self.contract)

        self.assertEqual(result.execution_status, "succeeded")
        self.assertEqual(result.scientific_status, "limited")
        self.assertFalse(result.fixture_only)
        estimate = result.executions[0].estimates[0]
        self.assertEqual(estimate["term"], "x")
        self.assertGreater(estimate["coefficient"], 0)
        diagnostics = result.executions[0].diagnostic_results
        self.assertTrue(diagnostics["entity_fixed_effects"])
        self.assertTrue(diagnostics["time_fixed_effects"])
        self.assertEqual(diagnostics["entity_count"], 4)
        self.assertEqual(diagnostics["singleton_rows_dropped"], 1)
        self.assertEqual(diagnostics["rows_used"], 16)
        self.assertEqual(
            diagnostics["cluster_correction"],
            "stata_reghdfe_compatible_entity_cluster",
        )
        self.assertIn("r_squared_adjusted_inclusive", diagnostics)
        self.assertNotIn("机制", "".join(result.warnings))

    async def test_executes_frozen_diagnostics_robustness_falsification_and_mechanism(self) -> None:
        result = self.engine.execute(_extended_contract(self.dataset_ref))

        self.assertEqual(result.execution_status, "succeeded")
        by_type = {execution.run_type: execution for execution in result.executions}
        self.assertEqual(
            set(by_type),
            {"baseline", "diagnostic", "robustness", "falsification", "mechanism"},
        )
        self.assertGreater(
            by_type["diagnostic"].diagnostic_results["within_variance"]["x"],
            0,
        )
        self.assertEqual(by_type["robustness"].estimates[0]["term"], "x")
        self.assertTrue(by_type["falsification"].diagnostic_results["feasible"])
        mechanism_terms = {item["term"] for item in by_type["mechanism"].estimates}
        self.assertIn("x_x_mediator", mechanism_terms)
        self.assertFalse(any("尚未执行" in warning for warning in result.warnings))

    async def test_constructs_frozen_lead_exposure_for_falsification(self) -> None:
        contract = _contract(self.dataset_ref)
        contract.approved_plan.falsification_tests = [
            PlannedStep(
                step_id="fal_lead",
                name="前导解释变量证伪",
                rationale="检验未来一期解释变量是否预测当前结果",
                required_data_fields=["firm", "year", "x"],
                parameters={
                    "lead_exposure": "x_lead1",
                    "lead_source": "x",
                    "lead_periods": 1,
                },
            )
        ]

        result = self.engine.execute(contract)

        falsification = next(
            execution
            for execution in result.executions
            if execution.run_type == "falsification"
        )
        self.assertEqual(falsification.execution_status, "succeeded")
        self.assertEqual(falsification.estimates[0]["term"], "x_lead1")
        self.assertEqual(falsification.diagnostic_results["rows_used"], 12)

    async def test_executes_frozen_spatial_durbin_panel_and_decomposes_effects(self) -> None:
        regions = ("A", "B", "C", "D", "E")
        weights_path = self.root / "spatial_weights.csv"
        weights_path.write_text(
            "spatial_id,A,B,C,D,E\n"
            "A,0,0.5,0,0,0.5\n"
            "B,0.5,0,0.5,0,0\n"
            "C,0,0.5,0,0.5,0\n"
            "D,0,0,0.5,0,0.5\n"
            "E,0.5,0,0,0.5,0\n",
            encoding="utf-8",
        )
        weight_digest = hashlib.sha256(weights_path.read_bytes()).hexdigest()
        weight_ref = DatasetRef(
            dataset_id=f"ds_{weight_digest[:16]}",
            role="supplementary",
            filename="spatial_weights.csv",
            sha256=weight_digest,
            size_bytes=weights_path.stat().st_size,
        )
        self.registry.register(weight_ref, weights_path)

        matrix = [
            [0, 0.5, 0, 0, 0.5],
            [0.5, 0, 0.5, 0, 0],
            [0, 0.5, 0, 0.5, 0],
            [0, 0, 0.5, 0, 0.5],
            [0.5, 0, 0, 0.5, 0],
        ]
        rows = ["region,year,y,x,size"]
        for year_index, year in enumerate(range(2015, 2023)):
            x = [0.2 * year_index + index * 0.3 + ((year_index + index) % 2) * 0.1 for index in range(len(regions))]
            wx = [sum(matrix[i][j] * x[j] for j in range(len(regions))) for i in range(len(regions))]
            structural = [0.6 * x[i] + 0.25 * wx[i] + i * 0.15 + year_index * 0.08 for i in range(len(regions))]
            # A deterministic fixed-point iteration creates a spatially lagged outcome.
            current = structural[:]
            for _ in range(40):
                current = [structural[i] + 0.3 * sum(matrix[i][j] * current[j] for j in range(len(regions))) for i in range(len(regions))]
            for index, region in enumerate(regions):
                size = 1.0 + index * 0.2 + year_index * 0.03
                rows.append(f"{region},{year},{current[index] + 0.1 * size},{x[index]},{size}")
        spatial_data = self.root / "spatial_panel.csv"
        spatial_data.write_text("\n".join(rows) + "\n", encoding="utf-8")
        data_digest = hashlib.sha256(spatial_data.read_bytes()).hexdigest()
        data_ref = DatasetRef(
            dataset_id=f"ds_{data_digest[:16]}",
            filename="spatial_panel.csv",
            sha256=data_digest,
            size_bytes=spatial_data.stat().st_size,
        )
        self.registry.register(data_ref, spatial_data)

        result = self.engine.execute(_spatial_contract(data_ref, weight_ref))

        self.assertEqual(result.execution_status, "succeeded")
        estimates = result.executions[0].estimates
        effect_types = {item.get("effect_type") for item in estimates}
        self.assertTrue({"direct", "indirect", "total"}.issubset(effect_types))
        rho = next(item for item in estimates if item.get("term") == "rho")
        self.assertTrue(math.isfinite(rho["coefficient"]))
        diagnostics = result.executions[0].diagnostic_results
        self.assertEqual(diagnostics["spatial_units"], 5)
        self.assertEqual(diagnostics["weight_matrix_row_sum_max_error"], 0.0)

    async def test_api_reports_capability_and_returns_schema_bound_run(self) -> None:
        original_engine = research_api_module.engine
        research_api_module.engine = self.engine
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=research_api_module.app),
            base_url="http://127.0.0.1",
        )
        try:
            health = await client.get("/v1/health")
            response = await client.post(
                "/v1/runs", json={"contract": self.contract.model_dump(mode="json")}
            )
        finally:
            await client.aclose()
            research_api_module.engine = original_engine

        self.assertEqual(health.status_code, 200)
        self.assertIn("panel_association", health.json()["supported_methods"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution_status"], "succeeded")


def _contract(dataset_ref: DatasetRef) -> FormalResearchContract:
    baseline = ModelSpec(
        step_id="model_baseline",
        name="基准模型",
        rationale="检验面板关联",
        estimator="双向固定效应面板模型",
        formula="y ~ x + size",
        outcome="y",
        treatments_or_exposures=["x"],
        controls=["size"],
        fixed_effects=["firm", "year"],
        standard_error_strategy="按企业聚类",
    )
    plan = AnalysisPlan(
        plan_id="plan-test",
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
        required_data_fields=["firm", "year", "y", "x", "size"],
        unsupported_requested_analyses=[],
    )
    return FormalResearchContract(
        contract_id="contract-test",
        case_id="case-test",
        approved_at="2026-07-14T00:00:00Z",
        approved_by="tester",
        decision_record_id="decision-test",
        research_package_hash="package-hash",
        data_hashes=[dataset_ref.sha256],
        dataset_refs=[dataset_ref],
        approved_plan_hash="plan-hash",
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )


def _extended_contract(dataset_ref: DatasetRef) -> FormalResearchContract:
    contract = _contract(dataset_ref)
    contract.approved_plan.diagnostics = [
        PlannedStep(
            step_id="diag_within",
            name="组内变异和缺失诊断",
            rationale="确认面板识别所需的组内变异和字段可用性",
            required_data_fields=["firm", "year", "x", "y", "mediator"],
            parameters={
                "checks": [
                    "within_variance(x)",
                    "within_variance(y)",
                    "missing_pattern(mediator)",
                ]
            },
        )
    ]
    contract.approved_plan.robustness_tests = [
        PlannedStep(
            step_id="rob_alt_outcome",
            name="替代结果变量",
            rationale="检查结论对结果变量口径的敏感性",
            required_data_fields=["y_alt"],
            parameters={"alternative_outcome": "y_alt"},
        )
    ]
    contract.approved_plan.falsification_tests = [
        PlannedStep(
            step_id="fal_feasibility",
            name="字段可执行性边界",
            rationale="在运行前检查有效观测是否达到冻结阈值",
            required_data_fields=["mediator"],
            parameters={"min_valid_obs_threshold": 10},
        )
    ]
    contract.approved_plan.mechanism_tests = [
        PlannedStep(
            step_id="mech_interaction",
            name="交互机制边界",
            rationale="检验候选机制变量是否改变核心关联",
            required_data_fields=["mediator"],
            parameters={
                "mediator": "mediator",
                "test_type": "interaction_and_mediation_boundary",
            },
        )
    ]
    return contract


def _spatial_contract(data_ref: DatasetRef, weight_ref: DatasetRef) -> FormalResearchContract:
    baseline = ModelSpec(
        step_id="model_spatial_baseline",
        name="空间基准模型",
        rationale="同时估计本地与跨地区关联",
        estimator="Spatial Durbin panel model with entity and time fixed effects",
        formula="y ~ x + size + W:y + W:x + W:size",
        outcome="y",
        treatments_or_exposures=["x"],
        controls=["size"],
        fixed_effects=["region", "year"],
        standard_error_strategy="maximum-likelihood approximation",
        parameters={
            "spatial_model": "sdm",
            "spatial_id": "region",
            "spatial_weights_dataset_id": weight_ref.dataset_id,
            "spatially_lagged_covariates": ["x", "size"],
            "effect_decomposition": ["direct", "indirect", "total"],
        },
    )
    plan = AnalysisPlan(
        plan_id="plan-spatial",
        plan_version=1,
        method_family="spatial",
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
        required_data_fields=["region", "year", "y", "x", "size"],
        unsupported_requested_analyses=[],
    )
    return FormalResearchContract(
        contract_id="contract-spatial",
        case_id="case-spatial",
        approved_at="2026-07-15T00:00:00Z",
        approved_by="tester",
        decision_record_id="decision-spatial",
        research_package_hash="package-hash",
        data_hashes=[data_ref.sha256, weight_ref.sha256],
        dataset_refs=[data_ref, weight_ref],
        approved_plan_hash="plan-hash",
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )


if __name__ == "__main__":
    unittest.main()
