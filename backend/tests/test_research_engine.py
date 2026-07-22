from __future__ import annotations

import hashlib
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import hypoweaver.research_api as research_api_module
from hypoweaver.case_import import DatasetRegistry
from hypoweaver.models import (
    AnalysisPlan,
    ContractBudget,
    DatasetRef,
    FormalResearchContract,
    ModelSpec,
    PlannedStep,
)
from hypoweaver.policy_causal import POLICY_PRIMARY_IMPLEMENTATION_ID
from hypoweaver.reproducer import ResearchReproducer, compare_panel_reproduction
from hypoweaver.research_engine import PanelResearchEngine
from hypoweaver.seal import canonical_sha256
from hypoweaver.test_dag import THREAT_FE_CLUSTER_FEASIBILITY


class ContractBudgetCompatibilityTests(unittest.TestCase):
    def test_legacy_llm_limit_is_accepted_but_not_serialized(self) -> None:
        budget = ContractBudget.model_validate(
            {
                "max_executions": 12,
                "max_llm_calls": 40,
                "max_wall_time_seconds": 1800,
                "max_end_to_end_wall_time_seconds": 2700,
            }
        )

        self.assertNotIn("max_llm_calls", budget.model_dump(mode="json"))
        self.assertFalse(hasattr(budget, "max_llm_calls"))


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

    async def test_small_cluster_diagnostic_runs_frozen_wild_bootstrap(self) -> None:
        baseline = self.contract.approved_plan.baseline_models[0]
        diagnostic = PlannedStep(
            step_id="check-panel-fe-cluster-feasibility",
            name="固定效应与小聚类灵敏性",
            rationale="在冻结的实体聚类下检验有限聚类风险",
            required_data_fields=["firm", "year", "y", "x", "size"],
            parameters={
                "wild_cluster_bootstrap_replications": 99,
                "wild_cluster_bootstrap_seed": 20260720,
            },
            threat_id=THREAT_FE_CLUSTER_FEASIBILITY,
        )

        execution = self.engine._run_panel_diagnostic(
            self.csv_path,
            baseline,
            diagnostic,
        )

        self.assertEqual(execution.execution_status, "succeeded")
        values = execution.diagnostic_results
        self.assertEqual(values["target"], "x")
        self.assertEqual(values["replications"], 99)
        self.assertEqual(values["replications_completed"], 99)
        self.assertEqual(values["cluster_count"], 4)
        self.assertGreaterEqual(values["p_value_two_sided"], 0)
        self.assertLessEqual(values["p_value_two_sided"], 1)

    async def test_executes_policy_did_v2_and_maps_all_frozen_checks(self) -> None:
        policy_path = self.root / "policy.csv"
        rows = ["firm,year,group,y,y_alt"]
        for firm_index in range(12):
            firm = f"F{firm_index:02d}"
            for year in range(2004, 2012):
                group = int(firm_index >= 6)
                if firm_index == 0 and year >= 2010:
                    group = 1
                policy_weight = 0.0 if year < 2007 else 0.5 if year == 2007 else 1.0
                exposure = group * policy_weight
                noise = ((firm_index * 11 + year * 7) % 13 - 6) * 0.01
                outcome = (
                    firm_index * 0.15
                    + (year - 2004) * 0.08
                    - 0.45 * exposure
                    + noise
                )
                alternative = 0.7 * outcome + noise * 0.2
                rows.append(
                    f"{firm},{year},{group},{outcome:.8f},{alternative:.8f}"
                )
        policy_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        policy_ref = DatasetRef(
            dataset_id=f"ds_{digest[:16]}",
            filename="policy.csv",
            sha256=digest,
            size_bytes=policy_path.stat().st_size,
        )
        self.registry.register(policy_ref, policy_path)

        contract = _policy_contract(policy_ref)
        contract.approved_plan_hash = canonical_sha256(
            contract.approved_plan.model_dump(mode="json")
        )
        result = self.engine.execute(contract)

        self.assertEqual(result.execution_status, "succeeded")
        self.assertEqual(result.scientific_status, "limited")
        by_step = {item.plan_step_id: item for item in result.executions}
        self.assertEqual(
            set(by_step),
            {
                "check-policy-support",
                "model_baseline",
                "check-policy-group-fixed-pre",
                "check-policy-group-stable-only",
                "check-policy-cluster-entity",
                "check-policy-alternative-outcome",
                "check-policy-event-study",
                "check-policy-placebo-time",
                "check-policy-permutation-placebo",
                "check-policy-independent-replication",
            },
        )
        self.assertEqual(
            by_step["check-policy-support"].diagnostic_results[
                "group_switcher_entities"
            ],
            1,
        )
        baseline = by_step["model_baseline"]
        self.assertEqual(baseline.execution_status, "succeeded")
        self.assertEqual(
            baseline.provenance.implementation_id,
            POLICY_PRIMARY_IMPLEMENTATION_ID,
        )
        self.assertEqual(
            baseline.diagnostic_results["standard_errors"],
            "clustered_by_interaction",
        )
        event = by_step["check-policy-event-study"]
        self.assertEqual(event.execution_status, "succeeded")
        self.assertIn("joint_pretrend_p_value", event.diagnostic_results)
        self.assertEqual(
            event.diagnostic_results["generated_remote_pre_years"],
            [2004],
        )
        self.assertTrue(event.diagnostic_results["remote_pre_requested"])
        self.assertEqual(event.diagnostic_results["remote_pre_status"], "complete")
        self.assertIn(
            "event_remote_pre",
            event.diagnostic_results["joint_pretrend"]["terms"],
        )
        self.assertEqual(
            event.diagnostic_results["event_term_scaling"],
            "binary_group_year_contrast",
        )
        self.assertEqual(
            event.diagnostic_results["policy_year_event_regressor_weight"],
            1.0,
        )
        self.assertEqual(
            event.diagnostic_results["baseline_policy_start_weight"],
            0.5,
        )
        self.assertFalse(
            event.diagnostic_results[
                "policy_year_event_coefficient_directly_comparable_to_baseline"
            ]
        )
        placebo = by_step["check-policy-placebo-time"]
        self.assertEqual(placebo.execution_status, "succeeded")
        self.assertEqual(placebo.diagnostic_results["sample_end_year"], 2006)
        self.assertEqual(
            placebo.diagnostic_results["true_policy_contamination_rows"],
            0,
        )
        self.assertEqual(
            placebo.diagnostic_results["observed_years"],
            [2004, 2005, 2006],
        )
        self.assertEqual(placebo.diagnostic_results["time_period_count"], 3)
        self.assertEqual(placebo.diagnostic_results["entities_spanning_policy"], 0)
        self.assertNotIn("permutation_repetitions", placebo.diagnostic_results)
        permutation = by_step["check-policy-permutation-placebo"]
        self.assertEqual(permutation.execution_status, "succeeded")
        self.assertEqual(permutation.estimates, [])
        self.assertEqual(permutation.diagnostic_results["repetitions_completed"], 25)
        self.assertEqual(
            permutation.diagnostic_results["scheme"],
            "assignment_unit_label",
        )
        fixed = by_step["check-policy-group-fixed-pre"]
        self.assertEqual(
            fixed.diagnostic_results["group_assignment_mode"],
            "fixed_last_pre_policy",
        )
        stable = by_step["check-policy-group-stable-only"]
        self.assertEqual(
            stable.diagnostic_results["group_assignment_mode"],
            "stable_entities_only",
        )
        entity_cluster = by_step["check-policy-cluster-entity"]
        self.assertEqual(entity_cluster.diagnostic_results["cluster_fields"], ["firm"])
        self.assertEqual(
            entity_cluster.diagnostic_results["entities_spanning_multiple_clusters"],
            0,
        )
        replication = by_step["check-policy-independent-replication"]
        self.assertEqual(replication.execution_status, "not_executed")
        self.assertEqual(replication.run_type, "replication")
        self.assertEqual(
            replication.not_executed_reason_code,
            "external_replication_pending",
        )
        self.assertIn("ReproductionAudit", replication.error or "")
        self.assertTrue(
            all(
                item.provenance.implementation_id == POLICY_PRIMARY_IMPLEMENTATION_ID
                for item in result.executions
                if item.estimates
            )
        )
        self.assertFalse(any("尚未" in warning and "置换" in warning for warning in result.warnings))

        reproduction = ResearchReproducer(self.registry).execute(contract)
        audit = compare_panel_reproduction(result, reproduction)
        self.assertEqual(audit.status, "matched", audit.differences)
        self.assertEqual(audit.independence_scope, "estimator_only")
        self.assertIn(
            "policy event/placebo regressor construction",
            audit.shared_components,
        )
        self.assertEqual(
            set(audit.covered_plan_step_ids),
            {
                "model_baseline",
                "check-policy-group-fixed-pre",
                "check-policy-group-stable-only",
                "check-policy-cluster-entity",
                "check-policy-alternative-outcome",
                "check-policy-event-study",
                "check-policy-placebo-time",
            },
        )
        replica_by_step = {
            item.plan_step_id: item for item in reproduction.executions
        }
        self.assertEqual(
            replica_by_step["check-policy-event-study"].diagnostic_results[
                "generated_remote_pre_years"
            ],
            [2004],
        )
        self.assertTrue(
            replica_by_step["check-policy-event-study"].diagnostic_results[
                "remote_pre_requested"
            ]
        )
        self.assertEqual(
            replica_by_step["check-policy-event-study"].diagnostic_results[
                "remote_pre_status"
            ],
            "complete",
        )
        self.assertEqual(
            replica_by_step["check-policy-event-study"].diagnostic_results[
                "event_term_scaling"
            ],
            "binary_group_year_contrast",
        )
        self.assertEqual(
            replica_by_step["check-policy-placebo-time"].diagnostic_results[
                "observed_years"
            ],
            [2004, 2005, 2006],
        )
        for step_id in (
            "check-policy-group-fixed-pre",
            "check-policy-group-stable-only",
        ):
            diagnostics = replica_by_step[step_id].diagnostic_results
            self.assertEqual(
                diagnostics["rows_after_sample_filter"],
                diagnostics["rows_used"],
            )

    async def test_policy_preflight_failure_closes_the_complete_dag(self) -> None:
        contract = _policy_contract(self.dataset_ref)
        contract.data_hashes = ["0" * 64]

        result = self.engine.execute(contract)

        self.assertEqual(result.execution_status, "failed")
        self.assertEqual(len(result.executions), 10)
        self.assertTrue(
            all(item.execution_status in {"failed", "not_executed"} for item in result.executions)
        )
        self.assertTrue(
            all(item.check_id == item.plan_step_id for item in result.executions)
        )
        self.assertTrue(
            all(
                item.provenance.implementation_id == POLICY_PRIMARY_IMPLEMENTATION_ID
                for item in result.executions
            )
        )

    async def test_policy_baseline_cardinality_fails_before_both_implementations_read_data(
        self,
    ) -> None:
        contract = _policy_contract(self.dataset_ref)
        contract.approved_plan.baseline_models.append(
            contract.approved_plan.baseline_models[0].model_copy(
                update={"step_id": "model_baseline_secondary"}
            )
        )
        contract.approved_plan_hash = canonical_sha256(
            contract.approved_plan.model_dump(mode="json")
        )
        reproducer = ResearchReproducer(self.registry)

        with (
            patch.object(self.engine, "_verify_file") as verify_file,
            patch.object(reproducer, "_resolve_source") as resolve_source,
        ):
            primary = self.engine.execute(contract)
            replication = reproducer.execute(contract)

        verify_file.assert_not_called()
        resolve_source.assert_not_called()
        for result in (primary, replication):
            self.assertEqual(result.execution_status, "failed")
            self.assertIn(
                "policy-did-v2 requires exactly one baseline model",
                result.not_executed_reason or "",
            )
            self.assertEqual(result.executions, [])

    async def test_contract_wall_time_timeout_discards_late_primary_result(self) -> None:
        now = [0.0]
        contract = self.contract.model_copy(deep=True)
        contract.budget.max_wall_time_seconds = 60
        engine = PanelResearchEngine(self.registry, clock=lambda: now[0])
        original_fit = engine._fit_panel

        def finish_after_deadline(*args, **kwargs):
            execution = original_fit(*args, **kwargs)
            now[0] = 60.0
            return execution

        with patch.object(engine, "_fit_panel", side_effect=finish_after_deadline):
            result = engine.execute(contract)

        self.assertEqual(result.execution_status, "failed")
        baseline = next(
            item for item in result.executions if item.run_type == "baseline"
        )
        self.assertEqual(baseline.execution_status, "failed")
        self.assertEqual(
            baseline.not_executed_reason_code,
            "budget_exhausted",
        )
        self.assertEqual(baseline.estimates, [])
        self.assertIn("墙钟时间预算已用完", baseline.error or "")

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
        original_reproducer = research_api_module.reproducer
        research_api_module.engine = self.engine
        research_api_module.reproducer = ResearchReproducer(self.registry)
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
            research_api_module.reproducer = original_reproducer

        self.assertEqual(health.status_code, 200)
        health_payload = health.json()
        self.assertIs(
            research_api_module.engine.registry,
            research_api_module.reproducer.registry,
        )
        self.assertEqual(
            health_payload["service"], "hypoweaver-research-engine"
        )
        self.assertIn("panel_association", health_payload["supported_methods"])
        self.assertIn(
            "panel_association",
            health_payload["independent_reproduction_methods"],
        )
        self.assertEqual(
            health_payload["reproduction_scope_by_method"]["policy_causal"],
            "estimator_only",
        )
        self.assertEqual(len(health_payload["source_sha256"]), 64)
        self.assertEqual(len(health_payload["environment_sha256"]), 64)
        self.assertEqual(
            health_payload["dataset_registry_path_sha256"],
            research_api_module.registry_path_sha256(self.registry.path),
        )
        self.assertNotEqual(
            health_payload["primary_implementation_ids"][0],
            health_payload["reproduction_implementation_id"],
        )
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


