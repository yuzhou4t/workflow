from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import sys
import time
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import pandas as pd
from linearmodels.panel import PanelOLS
from scipy.optimize import minimize_scalar
from scipy.stats import norm

from .case_import import CaseImportError, DatasetRegistry
from .models import (
    ExecutionProvenance,
    ExecutionRecord,
    FormalResearchContract,
    ModelSpec,
    PlannedStep,
    ResearchRun,
)
from .policy_causal import (
    POLICY_PRIMARY_IMPLEMENTATION_ID,
    PolicyCausalError,
    estimate_policy_baseline,
    estimate_policy_core,
    estimate_policy_permutation,
    parse_policy_design,
)
from .spatial import SpatialWeights, is_spatial_weights_filename
from .test_dag import (
    THREAT_FE_CLUSTER_FEASIBILITY,
    finalize_test_dag_executions,
    is_estimative_test_step,
    schedule_test_dag,
    select_primary_test_dag_with_budget,
)


SUPPORTED_METHODS = {
    "policy_causal",
    "panel_association",
    "mechanism_boundary",
    "spatial",
}
PANEL_IMPLEMENTATION_ID = "linearmodels-panelols-v1"
SPATIAL_IMPLEMENTATION_ID = "hypoweaver-spatial-primary-v1"


class ResearchEngineError(ValueError):
    pass


class _ContractWallTimeExceeded(ResearchEngineError):
    pass


