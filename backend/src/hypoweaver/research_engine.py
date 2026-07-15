from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
from linearmodels.panel import PanelOLS
from scipy.optimize import minimize_scalar
from scipy.stats import norm

from .case_import import CaseImportError, DatasetRegistry
from .models import (
    ExecutionRecord,
    FormalResearchContract,
    ModelSpec,
    PlannedStep,
    ResearchRun,
)
from .spatial import SpatialWeights, is_spatial_weights_filename


SUPPORTED_METHODS = {"panel_association", "mechanism_boundary", "spatial"}


class ResearchEngineError(ValueError):
    pass


class PanelResearchEngine:
    """Deterministic executor for frozen panel-regression contracts."""

    def __init__(self, registry: DatasetRegistry | None = None) -> None:
        self.registry = registry or DatasetRegistry()

    def execute(self, contract: FormalResearchContract) -> ResearchRun:
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
            main_ref = next(
                (item for item in contract.dataset_refs if item.role == "main"),
                contract.dataset_refs[0],
            )
            source = self.registry.resolve(main_ref)
            self._verify_file(source, main_ref.sha256)
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
                self._verify_file(weights_source, weights_ref.sha256)
                execution = self._fit_spatial(source, weights_source, model)
            else:
                execution = self._fit_panel(source, plan.baseline_models[0])
        except (CaseImportError, ResearchEngineError, OSError, ValueError) as error:
            return self._failed_run(contract, str(error))

        executions = [execution]
        failed_runs: list[str] = []
        if plan.method_family != "spatial":
            planned = [
                ("diagnostic", step)
                for step in plan.diagnostics
            ] + [
                ("robustness", step)
                for step in plan.robustness_tests
            ] + [
                ("falsification", step)
                for step in plan.falsification_tests
            ] + [
                ("mechanism", step)
                for step in plan.mechanism_tests
            ] + [
                ("heterogeneity", step)
                for step in plan.heterogeneity_tests
            ]
            for run_type, step in planned[: max(contract.budget.max_executions - 1, 0)]:
                try:
                    execution = self._execute_panel_step(
                        source,
                        plan.baseline_models[0],
                        step,
                        run_type,
                    )
                except (ResearchEngineError, OSError, ValueError) as error:
                    execution = ExecutionRecord(
                        execution_id=f"execution-{uuid4()}",
                        run_type=run_type,
                        plan_step_id=step.step_id,
                        execution_status="failed",
                        error=str(error),
                        warnings=["该冻结步骤失败；没有用其他模型替换。"],
                    )
                    failed_runs.append(f"{step.step_id}: {error}")
                executions.append(execution)

            for run_type, step in planned[len(executions) - 1 :]:
                reason = "冻结合同的最大执行次数预算已用完。"
                executions.append(
                    ExecutionRecord(
                        execution_id=f"execution-{uuid4()}",
                        run_type=run_type,
                        plan_step_id=step.step_id,
                        execution_status="not_executed",
                        error=reason,
                        warnings=[reason],
                    )
                )
                failed_runs.append(f"{step.step_id}: {reason}")

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

    @staticmethod
    def _verify_file(path: Path, expected_sha256: str) -> None:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
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
        if run_type == "falsification" and not any(
            key in step.parameters
            for key in ("alternative_outcome", "placebo_outcome", "lead_exposure")
        ):
            return self._run_feasibility_check(path, baseline, step)
        if run_type == "heterogeneity":
            raise ResearchEngineError(
                "异质性步骤必须由合同明确给出 subgroup_variable 与 subgroup_value；当前执行器不猜测分组。"
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
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="diagnostic",
            plan_step_id=step.step_id,
            execution_status="succeeded",
            diagnostic_results={
                "rows_inspected": len(frame),
                "duplicate_primary_key_rows": duplicate_rows,
                "singleton_rows": singleton_rows,
                "within_variance": within_variance,
                "missing_rate": missing_rate,
            },
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
        required = [entity, time, model.outcome, *regressors]
        required = list(dict.fromkeys(required))
        source_fields = [
            field
            for field in required
            if field not in generated_fields
        ] + interaction_inputs + lead_inputs
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

        if derived_leads:
            frame = frame.sort_values([entity, time])
            for name, specification in derived_leads.items():
                source = specification["source"]
                periods = specification["periods"]
                if not source:
                    raise ResearchEngineError("前导变量构造缺少源字段。")
                numeric = pd.to_numeric(frame[source], errors="coerce")
                frame[name] = numeric.groupby(frame[entity]).shift(-periods)

        original_rows = len(frame)
        for column in [time, model.outcome, *regressors]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=required)
        frame = frame.loc[~frame.duplicated(subset=[entity, time], keep=False)]
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
                check_rank=False,
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
            "rows_used": int(result.nobs),
            "rows_dropped": original_rows - int(result.nobs),
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
    def _failed_run(contract: FormalResearchContract, reason: str) -> ResearchRun:
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=contract.approved_plan.plan_version,
            execution_status="failed",
            scientific_status="invalid",
            fixture_only=False,
            not_executed_reason=reason,
            executions=[
                ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type="baseline",
                    plan_step_id="model_baseline",
                    execution_status="failed",
                    error=reason,
                )
            ],
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


def _read_csv(path: Path, usecols: list[str]) -> pd.DataFrame:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(path, encoding=encoding, usecols=lambda name: name in usecols)
        except UnicodeDecodeError as error:
            last_error = error
    raise ResearchEngineError("CSV 编码必须是 UTF-8 或 GB18030。") from last_error


def _finite_or_none(value: Any) -> float | None:
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None
