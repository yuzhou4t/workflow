from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from .case_import import CaseImportError, DatasetRegistry
from .models import (
    ExecutionProvenance,
    ExecutionRecord,
    FormalResearchContract,
    ModelSpec,
    PlannedStep,
    ReproductionAudit,
    ResearchRun,
)
from .policy_causal import (
    POLICY_PRIMARY_IMPLEMENTATION_ID,
    POLICY_REPRODUCTION_IMPLEMENTATION_ID,
    PolicyCausalError,
    reproduce_policy_baseline,
    reproduce_policy_model,
)
from .test_dag import (
    is_estimative_test_step,
    select_primary_test_dag_with_budget,
    validate_policy_did_execution_plan,
)


PANEL_METHODS = {"policy_causal", "panel_association", "mechanism_boundary"}
IMPLEMENTATION_ID = "numpy-two-way-within-v1"
IMPLEMENTATION_VERSION = "1.0.0"
WITHIN_RELATIVE_TOLERANCE = 1e-12
WITHIN_MAX_ITERATIONS = 10_000
DEFAULT_ABSOLUTE_TOLERANCE = 1e-8
DEFAULT_RELATIVE_TOLERANCE = 1e-6


class ReproductionError(ValueError):
    pass


class _ContractWallTimeExceeded(ReproductionError):
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


@dataclass(frozen=True)
class _EstimationSpec:
    step_id: str
    run_type: Literal[
        "baseline", "robustness", "falsification", "mechanism", "heterogeneity"
    ]
    outcome: str
    treatments: tuple[str, ...]
    controls: tuple[str, ...]
    entity: str
    time: str
    parameters: dict[str, Any] = field(default_factory=dict)
    subgroup_variable: str | None = None
    subgroup_value: Any = None