class _ContractDeadline:
    def __init__(self, seconds: int, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._deadline = clock() + seconds
        self._seconds = seconds

    def check(self) -> None:
        if self._clock() >= self._deadline:
            raise _ContractWallTimeExceeded(
                f"冻结合同的墙钟时间预算已用完（{self._seconds} 秒）。"
            )


class PanelResearchEngine:
    """Deterministic executor for frozen panel-regression contracts."""

    def __init__(
        self,
        registry: DatasetRegistry | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry or DatasetRegistry()
        self._clock = clock

    def execute(self, contract: FormalResearchContract) -> ResearchRun:
        deadline = _ContractDeadline(
            contract.budget.max_wall_time_seconds,
            self._clock,
        )
        plan = contract.approved_plan
        if plan.method_family not in SUPPORTED_METHODS:
            return self._failed_run(
                contract,
                f"本地执行器尚不支持 {plan.method_family}；当前仅支持面板关联与其机制主模型。",
            )
        if not contract.dataset_refs:
            return self._failed_run(contract, "冻结合同中没有可执行数据资产。")
        if not plan.baseline_models:
            return self._failed_run(contract, "冻结合同中没有基准模型。")

        try:
            deadline.check()
            frozen_hashes = [item.sha256 for item in contract.dataset_refs]
            if contract.data_hashes != frozen_hashes:
                raise ResearchEngineError(
                    "冻结合同 data_hashes 与 dataset_refs 的顺序或内容不一致。"
                )
            for dataset_ref in contract.dataset_refs:
                self._verify_file(
                    self.registry.resolve(dataset_ref),
                    dataset_ref.sha256,
                    deadline=deadline,
                )
            main_ref = next(
                (item for item in contract.dataset_refs if item.role == "main"),
                contract.dataset_refs[0],
            )
            source = self.registry.resolve(main_ref)
            self._verify_file(source, main_ref.sha256, deadline=deadline)
            if plan.method_family == "spatial":
                model = plan.baseline_models[0]
                weights_id = str(model.parameters.get("spatial_weights_dataset_id", ""))
                weights_ref = next(
                    (
                        item
                        for item in contract.dataset_refs
                        if item.dataset_id == weights_id
                        or (
                            item.role == "supplementary"
                            and is_spatial_weights_filename(item.filename)
                        )
                    ),
                    None,
                )
                if weights_ref is None:
                    raise ResearchEngineError("冻结合同中没有 spatial_weights.csv 空间权重资产。")
                weights_source = self.registry.resolve(weights_ref)
                self._verify_file(
                    weights_source,
                    weights_ref.sha256,
                    deadline=deadline,
                )
                deadline.check()
                execution = self._fit_spatial(source, weights_source, model)
                deadline.check()
        except _ContractWallTimeExceeded as error:
            return self._failed_run(
                contract,
                str(error),
                reason_code="budget_exhausted",
            )
        except (CaseImportError, ResearchEngineError, OSError, ValueError) as error:
            return self._failed_run(contract, str(error))

        if plan.method_family == "policy_causal":
            return self._execute_policy_contract(contract, source, deadline)
        if plan.method_family != "spatial":
            return self._execute_panel_contract(contract, source, deadline)

        executions = [execution]
        failed_runs: list[str] = []

        provenance = _primary_provenance(contract)
        executions = [
            item.model_copy(
                update={
                    "check_id": item.check_id or item.plan_step_id,
                    "provenance": item.provenance or provenance,
                }
            )
            for item in executions
        ]

        warnings = [
            "执行器已运行冻结的基准双向固定效应空间模型。"
            if plan.method_family == "spatial"
            else "执行器已按冻结合同运行基准模型与可支持的附加步骤。"
        ]
        if plan.method_family == "spatial":
            warnings.append("空间效应只适用于 H2 冻结的权重矩阵；更换矩阵可能改变直接、间接和总效应。")
        incomplete = [
            item.plan_step_id
            for item in executions
            if item.execution_status not in {"succeeded"}
        ]
        if incomplete:
            warnings.append(
                "以下冻结步骤没有成功完成："
                + "、".join(incomplete)
                + "；科学状态保持 limited。"
            )
        else:
            warnings.append(
                "当前基准设计仅支持受限解释，因此科学状态标记为 limited。"
            )
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=plan.plan_version,
            execution_status="succeeded",
            scientific_status="limited",
            fixture_only=False,
            executions=executions,
            failed_runs=failed_runs,
            warnings=warnings,
        )

    def _execute_policy_contract(
        self,
        contract: FormalResearchContract,
        source: Path,
        deadline: _ContractDeadline,
    ) -> ResearchRun:
        """Execute the code-owned policy-did-v2 DAG without guessing steps.

        ``estimate_policy_model`` deliberately owns the statistical
        implementation.  This adapter only schedules frozen checks, maps its
        structured outputs to the workflow schema, and explicitly closes every
        omitted, failed, or independently reproduced step.
        """

        plan = contract.approved_plan
        scheduled = schedule_test_dag(plan)
        primary_schedule = [
            item for item in scheduled if item.run_type != "replication"
        ]
        budgeted = select_primary_test_dag_with_budget(
            plan,
            contract.budget.max_executions,
        )
        selected_ids = {item.step.step_id for item in budgeted.selected}

        baseline = plan.baseline_models[0]
        executions: list[ExecutionRecord] = []
        failed_runs: list[str] = []
        reason_codes: dict[str, str] = {}
        reasons: dict[str, str] = {}
        wall_time_exhausted = False
        primary_attempted = False
        primary_result: dict[str, Any] | None = None
        primary_error: PolicyCausalError | OSError | ValueError | None = None

        def load_primary_result() -> dict[str, Any]:
            nonlocal primary_attempted, primary_result, primary_error
            if not primary_attempted:
                primary_attempted = True
                try:
                    primary_result = estimate_policy_core(source, baseline)
                except (PolicyCausalError, OSError, ValueError) as error:
                    primary_error = error
            if primary_error is not None:
                raise primary_error
            if primary_result is None:
                raise ResearchEngineError("政策 DID 主实现没有返回结构化结果。")
            return primary_result

        for scheduled_test in primary_schedule:
            step = scheduled_test.step
            if step.not_executable_reason is not None:
                reason_codes[step.step_id] = "not_executable"
                reasons[step.step_id] = step.not_executable_reason
                continue
            if wall_time_exhausted:
                reason = "冻结合同的墙钟时间预算已用完；该步骤未执行。"
                reason_codes[step.step_id] = "budget_exhausted"
                reasons[step.step_id] = reason
                failed_runs.append(f"{step.step_id}: {reason}")
                continue
            if step.step_id not in selected_ids:
                reason = "冻结合同的最大执行次数预算已用完。"
                reason_codes[step.step_id] = "budget_exhausted"
                reasons[step.step_id] = reason
                failed_runs.append(f"{step.step_id}: {reason}")
                continue

            try:
                deadline.check()
                if step.step_id == "check-policy-support":
                    execution = self._run_policy_support_diagnostic(
                        source,
                        baseline,
                        step,
                    )
                elif scheduled_test.run_type == "baseline":
                    if not isinstance(step, ModelSpec):
                        raise ResearchEngineError(
                            "policy-did-v2 的 baseline 步骤不是冻结 ModelSpec。"
                        )
                    execution = self._policy_baseline_execution(
                        step,
                        load_primary_result(),
                        run_type="baseline",
                    )
                elif step.step_id in {
                    "check-policy-group-fixed-pre",
                    "check-policy-group-stable-only",
                }:
                    group_assignment_mode = str(
                        step.parameters.get("group_assignment_mode", "")
                    )
                    policy_design = dict(
                        baseline.parameters.get("policy_design", {})
                    )
                    policy_design["group_assignment_mode"] = group_assignment_mode
                    sensitivity_model = baseline.model_copy(
                        deep=True,
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
                            "rationale": step.rationale,
                            "parameters": {
                                **baseline.parameters,
                                "policy_design": policy_design,
                            },
                        },
                    )
                    sensitivity_result = estimate_policy_baseline(
                        source,
                        sensitivity_model,
                    )
                    execution = self._policy_baseline_execution(
                        sensitivity_model,
                        sensitivity_result,
                        run_type="robustness",
                    )
                elif step.step_id == "check-policy-cluster-entity":
                    cluster_fields = step.parameters.get("cluster_fields")
                    if not isinstance(cluster_fields, list) or not cluster_fields:
                        raise ResearchEngineError(
                            "实体聚类敏感性没有冻结 cluster_fields。"
                        )
                    policy_design = dict(
                        baseline.parameters.get("policy_design", {})
                    )
                    policy_design["cluster_fields"] = [
                        str(field) for field in cluster_fields
                    ]
                    sensitivity_model = baseline.model_copy(
                        deep=True,
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
                            "rationale": step.rationale,
                            "standard_error_strategy": (
                                "cluster_interaction("
                                + ",".join(policy_design["cluster_fields"])
                                + ")"
                            ),
                            "parameters": {
                                **baseline.parameters,
                                "policy_design": policy_design,
                            },
                        },
                    )
                    sensitivity_result = estimate_policy_baseline(
                        source,
                        sensitivity_model,
                    )
                    execution = self._policy_baseline_execution(
                        sensitivity_model,
                        sensitivity_result,
                        run_type="robustness",
                    )
                elif step.step_id == "check-policy-alternative-outcome":
                    alternative_outcome = str(
                        step.parameters.get("alternative_outcome", "")
                    ).strip()
                    if not alternative_outcome:
                        raise ResearchEngineError(
                            "替代结果检查没有冻结 alternative_outcome。"
                        )
                    alternative_model = baseline.model_copy(
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
                            "rationale": step.rationale,
                            "outcome": alternative_outcome,
                            "required_data_fields": list(
                                dict.fromkeys(
                                    [
                                        *baseline.required_data_fields,
                                        *step.required_data_fields,
                                        alternative_outcome,
                                    ]
                                )
                            ),
                        }
                    )
                    alternative_result = estimate_policy_baseline(
                        source,
                        alternative_model,
                    )
                    execution = self._policy_baseline_execution(
                        alternative_model,
                        alternative_result,
                        run_type="robustness",
                    )
                elif step.step_id == "check-policy-event-study":
                    execution = self._policy_event_execution(
                        step,
                        load_primary_result(),
                    )
                elif step.step_id == "check-policy-placebo-time":
                    execution = self._policy_placebo_execution(
                        step,
                        load_primary_result(),
                    )
                elif step.step_id == "check-policy-permutation-placebo":
                    step_policy_design = step.parameters.get("policy_design")
                    if not isinstance(step_policy_design, dict):
                        raise ResearchEngineError(
                            "随机置换步骤没有冻结独立的 policy_design。"
                        )
                    permutation_model = baseline.model_copy(
                        deep=True,
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
                            "rationale": step.rationale,
                            "parameters": {
                                **baseline.parameters,
                                "policy_design": dict(step_policy_design),
                            },
                        },
                    )
                    execution = self._policy_permutation_execution(
                        step,
                        estimate_policy_permutation(source, permutation_model),
                    )
                else:
                    reason = (
                        "policy-did-v2 不认识该冻结步骤；"
                        "执行器没有把未知参数解释为基准模型重跑。"
                    )
                    execution = ExecutionRecord(
                        execution_id=f"execution-{uuid4()}",
                        run_type=scheduled_test.run_type,
                        plan_step_id=step.step_id,
                        check_id=step.step_id,
                        execution_status="not_executed",
                        not_executed_reason_code="not_executable",
                        error=reason,
                        warnings=[reason],
                    )
                deadline.check()
            except _ContractWallTimeExceeded as error:
                wall_time_exhausted = True
                execution = ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type=scheduled_test.run_type,
                    plan_step_id=step.step_id,
                    execution_status="failed",
                    check_id=step.step_id,
                    not_executed_reason_code="budget_exhausted",
                    error=str(error),
                    warnings=["该冻结步骤超过合同墙钟预算；统计结果已丢弃。"],
                )
                failed_runs.append(f"{step.step_id}: {error}")
            except (PolicyCausalError, ResearchEngineError, OSError, ValueError) as error:
                execution = ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type=scheduled_test.run_type,
                    plan_step_id=step.step_id,
                    execution_status="failed",
                    check_id=step.step_id,
                    not_executed_reason_code="dependency_failed",
                    error=str(error),
                    warnings=["该冻结政策步骤失败；没有改写分组、时点或模型来补造结果。"],
                )
                failed_runs.append(f"{step.step_id}: {error}")
            executions.append(execution)

        for item in scheduled:
            if item.run_type != "replication":
                continue
            reason_codes[item.step.step_id] = "external_replication_pending"
            reasons[item.step.step_id] = (
                "该冻结检查由不同实现的独立复算服务执行；"
                "主 ResearchRun 仅保留非失败占位，最终状态以 ReproductionAudit 为准。"
            )

        executions = finalize_test_dag_executions(
            plan,
            executions,
            reason_codes=reason_codes,
            reasons=reasons,
        )
        provenance = _primary_provenance(contract)
        executions = [
            item.model_copy(
                update={
                    "check_id": item.check_id or item.plan_step_id,
                    "provenance": item.provenance or provenance,
                }
            )
            for item in executions
        ]

        baseline_succeeded = any(
            item.run_type == "baseline" and item.execution_status == "succeeded"
            for item in executions
        )
        incomplete = [
            item.plan_step_id
            for item in executions
            if item.run_type != "replication"
            and item.execution_status != "succeeded"
        ]
        warnings = [
            "执行器已按 policy-did-v2 映射支持诊断、基准 DID、固定分组敏感性、事件研究、伪政策时点、随机置换和替代结果；独立复算由第二实现完成。"
        ]
        warnings.extend(plan.unsupported_requested_analyses)
        if incomplete:
            warnings.append(
                "以下冻结政策步骤没有成功完成："
                + "、".join(incomplete)
                + "；没有用未冻结分析替换。"
            )
        else:
            warnings.append(
                "政策支持与证伪检查已执行；因识别假设仍需审计，科学状态保持 limited。"
            )
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=plan.plan_version,
            execution_status="succeeded" if baseline_succeeded else "failed",
            scientific_status="limited" if baseline_succeeded else "invalid",
            fixture_only=False,
            not_executed_reason=(
                None if baseline_succeeded else "冻结政策 DID 基准模型未成功执行。"
            ),
            executions=executions,
            failed_runs=failed_runs,
            warnings=warnings,
        )

    @staticmethod
    def _run_policy_support_diagnostic(
        path: Path,
        baseline: ModelSpec,
        step: PlannedStep,
    ) -> ExecutionRecord:
        design = parse_policy_design(baseline)
        entity = design.fixed_effects[0]
        fields = list(
            dict.fromkeys(
                [
                    entity,
                    design.time_field,
                    design.group_field,
                    *design.cluster_fields,
                    *step.required_data_fields,
                ]
            )
        )
        frame = _read_csv(path, fields)
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise ResearchEngineError(
                "数据缺少冻结政策支持字段：" + "、".join(missing)
            )
        rows_inspected = len(frame)
        frame[design.time_field] = pd.to_numeric(
            frame[design.time_field], errors="coerce"
        )
        frame[design.group_field] = pd.to_numeric(
            frame[design.group_field], errors="coerce"
        )
        frame = frame.dropna(subset=fields).copy()
        if frame.empty:
            raise ResearchEngineError("政策支持诊断没有完整观测。")
        invalid_groups = sorted(
            float(value)
            for value in set(frame[design.group_field].unique()) - {0.0, 1.0}
        )
        entity_groups = frame.groupby(entity, observed=True)[design.group_field]
        entity_sizes = frame.groupby(entity, observed=True).size()
        entity_year_support = frame.groupby(entity, observed=True)[
            design.time_field
        ].agg(["min", "max"])
        observed_years = sorted(
            int(value) for value in frame[design.time_field].unique()
        )
        missing_years = [
            year
            for year in range(min(observed_years), max(observed_years) + 1)
            if year not in observed_years
        ]
        period = np.select(
            [
                frame[design.time_field] < design.policy_start_year,
                frame[design.time_field] == design.policy_start_year,
                frame[design.time_field] > design.policy_start_year,
            ],
            ["pre", "start", "post"],
            default="unknown",
        )
        support = (
            frame.assign(_policy_period=period)
            .groupby([design.group_field, "_policy_period"], observed=True)
            .size()
        )
        group_period_counts = {
            f"group_{int(group)}_{period_name}": int(count)
            for (group, period_name), count in support.items()
        }
        diagnostics = {
            "rows_inspected": rows_inspected,
            "rows_complete": len(frame),
            "rows_dropped_for_missing_support_fields": rows_inspected - len(frame),
            "duplicate_primary_key_rows": int(
                frame.duplicated([entity, design.time_field], keep=False).sum()
            ),
            "entity_count": int(frame[entity].nunique()),
            "treated_entity_count": int(entity_groups.max().eq(1).sum()),
            "control_entity_count": int(entity_groups.max().eq(0).sum()),
            "group_switcher_entities": int(entity_groups.nunique().gt(1).sum()),
            "singleton_entities": int(entity_sizes.eq(1).sum()),
            "entities_spanning_policy": int(
                (
                    entity_year_support["min"].lt(design.policy_start_year)
                    & entity_year_support["max"].ge(design.policy_start_year)
                ).sum()
            ),
            "invalid_group_values": invalid_groups,
            "group_period_row_counts": group_period_counts,
            "observed_years": observed_years,
            "missing_calendar_years": missing_years,
            "calendar_years_imputed": [],
            "policy_start_year": design.policy_start_year,
            "policy_start_weight": design.policy_start_weight,
            "cluster_fields": list(design.cluster_fields),
            "cluster_composition": design.cluster_composition,
            "cluster_count": int(
                frame[list(design.cluster_fields)].drop_duplicates().shape[0]
            ),
        }
        warnings = []
        if diagnostics["group_switcher_entities"]:
            warnings.append(
                "政策组别在部分实体内发生变化；该运行不能单独建立永久处理组 DID 解释。"
            )
        if invalid_groups:
            warnings.append("政策组别包含 0/1 之外的值；后续估计器将拒绝该样本。")
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="diagnostic",
            plan_step_id=step.step_id,
            check_id=step.step_id,
            execution_status="succeeded",
            diagnostic_results=diagnostics,
            warnings=warnings,
        )

    @staticmethod
    def _policy_baseline_execution(
        model: ModelSpec,
        result: dict[str, Any],
        *,
        run_type: str,
    ) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type=run_type,
            plan_step_id=model.step_id,
            check_id=model.step_id,
            execution_status="succeeded",
            estimates=list(result["estimates"]),
            diagnostic_results=_policy_estimation_diagnostics(result),
        )

    @staticmethod
    def _policy_event_execution(
        step: PlannedStep,
        result: dict[str, Any],
    ) -> ExecutionRecord:
        event = result.get("event_study")
        if not isinstance(event, dict):
            raise ResearchEngineError("政策 DID 主实现缺少 event_study 结果。")
        if event.get("status") != "succeeded":
            reason = str(
                event.get("reason")
                or event.get("joint_pretrend", {}).get("reason")
                or "冻结事件年份没有可估计支持。"
            )
            return ExecutionRecord(
                execution_id=f"execution-{uuid4()}",
                run_type="falsification",
                plan_step_id=step.step_id,
                check_id=step.step_id,
                execution_status="not_executed",
                not_executed_reason_code="not_executable",
                diagnostic_results={
                    **_policy_estimation_diagnostics(result),
                    **event,
                    "joint_pretrend_p_value": None,
                },
                error=reason,
                warnings=[reason],
            )
        joint_pretrend = event.get("joint_pretrend", {})
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="falsification",
            plan_step_id=step.step_id,
            check_id=step.step_id,
            execution_status="succeeded",
            estimates=list(event.get("estimates", [])),
            diagnostic_results={
                **_policy_estimation_diagnostics(result),
                **event,
                "joint_pretrend_p_value": joint_pretrend.get("p_value"),
            },
        )

    @staticmethod
    def _policy_placebo_execution(
        step: PlannedStep,
        result: dict[str, Any],
    ) -> ExecutionRecord:
        placebo = result.get("placebo")
        if not isinstance(placebo, dict) or placebo.get("status") != "succeeded":
            reason = (
                str(placebo.get("reason"))
                if isinstance(placebo, dict) and placebo.get("reason")
                else "冻结合约没有可估计的伪政策时点；未执行随机置换安慰剂。"
            )
            return ExecutionRecord(
                execution_id=f"execution-{uuid4()}",
                run_type="falsification",
                plan_step_id=step.step_id,
                check_id=step.step_id,
                execution_status="not_executed",
                not_executed_reason_code="not_executable",
                diagnostic_results={
                    **_policy_estimation_diagnostics(result),
                    **(placebo or {}),
                },
                error=reason,
                warnings=[reason],
            )
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="falsification",
            plan_step_id=step.step_id,
            check_id=step.step_id,
            execution_status="succeeded",
            estimates=[dict(placebo["estimate"])],
            diagnostic_results={
                **_policy_estimation_diagnostics(result),
                **placebo,
            },
            warnings=["该结果只对应冻结伪政策时点；随机置换由独立步骤报告。"],
        )

    @staticmethod
    def _policy_permutation_execution(
        step: PlannedStep,
        result: dict[str, Any],
    ) -> ExecutionRecord:
        permutation = result.get("permutation_placebo")
        if not isinstance(permutation, dict) or permutation.get("status") != "succeeded":
            reason = (
                str(permutation.get("reason"))
                if isinstance(permutation, dict) and permutation.get("reason")
                else "冻结随机置换安慰剂没有完整执行。"
            )
            return ExecutionRecord(
                execution_id=f"execution-{uuid4()}",
                run_type="falsification",
                plan_step_id=step.step_id,
                check_id=step.step_id,
                execution_status="not_executed",
                not_executed_reason_code="not_executable",
                diagnostic_results={
                    **_policy_estimation_diagnostics(result),
                    **(permutation or {}),
                },
                error=reason,
                warnings=[reason],
            )
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="falsification",
            plan_step_id=step.step_id,
            check_id=step.step_id,
            execution_status="succeeded",
            diagnostic_results={
                **_policy_estimation_diagnostics(result),
                **permutation,
            },
            warnings=[str(permutation["interpretation_boundary"])],
        )

    def _execute_panel_contract(
        self,
        contract: FormalResearchContract,
        source: Path,
        deadline: _ContractDeadline,
    ) -> ResearchRun:
        plan = contract.approved_plan
        scheduled = schedule_test_dag(plan)
        primary_schedule = [
            item for item in scheduled if item.run_type != "replication"
        ]
        budgeted = select_primary_test_dag_with_budget(
            plan,
            contract.budget.max_executions,
        )
        selected_ids = {
            item.step.step_id
            for item in budgeted.selected
        }

        executions: list[ExecutionRecord] = []
        failed_runs: list[str] = []
        reason_codes: dict[str, str] = {}
        reasons: dict[str, str] = {}
        wall_time_exhausted = False
        for scheduled_test in primary_schedule:
            step = scheduled_test.step
            if step.not_executable_reason is not None:
                reason_codes[step.step_id] = "not_executable"
                reasons[step.step_id] = step.not_executable_reason
                continue
            if wall_time_exhausted:
                reason = (
                    "冻结合同的墙钟时间预算已用完；"
                    "该步骤未执行。"
                )
                reason_codes[step.step_id] = "budget_exhausted"
                reasons[step.step_id] = reason
                failed_runs.append(f"{step.step_id}: {reason}")
                continue
            if step.step_id not in selected_ids:
                reason = "冻结合同的最大执行次数预算已用完。"
                reason_codes[step.step_id] = "budget_exhausted"
                reasons[step.step_id] = reason
                failed_runs.append(f"{step.step_id}: {reason}")
                continue
            try:
                deadline.check()
                if scheduled_test.run_type == "baseline":
                    if not isinstance(step, ModelSpec):
                        raise ResearchEngineError(
                            "Test DAG 的 baseline 步骤不是冻结 ModelSpec。"
                        )
                    execution = self._fit_panel(source, step)
                else:
                    execution = self._execute_panel_step(
                        source,
                        plan.baseline_models[0],
                        step,
                        scheduled_test.run_type,
                    )
                deadline.check()
            except _ContractWallTimeExceeded as error:
                wall_time_exhausted = True
                execution = ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type=scheduled_test.run_type,
                    plan_step_id=step.step_id,
                    execution_status="failed",
                    check_id=step.step_id,
                    not_executed_reason_code="budget_exhausted",
                    error=str(error),
                    warnings=["该冻结步骤超过合同墙钟预算；统计结果已丢弃。"],
                )
                failed_runs.append(f"{step.step_id}: {error}")
            except (ResearchEngineError, OSError, ValueError) as error:
                execution = ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type=scheduled_test.run_type,
                    plan_step_id=step.step_id,
                    execution_status="failed",
                    check_id=step.step_id,
                    not_executed_reason_code="dependency_failed",
                    error=str(error),
                    warnings=["该冻结步骤失败；没有用其他模型替换。"],
                )
                failed_runs.append(f"{step.step_id}: {error}")
            executions.append(execution)

        replication_steps = [
            item for item in scheduled if item.run_type == "replication"
        ]
        for item in replication_steps:
            reason_codes[item.step.step_id] = "external_replication_pending"
            reasons[item.step.step_id] = (
                "该冻结检查由独立复算服务在主实现完成后执行；"
                "主 ResearchRun 暂存非失败占位，最终状态以 ReproductionAudit 为准。"
            )

        executions = finalize_test_dag_executions(
            plan,
            executions,
            reason_codes=reason_codes,
            reasons=reasons,
        )
        provenance = _primary_provenance(contract)
        executions = [
            item.model_copy(
                update={
                    "check_id": item.check_id or item.plan_step_id,
                    "provenance": item.provenance or provenance,
                }
            )
            for item in executions
        ]
        baseline_succeeded = any(
            item.run_type == "baseline" and item.execution_status == "succeeded"
            for item in executions
        )
        incomplete = [
            item.plan_step_id
            for item in executions
            if item.run_type != "replication"
            and item.execution_status != "succeeded"
        ]
        warnings = [
            "执行器已按 Test DAG 运行必做诊断、基准模型、必做检验与可选步骤；独立复算留给第二实现。"
        ]
        if incomplete:
            warnings.append(
                "以下冻结步骤没有成功完成："
                + "、".join(incomplete)
                + "；科学状态保持 limited。"
            )
        else:
            warnings.append(
                "当前基准设计仅支持受限解释，因此科学状态标记为 limited。"
            )
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=plan.plan_version,
            execution_status="succeeded" if baseline_succeeded else "failed",
            scientific_status="limited" if baseline_succeeded else "invalid",
            fixture_only=False,
            not_executed_reason=(
                None if baseline_succeeded else "冻结基准模型未成功执行。"
            ),
            executions=executions,
            failed_runs=failed_runs,
            warnings=warnings,
        )

    @staticmethod
    def _verify_file(
        path: Path,
        expected_sha256: str,
        *,
        deadline: _ContractDeadline | None = None,
    ) -> None:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if deadline is not None:
                    deadline.check()
                hasher.update(chunk)
        if deadline is not None:
            deadline.check()
        if hasher.hexdigest() != expected_sha256:
            raise ResearchEngineError("数据文件哈希与冻结合同不一致。")

    def _execute_panel_step(
        self,
        path: Path,
        baseline: ModelSpec,
        step: PlannedStep,
        run_type: str,
    ) -> ExecutionRecord:
        if run_type == "diagnostic":
            return self._run_panel_diagnostic(path, baseline, step)
        if run_type == "robustness" and not is_estimative_test_step(
            step,
            "robustness",
        ):
            reason = (
                "冻结稳健性步骤没有当前执行器支持的估计参数；"
                "未将未知参数静默解释为基准模型重跑。"
            )
            return ExecutionRecord(
                execution_id=f"execution-{uuid4()}",
                run_type="robustness",
                plan_step_id=step.step_id,
                check_id=step.step_id,
                execution_status="not_executed",
                not_executed_reason_code="not_executable",
                error=reason,
                warnings=[reason],
            )
        if run_type == "falsification" and not is_estimative_test_step(
            step,
            "falsification",
        ):
            return self._run_feasibility_check(path, baseline, step)
        if run_type == "heterogeneity":
            subgroup_variable = str(
                step.parameters.get("subgroup_variable") or ""
            ).strip()
            if not subgroup_variable or "subgroup_value" not in step.parameters:
                raise ResearchEngineError(
                    "异质性步骤必须冻结 subgroup_variable 与 subgroup_value。"
                )

        model = self._model_for_step(baseline, step, run_type)
        return self._fit_panel(path, model, run_type=run_type)

    @staticmethod
    def _model_for_step(
        baseline: ModelSpec,
        step: PlannedStep,
        run_type: str,
    ) -> ModelSpec:
        parameters = {**baseline.parameters, **step.parameters}
        outcome = str(
            parameters.get("alternative_outcome")
            or parameters.get("placebo_outcome")
            or baseline.outcome
            or ""
        )
        treatments = list(baseline.treatments_or_exposures)
        alternative_exposure = parameters.get("alternative_exposure")
        if alternative_exposure:
            treatments = [str(alternative_exposure)]
        controls = list(baseline.controls)

        if run_type == "mechanism":
            mechanism = str(
                parameters.get("mediator")
                or parameters.get("moderator")
                or parameters.get("mechanism_variable")
                or ""
            ).strip()
            if not mechanism:
                raise ResearchEngineError("机制步骤没有冻结 mediator 或 moderator 字段。")
            if not treatments:
                raise ResearchEngineError("机制步骤缺少核心解释变量。")
            exposure = treatments[0]
            interaction = str(
                parameters.get("interaction_term") or f"{exposure}_x_{mechanism}"
            )
            parameters["derived_interactions"] = {
                interaction: [exposure, mechanism]
            }
            treatments = [exposure, interaction]
            controls = list(dict.fromkeys([mechanism, *controls]))

        lead_exposure = parameters.get("lead_exposure")
        if lead_exposure:
            lead_name = str(lead_exposure)
            lead_source = str(
                parameters.get("lead_source")
                or (baseline.treatments_or_exposures[0] if baseline.treatments_or_exposures else "")
            )
            if not lead_source:
                raise ResearchEngineError("前导变量步骤缺少 lead_source。")
            lead_periods = int(parameters.get("lead_periods", 1))
            if lead_periods < 1:
                raise ResearchEngineError("lead_periods 必须是正整数。")
            parameters["derived_leads"] = {
                lead_name: {
                    "source": lead_source,
                    "periods": lead_periods,
                }
            }
            treatments = [lead_name]

        return baseline.model_copy(
            update={
                "step_id": step.step_id,
                "name": step.name,
                "rationale": step.rationale,
                "required_data_fields": list(
                    dict.fromkeys(
                        [*baseline.required_data_fields, *step.required_data_fields]
                    )
                ),
                "outcome": outcome,
                "treatments_or_exposures": treatments,
                "controls": controls,
                "parameters": parameters,
            }
        )

    @staticmethod
    def _run_panel_diagnostic(
        path: Path,
        baseline: ModelSpec,
        step: PlannedStep,
    ) -> ExecutionRecord:
        entity, time = _panel_keys(baseline.fixed_effects)
        fields = list(dict.fromkeys([entity, time, *step.required_data_fields]))
        frame = _read_csv(path, fields)
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise ResearchEngineError(
                f"数据缺少冻结诊断字段：{', '.join(missing)}"
            )
        duplicate_rows = int(frame.duplicated(subset=[entity, time], keep=False).sum())
        entity_counts = frame.groupby(entity, dropna=False)[entity].transform("size")
        singleton_rows = int((entity_counts <= 1).sum())
        requested_checks = [str(value) for value in step.parameters.get("checks", [])]
        within_fields = {
            value[value.find("(") + 1 : value.rfind(")")]
            for value in requested_checks
            if "within_variance(" in value and value.endswith(")")
        }
        if not within_fields:
            within_fields = {
                field
                for field in step.required_data_fields
                if field not in {entity, time}
            }
        within_variance: dict[str, float | None] = {}
        missing_rate: dict[str, float] = {}
        for field in step.required_data_fields:
            if field in {entity, time}:
                continue
            missing_rate[field] = float(frame[field].isna().mean())
            if field in within_fields:
                numeric = pd.to_numeric(frame[field], errors="coerce")
                demeaned = numeric - numeric.groupby(frame[entity]).transform("mean")
                within_variance[field] = _finite_or_none(demeaned.var())
        diagnostics: dict[str, Any] = {
            "rows_inspected": len(frame),
            "duplicate_primary_key_rows": duplicate_rows,
            "singleton_rows": singleton_rows,
            "within_variance": within_variance,
            "missing_rate": missing_rate,
        }
        if step.threat_id == THREAT_FE_CLUSTER_FEASIBILITY:
            diagnostics.update(
                _wild_cluster_sensitivity(
                    path,
                    baseline,
                    replications=int(
                        step.parameters.get(
                            "wild_cluster_bootstrap_replications",
                            999,
                        )
                    ),
                    seed=int(
                        step.parameters.get(
                            "wild_cluster_bootstrap_seed",
                            20260720,
                        )
                    ),
                )
            )
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="diagnostic",
            plan_step_id=step.step_id,
            execution_status="succeeded",
            diagnostic_results=diagnostics,
        )

    @staticmethod
    def _run_feasibility_check(
        path: Path,
        baseline: ModelSpec,
        step: PlannedStep,
    ) -> ExecutionRecord:
        entity, time = _panel_keys(baseline.fixed_effects)
        fields = list(dict.fromkeys([entity, time, *step.required_data_fields]))
        frame = _read_csv(path, fields)
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise ResearchEngineError(
                f"数据缺少冻结证伪字段：{', '.join(missing)}"
            )
        counts = {
            field: int(frame[field].notna().sum())
            for field in step.required_data_fields
        }
        threshold = int(step.parameters.get("min_valid_obs_threshold", 1))
        feasible = bool(counts) and min(counts.values()) >= threshold
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="falsification",
            plan_step_id=step.step_id,
            execution_status="succeeded" if feasible else "not_executed",
            diagnostic_results={
                "valid_observations_by_field": counts,
                "minimum_required": threshold,
                "feasible": feasible,
            },
            warnings=(
                []
                if feasible
                else ["有效观测不足，按冻结规则标记为 not_executed。"]
            ),
        )

    @staticmethod
    def _fit_panel(
        path: Path,
        model: ModelSpec,
        *,
        run_type: str = "baseline",
    ) -> ExecutionRecord:
        if not model.outcome or not model.treatments_or_exposures:
            raise ResearchEngineError("基准模型缺少结果变量或核心解释变量。")
        if len(model.fixed_effects) < 2:
            raise ResearchEngineError("双向固定效应模型需要实体和时间变量。")

        entity, time = _panel_keys(model.fixed_effects)
        regressors = [*model.treatments_or_exposures, *model.controls]
        derived_interactions = {
            str(name): [str(value) for value in values]
            for name, values in model.parameters.get("derived_interactions", {}).items()
        }
        derived_leads = {
            str(name): {
                "source": str(specification.get("source", "")),
                "periods": int(specification.get("periods", 1)),
            }
            for name, specification in model.parameters.get("derived_leads", {}).items()
        }
        generated_fields = {*derived_interactions, *derived_leads}
        interaction_inputs = [
            field
            for fields in derived_interactions.values()
            for field in fields
        ]
        lead_inputs = [item["source"] for item in derived_leads.values()]
        subgroup_variable = None
        subgroup_value: Any = None
        if run_type == "heterogeneity":
            subgroup_variable = str(
                model.parameters.get("subgroup_variable") or ""
            ).strip()
            if not subgroup_variable or "subgroup_value" not in model.parameters:
                raise ResearchEngineError(
                    "异质性步骤必须冻结 subgroup_variable 与 subgroup_value。"
                )
            subgroup_value = model.parameters["subgroup_value"]
        sample_filter = model.parameters.get("sample_filter")
        sample_filter_column = None
        if sample_filter is not None:
            if not isinstance(sample_filter, str):
                raise ResearchEngineError("sample_filter 必须是冻结的简单比较字符串。")
            sample_filter_column = _parse_sample_filter(sample_filter)[0]
        required = [entity, time, model.outcome, *regressors]
        required = list(dict.fromkeys(required))
        source_fields = [
            field
            for field in required
            if field not in generated_fields
        ] + interaction_inputs + lead_inputs + (
            [sample_filter_column] if sample_filter_column else []
        )
        if subgroup_variable:
            source_fields.append(subgroup_variable)
        source_fields = list(dict.fromkeys(source_fields))
        frame = _read_csv(path, source_fields)
        missing = [column for column in source_fields if column not in frame.columns]
        if missing:
            raise ResearchEngineError(f"数据缺少冻结模型字段：{', '.join(missing)}")

        for name, components in derived_interactions.items():
            if len(components) != 2:
                raise ResearchEngineError("交互项必须且只能绑定两个冻结字段。")
            left = pd.to_numeric(frame[components[0]], errors="coerce")
            right = pd.to_numeric(frame[components[1]], errors="coerce")
            frame[name] = left * right

        dropped_gap_pairs_by_lead: dict[str, int] = {}
        if derived_leads:
            frame[time] = pd.to_numeric(frame[time], errors="coerce")
            frame = frame.sort_values([entity, time], kind="mergesort")
            valid_key_mask = frame[entity].notna() & frame[time].notna()
            duplicate_key_rows = int(
                frame.loc[valid_key_mask].duplicated(
                    subset=[entity, time], keep=False
                ).sum()
            )
            if duplicate_key_rows:
                raise ResearchEngineError(
                    f"实体—时间主键存在 {duplicate_key_rows} 条重复记录；主实现拒绝静默删除。"
                )
            calendar_keys = set(
                zip(
                    frame.loc[valid_key_mask, entity],
                    frame.loc[valid_key_mask, time],
                )
            )
            for name, specification in derived_leads.items():
                source = specification["source"]
                periods = specification["periods"]
                if not source or periods < 1:
                    raise ResearchEngineError(
                        "前导变量构造必须绑定源字段和正整数期数。"
                    )
                numeric = pd.to_numeric(frame[source], errors="coerce")
                source_by_key = dict(
                    zip(
                        zip(
                            frame.loc[valid_key_mask, entity],
                            frame.loc[valid_key_mask, time],
                        ),
                        numeric.loc[valid_key_mask],
                    )
                )
                target_keys = [
                    (
                        entity_value,
                        time_value + periods,
                    )
                    if pd.notna(entity_value) and pd.notna(time_value)
                    else None
                    for entity_value, time_value in zip(frame[entity], frame[time])
                ]
                target_exists = pd.Series(
                    [
                        key in calendar_keys if key is not None else False
                        for key in target_keys
                    ],
                    index=frame.index,
                )
                frame[name] = pd.Series(
                    [
                        source_by_key.get(key, np.nan) if key is not None else np.nan
                        for key in target_keys
                    ],
                    index=frame.index,
                    dtype=float,
                )
                ordinal_lead = numeric.groupby(frame[entity], sort=False).shift(
                    -periods
                )
                dropped_gap_pairs_by_lead[name] = int(
                    (valid_key_mask & ordinal_lead.notna() & ~target_exists).sum()
                )

        original_rows = len(frame)
        subgroup_switcher_entities = 0
        if subgroup_variable:
            subgroup_nonmissing = frame.loc[
                frame[subgroup_variable].notna(),
                [entity, subgroup_variable],
            ]
            subgroup_switcher_entities = int(
                (
                    subgroup_nonmissing.groupby(entity)[subgroup_variable].nunique()
                    > 1
                ).sum()
            )
            if subgroup_switcher_entities:
                raise ResearchEngineError(
                    "subgroup_variable 必须在实体内稳定；"
                    f"检测到 {subgroup_switcher_entities} 个分组切换实体。"
                )
            frame = frame.loc[frame[subgroup_variable] == subgroup_value]
        rows_after_subgroup_filter = len(frame)
        if isinstance(sample_filter, str):
            frame = _apply_sample_filter(frame, sample_filter)
        rows_after_sample_filter = len(frame)
        for column in [time, model.outcome, *regressors]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=required)
        duplicate_rows = int(
            frame.duplicated(subset=[entity, time], keep=False).sum()
        )
        if duplicate_rows:
            raise ResearchEngineError(
                f"实体—时间主键存在 {duplicate_rows} 条重复记录；主实现拒绝静默删除。"
            )
        singleton_entities_dropped = 0
        singleton_rows_dropped = 0
        if bool(model.parameters.get("drop_singletons", True)):
            entity_counts = frame.groupby(entity)[entity].transform("size")
            singleton_mask = entity_counts <= 1
            singleton_rows_dropped = int(singleton_mask.sum())
            singleton_entities_dropped = int(frame.loc[singleton_mask, entity].nunique())
            frame = frame.loc[~singleton_mask]
        if len(frame) <= len(regressors) + 2:
            raise ResearchEngineError("删除缺失值和重复主键后，有效样本不足。")

        frame = frame.set_index([entity, time]).sort_index()
        outcome = frame[model.outcome].astype(float)
        exog = frame[regressors].astype(float)
        try:
            result = PanelOLS(
                outcome,
                exog,
                entity_effects=True,
                time_effects=True,
                drop_absorbed=True,
                check_rank=True,
            ).fit(
                cov_type="clustered",
                cluster_entity=True,
                debiased=True,
                auto_df=False,
                count_effects=False,
                group_debias=True,
            )
        except Exception as error:
            raise ResearchEngineError(f"面板模型估计失败：{error}") from error

        estimates: list[dict[str, Any]] = []
        for variable in model.treatments_or_exposures:
            if variable not in result.params.index:
                continue
            coefficient = float(result.params[variable])
            standard_error = float(result.std_errors[variable])
            confidence_interval = result.conf_int().loc[variable]
            estimates.append(
                {
                    "term": variable,
                    "coefficient": coefficient,
                    "standard_error": standard_error,
                    "t_statistic": float(result.tstats[variable]),
                    "p_value": float(result.pvalues[variable]),
                    "confidence_interval_95": [
                        float(confidence_interval.iloc[0]),
                        float(confidence_interval.iloc[1]),
                    ],
                    "nobs": int(result.nobs),
                }
            )
        if not estimates:
            raise ResearchEngineError("核心解释变量被固定效应完全吸收，未得到可报告估计。")

        r_squared_inclusive = _finite_or_none(result.rsquared_inclusive)
        adjusted_inclusive = None
        if r_squared_inclusive is not None and result.df_resid > 0:
            adjusted_inclusive = 1 - (
                (1 - r_squared_inclusive)
                * (int(result.nobs) - 1)
                / int(result.df_resid)
            )
        diagnostics = {
            "rows_input": original_rows,
            "rows_after_subgroup_filter": rows_after_subgroup_filter,
            "rows_after_sample_filter": rows_after_sample_filter,
            "rows_used": int(result.nobs),
            "rows_dropped": original_rows - int(result.nobs),
            "dropped_gap_pairs": sum(dropped_gap_pairs_by_lead.values()),
            "dropped_gap_pairs_by_lead": dropped_gap_pairs_by_lead,
            "subgroup_variable": subgroup_variable,
            "subgroup_value": subgroup_value,
            "subgroup_switcher_entities": subgroup_switcher_entities,
            "duplicate_rows_dropped": 0,
            "singleton_entities_dropped": singleton_entities_dropped,
            "singleton_rows_dropped": singleton_rows_dropped,
            "entity_count": int(frame.index.get_level_values(0).nunique()),
            "time_period_count": int(frame.index.get_level_values(1).nunique()),
            "r_squared_model": _finite_or_none(result.rsquared),
            "r_squared_within": _finite_or_none(result.rsquared_within),
            "r_squared_between": _finite_or_none(result.rsquared_between),
            "r_squared_overall": _finite_or_none(result.rsquared_overall),
            "r_squared_inclusive": r_squared_inclusive,
            "r_squared_adjusted_inclusive": _finite_or_none(adjusted_inclusive),
            "entity_fixed_effects": True,
            "time_fixed_effects": True,
            "standard_errors": "clustered_by_entity",
            "cluster_variable": entity,
            "cluster_correction": "stata_reghdfe_compatible_entity_cluster",
            "degrees_of_freedom": {
                "model": int(result.df_model),
                "residual": int(result.df_resid),
            },
        }
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type=run_type,
            plan_step_id=model.step_id,
            execution_status="succeeded",
            estimates=estimates,
            diagnostic_results=diagnostics,
            warnings=[],
        )

    @staticmethod
    def _fit_spatial(
        path: Path,
        weights_path: Path,
        model: ModelSpec,
    ) -> ExecutionRecord:
        if not model.outcome or not model.treatments_or_exposures:
            raise ResearchEngineError("空间基准模型缺少结果变量或核心解释变量。")
        if len(model.fixed_effects) < 2:
            raise ResearchEngineError("空间面板模型需要实体和时间固定效应字段。")
        if str(model.parameters.get("spatial_model", "")).casefold() != "sdm":
            raise ResearchEngineError("当前空间执行器只支持 H2 明确冻结的 SDM。")

        entity, time = _panel_keys(model.fixed_effects)
        spatial_id = str(model.parameters.get("spatial_id", "")).strip()
        if not spatial_id:
            raise ResearchEngineError("空间基准模型没有冻结 spatial_id 字段。")
        regressors = [*model.treatments_or_exposures, *model.controls]
        lagged_covariates = [
            str(value)
            for value in model.parameters.get(
                "spatially_lagged_covariates",
                regressors,
            )
        ]
        if not set(lagged_covariates).issubset(regressors):
            raise ResearchEngineError("空间滞后协变量必须来自冻结的解释变量集合。")
        if not set(model.treatments_or_exposures).issubset(lagged_covariates):
            raise ResearchEngineError("效应分解要求核心解释变量同时进入空间滞后项。")

        required = list(
            dict.fromkeys([entity, time, spatial_id, model.outcome, *regressors])
        )
        frame = _read_csv(path, required)
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise ResearchEngineError(
                f"数据缺少冻结空间模型字段：{', '.join(missing)}"
            )
        original_rows = len(frame)
        for column in [time, model.outcome, *regressors]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame[spatial_id] = frame[spatial_id].astype(str).str.strip()
        frame = frame.dropna(subset=required)
        if frame.duplicated(subset=[spatial_id, time], keep=False).any():
            raise ResearchEngineError("空间面板存在重复的 spatial_id—time 主键。")

        weights = SpatialWeights.from_csv(weights_path)
        labels = list(weights.labels)
        matrix = weights.matrix
        weights.aligned(sorted(frame[spatial_id].unique()))
        times = sorted(frame[time].unique())
        if len(frame) != len(labels) * len(times):
            raise ResearchEngineError(
                "当前 SDM 执行器要求与冻结矩阵完全对齐的平衡空间面板。"
            )

        def panel_matrix(column: str) -> np.ndarray:
            pivot = frame.pivot(index=time, columns=spatial_id, values=column)
            pivot = pivot.reindex(index=times, columns=labels)
            if pivot.isna().any().any():
                raise ResearchEngineError(
                    f"字段 {column} 无法形成完整的平衡空间面板。"
                )
            return pivot.to_numpy(float)

        def two_way_within(values: np.ndarray) -> np.ndarray:
            return (
                values
                - values.mean(axis=0, keepdims=True)
                - values.mean(axis=1, keepdims=True)
                + values.mean()
            )

        outcome_raw = panel_matrix(model.outcome)
        outcome = two_way_within(outcome_raw).reshape(-1)
        spatial_outcome = two_way_within(outcome_raw @ matrix.T).reshape(-1)
        raw_regressors = {name: panel_matrix(name) for name in regressors}
        design_names = [
            *regressors,
            *[f"W:{name}" for name in lagged_covariates],
        ]
        design = np.column_stack(
            [
                two_way_within(raw_regressors[name]).reshape(-1)
                for name in regressors
            ]
            + [
                two_way_within(raw_regressors[name] @ matrix.T).reshape(-1)
                for name in lagged_covariates
            ]
        )
        if len(outcome) <= design.shape[1] + 2:
            raise ResearchEngineError("空间固定效应变换后有效样本不足。")

        identity = np.eye(len(labels))
        periods = len(times)

        def profile(rho: float) -> tuple[float, np.ndarray, float]:
            transformed = outcome - rho * spatial_outcome
            coefficients = np.linalg.lstsq(design, transformed, rcond=None)[0]
            residuals = transformed - design @ coefficients
            sigma2 = float(residuals @ residuals / len(residuals))
            sign, logdet = np.linalg.slogdet(identity - rho * matrix)
            if sign <= 0 or sigma2 <= 0 or not math.isfinite(sigma2):
                return math.inf, coefficients, sigma2
            negative_log_likelihood = (
                len(residuals) / 2 * (math.log(2 * math.pi * sigma2) + 1)
                - periods * logdet
            )
            return float(negative_log_likelihood), coefficients, sigma2

        optimization = minimize_scalar(
            lambda rho: profile(float(rho))[0],
            bounds=(-0.99, 0.99),
            method="bounded",
            options={"xatol": 1e-9},
        )
        if not optimization.success or not math.isfinite(float(optimization.fun)):
            raise ResearchEngineError("空间杜宾模型的 rho 优化未收敛。")
        rho = float(optimization.x)
        negative_log_likelihood, coefficients, sigma2 = profile(rho)
        covariance = sigma2 * np.linalg.pinv(design.T @ design)
        coefficient_errors = np.sqrt(np.maximum(np.diag(covariance), 0))

        rho_step = 1e-4
        if -0.99 < rho - rho_step and rho + rho_step < 0.99:
            curvature = (
                profile(rho + rho_step)[0]
                - 2 * negative_log_likelihood
                + profile(rho - rho_step)[0]
            ) / (rho_step**2)
            rho_error = math.sqrt(1 / curvature) if curvature > 0 else None
        else:
            rho_error = None

        def estimate_record(
            term: str,
            coefficient: float,
            standard_error: float | None,
            **extra: Any,
        ) -> dict[str, Any]:
            record: dict[str, Any] = {
                "term": term,
                "coefficient": float(coefficient),
                "standard_error": (
                    float(standard_error)
                    if standard_error is not None
                    and math.isfinite(standard_error)
                    else None
                ),
                "nobs": len(outcome),
                **extra,
            }
            if record["standard_error"] not in (None, 0):
                statistic = record["coefficient"] / record["standard_error"]
                record["z_statistic"] = float(statistic)
                record["p_value"] = float(2 * norm.sf(abs(statistic)))
                record["confidence_interval_95"] = [
                    record["coefficient"] - 1.96 * record["standard_error"],
                    record["coefficient"] + 1.96 * record["standard_error"],
                ]
            return record

        estimates = [
            estimate_record(
                name,
                coefficients[index],
                coefficient_errors[index],
                estimate_type="structural_coefficient",
            )
            for index, name in enumerate(design_names)
        ]
        estimates.append(
            estimate_record(
                "rho",
                rho,
                rho_error,
                estimate_type="spatial_autoregressive_parameter",
            )
        )

        multiplier = np.linalg.inv(identity - rho * matrix)
        rho_variance = rho_error**2 if rho_error is not None else 0.0
        for treatment in model.treatments_or_exposures:
            beta_index = design_names.index(treatment)
            theta_index = design_names.index(f"W:{treatment}")
            beta = float(coefficients[beta_index])
            theta = float(coefficients[theta_index])
            impact = multiplier @ (beta * identity + theta * matrix)
            impact_rho_derivative = multiplier @ matrix @ impact

            direct = float(np.trace(impact) / len(labels))
            total = float(impact.sum(axis=1).mean())
            indirect = total - direct
            direct_gradient = np.array(
                [
                    float(np.trace(multiplier) / len(labels)),
                    float(np.trace(multiplier @ matrix) / len(labels)),
                    float(np.trace(impact_rho_derivative) / len(labels)),
                ]
            )
            total_gradient = np.array(
                [
                    float(multiplier.sum(axis=1).mean()),
                    float((multiplier @ matrix).sum(axis=1).mean()),
                    float(impact_rho_derivative.sum(axis=1).mean()),
                ]
            )
            effect_covariance = np.array(
                [
                    [
                        covariance[beta_index, beta_index],
                        covariance[beta_index, theta_index],
                        0.0,
                    ],
                    [
                        covariance[theta_index, beta_index],
                        covariance[theta_index, theta_index],
                        0.0,
                    ],
                    [0.0, 0.0, rho_variance],
                ]
            )

            def effect_error(gradient: np.ndarray) -> float | None:
                variance = float(gradient @ effect_covariance @ gradient)
                return (
                    math.sqrt(variance)
                    if variance >= 0 and math.isfinite(variance)
                    else None
                )

            for effect_type, value, gradient in (
                ("direct", direct, direct_gradient),
                ("indirect", indirect, total_gradient - direct_gradient),
                ("total", total, total_gradient),
            ):
                estimates.append(
                    estimate_record(
                        treatment,
                        value,
                        effect_error(gradient),
                        estimate_type="average_marginal_effect",
                        effect_type=effect_type,
                    )
                )

        boundary = abs(rho) >= 0.98
        row_sum_error = float(np.max(np.abs(matrix.sum(axis=1) - 1.0)))
        warnings = [
            "标准误采用条件协方差与 rho 剖面曲率的近似 Delta 方法，不等同于聚类稳健推断。"
        ]
        if boundary:
            warnings.append(
                "rho 位于预设稳定区间边界附近，空间参数与效应分解需谨慎解释。"
            )
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="baseline",
            plan_step_id=model.step_id,
            execution_status="succeeded",
            estimates=estimates,
            diagnostic_results={
                "rows_input": original_rows,
                "rows_used": len(outcome),
                "rows_dropped": original_rows - len(outcome),
                "spatial_units": len(labels),
                "time_period_count": periods,
                "entity_fixed_effects": True,
                "time_fixed_effects": True,
                "spatial_model": "sdm",
                "spatial_weights_filename": weights_path.name,
                "weight_matrix_row_sum_max_error": row_sum_error,
                "rho_boundary_warning": boundary,
                "log_likelihood": -negative_log_likelihood,
                "inference": (
                    "profile_likelihood_and_block_diagonal_delta_approximation"
                ),
            },
            warnings=warnings,
        )

    @staticmethod
    def _failed_run(
        contract: FormalResearchContract,
        reason: str,
        *,
        reason_code: str = "dependency_failed",
    ) -> ResearchRun:
        baseline_step_id = (
            contract.approved_plan.baseline_models[0].step_id
            if contract.approved_plan.baseline_models
            else "model_baseline"
        )
        executions = [
            ExecutionRecord(
                execution_id=f"execution-{uuid4()}",
                run_type="baseline",
                plan_step_id=baseline_step_id,
                execution_status="failed",
                check_id=baseline_step_id,
                not_executed_reason_code=reason_code,
                error=reason,
            )
        ]
        if (
            contract.approved_plan.method_family
            in {"policy_causal", "panel_association", "mechanism_boundary"}
            and contract.approved_plan.baseline_models
        ):
            scheduled = schedule_test_dag(contract.approved_plan)
            dependency_reasons = {
                item.step.step_id: (
                    "上游合同、数据或基准执行失败，因此该冻结步骤未执行："
                    + reason
                )
                for item in scheduled
                if item.step.step_id != baseline_step_id
            }
            executions = finalize_test_dag_executions(
                contract.approved_plan,
                executions,
                reason_codes={
                    step_id: reason_code
                    for step_id in dependency_reasons
                },
                reasons=dependency_reasons,
            )
        provenance = _primary_provenance(contract)
        executions = [
            item.model_copy(update={"provenance": item.provenance or provenance})
            for item in executions
        ]
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=contract.approved_plan.plan_version,
            execution_status="failed",
            scientific_status="invalid",
            fixture_only=False,
            not_executed_reason=reason,
            executions=executions,
            failed_runs=[reason],
            warnings=["没有生成或补造任何统计结果。"],
        )


