from __future__ import annotations

import hashlib
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
)
from hypoweaver.research_engine import PanelResearchEngine


class PanelResearchEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.csv_path = self.root / "panel.csv"
        rows = ["firm,year,y,x,size"]
        for firm_index, firm in enumerate(("A", "B", "C", "D"), start=1):
            for year in (2019, 2020, 2021, 2022):
                x = firm_index * (year - 2017) + (firm_index % 2)
                size = 10 + firm_index + (year - 2019) * 0.2
                y = 0.8 * x + 0.3 * size + firm_index + (year - 2019) * 0.5
                rows.append(f"{firm},{year},{y},{x},{size}")
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


if __name__ == "__main__":
    unittest.main()