def _policy_contract(dataset_ref: DatasetRef) -> FormalResearchContract:
    policy_design = {
        "group_field": "group",
        "time_field": "year",
        "policy_start_year": 2007,
        "policy_start_month": 7,
        "policy_start_weight": 0.5,
        "post_start_weight": 1.0,
        "exposure_name": "policy_exposure",
        "fixed_effects": ["firm", "year"],
        "cluster_fields": ["firm", "year"],
        "cluster_composition": "interaction",
        "event_reference_year": 2006,
        "event_years": [2005, 2007, 2008, 2009, 2010, 2011],
        "event_remote_pre_years": [2004],
        "event_term_scaling": "binary_group_year_contrast",
        "placebo_start_year": 2005,
        "placebo_repetitions": 25,
        "permutation_scheme": "assignment_unit_label",
        "permutation_unit_field": "firm",
        "random_seed": 12345,
        "group_assignment_mode": "observed_time_varying",
    }
    baseline = ModelSpec(
        step_id="model_baseline",
        name="政策 DID 基准模型",
        rationale="估计冻结的政策暴露效应",
        estimator="absorbing-least-squares policy DID",
        formula="y ~ policy_exposure",
        outcome="y",
        treatments_or_exposures=["policy_exposure"],
        controls=[],
        fixed_effects=["firm", "year"],
        standard_error_strategy="cluster_interaction(firm,year)",
        parameters={"policy_design": policy_design},
        required_for_admission=True,
    )
    support = PlannedStep(
        step_id="check-policy-support",
        name="政策支持诊断",
        rationale="核对处理组、政策时点和样本支持",
        required_data_fields=["firm", "year", "group"],
        parameters={"policy_design": policy_design, "check": "policy_support"},
        required_for_admission=True,
        test_role="diagnostic",
    )
    alternative = PlannedStep(
        step_id="check-policy-alternative-outcome",
        name="替代结果",
        rationale="检验替代结果口径",
        required_data_fields=["y_alt"],
        parameters={
            "alternative_outcome": "y_alt",
            "policy_design": policy_design,
        },
        required_for_admission=True,
        test_role="robustness",
    )
    fixed_group = PlannedStep(
        step_id="check-policy-group-fixed-pre",
        name="政策前固定分组",
        rationale="用最后一个政策前分组固定实体",
        required_data_fields=["firm", "year", "group"],
        parameters={
            "group_assignment_mode": "fixed_last_pre_policy",
            "policy_design": {
                **policy_design,
                "group_assignment_mode": "fixed_last_pre_policy",
            },
        },
        required_for_admission=True,
        test_role="robustness",
    )
    stable_group = PlannedStep(
        step_id="check-policy-group-stable-only",
        name="稳定分组实体",
        rationale="剔除组别切换实体",
        required_data_fields=["firm", "year", "group"],
        parameters={
            "group_assignment_mode": "stable_entities_only",
            "policy_design": {
                **policy_design,
                "group_assignment_mode": "stable_entities_only",
            },
        },
        required_for_admission=False,
        test_role="robustness",
    )
    entity_cluster = PlannedStep(
        step_id="check-policy-cluster-entity",
        name="实体聚类敏感性",
        rationale="按实体层级重新计算聚类协方差",
        required_data_fields=["firm"],
        parameters={
            "cluster_fields": ["firm"],
            "policy_design": {
                **policy_design,
                "cluster_fields": ["firm"],
            },
        },
        required_for_admission=True,
        test_role="robustness",
    )
    event = PlannedStep(
        step_id="check-policy-event-study",
        name="事件研究",
        rationale="检验政策前动态",
        required_data_fields=["year", "group"],
        parameters={"policy_event_study": True, "policy_design": policy_design},
        required_for_admission=True,
        test_role="falsification",
    )
    placebo = PlannedStep(
        step_id="check-policy-placebo-time",
        name="伪政策时点",
        rationale="检验预先冻结的伪政策年份",
        required_data_fields=["year", "group"],
        parameters={"policy_placebo": True, "policy_design": policy_design},
        required_for_admission=True,
        test_role="falsification",
    )
    permutation = PlannedStep(
        step_id="check-policy-permutation-placebo",
        name="随机置换暴露",
        rationale="运行冻结次数的随机置换",
        required_data_fields=["year", "group"],
        parameters={
            "policy_permutation_placebo": True,
            "repetitions": 25,
            "random_seed": 12345,
            "scheme": "assignment_unit_label",
            "policy_design": {
                **policy_design,
                "group_assignment_mode": "fixed_last_pre_policy",
            },
        },
        required_for_admission=True,
        test_role="falsification",
    )
    replication = PlannedStep(
        step_id="check-policy-independent-replication",
        name="独立复算",
        rationale="使用第二实现复算",
        required_data_fields=["firm", "year", "group", "y"],
        parameters={"implementation": "independent_multiway_within"},
        required_for_admission=True,
        test_role="replication",
    )
    plan = AnalysisPlan(
        plan_id="plan-policy",
        plan_version=1,
        method_family="policy_causal",
        design_only=False,
        estimands=[],
        sample_rules=[],
        variable_construction=[],
        baseline_models=[baseline],
        diagnostics=[support],
        robustness_tests=[
            fixed_group,
            stable_group,
            entity_cluster,
            alternative,
            replication,
        ],
        falsification_tests=[event, placebo, permutation],
        mechanism_tests=[],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=["firm", "year", "group", "y"],
        unsupported_requested_analyses=[],
        check_registry_version="policy-did-v2",
    )
    return FormalResearchContract(
        contract_id="contract-policy",
        case_id="case-policy",
        approved_at="2026-07-18T00:00:00Z",
        approved_by="tester",
        decision_record_id="decision-policy",
        research_package_hash="package-hash",
        data_hashes=[dataset_ref.sha256],
        dataset_refs=[dataset_ref],
        approved_plan_hash="plan-hash",
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )


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