def _panel_keys(fixed_effects: list[str]) -> tuple[str, str]:
    time_markers = {"year", "time", "年份", "年度"}
    time = next(
        (name for name in fixed_effects if name.replace("_", "").casefold() in time_markers),
        fixed_effects[-1],
    )
    entity = next((name for name in fixed_effects if name != time), fixed_effects[0])
    return entity, time


def _alternating_two_way_within(
    values: np.ndarray,
    entities: np.ndarray,
    times: np.ndarray,
) -> np.ndarray:
    transformed = np.asarray(values, dtype=float).copy()
    if transformed.ndim == 1:
        transformed = transformed[:, None]
    for _ in range(10_000):
        previous = transformed.copy()
        transformed -= (
            pd.DataFrame(transformed)
            .groupby(entities, sort=False)
            .transform("mean")
            .to_numpy()
        )
        transformed -= (
            pd.DataFrame(transformed)
            .groupby(times, sort=False)
            .transform("mean")
            .to_numpy()
        )
        transformed += transformed.mean(axis=0, keepdims=True)
        if float(np.max(np.abs(transformed - previous))) <= 1e-12:
            return transformed
    raise ResearchEngineError("小聚类灵敏性的双向去均值未收敛。")


def _entity_cluster_covariance(
    regressors: np.ndarray,
    residuals: np.ndarray,
    clusters: np.ndarray,
) -> np.ndarray:
    labels = pd.unique(clusters)
    observations, parameters = regressors.shape
    if len(labels) < 2 or observations <= parameters:
        raise ResearchEngineError("小聚类灵敏性需要至少两个聚类且有效样本大于参数数。")
    meat = np.zeros((parameters, parameters), dtype=float)
    for label in labels:
        mask = clusters == label
        score = regressors[mask].T @ residuals[mask]
        meat += np.outer(score, score)
    correction = (len(labels) / (len(labels) - 1)) * (
        (observations - 1) / (observations - parameters)
    )
    bread = np.linalg.pinv(regressors.T @ regressors)
    return correction * bread @ meat @ bread