class ResearchReproducer:
    """Independent two-way fixed-effect implementation for frozen panel contracts.

    This implementation intentionally does not import or call PanelResearchEngine or
    linearmodels. It resolves the frozen data again, verifies the bytes, constructs
    every supported estimative step, removes both fixed effects by alternating
    projections, estimates with NumPy least squares, and computes the entity-clustered
    covariance directly.
    """

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
        if contract.approved_plan.method_family == "policy_causal":
            return self._execute_policy_contract(contract, deadline)
        specifications: list[_EstimationSpec] = []
        try:
            specifications = self._estimation_specs(contract)
            if not specifications:
                raise ReproductionError(
                    "冻结合同中没有可由独立面板实现复算的估计步骤。"
                )
            deadline.check()
            source, actual_hashes = self._resolve_source(contract, deadline)
            provenance = _provenance(contract, actual_hashes)
            executions: list[ExecutionRecord] = []
            failed_runs: list[str] = []
            wall_time_exhausted = False
            for specification in specifications:
                if wall_time_exhausted:
                    reason = (
                        "冻结合同的墙钟时间预算已用完；"
                        "该估计步骤未执行。"
                    )
                    executions.append(
                        self._failed_execution(
                            specification,
                            provenance,
                            reason,
                            reason_code="budget_exhausted",
                        )
                    )
                    failed_runs.append(f"{specification.step_id}: {reason}")
                    continue
                try:
                    deadline.check()
                    execution = self._fit(
                        source,
                        specification,
                        provenance,
                        deadline=deadline,
                    )
                    deadline.check()
                except _ContractWallTimeExceeded as error:
                    wall_time_exhausted = True
                    execution = self._failed_execution(
                        specification,
                        provenance,
                        str(error),
                        reason_code="budget_exhausted",
                    )
                    failed_runs.append(f"{specification.step_id}: {error}")
                except (OSError, ReproductionError, ValueError) as error:
                    execution = self._failed_execution(
                        specification,
                        provenance,
                        str(error),
                    )
                    failed_runs.append(f"{specification.step_id}: {error}")
                executions.append(execution)
        except _ContractWallTimeExceeded as error:
            return self._failed_run(
                contract,
                str(error),
                reason_code="budget_exhausted",
                specifications=specifications,
            )
        except (CaseImportError, OSError, ReproductionError, ValueError) as error:
            return self._failed_run(contract, str(error))

        succeeded = not failed_runs
        return ResearchRun(
            research_run_id=f"replication-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=contract.approved_plan.plan_version,
            execution_status="succeeded" if succeeded else "failed",
            scientific_status="pending_review" if succeeded else "invalid",
            fixture_only=False,
            not_executed_reason=(
                None
                if succeeded
                else "独立复算失败：" + "; ".join(failed_runs)
            ),
            executions=executions,
            failed_runs=failed_runs,
            warnings=[
                "独立实现已重新解析冻结数据，并用交替去均值、NumPy OLS 与手工实体聚类协方差复算。"
            ],
        )

    def _execute_policy_contract(
        self,
        contract: FormalResearchContract,
        deadline: _ContractDeadline,
    ) -> ResearchRun:
        """Re-estimate selected policy-did-v2 steps with the NumPy implementation."""

        plan = contract.approved_plan
        try:
            baseline = validate_policy_did_execution_plan(plan)
            deadline.check()
            source, actual_hashes = self._resolve_source(contract, deadline)
            provenance = _policy_provenance(contract, actual_hashes)
            budgeted = select_primary_test_dag_with_budget(
                plan,
                contract.budget.max_executions,
            )
            selected_ids = {item.step.step_id for item in budgeted.selected}
            result = reproduce_policy_model(source, baseline)
            deadline.check()
        except _ContractWallTimeExceeded as error:
            return self._failed_run(
                contract,
                str(error),
                reason_code="budget_exhausted",
            )
        except (CaseImportError, OSError, PolicyCausalError, ReproductionError, ValueError) as error:
            return self._failed_run(contract, str(error))

        executions: list[ExecutionRecord] = []
        failed_runs: list[str] = []
        if baseline.step_id in selected_ids:
            executions.append(
                _policy_execution_record(
                    step_id=baseline.step_id,
                    run_type="baseline",
                    result=result,
                    estimates=result["estimates"],
                    extra_diagnostics={"policy_component": "baseline"},
                    provenance=provenance,
                )
            )

        for step in plan.robustness_tests:
            if step.step_id not in selected_ids:
                continue
            try:
                deadline.check()
                if "alternative_outcome" in step.parameters:
                    alternative = baseline.model_copy(
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
                            "outcome": str(step.parameters["alternative_outcome"]),
                        }
                    )
                    policy_component = "alternative_outcome"
                    extra = {"alternative_outcome": alternative.outcome}
                elif "group_assignment_mode" in step.parameters:
                    policy_design = dict(
                        baseline.parameters.get("policy_design", {})
                    )
                    policy_design["group_assignment_mode"] = str(
                        step.parameters["group_assignment_mode"]
                    )
                    alternative = baseline.model_copy(
                        deep=True,
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
                            "parameters": {
                                **baseline.parameters,
                                "policy_design": policy_design,
                            },
                        },
                    )
                    policy_component = "group_assignment_sensitivity"
                    extra = {
                        "group_assignment_mode": policy_design[
                            "group_assignment_mode"
                        ]
                    }
                elif "cluster_fields" in step.parameters:
                    cluster_fields = step.parameters["cluster_fields"]
                    if not isinstance(cluster_fields, list) or not cluster_fields:
                        raise ReproductionError(
                            "实体聚类敏感性没有冻结 cluster_fields。"
                        )
                    policy_design = dict(
                        baseline.parameters.get("policy_design", {})
                    )
                    policy_design["cluster_fields"] = [
                        str(field) for field in cluster_fields
                    ]
                    alternative = baseline.model_copy(
                        deep=True,
                        update={
                            "step_id": step.step_id,
                            "name": step.name,
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
                    policy_component = "cluster_sensitivity"
                    extra = {"cluster_fields": policy_design["cluster_fields"]}
                else:
                    continue
                alternative_result = reproduce_policy_baseline(
                    source,
                    alternative,
                )
                deadline.check()
                executions.append(
                    _policy_execution_record(
                        step_id=step.step_id,
                        run_type="robustness",
                        result=alternative_result,
                        estimates=alternative_result["estimates"],
                        extra_diagnostics={
                            "policy_component": policy_component,
                            **extra,
                        },
                        provenance=provenance,
                    )
                )
            except _ContractWallTimeExceeded as error:
                failed_runs.append(f"{step.step_id}: {error}")
                executions.append(
                    _policy_failed_execution(
                        step.step_id,
                        "robustness",
                        provenance,
                        str(error),
                        reason_code="budget_exhausted",
                    )
                )
            except (OSError, PolicyCausalError, ValueError) as error:
                failed_runs.append(f"{step.step_id}: {error}")
                executions.append(
                    _policy_failed_execution(
                        step.step_id,
                        "robustness",
                        provenance,
                        str(error),
                    )
                )

        event_step = next(
            (
                step
                for step in plan.falsification_tests
                if step.step_id == "check-policy-event-study"
                and step.step_id in selected_ids
            ),
            None,
        )
        if event_step is not None:
            event = result["event_study"]
            if event.get("status") == "succeeded" and event.get("estimates"):
                joint = event.get("joint_pretrend") or {}
                executions.append(
                    _policy_execution_record(
                        step_id=event_step.step_id,
                        run_type="falsification",
                        result=result,
                        estimates=event["estimates"],
                        extra_diagnostics={
                            "policy_component": "event_study",
                            "reference_year": event.get("reference_year"),
                            "requested_event_years": event.get("requested_event_years", []),
                            "generated_event_years": event.get("generated_event_years", []),
                            "unavailable_event_years": event.get("unavailable_event_years", []),
                            "collinear_event_years": event.get("collinear_event_years", []),
                            "requested_remote_pre_years": event.get(
                                "requested_remote_pre_years", []
                            ),
                            "generated_remote_pre_years": event.get(
                                "generated_remote_pre_years", []
                            ),
                            "unavailable_remote_pre_years": event.get(
                                "unavailable_remote_pre_years", []
                            ),
                            "remote_pre_term": event.get("remote_pre_term"),
                            "remote_pre_requested": event.get(
                                "remote_pre_requested", False
                            ),
                            "remote_pre_status": event.get(
                                "remote_pre_status", "not_applicable"
                            ),
                            "remote_pre_complete": event.get(
                                "remote_pre_complete", True
                            ),
                            "collinear_remote_pre": event.get(
                                "collinear_remote_pre", False
                            ),
                            "event_term_scaling": event.get("event_term_scaling"),
                            "policy_year_event_term": event.get(
                                "policy_year_event_term"
                            ),
                            "policy_year_event_requested": event.get(
                                "policy_year_event_requested"
                            ),
                            "policy_year_event_regressor_weight": event.get(
                                "policy_year_event_regressor_weight"
                            ),
                            "baseline_policy_start_weight": event.get(
                                "baseline_policy_start_weight"
                            ),
                            "policy_year_event_uses_baseline_policy_start_weight": (
                                event.get(
                                    "policy_year_event_uses_baseline_policy_start_weight"
                                )
                            ),
                            "policy_year_event_coefficient_directly_comparable_to_baseline": (
                                event.get(
                                    "policy_year_event_coefficient_directly_comparable_to_baseline"
                                )
                            ),
                            "policy_year_event_comparability_note": event.get(
                                "policy_year_event_comparability_note"
                            ),
                            "joint_pretrend": joint,
                            "joint_pretrend_p_value": joint.get("p_value"),
                        },
                        provenance=provenance,
                    )
                )
            else:
                reason = str(
                    (event.get("joint_pretrend") or {}).get("reason")
                    or "冻结事件研究没有可估计系数。"
                )
                failed_runs.append(f"{event_step.step_id}: {reason}")
                executions.append(
                    _policy_failed_execution(
                        event_step.step_id,
                        "falsification",
                        provenance,
                        reason,
                    )
                )

        placebo_step = next(
            (
                step
                for step in plan.falsification_tests
                if step.step_id == "check-policy-placebo-time"
                and step.step_id in selected_ids
            ),
            None,
        )
        if placebo_step is not None:
            placebo = result.get("placebo") or {}
            estimate = placebo.get("estimate")
            if placebo.get("status") == "succeeded" and isinstance(estimate, dict):
                executions.append(
                    _policy_execution_record(
                        step_id=placebo_step.step_id,
                        run_type="falsification",
                        result=result,
                        estimates=[estimate],
                        extra_diagnostics={
                            "policy_component": "fake_policy_timing",
                            "policy_start_year": placebo.get("policy_start_year"),
                            "placebo_start_year": placebo.get("placebo_start_year"),
                            "sample_start_year": placebo.get("sample_start_year"),
                            "sample_end_year": placebo.get("sample_end_year"),
                            "rows_after_sample_filter": placebo.get(
                                "rows_after_sample_filter"
                            ),
                            "rows_used": placebo.get("rows_used"),
                            "rows_dropped": placebo.get("rows_dropped"),
                            "rows_excluded_at_or_after_true_policy": placebo.get(
                                "rows_excluded_at_or_after_true_policy"
                            ),
                            "true_policy_contamination_rows": placebo.get(
                                "true_policy_contamination_rows"
                            ),
                            "pseudo_period_group_row_counts": placebo.get(
                                "pseudo_period_group_row_counts", {}
                            ),
                            "pseudo_pre_support": placebo.get("pseudo_pre_support"),
                            "pseudo_post_support": placebo.get("pseudo_post_support"),
                            "entity_count": placebo.get("entity_count"),
                            "treated_entity_count": placebo.get(
                                "treated_entity_count"
                            ),
                            "control_entity_count": placebo.get(
                                "control_entity_count"
                            ),
                            "group_switcher_entities": placebo.get(
                                "group_switcher_entities"
                            ),
                            "analysis_group_switcher_entities": placebo.get(
                                "analysis_group_switcher_entities"
                            ),
                            "singleton_entities": placebo.get(
                                "singleton_entities"
                            ),
                            "entities_spanning_policy": placebo.get(
                                "entities_spanning_policy"
                            ),
                            "group_row_counts": placebo.get("group_row_counts", {}),
                            "observed_years": placebo.get("observed_years", []),
                            "missing_calendar_years": placebo.get(
                                "missing_calendar_years", []
                            ),
                            "calendar_years_imputed": placebo.get(
                                "calendar_years_imputed", []
                            ),
                            "time_period_count": placebo.get("time_period_count"),
                            "cluster_count": placebo.get("cluster_count"),
                            "cluster_size_min": placebo.get("cluster_size_min"),
                            "cluster_size_median": placebo.get(
                                "cluster_size_median"
                            ),
                            "cluster_size_max": placebo.get("cluster_size_max"),
                            "singleton_cluster_count": placebo.get(
                                "singleton_cluster_count"
                            ),
                            "singleton_cluster_share": placebo.get(
                                "singleton_cluster_share"
                            ),
                            "entities_spanning_multiple_clusters": placebo.get(
                                "entities_spanning_multiple_clusters"
                            ),
                            "fixed_effect_level_counts": placebo.get(
                                "fixed_effect_level_counts", {}
                            ),
                            "fixed_effect_singleton_level_counts": placebo.get(
                                "fixed_effect_singleton_level_counts", {}
                            ),
                            "random_seed": placebo.get("random_seed"),
                            "fit_diagnostics": placebo.get("fit_diagnostics", {}),
                        },
                        provenance=provenance,
                    )
                )
            else:
                reason = str(placebo.get("reason") or "冻结伪政策时点不可估计。")
                failed_runs.append(f"{placebo_step.step_id}: {reason}")
                executions.append(
                    _policy_failed_execution(
                        placebo_step.step_id,
                        "falsification",
                        provenance,
                        reason,
                    )
                )

        succeeded = not failed_runs and bool(executions)
        return ResearchRun(
            research_run_id=f"replication-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=plan.plan_version,
            execution_status="succeeded" if succeeded else "failed",
            scientific_status="pending_review" if succeeded else "invalid",
            fixture_only=False,
            not_executed_reason=(
                None if succeeded else "独立政策复算失败：" + "; ".join(failed_runs)
            ),
            executions=executions,
            failed_runs=failed_runs,
            warnings=[
                "独立实现已重新读取冻结数据，以 NumPy 多维组内变换和手工交互聚类协方差复算 policy-did-v2。"
            ],
        )

    def _resolve_source(
        self,
        contract: FormalResearchContract,
        deadline: _ContractDeadline,
    ) -> tuple[Path, list[str]]:
        if contract.status != "frozen":
            raise ReproductionError("独立复算只接受状态为 frozen 的研究合同。")
        if _sha256_json(contract.approved_plan.model_dump(mode="json")) != (
            contract.approved_plan_hash
        ):
            raise ReproductionError("冻结合同 approved_plan_hash 与计划正文不一致。")
        if contract.approved_plan.design_only:
            raise ReproductionError("仅研究设计的合同没有可复算结果。")
        if contract.approved_plan.method_family not in PANEL_METHODS:
            raise ReproductionError(
                "独立复算器当前只支持 policy_causal、panel_association 和 mechanism_boundary。"
            )
        main_refs = [item for item in contract.dataset_refs if item.role == "main"]
        if len(main_refs) != 1:
            raise ReproductionError("冻结合同必须恰好绑定一个 main 数据资产。")
        main_ref = main_refs[0]
        frozen_hashes = [item.sha256 for item in contract.dataset_refs]
        if contract.data_hashes != frozen_hashes:
            raise ReproductionError(
                "冻结合同 data_hashes 与 dataset_refs 的顺序或内容不一致。"
            )
        actual_hashes: list[str] = []
        source: Path | None = None
        for dataset_ref in contract.dataset_refs:
            deadline.check()
            resolved = self.registry.resolve(dataset_ref)
            actual_hash = _sha256_file(resolved, deadline=deadline)
            if actual_hash != dataset_ref.sha256:
                raise ReproductionError(
                    f"数据文件 {dataset_ref.filename} 哈希与冻结合同不一致。"
                )
            actual_hashes.append(actual_hash)
            if dataset_ref.dataset_id == main_ref.dataset_id:
                source = resolved
        if source is None:
            raise ReproductionError("无法解析冻结合同的 main 数据资产。")
        return source, actual_hashes

    @staticmethod
    def _failed_execution(
        specification: _EstimationSpec,
        provenance: ExecutionProvenance,
        reason: str,
        *,
        reason_code: str = "dependency_failed",
    ) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=f"replication-execution-{uuid4()}",
            run_type=specification.run_type,
            plan_step_id=specification.step_id,
            check_id=specification.step_id,
            execution_status="failed",
            not_executed_reason_code=reason_code,
            error=reason,
            warnings=["独立复算失败；没有退回主实现重跑。"],
            provenance=provenance,
        )

    @staticmethod
    def _estimation_specs(contract: FormalResearchContract) -> list[_EstimationSpec]:
        plan = contract.approved_plan
        if len(plan.baseline_models) != 1:
            raise ReproductionError("独立复算要求冻结计划恰好包含一个基准模型。")
        baseline = plan.baseline_models[0]
        _validate_baseline(baseline)
        budgeted = select_primary_test_dag_with_budget(
            plan,
            contract.budget.max_executions,
        )
        selected_step_ids = {
            item.step.step_id
            for item in budgeted.selected
        }
        specifications = []
        if baseline.step_id in selected_step_ids:
            specifications.append(_specification_for(baseline, None, "baseline"))
        for run_type, steps in (
            ("robustness", plan.robustness_tests),
            ("falsification", plan.falsification_tests),
            ("mechanism", plan.mechanism_tests),
            ("heterogeneity", plan.heterogeneity_tests),
        ):
            for step in steps:
                if (
                    step.step_id in selected_step_ids
                    and is_estimative_test_step(step, run_type)  # type: ignore[arg-type]
                ):
                    specifications.append(_specification_for(baseline, step, run_type))
        step_ids = [item.step_id for item in specifications]
        if len(step_ids) != len(set(step_ids)):
            raise ReproductionError("冻结估计步骤的 step_id 必须全局唯一。")
        return specifications

    @staticmethod
    def _fit(
        path: Path,
        specification: _EstimationSpec,
        provenance: ExecutionProvenance,
        *,
        deadline: _ContractDeadline,
    ) -> ExecutionRecord:
        deadline.check()
        regressors = list(dict.fromkeys([*specification.treatments, *specification.controls]))
        if not regressors:
            raise ReproductionError("冻结估计步骤没有解释变量。")

        interactions = _derived_interactions(specification.parameters)
        leads = _derived_leads(specification.parameters)
        sample_filter = specification.parameters.get("sample_filter")
        sample_filter_column = None
        if sample_filter is not None:
            if not isinstance(sample_filter, str):
                raise ReproductionError(
                    "sample_filter 必须是冻结的简单比较字符串。"
                )
            sample_filter_column = _parse_sample_filter(sample_filter)[0]
        generated_fields = {*interactions, *leads}
        source_fields = [
            specification.entity,
            specification.time,
            specification.outcome,
            *[item for item in regressors if item not in generated_fields],
            *[component for components in interactions.values() for component in components],
            *[item["source"] for item in leads.values()],
        ]
        if specification.subgroup_variable:
            source_fields.append(specification.subgroup_variable)
        if sample_filter_column:
            source_fields.append(sample_filter_column)
        source_fields = list(dict.fromkeys(source_fields))
        frame = _read_csv(path, source_fields)
        deadline.check()
        missing = [field for field in source_fields if field not in frame.columns]
        if missing:
            raise ReproductionError(f"数据缺少冻结模型字段：{', '.join(missing)}")

        for name, components in interactions.items():
            if len(components) != 2:
                raise ReproductionError("交互项必须且只能绑定两个冻结字段。")
            frame[name] = (
                pd.to_numeric(frame[components[0]], errors="coerce")
                * pd.to_numeric(frame[components[1]], errors="coerce")
            )
        dropped_gap_pairs_by_lead: dict[str, int] = {}
        if leads:
            frame[specification.time] = pd.to_numeric(
                frame[specification.time], errors="coerce"
            )
            frame = frame.sort_values(
                [specification.entity, specification.time], kind="mergesort"
            )
            valid_key_mask = (
                frame[specification.entity].notna()
                & frame[specification.time].notna()
            )
            duplicate_key_rows = int(
                frame.loc[valid_key_mask].duplicated(
                    subset=[specification.entity, specification.time], keep=False
                ).sum()
            )
            if duplicate_key_rows:
                raise ReproductionError(
                    f"实体—时间主键存在 {duplicate_key_rows} 条重复记录；独立复算拒绝静默删除。"
                )
            calendar_keys = set(
                zip(
                    frame.loc[valid_key_mask, specification.entity],
                    frame.loc[valid_key_mask, specification.time],
                )
            )
            for name, lead in leads.items():
                if lead["periods"] < 1 or not lead["source"]:
                    raise ReproductionError("前导变量必须绑定源字段和正整数期数。")
                numeric = pd.to_numeric(frame[lead["source"]], errors="coerce")
                source_by_key = dict(
                    zip(
                        zip(
                            frame.loc[valid_key_mask, specification.entity],
                            frame.loc[valid_key_mask, specification.time],
                        ),
                        numeric.loc[valid_key_mask],
                    )
                )
                target_keys = [
                    (entity_value, time_value + lead["periods"])
                    if pd.notna(entity_value) and pd.notna(time_value)
                    else None
                    for entity_value, time_value in zip(
                        frame[specification.entity], frame[specification.time]
                    )
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
                ordinal_lead = numeric.groupby(
                    frame[specification.entity], sort=False
                ).shift(-lead["periods"])
                dropped_gap_pairs_by_lead[name] = int(
                    (valid_key_mask & ordinal_lead.notna() & ~target_exists).sum()
                )

        original_rows = len(frame)
        subgroup_switcher_entities = 0
        if specification.subgroup_variable:
            subgroup_nonmissing = frame.loc[
                frame[specification.subgroup_variable].notna(),
                [specification.entity, specification.subgroup_variable],
            ]
            subgroup_switcher_entities = int(
                (
                    subgroup_nonmissing.groupby(specification.entity)[
                        specification.subgroup_variable
                    ].nunique()
                    > 1
                ).sum()
            )
            if subgroup_switcher_entities:
                raise ReproductionError(
                    "subgroup_variable 必须在实体内稳定；"
                    f"检测到 {subgroup_switcher_entities} 个分组切换实体。"
                )
            frame = frame.loc[
                frame[specification.subgroup_variable] == specification.subgroup_value
            ]
        rows_after_subgroup_filter = len(frame)
        if isinstance(sample_filter, str):
            frame = _apply_sample_filter(frame, sample_filter)
        rows_after_sample_filter = len(frame)
        numeric_fields = [specification.time, specification.outcome, *regressors]
        for field in numeric_fields:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        required = list(
            dict.fromkeys(
                [
                    specification.entity,
                    specification.time,
                    specification.outcome,
                    *regressors,
                ]
            )
        )
        frame = frame.dropna(subset=required)
        duplicate_mask = frame.duplicated(
            subset=[specification.entity, specification.time], keep=False
        )
        duplicate_rows = int(duplicate_mask.sum())
        if duplicate_rows:
            raise ReproductionError(
                f"实体—时间主键存在 {duplicate_rows} 条重复记录；独立复算拒绝静默删除。"
            )

        singleton_rows_dropped = 0
        singleton_entities_dropped = 0
        if bool(specification.parameters.get("drop_singletons", True)):
            entity_counts = frame.groupby(specification.entity)[
                specification.entity
            ].transform("size")
            singleton_mask = entity_counts <= 1
            singleton_rows_dropped = int(singleton_mask.sum())
            singleton_entities_dropped = int(
                frame.loc[singleton_mask, specification.entity].nunique()
            )
            frame = frame.loc[~singleton_mask]
        if len(frame) <= len(regressors) + 2:
            raise ReproductionError("删除缺失值、重复主键和单例后，有效样本不足。")

        frame = frame.sort_values([specification.entity, specification.time])
        entity_codes, entities = pd.factorize(frame[specification.entity], sort=True)
        time_codes, times = pd.factorize(frame[specification.time], sort=True)
        raw = frame[[specification.outcome, *regressors]].to_numpy(float)
        transformed, within_iterations = _alternating_two_way_demean(
            raw,
            entity_codes,
            time_codes,
            deadline_check=deadline.check,
        )
        y = transformed[:, 0]
        x_full = transformed[:, 1:]
        kept_indices = _independent_columns(x_full)
        kept_regressors = [regressors[index] for index in kept_indices]
        x = x_full[:, kept_indices]
        missing_treatments = set(specification.treatments).difference(kept_regressors)
        if missing_treatments:
            raise ReproductionError(
                "核心解释变量被固定效应或其他回归量完全吸收："
                + ", ".join(sorted(missing_treatments))
            )

        coefficients, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
        if int(rank) != x.shape[1]:
            raise ReproductionError("独立去均值后设计矩阵不满列秩。")
        residuals = y - x @ coefficients
        deadline.check()
        covariance = _entity_clustered_covariance(
            x,
            residuals,
            entity_codes,
            deadline_check=deadline.check,
        )
        deadline.check()
        standard_errors = np.sqrt(np.diag(covariance))

        entity_count = len(entities)
        time_count = len(times)
        nobs = len(frame)
        effect_degrees = entity_count + time_count - 1
        df_resid = nobs - x.shape[1] - effect_degrees
        if df_resid <= 0:
            raise ReproductionError("固定效应和回归量耗尽了残差自由度。")
        critical_value = float(student_t.ppf(0.975, df_resid))
        estimates: list[dict[str, Any]] = []
        for treatment in specification.treatments:
            index = kept_regressors.index(treatment)
            coefficient = float(coefficients[index])
            standard_error = float(standard_errors[index])
            if not math.isfinite(standard_error) or standard_error <= 0:
                raise ReproductionError(f"变量 {treatment} 的聚类标准误无效。")
            statistic = coefficient / standard_error
            p_value = float(2 * student_t.sf(abs(statistic), df_resid))
            estimates.append(
                {
                    "term": treatment,
                    "coefficient": coefficient,
                    "standard_error": standard_error,
                    "t_statistic": statistic,
                    "p_value": p_value,
                    "confidence_interval_95": [
                        coefficient - critical_value * standard_error,
                        coefficient + critical_value * standard_error,
                    ],
                    "nobs": nobs,
                }
            )

        residual_ss = float(residuals @ residuals)
        total_ss = float(y @ y)
        diagnostics = {
            "rows_input": original_rows,
            "rows_after_subgroup_filter": rows_after_subgroup_filter,
            "rows_after_sample_filter": rows_after_sample_filter,
            "rows_used": nobs,
            "rows_dropped": original_rows - nobs,
            "dropped_gap_pairs": sum(dropped_gap_pairs_by_lead.values()),
            "dropped_gap_pairs_by_lead": dropped_gap_pairs_by_lead,
            "subgroup_variable": specification.subgroup_variable,
            "subgroup_value": specification.subgroup_value,
            "subgroup_switcher_entities": subgroup_switcher_entities,
            "duplicate_rows_dropped": 0,
            "singleton_entities_dropped": singleton_entities_dropped,
            "singleton_rows_dropped": singleton_rows_dropped,
            "entity_count": entity_count,
            "time_period_count": time_count,
            "r_squared_model": 1 - residual_ss / total_ss if total_ss > 0 else 0.0,
            "entity_fixed_effects": True,
            "time_fixed_effects": True,
            "standard_errors": "clustered_by_entity",
            "cluster_variable": specification.entity,
            "cluster_correction": "stata_reghdfe_compatible_entity_cluster",
            "within_algorithm": "alternating_two_way_demean",
            "within_tolerance": WITHIN_RELATIVE_TOLERANCE,
            "within_iterations": within_iterations,
            "estimator": "numpy.linalg.lstsq",
            "degrees_of_freedom": {
                "model": x.shape[1] + effect_degrees,
                "residual": df_resid,
            },
        }
        return ExecutionRecord(
            execution_id=f"replication-execution-{uuid4()}",
            run_type=specification.run_type,
            plan_step_id=specification.step_id,
            check_id=specification.step_id,
            execution_status="succeeded",
            estimates=estimates,
            diagnostic_results=diagnostics,
            warnings=[],
            provenance=provenance,
        )

    @staticmethod
    def _failed_run(
        contract: FormalResearchContract,
        reason: str,
        *,
        reason_code: str = "dependency_failed",
        specifications: list[_EstimationSpec] | None = None,
    ) -> ResearchRun:
        provenance = _provenance(contract, [])
        if specifications:
            executions = [
                ResearchReproducer._failed_execution(
                    specification,
                    provenance,
                    reason,
                    reason_code=reason_code,
                )
                for specification in specifications
            ]
            failed_runs = [
                f"{specification.step_id}: {reason}"
                for specification in specifications
            ]
        elif (
            contract.approved_plan.method_family == "policy_causal"
            and len(contract.approved_plan.baseline_models) != 1
        ):
            executions = []
            failed_runs = [reason]
        else:
            baseline_step_id = (
                contract.approved_plan.baseline_models[0].step_id
                if contract.approved_plan.baseline_models
                else "model_baseline"
            )
            executions = [
                ExecutionRecord(
                    execution_id=f"replication-execution-{uuid4()}",
                    run_type="baseline",
                    plan_step_id=baseline_step_id,
                    check_id=baseline_step_id,
                    execution_status="failed",
                    not_executed_reason_code=reason_code,
                    error=reason,
                    warnings=["独立复算失败；没有生成或补造任何统计结果。"],
                    provenance=provenance,
                )
            ]
            failed_runs = [reason]
        return ResearchRun(
            research_run_id=f"replication-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=contract.approved_plan.plan_version,
            execution_status="failed",
            scientific_status="invalid",
            fixture_only=False,
            not_executed_reason=reason,
            executions=executions,
            failed_runs=failed_runs,
            warnings=["没有退回主实现重跑。"],
        )


def _policy_provenance(
    contract: FormalResearchContract,
    data_sha256: list[str],
) -> ExecutionProvenance:
    base = _provenance(contract, data_sha256)
    policy_code = Path(__file__).with_name("policy_causal.py")
    return base.model_copy(
        update={
            "implementation_id": POLICY_REPRODUCTION_IMPLEMENTATION_ID,
            "implementation_version": "1.0.0",
            "code_sha256": _sha256_file(policy_code),
        }
    )


def _policy_diagnostics(
    result: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    source = dict(result["diagnostics"])
    rows_used = int(source["rows_used"])
    fixed_effects = [str(value) for value in source.get("fixed_effects", [])]
    cluster_fields = [str(value) for value in source.get("cluster_fields", [])]
    observed_years = list(source.get("observed_years", []))
    return {
        **source,
        "rows_after_sample_filter": rows_used,
        "duplicate_rows_dropped": 0,
        "singleton_entities_dropped": 0,
        "singleton_rows_dropped": 0,
        "time_period_count": len(observed_years),
        "entity_fixed_effects": bool(fixed_effects),
        "time_fixed_effects": len(fixed_effects) >= 2,
        "standard_errors": "clustered_by_interaction",
        "cluster_variable": "×".join(cluster_fields),
        "cluster_correction": "finite_sample_interaction_cluster",
        **extra,
    }


def _policy_execution_record(
    *,
    step_id: str,
    run_type: str,
    result: dict[str, Any],
    estimates: list[dict[str, Any]],
    extra_diagnostics: dict[str, Any],
    provenance: ExecutionProvenance,
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=f"replication-execution-{uuid4()}",
        run_type=run_type,  # type: ignore[arg-type]
        plan_step_id=step_id,
        check_id=step_id,
        execution_status="succeeded",
        estimates=estimates,
        diagnostic_results=_policy_diagnostics(result, extra_diagnostics),
        provenance=provenance,
    )


def _policy_failed_execution(
    step_id: str,
    run_type: str,
    provenance: ExecutionProvenance,
    reason: str,
    *,
    reason_code: str = "dependency_failed",
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=f"replication-execution-{uuid4()}",
        run_type=run_type,  # type: ignore[arg-type]
        plan_step_id=step_id,
        check_id=step_id,
        execution_status="failed",
        not_executed_reason_code=reason_code,  # type: ignore[arg-type]
        error=reason,
        warnings=["独立政策复算失败；没有退回主实现重跑。"],
        provenance=provenance,
    )


def compare_panel_reproduction(
    primary: ResearchRun,
    replication: ResearchRun,
    *,
    absolute_tolerance: float = DEFAULT_ABSOLUTE_TOLERANCE,
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
) -> ReproductionAudit:
    """Compare panel estimates, sample flow, settings, hashes, and implementations."""
    differences: list[str] = []
    metric_differences: list[dict[str, Any]] = []
    compared_fields = [
        "binding",
        "implementation_id",
        "contract_sha256",
        "data_sha256",
        "covered_plan_step_ids",
        "sample_flow",
        "fixed_effects",
        "cluster_strategy",
        "coefficient",
        "standard_error",
    ]

    for field_name in ("case_id", "contract_hash", "plan_version"):
        if getattr(primary, field_name) != getattr(replication, field_name):
            differences.append(f"{field_name} 不一致。")
    if primary.execution_status != "succeeded":
        differences.append("主运行未成功。")
    if replication.execution_status != "succeeded":
        differences.append("独立复算未成功。")

    primary_executions, primary_duplicates = _successful_estimation_map(primary)
    replica_executions, replica_duplicates = _successful_estimation_map(replication)
    if primary_duplicates:
        differences.append("主运行存在重复的估计 step_id：" + "、".join(primary_duplicates))
    if replica_duplicates:
        differences.append("复算运行存在重复的估计 step_id：" + "、".join(replica_duplicates))
    primary_steps = set(primary_executions)
    replica_steps = set(replica_executions)
    if primary_steps != replica_steps:
        missing = sorted(primary_steps - replica_steps)
        extra = sorted(replica_steps - primary_steps)
        if missing:
            differences.append("独立复算缺少估计步骤：" + "、".join(missing))
        if extra:
            differences.append("独立复算多出估计步骤：" + "、".join(extra))

    primary_provenances = _provenances(primary_executions.values())
    replica_provenances = _provenances(replica_executions.values())
    primary_implementation = _single_provenance_value(
        primary_provenances, "implementation_id", differences, "主运行"
    )
    replica_implementation = _single_provenance_value(
        replica_provenances, "implementation_id", differences, "独立复算"
    )
    if (
        primary_implementation is not None
        and replica_implementation is not None
        and primary_implementation == replica_implementation
    ):
        differences.append("主运行与复算运行的 implementation_id 必须不同。")
    for field_name in ("contract_sha256", "data_sha256"):
        primary_value = _single_provenance_value(
            primary_provenances, field_name, differences, "主运行"
        )
        replica_value = _single_provenance_value(
            replica_provenances, field_name, differences, "独立复算"
        )
        if (
            primary_value is not None
            and replica_value is not None
            and primary_value != replica_value
        ):
            differences.append(f"主运行与复算运行的 {field_name} 不一致。")

    sample_fields = (
        "rows_input",
        "rows_after_subgroup_filter",
        "rows_after_sample_filter",
        "rows_used",
        "rows_dropped",
        "dropped_gap_pairs",
        "dropped_gap_pairs_by_lead",
        "subgroup_variable",
        "subgroup_value",
        "subgroup_switcher_entities",
        "duplicate_rows_dropped",
        "singleton_entities_dropped",
        "singleton_rows_dropped",
        "entity_count",
        "time_period_count",
        "treated_entity_count",
        "control_entity_count",
        "group_switcher_entities",
        "analysis_group_switcher_entities",
        "singleton_entities",
        "entities_spanning_policy",
        "group_row_counts",
        "observed_years",
        "missing_calendar_years",
        "calendar_years_imputed",
        "cluster_count",
        "cluster_size_min",
        "cluster_size_median",
        "cluster_size_max",
        "singleton_cluster_count",
        "singleton_cluster_share",
        "entities_spanning_multiple_clusters",
        "fixed_effect_level_counts",
        "fixed_effect_singleton_level_counts",
        "sample_start_year",
        "sample_end_year",
        "rows_excluded_at_or_after_true_policy",
        "true_policy_contamination_rows",
        "pseudo_period_group_row_counts",
        "pseudo_pre_support",
        "pseudo_post_support",
        "requested_remote_pre_years",
        "generated_remote_pre_years",
        "unavailable_remote_pre_years",
        "remote_pre_term",
        "remote_pre_requested",
        "remote_pre_status",
        "remote_pre_complete",
        "collinear_remote_pre",
        "event_term_scaling",
        "policy_year_event_term",
        "policy_year_event_requested",
        "policy_year_event_regressor_weight",
        "baseline_policy_start_weight",
        "policy_year_event_uses_baseline_policy_start_weight",
        "policy_year_event_coefficient_directly_comparable_to_baseline",
        "policy_year_event_comparability_note",
    )
    setting_fields = (
        "entity_fixed_effects",
        "time_fixed_effects",
        "standard_errors",
        "cluster_variable",
        "cluster_correction",
    )
    for step_id in sorted(primary_steps & replica_steps):
        primary_execution = primary_executions[step_id]
        replica_execution = replica_executions[step_id]
        for field_name in (*sample_fields, *setting_fields):
            left = primary_execution.diagnostic_results.get(field_name)
            right = replica_execution.diagnostic_results.get(field_name)
            if left != right:
                differences.append(
                    f"{step_id}.{field_name} 不一致：{left!r} != {right!r}。"
                )
        primary_estimates = {
            str(item.get("term")): item for item in primary_execution.estimates
        }
        replica_estimates = {
            str(item.get("term")): item for item in replica_execution.estimates
        }
        if set(primary_estimates) != set(replica_estimates):
            differences.append(f"{step_id} 的核心估计项不一致。")
            continue
        for term in sorted(primary_estimates):
            for metric in ("coefficient", "standard_error"):
                left = _finite_float(primary_estimates[term].get(metric))
                right = _finite_float(replica_estimates[term].get(metric))
                absolute_difference = abs(left - right)
                relative_difference = absolute_difference / max(abs(left), abs(right), 1e-300)
                metric_differences.append(
                    {
                        "plan_step_id": step_id,
                        "term": term,
                        "metric": metric,
                        "primary": left,
                        "replication": right,
                        "absolute_difference": absolute_difference,
                        "relative_difference": relative_difference,
                        "matched": (
                            absolute_difference <= absolute_tolerance
                            or relative_difference <= relative_tolerance
                        ),
                    }
                )
                if (
                    absolute_difference > absolute_tolerance
                    and relative_difference > relative_tolerance
                ):
                    differences.append(
                        f"{step_id}.{term}.{metric} 超出容差："
                        f"abs={absolute_difference:.3g}, rel={relative_difference:.3g}。"
                    )

    status: Literal["matched", "diverged", "failed"]
    status = "matched" if not differences else "diverged"
    if primary.execution_status != "succeeded" or replication.execution_status != "succeeded":
        status = "failed"
    policy_estimator_only = (
        primary_implementation == POLICY_PRIMARY_IMPLEMENTATION_ID
        and replica_implementation == POLICY_REPRODUCTION_IMPLEMENTATION_ID
    )
    return ReproductionAudit(
        audit_id=f"reproduction-{uuid4()}",
        primary_run_id=primary.research_run_id,
        replication_run_id=replication.research_run_id,
        status=status,
        compared_fields=compared_fields,
        differences=differences,
        numeric_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
        mode="independent_implementation",
        independence_scope=(
            "estimator_only"
            if policy_estimator_only
            else "data_preparation_and_estimator"
        ),
        shared_components=(
            [
                "policy_causal analysis-table preparation",
                "policy event/placebo regressor construction",
            ]
            if policy_estimator_only
            else []
        ),
        covered_plan_step_ids=sorted(primary_steps & replica_steps),
        primary_implementation_id=primary_implementation,
        replication_implementation_id=replica_implementation,
        metric_differences=metric_differences,
    )


def _validate_baseline(baseline: ModelSpec) -> None:
    if not baseline.outcome or not baseline.treatments_or_exposures:
        raise ReproductionError("基准模型缺少结果变量或核心解释变量。")
    if len(baseline.fixed_effects) != 2:
        raise ReproductionError("独立面板复算要求恰好一个实体和一个时间固定效应。")
    entity, _ = _panel_keys(baseline.fixed_effects)
    cluster_variable = str(baseline.parameters.get("cluster_variable", "")).strip()
    if cluster_variable and cluster_variable != entity:
        raise ReproductionError("冻结合同的聚类变量必须与实体固定效应字段一致。")
    strategy = re.sub(
        r"[\s_-]+",
        " ",
        str(baseline.standard_error_strategy or "").casefold(),
    ).strip()
    entity_label = re.sub(r"[\s_-]+", " ", entity.casefold()).strip()
    entity_aliases = {
        f"cluster by {entity_label}",
        f"clustered by {entity_label}",
    }
    entity_markers = ("entity", "firm", "企业", "实体")
    if not cluster_variable and not (
        "cluster" in strategy or "聚类" in strategy
    ):
        raise ReproductionError("冻结合同未明确指定聚类标准误。")
    if (
        not cluster_variable
        and strategy not in entity_aliases
        and not any(marker in strategy for marker in entity_markers)
    ):
        raise ReproductionError("冻结合同必须明确按实体层级聚类标准误。")


def _specification_for(
    baseline: ModelSpec,
    step: PlannedStep | None,
    run_type: str,
) -> _EstimationSpec:
    parameters = {**baseline.parameters, **(step.parameters if step else {})}
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
        if not mechanism or not treatments:
            raise ReproductionError("机制步骤必须冻结机制变量和核心解释变量。")
        exposure = treatments[0]
        interaction = str(
            parameters.get("interaction_term") or f"{exposure}_x_{mechanism}"
        )
        parameters["derived_interactions"] = {interaction: [exposure, mechanism]}
        treatments = [exposure, interaction]
        controls = list(dict.fromkeys([mechanism, *controls]))

    lead_exposure = parameters.get("lead_exposure")
    if lead_exposure:
        lead_name = str(lead_exposure)
        lead_source = str(
            parameters.get("lead_source")
            or (baseline.treatments_or_exposures[0] if baseline.treatments_or_exposures else "")
        )
        parameters["derived_leads"] = {
            lead_name: {
                "source": lead_source,
                "periods": int(parameters.get("lead_periods", 1)),
            }
        }
        treatments = [lead_name]

    entity, time = _panel_keys(baseline.fixed_effects)
    subgroup_variable = None
    subgroup_value: Any = None
    if run_type == "heterogeneity":
        subgroup_variable = str(parameters.get("subgroup_variable") or "").strip()
        if not subgroup_variable or "subgroup_value" not in parameters:
            raise ReproductionError(
                "异质性步骤必须冻结 subgroup_variable 与 subgroup_value。"
            )
        subgroup_value = parameters["subgroup_value"]

    return _EstimationSpec(
        step_id=(step.step_id if step else baseline.step_id),
        run_type=run_type,  # type: ignore[arg-type]
        outcome=outcome,
        treatments=tuple(treatments),
        controls=tuple(controls),
        entity=entity,
        time=time,
        parameters=parameters,
        subgroup_variable=subgroup_variable,
        subgroup_value=subgroup_value,
    )


def _derived_interactions(parameters: dict[str, Any]) -> dict[str, list[str]]:
    return {
        str(name): [str(value) for value in values]
        for name, values in parameters.get("derived_interactions", {}).items()
    }


def _derived_leads(parameters: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(name): {
            "source": str(specification.get("source", "")),
            "periods": int(specification.get("periods", 1)),
        }
        for name, specification in parameters.get("derived_leads", {}).items()
    }


_SAMPLE_FILTER_RE = re.compile(
    r"^\s*([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)\s*"
    r"(==|!=|>=|<=|>|<)\s*([-+]?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")\s*$"
)


def _parse_sample_filter(expression: str) -> tuple[str, str, float | str]:
    match = _SAMPLE_FILTER_RE.fullmatch(expression)
    if match is None:
        raise ReproductionError(
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
        raise ReproductionError(f"sample_filter 字段不存在：{column}")
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


def _alternating_two_way_demean(
    values: np.ndarray,
    entity_codes: np.ndarray,
    time_codes: np.ndarray,
    *,
    deadline_check: Callable[[], None] | None = None,
) -> tuple[np.ndarray, int]:
    current = np.asarray(values, dtype=float).copy()
    scale = max(float(np.max(np.abs(current))), np.finfo(float).eps)
    tolerance = WITHIN_RELATIVE_TOLERANCE * scale
    for iteration in range(1, WITHIN_MAX_ITERATIONS + 1):
        if deadline_check is not None:
            deadline_check()
        previous = current.copy()
        current = _group_demean(current, entity_codes)
        current = _group_demean(current, time_codes)
        if float(np.max(np.abs(current - previous))) <= tolerance:
            return current, iteration
    raise ReproductionError(
        f"交替双向去均值在 {WITHIN_MAX_ITERATIONS} 次迭代后仍未收敛。"
    )


def _group_demean(values: np.ndarray, codes: np.ndarray) -> np.ndarray:
    group_count = int(codes.max()) + 1
    totals = np.zeros((group_count, values.shape[1]), dtype=float)
    counts = np.bincount(codes, minlength=group_count).astype(float)
    np.add.at(totals, codes, values)
    return values - totals[codes] / counts[codes, None]


def _independent_columns(values: np.ndarray) -> list[int]:
    kept: list[int] = []
    current_rank = 0
    for index in range(values.shape[1]):
        candidate = values[:, [*kept, index]]
        candidate_rank = int(np.linalg.matrix_rank(candidate))
        if candidate_rank > current_rank:
            kept.append(index)
            current_rank = candidate_rank
    if not kept:
        raise ReproductionError("所有解释变量均被双向固定效应吸收。")
    return kept


def _entity_clustered_covariance(
    x: np.ndarray,
    residuals: np.ndarray,
    entity_codes: np.ndarray,
    *,
    deadline_check: Callable[[], None] | None = None,
) -> np.ndarray:
    nobs, nvar = x.shape
    groups = np.unique(entity_codes)
    if len(groups) <= 1:
        raise ReproductionError("实体聚类协方差至少需要两个实体。")
    if nobs <= nvar:
        raise ReproductionError("样本数不足以计算有限样本聚类协方差。")
    bread = np.linalg.inv(x.T @ x)
    meat = np.zeros((nvar, nvar), dtype=float)
    scores = x * residuals[:, None]
    for group in groups:
        if deadline_check is not None:
            deadline_check()
        group_score = scores[entity_codes == group].sum(axis=0)
        meat += np.outer(group_score, group_score)
    regression_debias = nobs / (nobs - nvar)
    group_debias = (len(groups) / (len(groups) - 1)) * ((nobs - 1) / nobs)
    covariance = bread @ meat @ bread * regression_debias * group_debias
    return (covariance + covariance.T) / 2


def _panel_keys(fixed_effects: list[str]) -> tuple[str, str]:
    time_markers = {"year", "time", "年份", "年度"}
    time = next(
        (
            name
            for name in fixed_effects
            if name.replace("_", "").casefold() in time_markers
        ),
        fixed_effects[-1],
    )
    entity = next(name for name in fixed_effects if name != time)
    return entity, time


def _read_csv(path: Path, usecols: list[str]) -> pd.DataFrame:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(
                path, encoding=encoding, usecols=lambda name: name in usecols
            )
        except UnicodeDecodeError as error:
            last_error = error
    raise ReproductionError("CSV 编码必须是 UTF-8 或 GB18030。") from last_error


def _sha256_file(
    path: Path,
    *,
    deadline: _ContractDeadline | None = None,
) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if deadline is not None:
                deadline.check()
            hasher.update(chunk)
    if deadline is not None:
        deadline.check()
    return hasher.hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provenance(
    contract: FormalResearchContract, data_sha256: list[str]
) -> ExecutionProvenance:
    environment = {
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "platform": platform.platform(),
    }
    return ExecutionProvenance(
        implementation_id=IMPLEMENTATION_ID,
        implementation_version=IMPLEMENTATION_VERSION,
        code_sha256=_sha256_file(Path(__file__)),
        environment_sha256=_sha256_json(environment),
        contract_sha256=_sha256_json(contract.model_dump(mode="json")),
        data_sha256=data_sha256,
    )


def _successful_estimation_map(
    run: ResearchRun,
) -> tuple[dict[str, ExecutionRecord], list[str]]:
    result: dict[str, ExecutionRecord] = {}
    duplicates: list[str] = []
    for execution in run.executions:
        if execution.execution_status != "succeeded" or not execution.estimates:
            continue
        if execution.plan_step_id in result:
            duplicates.append(execution.plan_step_id)
        result[execution.plan_step_id] = execution
    return result, sorted(set(duplicates))


def _provenances(
    executions: Any,
) -> list[ExecutionProvenance]:
    return [item.provenance for item in executions if item.provenance is not None]


def _single_provenance_value(
    provenances: list[ExecutionProvenance],
    field_name: str,
    differences: list[str],
    label: str,
) -> Any:
    if not provenances:
        differences.append(f"{label}缺少 execution provenance。")
        return None
    values = {
        json.dumps(getattr(item, field_name), ensure_ascii=False, sort_keys=True)
        for item in provenances
    }
    if len(values) != 1:
        differences.append(f"{label}的 {field_name} 在估计步骤之间不一致。")
        return None
    return getattr(provenances[0], field_name)


def _finite_float(value: Any) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ReproductionError("复算比较不接受非有限数值。")
    return numeric
