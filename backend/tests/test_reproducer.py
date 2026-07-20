from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

import hypoweaver.research_api as research_api_module
from hypoweaver.case_import import DatasetRegistry
from hypoweaver.models import (
    AnalysisPlan,
    DatasetRef,
    FormalResearchContract,
    ModelSpec,
    PlannedStep,
)
from hypoweaver.reproducer import (
    ReproductionError,
    ResearchReproducer,
    _alternating_two_way_demean,
    compare_panel_reproduction,
)
from hypoweaver.research_engine import PanelResearchEngine
from hypoweaver.seal import canonical_sha256


class ResearchReproducerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.csv_path = self.root / "panel.csv"
        rows = [
            "firm,year,y,y_alt,x,x_alt,size,mediator,stable_soe,stable_nonsoe"
        ]
        for firm_index, firm in enumerate(("A", "B", "C", "D", "E", "F"), start=1):
            for time_index, year in enumerate((2018, 2019, 2020, 2021, 2022)):
                if firm == "F" and year == 2019:
                    continue
                x = firm_index * (time_index + 1) + ((firm_index + time_index) % 3) * 0.2
                x_alt = 0.75 * x + ((2 * firm_index + time_index) % 4) * 0.1
                size = 10 + firm_index * 0.7 + time_index * 0.3 + ((firm_index + 2 * time_index) % 4) * 0.11
                mediator = (
                    firm_index * 0.19
                    + time_index * 0.11
                    + ((firm_index * time_index + 2 * firm_index + time_index) % 7) * 0.17
                )
                noise = ((3 * firm_index + 2 * time_index) % 7 - 3) * 0.09
                y = 0.8 * x + 0.27 * size + 0.31 * x * mediator + firm_index + time_index * 0.4 + noise
                y_alt = 0.65 * y + 0.18 * x_alt + ((firm_index + time_index) % 3) * 0.07
                x_text = "" if firm == "C" and year == 2020 else str(x)
                stable_soe = firm_index % 2
                rows.append(
                    f"{firm},{year},{y},{y_alt},{x_text},{x_alt},{size},{mediator},{stable_soe},{1 - stable_soe}"
                )
        rows.append("G,2022,14,10,7,5.3,16,1.4,1,0")
        self.csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        digest = hashlib.sha256(self.csv_path.read_bytes()).hexdigest()
        self.dataset_ref = DatasetRef(
            dataset_id=f"ds_{digest[:16]}",
            role="main",
            filename="panel.csv",
            sha256=digest,
            size_bytes=self.csv_path.stat().st_size,
        )
        self.registry = DatasetRegistry(self.root / "datasets.json")
        self.registry.register(self.dataset_ref, self.csv_path)
        self.contract = _contract(self.dataset_ref)
        self.primary_engine = PanelResearchEngine(self.registry)
        self.reproducer = ResearchReproducer(self.registry)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_matches_panelols_on_unbalanced_missing_duplicate_and_singleton_data(self) -> None:
        primary = self.primary_engine.execute(self.contract)
        replication = self.reproducer.execute(self.contract)

        self.assertEqual(primary.execution_status, "succeeded")
        self.assertEqual(replication.execution_status, "succeeded")
        diagnostics = replication.executions[0].diagnostic_results
        self.assertEqual(diagnostics["duplicate_rows_dropped"], 0)
        self.assertEqual(diagnostics["singleton_rows_dropped"], 1)
        self.assertLess(diagnostics["within_iterations"], 10_000)
        self.assertEqual(
            replication.executions[0].provenance.implementation_id,
            "numpy-two-way-within-v1",
        )

        self.assertEqual(
            primary.executions[0].provenance.implementation_id,
            "linearmodels-panelols-v1",
        )
        self.assertEqual(
            primary.executions[0].provenance.contract_sha256,
            replication.executions[0].provenance.contract_sha256,
        )
        audit = compare_panel_reproduction(primary, replication)

        self.assertEqual(audit.status, "matched", audit.differences)
        self.assertEqual(audit.mode, "independent_implementation")
        self.assertEqual(audit.covered_plan_step_ids, ["model_baseline"])
        self.assertTrue(all(item["matched"] for item in audit.metric_differences))

    async def test_reproduces_all_supported_estimative_steps(self) -> None:
        contract = _extended_contract(self.dataset_ref)
        primary = self.primary_engine.execute(contract)
        replication = self.reproducer.execute(contract)

        self.assertEqual(replication.execution_status, "succeeded")
        self.assertEqual(
            {item.plan_step_id for item in replication.executions},
            {"model_baseline", "rob_alt", "fal_lead", "mech_interaction"},
        )
        self.assertEqual(
            {item.run_type for item in replication.executions},
            {"baseline", "robustness", "falsification", "mechanism"},
        )
        primary_lead = next(
            item for item in primary.executions if item.plan_step_id == "fal_lead"
        )
        replica_lead = next(
            item for item in replication.executions if item.plan_step_id == "fal_lead"
        )
        self.assertEqual(primary_lead.diagnostic_results["rows_used"], 21)
        self.assertEqual(replica_lead.diagnostic_results["rows_used"], 21)
        self.assertEqual(primary_lead.diagnostic_results["dropped_gap_pairs"], 1)
        self.assertEqual(replica_lead.diagnostic_results["dropped_gap_pairs"], 1)
        audit = compare_panel_reproduction(primary, replication)
        self.assertEqual(audit.status, "matched", audit.differences)

    async def test_reproduces_frozen_stable_soe_and_nonsoe_filters(self) -> None:
        contract = _contract(self.dataset_ref)
        contract.approved_plan.heterogeneity_tests = [
            PlannedStep(
                step_id="heterogeneity_stable_soe",
                name="稳定国企子样本",
                rationale="仅执行合同冻结的稳定分组。",
                required_data_fields=["stable_soe"],
                parameters={
                    "subgroup_variable": "stable_soe",
                    "subgroup_value": 1,
                },
            ),
            PlannedStep(
                step_id="heterogeneity_stable_nonsoe",
                name="稳定非国企子样本",
                rationale="仅执行合同冻结的稳定分组。",
                required_data_fields=["stable_nonsoe"],
                parameters={
                    "subgroup_variable": "stable_nonsoe",
                    "subgroup_value": 1,
                },
            ),
        ]
        _refresh_plan_hash(contract)

        primary = self.primary_engine.execute(contract)
        replication = self.reproducer.execute(contract)
        primary_subgroup = next(
            item
            for item in primary.executions
            if item.plan_step_id == "heterogeneity_stable_soe"
        )
        replica_subgroup = next(
            item
            for item in replication.executions
            if item.plan_step_id == "heterogeneity_stable_soe"
        )
        primary_nonsoe = next(
            item
            for item in primary.executions
            if item.plan_step_id == "heterogeneity_stable_nonsoe"
        )
        replica_nonsoe = next(
            item
            for item in replication.executions
            if item.plan_step_id == "heterogeneity_stable_nonsoe"
        )

        self.assertEqual(primary_subgroup.execution_status, "succeeded")
        self.assertEqual(replica_subgroup.execution_status, "succeeded")
        self.assertEqual(
            primary_subgroup.diagnostic_results["rows_after_subgroup_filter"],
            16,
        )
        self.assertEqual(
            replica_subgroup.diagnostic_results["rows_after_subgroup_filter"],
            16,
        )
        self.assertEqual(primary_subgroup.diagnostic_results["rows_used"], 14)
        self.assertEqual(replica_subgroup.diagnostic_results["rows_used"], 14)
        self.assertEqual(primary_nonsoe.execution_status, "succeeded")
        self.assertEqual(replica_nonsoe.execution_status, "succeeded")
        self.assertEqual(
            primary_nonsoe.diagnostic_results["rows_after_subgroup_filter"],
            14,
        )
        self.assertEqual(
            replica_nonsoe.diagnostic_results["rows_after_subgroup_filter"],
            14,
        )
        audit = compare_panel_reproduction(primary, replication)
        self.assertEqual(audit.status, "matched", audit.differences)

    async def test_sample_filter_is_applied_and_independently_reproduced(self) -> None:
        contract = _contract(self.dataset_ref)
        contract.approved_plan.robustness_tests = [
            PlannedStep(
                step_id="rob_sample_filter",
                name="冻结样本边界",
                rationale="预注册样本敏感性",
                parameters={"sample_filter": "year >= 2020"},
            )
        ]
        _refresh_plan_hash(contract)

        primary = self.primary_engine.execute(contract)
        replication = self.reproducer.execute(contract)
        primary_filter = next(
            item
            for item in primary.executions
            if item.plan_step_id == "rob_sample_filter"
        )
        replica_filter = next(
            item
            for item in replication.executions
            if item.plan_step_id == "rob_sample_filter"
        )

        self.assertLess(
            primary_filter.diagnostic_results["rows_after_sample_filter"],
            primary_filter.diagnostic_results["rows_input"],
        )
        self.assertEqual(
            primary_filter.diagnostic_results["rows_after_sample_filter"],
            replica_filter.diagnostic_results["rows_after_sample_filter"],
        )
        audit = compare_panel_reproduction(primary, replication)
        self.assertEqual(audit.status, "matched", audit.differences)

    async def test_reproducer_obeys_the_same_frozen_primary_budget(self) -> None:
        contract = _extended_contract(self.dataset_ref)
        contract.budget.max_executions = 2

        primary = self.primary_engine.execute(contract)
        replication = self.reproducer.execute(contract)
        audit = compare_panel_reproduction(primary, replication)

        self.assertEqual(audit.status, "matched", audit.differences)
        self.assertEqual(
            audit.covered_plan_step_ids,
            ["model_baseline", "rob_alt"],
        )
        self.assertEqual(
            {item.plan_step_id for item in replication.executions},
            {"model_baseline", "rob_alt"},
        )

    async def test_contract_wall_time_timeout_blocks_all_replication_results(self) -> None:
        now = [0.0]
        contract = _extended_contract(self.dataset_ref)
        contract.budget.max_wall_time_seconds = 60
        reproducer = ResearchReproducer(self.registry, clock=lambda: now[0])
        original_fit = reproducer._fit

        def finish_after_deadline(*args, **kwargs):
            execution = original_fit(*args, **kwargs)
            now[0] = 60.0
            return execution

        with patch.object(
            reproducer,
            "_fit",
            side_effect=finish_after_deadline,
        ) as fit:
            result = reproducer.execute(contract)

        self.assertEqual(fit.call_count, 1)
        self.assertEqual(result.execution_status, "failed")
        expected_step_ids = {
            "model_baseline",
            "rob_alt",
            "fal_lead",
            "mech_interaction",
        }
        self.assertEqual(len(result.executions), len(expected_step_ids))
        self.assertEqual(
            {item.plan_step_id for item in result.executions},
            expected_step_ids,
        )
        self.assertEqual(
            len({item.plan_step_id for item in result.executions}),
            len(result.executions),
        )
        self.assertTrue(
            all(
                item.execution_status == "failed"
                and item.not_executed_reason_code == "budget_exhausted"
                and not item.estimates
                for item in result.executions
            )
        )
        self.assertTrue(
            any("墙钟时间预算已用完" in item.error for item in result.executions)
        )

    async def test_pre_estimation_hash_timeout_closes_every_estimative_step(self) -> None:
        now = [0.0]
        contract = _extended_contract(self.dataset_ref)
        contract.budget.max_wall_time_seconds = 60
        reproducer = ResearchReproducer(self.registry, clock=lambda: now[0])

        def expire_while_hashing(*args, **kwargs):
            deadline = kwargs.get("deadline")
            if deadline is not None:
                now[0] = 60.0
                deadline.check()
                raise AssertionError("deadline.check should have raised")
            return hashlib.sha256(Path(args[0]).read_bytes()).hexdigest()

        with (
            patch(
                "hypoweaver.reproducer._sha256_file",
                side_effect=expire_while_hashing,
            ) as hash_file,
            patch.object(reproducer, "_fit") as fit,
        ):
            result = reproducer.execute(contract)

        expected_step_ids = [
            "model_baseline",
            "rob_alt",
            "fal_lead",
            "mech_interaction",
        ]
        self.assertEqual(
            sum(
                "deadline" in call.kwargs
                for call in hash_file.call_args_list
            ),
            1,
        )
        fit.assert_not_called()
        self.assertEqual(result.execution_status, "failed")
        self.assertEqual(len(result.executions), len(expected_step_ids))
        self.assertEqual(
            [item.plan_step_id for item in result.executions],
            expected_step_ids,
        )
        self.assertEqual(
            len({item.plan_step_id for item in result.executions}),
            len(expected_step_ids),
        )
        self.assertTrue(
            all(
                item.execution_status == "failed"
                and item.not_executed_reason_code == "budget_exhausted"
                and not item.estimates
                for item in result.executions
            )
        )

    async def test_comparison_enforces_absolute_or_relative_tolerance(self) -> None:
        primary = self.primary_engine.execute(self.contract)
        replication = self.reproducer.execute(self.contract)
        within_tolerance = replication.model_copy(deep=True)
        within_tolerance.executions[0].estimates[0]["coefficient"] += 1e-9
        self.assertEqual(
            compare_panel_reproduction(primary, within_tolerance).status,
            "matched",
        )

        diverged = replication.model_copy(deep=True)
        diverged.executions[0].estimates[0]["standard_error"] *= 1.1
        audit = compare_panel_reproduction(primary, diverged)
        self.assertEqual(audit.status, "diverged")
        self.assertTrue(any("standard_error" in item for item in audit.differences))

    async def test_comparison_accepts_exact_absolute_tolerance_boundary(self) -> None:
        primary = self.primary_engine.execute(self.contract)
        replication = self.reproducer.execute(self.contract)
        primary.executions[0].estimates[0]["coefficient"] = 0.0
        replication.executions[0].estimates[0]["coefficient"] = 1e-8

        at_boundary = compare_panel_reproduction(primary, replication)
        self.assertEqual(at_boundary.status, "matched", at_boundary.differences)

        replication.executions[0].estimates[0]["coefficient"] = 1.000001e-8
        beyond_boundary = compare_panel_reproduction(primary, replication)
        self.assertEqual(beyond_boundary.status, "diverged")

    async def test_rejects_wrong_cluster_dimension(self) -> None:
        contract = self.contract.model_copy(deep=True)
        contract.approved_plan.baseline_models[0].parameters.pop("cluster_variable")
        contract.approved_plan.baseline_models[0].standard_error_strategy = "按年份聚类"
        _refresh_plan_hash(contract)

        result = self.reproducer.execute(contract)

        self.assertEqual(result.execution_status, "failed")
        self.assertIn("实体层级聚类", result.not_executed_reason)

    async def test_accepts_explicit_entity_id_cluster_alias(self) -> None:
        contract = self.contract.model_copy(deep=True)
        baseline = contract.approved_plan.baseline_models[0]
        baseline.parameters.pop("cluster_variable")
        baseline.standard_error_strategy = "cluster_by_firm"
        _refresh_plan_hash(contract)

        primary = self.primary_engine.execute(contract)
        replication = self.reproducer.execute(contract)
        audit = compare_panel_reproduction(primary, replication)

        self.assertEqual(replication.execution_status, "succeeded")
        self.assertEqual(audit.status, "matched", audit.differences)

    async def test_unsupported_robustness_is_not_a_silent_baseline_estimate(self) -> None:
        contract = self.contract.model_copy(deep=True)
        contract.approved_plan.robustness_tests = [
            PlannedStep(
                step_id="rob_se_alternative",
                name="alternative clustering",
                rationale="unsupported without a frozen implementation",
                parameters={"cluster_level": "industry_year"},
            ),
            PlannedStep(
                step_id="rob_exclude_outliers",
                name="trim outliers",
                rationale="incomplete trimming rule",
                parameters={"trim_percent": 5},
            ),
        ]
        _refresh_plan_hash(contract)

        primary = self.primary_engine.execute(contract)
        replication = self.reproducer.execute(contract)
        audit = compare_panel_reproduction(primary, replication)
        robustness = {
            item.plan_step_id: item
            for item in primary.executions
            if item.run_type == "robustness"
        }

        self.assertEqual(
            set(robustness),
            {"rob_se_alternative", "rob_exclude_outliers"},
        )
        self.assertTrue(
            all(item.execution_status == "not_executed" for item in robustness.values())
        )
        self.assertTrue(all(not item.estimates for item in robustness.values()))
        self.assertTrue(
            all(
                item.not_executed_reason_code == "not_executable"
                for item in robustness.values()
            )
        )
        self.assertEqual(replication.execution_status, "succeeded")
        self.assertEqual(audit.status, "matched", audit.differences)
        self.assertEqual(audit.covered_plan_step_ids, ["model_baseline"])

    async def test_rejects_approved_plan_hash_tampering(self) -> None:
        contract = self.contract.model_copy(deep=True)
        contract.approved_plan.baseline_models[0].outcome = "y_alt"

        result = self.reproducer.execute(contract)

        self.assertEqual(result.execution_status, "failed")
        self.assertIn("approved_plan_hash", result.not_executed_reason)

    async def test_real_enterprise_panel_regression_anchor(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        visible = json.loads(
            (project_root / "docs" / "CASE_001_ESG_SDLA_SAFE_INPUT.json").read_text(
                encoding="utf-8"
            )
        )
        dataset_ref = DatasetRef.model_validate(visible["case"]["dataset_refs"][0])
        registry = DatasetRegistry(project_root / "backend" / "var" / "datasets.json")
        source = registry.resolve(dataset_ref)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), dataset_ref.sha256)
        contract = _enterprise_panel_contract(dataset_ref)

        primary = PanelResearchEngine(registry).execute(contract)
        replication = ResearchReproducer(registry).execute(contract)
        audit = compare_panel_reproduction(primary, replication)

        self.assertEqual(primary.execution_status, "succeeded")
        self.assertEqual(replication.execution_status, "succeeded")
        self.assertEqual(audit.status, "matched", audit.differences)
        self.assertEqual(
            audit.covered_plan_step_ids,
            [
                "falsify_lead_exposure",
                "mech_info_transparency",
                "model_fe_ols",
                "robust_alt_outcome",
            ],
        )
        baseline = next(
            item for item in primary.executions if item.plan_step_id == "model_fe_ols"
        )
        estimate = baseline.estimates[0]
        self.assertEqual(estimate["nobs"], 29_919)
        self.assertAlmostEqual(estimate["coefficient"], -0.15155829474217664, places=12)
        self.assertAlmostEqual(estimate["standard_error"], 0.03362492840785544, places=12)
        self.assertTrue(all(item["matched"] for item in audit.metric_differences))
        self.assertLessEqual(
            max(item["absolute_difference"] for item in audit.metric_differences),
            1e-8,
        )

    async def test_rejects_data_changed_after_contract_freeze(self) -> None:
        self.csv_path.write_text(
            self.csv_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )

        result = self.reproducer.execute(self.contract)

        self.assertEqual(result.execution_status, "failed")
        self.assertIn("哈希", result.not_executed_reason)

    async def test_rejects_duplicate_primary_key_instead_of_silently_dropping(self) -> None:
        duplicate_path = self.root / "duplicates.csv"
        duplicate_path.write_text(
            self.csv_path.read_text(encoding="utf-8")
            + "A,2018,10,8,2,1.5,11,0.4\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(duplicate_path.read_bytes()).hexdigest()
        duplicate_ref = DatasetRef(
            dataset_id=f"ds_{digest[:16]}",
            role="main",
            filename="duplicates.csv",
            sha256=digest,
            size_bytes=duplicate_path.stat().st_size,
        )
        self.registry.register(duplicate_ref, duplicate_path)

        result = self.reproducer.execute(_contract(duplicate_ref))

        self.assertEqual(result.execution_status, "failed")
        self.assertIn("拒绝静默删除", result.not_executed_reason)

    async def test_api_exposes_independent_reproduction(self) -> None:
        original_engine = research_api_module.engine
        original = research_api_module.reproducer
        research_api_module.engine = PanelResearchEngine(self.registry)
        research_api_module.reproducer = self.reproducer
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=research_api_module.app),
            base_url="http://127.0.0.1",
        )
        try:
            health = await client.get("/v1/health")
            response = await client.post(
                "/v1/reproductions",
                json={"contract": self.contract.model_dump(mode="json")},
            )
        finally:
            await client.aclose()
            research_api_module.engine = original_engine
            research_api_module.reproducer = original

        self.assertEqual(health.status_code, 200)
        self.assertIn(
            "panel_association", health.json()["independent_reproduction_methods"]
        )
        self.assertEqual(
            health.json()["reproduction_scope_by_method"]["policy_causal"],
            "estimator_only",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution_status"], "succeeded")

    async def test_alternating_demean_converges_for_unbalanced_panel(self) -> None:
        values = np.array(
            [[1.0, 4.0], [2.0, 3.0], [5.0, 7.0], [6.0, 8.0], [9.0, 11.0]]
        )
        entity = np.array([0, 0, 1, 1, 2])
        time = np.array([0, 1, 0, 2, 1])

        transformed, iterations = _alternating_two_way_demean(values, entity, time)

        self.assertLess(iterations, 10_000)
        for codes in (entity, time):
            for group in np.unique(codes):
                np.testing.assert_allclose(
                    transformed[codes == group].mean(axis=0),
                    np.zeros(2),
                    atol=1e-10,
                )

    async def test_alternating_demean_converges_for_balanced_panel(self) -> None:
        values = np.array(
            [[1.0, 3.0], [2.0, 6.0], [4.0, 5.0], [7.0, 8.0], [9.0, 2.0], [3.0, 4.0]]
        )
        entity = np.array([0, 0, 1, 1, 2, 2])
        time = np.array([0, 1, 0, 1, 0, 1])

        transformed, iterations = _alternating_two_way_demean(values, entity, time)

        self.assertLess(iterations, 10_000)
        for codes in (entity, time):
            for group in np.unique(codes):
                np.testing.assert_allclose(
                    transformed[codes == group].mean(axis=0),
                    np.zeros(2),
                    atol=1e-10,
                )

    async def test_nonconvergent_within_transform_blocks_without_fallback(self) -> None:
        values = np.array(
            [[1.0, 4.0], [2.0, 3.0], [5.0, 7.0], [6.0, 8.0], [9.0, 11.0]]
        )
        entity = np.array([0, 0, 1, 1, 2])
        time = np.array([0, 1, 0, 2, 1])

        with patch("hypoweaver.reproducer.WITHIN_MAX_ITERATIONS", 1):
            with self.assertRaisesRegex(ReproductionError, "1 次迭代"):
                _alternating_two_way_demean(values, entity, time)


def _contract(dataset_ref: DatasetRef) -> FormalResearchContract:
    baseline = ModelSpec(
        step_id="model_baseline",
        name="基准模型",
        rationale="检验企业面板关联",
        estimator="双向固定效应面板模型",
        formula="y ~ x + size",
        outcome="y",
        treatments_or_exposures=["x"],
        controls=["size"],
        fixed_effects=["firm", "year"],
        standard_error_strategy="按实体（企业）聚类",
        parameters={"drop_singletons": True, "cluster_variable": "firm"},
    )
    plan = AnalysisPlan(
        plan_id="plan-reproduction-test",
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
        check_registry_version="enterprise-panel-v1",
    )
    return FormalResearchContract(
        contract_id="contract-reproduction-test",
        case_id="case-reproduction-test",
        approved_at="2026-07-16T00:00:00Z",
        approved_by="tester",
        decision_record_id="decision-reproduction-test",
        research_package_hash="package-hash",
        data_hashes=[dataset_ref.sha256],
        dataset_refs=[dataset_ref],
        approved_plan_hash=canonical_sha256(plan.model_dump(mode="json")),
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )


def _extended_contract(dataset_ref: DatasetRef) -> FormalResearchContract:
    contract = _contract(dataset_ref)
    contract.approved_plan.robustness_tests = [
        PlannedStep(
            step_id="rob_alt",
            name="替代结果变量",
            rationale="替代口径稳健性",
            parameters={"alternative_outcome": "y_alt"},
        )
    ]
    contract.approved_plan.falsification_tests = [
        PlannedStep(
            step_id="fal_lead",
            name="前导变量证伪",
            rationale="检验未来解释变量",
            parameters={
                "lead_exposure": "x_lead1",
                "lead_source": "x",
                "lead_periods": 1,
            },
        )
    ]
    contract.approved_plan.mechanism_tests = [
        PlannedStep(
            step_id="mech_interaction",
            name="交互机制边界",
            rationale="检验关联边界",
            parameters={"mediator": "mediator"},
        )
    ]
    _refresh_plan_hash(contract)
    return contract


def _refresh_plan_hash(contract: FormalResearchContract) -> None:
    contract.approved_plan_hash = canonical_sha256(
        contract.approved_plan.model_dump(mode="json")
    )


def _enterprise_panel_contract(dataset_ref: DatasetRef) -> FormalResearchContract:
    controls = [
        "SIZE_w",
        "LEV_w",
        "ROA_w",
        "GROWTH_w",
        "CF_w",
        "FA_w",
        "BOARD_w",
        "TOP1_w",
        "SOE_w",
        "MH_w",
    ]
    baseline = ModelSpec(
        step_id="model_fe_ols",
        name="双向固定效应模型",
        rationale="冻结企业与年度固定效应及企业聚类推断。",
        estimator="fe_ols",
        formula="SDLA_w ~ ESG_w + " + " + ".join(controls),
        outcome="SDLA_w",
        treatments_or_exposures=["ESG_w"],
        controls=controls,
        fixed_effects=["S", "YEAR"],
        standard_error_strategy="cluster_by_entity_finite_sample_correction",
        parameters={"drop_singletons": True, "cluster_variable": "S"},
    )
    plan = AnalysisPlan(
        plan_id="enterprise-panel-regression-anchor",
        plan_version=1,
        method_family="mechanism_boundary",
        design_only=False,
        estimands=[],
        sample_rules=[],
        variable_construction=[],
        baseline_models=[baseline],
        diagnostics=[],
        robustness_tests=[
            PlannedStep(
                step_id="robust_alt_outcome",
                name="替代结果变量",
                rationale="预先冻结 SLEV_w 替代口径。",
                parameters={"alternative_outcome": "SLEV_w"},
            )
        ],
        falsification_tests=[
            PlannedStep(
                step_id="falsify_lead_exposure",
                name="前导解释变量证伪",
                rationale="冻结未来一期 ESG_w。",
                parameters={
                    "lead_exposure": "ESG_w_lead1",
                    "lead_source": "ESG_w",
                    "lead_periods": 1,
                    "alpha": 0.05,
                },
            )
        ],
        mechanism_tests=[
            PlannedStep(
                step_id="mech_info_transparency",
                name="信息透明度交互边界",
                rationale="冻结 ABSDA1 交互项。",
                parameters={"mediator": "ABSDA1"},
            )
        ],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=[
            "S",
            "YEAR",
            "SDLA_w",
            "SLEV_w",
            "ESG_w",
            "ABSDA1",
            *controls,
        ],
        unsupported_requested_analyses=[],
        check_registry_version="enterprise-panel-v1",
    )
    return FormalResearchContract(
        contract_id="contract-enterprise-panel-regression-anchor",
        case_id="case_001_esg_sdla_retest_v5_20260715",
        approved_at="2026-07-16T00:00:00Z",
        approved_by="regression-test",
        decision_record_id="decision-enterprise-panel-regression-anchor",
        research_package_hash="frozen-visible-case",
        data_hashes=[dataset_ref.sha256],
        dataset_refs=[dataset_ref],
        approved_plan_hash=canonical_sha256(plan.model_dump(mode="json")),
        approved_plan=plan,
        prohibited_deviations=[],
        allowed_technical_repairs=[],
        unresolved_risks=[],
    )


if __name__ == "__main__":
    unittest.main()