def _wild_cluster_sensitivity(
    path: Path,
    baseline: ModelSpec,
    *,
    replications: int,
    seed: int,
) -> dict[str, Any]:
    if replications < 1:
        raise ResearchEngineError("小聚类灵敏性的重抽样次数必须为正整数。")
    if not baseline.outcome or not baseline.treatments_or_exposures:
        raise ResearchEngineError("小聚类灵敏性缺少结果变量或核心解释变量。")
    if baseline.parameters.get("derived_interactions") or baseline.parameters.get(
        "derived_leads"
    ):
        raise ResearchEngineError("小聚类灵敏性尚不支持派生交互项或前导项。")

    entity, time = _panel_keys(baseline.fixed_effects)
    regressors = [*baseline.treatments_or_exposures, *baseline.controls]
    required = list(dict.fromkeys([entity, time, baseline.outcome, *regressors]))
    frame = _read_csv(path, required)
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ResearchEngineError(
            "小聚类灵敏性缺少冻结字段：" + "、".join(missing)
        )
    sample_filter = baseline.parameters.get("sample_filter")
    if isinstance(sample_filter, str):
        frame = _apply_sample_filter(frame, sample_filter)
    for column in [time, baseline.outcome, *regressors]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required)
    if frame.duplicated([entity, time]).any():
        raise ResearchEngineError("小聚类灵敏性检测到重复的实体—时间键。")
    if bool(baseline.parameters.get("drop_singletons", True)):
        counts = frame.groupby(entity)[entity].transform("size")
        frame = frame.loc[counts > 1]

    entities = frame[entity].to_numpy()
    times = frame[time].to_numpy()
    outcome = _alternating_two_way_within(
        frame[baseline.outcome].to_numpy(float),
        entities,
        times,
    ).ravel()
    design = _alternating_two_way_within(
        frame[regressors].to_numpy(float),
        entities,
        times,
    )
    target = baseline.treatments_or_exposures[0]
    target_index = regressors.index(target)
    coefficients = np.linalg.lstsq(design, outcome, rcond=None)[0]
    residuals = outcome - design @ coefficients
    covariance = _entity_cluster_covariance(design, residuals, entities)
    standard_error = math.sqrt(max(float(covariance[target_index, target_index]), 0.0))
    if standard_error == 0 or not math.isfinite(standard_error):
        raise ResearchEngineError("小聚类灵敏性无法得到有限的观测 t 统计量。")
    observed_t = float(coefficients[target_index] / standard_error)

    restricted = np.delete(design, target_index, axis=1)
    if restricted.shape[1]:
        restricted_coefficients = np.linalg.lstsq(
            restricted,
            outcome,
            rcond=None,
        )[0]
        fitted = restricted @ restricted_coefficients
    else:
        fitted = np.zeros_like(outcome)
    null_residuals = outcome - fitted
    labels = pd.unique(entities)
    random = np.random.default_rng(seed)
    exceedances = 0
    valid = 0
    for _ in range(replications):
        draws = dict(zip(labels, random.choice((-1.0, 1.0), size=len(labels))))
        weights = np.array([draws[value] for value in entities], dtype=float)
        synthetic = fitted + null_residuals * weights
        bootstrap_coefficients = np.linalg.lstsq(design, synthetic, rcond=None)[0]
        bootstrap_residuals = synthetic - design @ bootstrap_coefficients
        bootstrap_covariance = _entity_cluster_covariance(
            design,
            bootstrap_residuals,
            entities,
        )
        bootstrap_error = math.sqrt(
            max(float(bootstrap_covariance[target_index, target_index]), 0.0)
        )
        if bootstrap_error == 0 or not math.isfinite(bootstrap_error):
            continue
        valid += 1
        statistic = float(bootstrap_coefficients[target_index] / bootstrap_error)
        if abs(statistic) >= abs(observed_t):
            exceedances += 1
    if valid == 0:
        raise ResearchEngineError("小聚类灵敏性没有产生有效重抽样。")
    return {
        "target": target,
        "replications": replications,
        "replications_completed": valid,
        "seed": seed,
        "scheme": "rademacher_entity_cluster_null_imposed",
        "cluster_count": int(len(labels)),
        "observed_t": observed_t,
        "p_value_two_sided": (exceedances + 1) / (valid + 1),
    }


_SAMPLE_FILTER_RE = re.compile(
    r"^\s*([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)\s*"
    r"(==|!=|>=|<=|>|<)\s*([-+]?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")\s*$"
)


def _parse_sample_filter(expression: str) -> tuple[str, str, float | str]:
    match = _SAMPLE_FILTER_RE.fullmatch(expression)
    if match is None:
        raise ResearchEngineError(
            "sample_filter 只支持一个字段与数值或引号字符串常量的简单比较。"
        )
    column, operator, raw_value = match.groups()
    if raw_value.startswith(("'", '"')):
        value: float | str = raw_value[1:-1]
    else:
        value = float(raw_value)
    return column, operator, value


def _apply_sample_filter(frame: pd.DataFrame, expression: str) -> pd.DataFrame:
    column, operator, value = _parse_sample_filter(expression)
    if column not in frame.columns:
        raise ResearchEngineError(f"sample_filter 字段不存在：{column}")
    source = frame[column]
    comparable = (
        pd.to_numeric(source, errors="coerce")
        if isinstance(value, float)
        else source.astype("string")
    )
    valid = comparable.notna()
    if operator == "==":
        mask = valid & comparable.eq(value)
    elif operator == "!=":
        mask = valid & comparable.ne(value)
    elif operator == ">=":
        mask = valid & comparable.ge(value)
    elif operator == "<=":
        mask = valid & comparable.le(value)
    elif operator == ">":
        mask = valid & comparable.gt(value)
    else:
        mask = valid & comparable.lt(value)
    return frame.loc[mask].copy()


def _read_csv(path: Path, usecols: list[str]) -> pd.DataFrame:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(path, encoding=encoding, usecols=lambda name: name in usecols)
        except UnicodeDecodeError as error:
            last_error = error
    raise ResearchEngineError("CSV 编码必须是 UTF-8 或 GB18030。") from last_error


def _primary_provenance(
    contract: FormalResearchContract,
) -> ExecutionProvenance:
    spatial = contract.approved_plan.method_family == "spatial"
    policy = contract.approved_plan.method_family == "policy_causal"
    environment = {
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "linearmodels": package_version("linearmodels"),
        "platform": platform.platform(),
    }
    code_path = (
        Path(__file__).with_name("policy_causal.py")
        if policy
        else Path(__file__)
    )
    code_sha256 = hashlib.sha256(code_path.read_bytes()).hexdigest()
    contract_payload = contract.model_dump(mode="json")
    contract_json = json.dumps(
        contract_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    environment_json = json.dumps(
        environment,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ExecutionProvenance(
        implementation_id=(
            SPATIAL_IMPLEMENTATION_ID
            if spatial
            else POLICY_PRIMARY_IMPLEMENTATION_ID
            if policy
            else PANEL_IMPLEMENTATION_ID
        ),
        implementation_version=(
            "1.0.0" if spatial or policy else package_version("linearmodels")
        ),
        code_sha256=code_sha256,
        environment_sha256=hashlib.sha256(environment_json).hexdigest(),
        contract_sha256=hashlib.sha256(contract_json).hexdigest(),
        data_sha256=list(contract.data_hashes),
    )


def _policy_estimation_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = dict(result.get("diagnostics", {}))
    rows_used = diagnostics.get("rows_used")
    observed_years = diagnostics.get("observed_years", [])
    cluster_fields = [str(value) for value in diagnostics.get("cluster_fields", [])]
    diagnostics.update(
        {
            "rows_after_sample_filter": rows_used,
            "duplicate_rows_dropped": 0,
            "singleton_entities_dropped": 0,
            "singleton_rows_dropped": 0,
            "time_period_count": len(observed_years),
            "entity_fixed_effects": True,
            "time_fixed_effects": True,
            "standard_errors": "clustered_by_interaction",
            "cluster_variable": "×".join(cluster_fields),
            "cluster_correction": "finite_sample_interaction_cluster",
        }
    )
    return diagnostics


def _finite_or_none(value: Any) -> float | None:
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None
