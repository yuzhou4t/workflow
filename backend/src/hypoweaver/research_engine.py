from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from linearmodels.panel import PanelOLS

from .case_import import CaseImportError, DatasetRegistry
from .models import ExecutionRecord, FormalResearchContract, ModelSpec, ResearchRun


SUPPORTED_METHODS = {"panel_association", "mechanism_boundary"}


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
            source = self.registry.resolve(contract.dataset_refs[0])
            self._verify_file(source, contract.dataset_refs[0].sha256)
            execution = self._fit_panel(source, plan.baseline_models[0])
        except (CaseImportError, ResearchEngineError, OSError, ValueError) as error:
            return self._failed_run(contract, str(error))

        warnings = [
            "当前执行器只运行冻结的基准双向固定效应模型。",
            "稳健性、证伪、机制和异质性步骤尚未执行，因此科学状态标记为 limited。",
        ]
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=plan.plan_version,
            execution_status="succeeded",
            scientific_status="limited",
            fixture_only=False,
            executions=[execution],
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

    @staticmethod
    def _fit_panel(path: Path, model: ModelSpec) -> ExecutionRecord:
        if not model.outcome or not model.treatments_or_exposures:
            raise ResearchEngineError("基准模型缺少结果变量或核心解释变量。")
        if len(model.fixed_effects) < 2:
            raise ResearchEngineError("双向固定效应模型需要实体和时间变量。")

        entity, time = _panel_keys(model.fixed_effects)
        regressors = [*model.treatments_or_exposures, *model.controls]
        required = [entity, time, model.outcome, *regressors]
        required = list(dict.fromkeys(required))
        frame = _read_csv(path, required)
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise ResearchEngineError(f"数据缺少冻结模型字段：{', '.join(missing)}")

        original_rows = len(frame)
        for column in [time, model.outcome, *regressors]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=required)
        frame = frame.loc[~frame.duplicated(subset=[entity, time], keep=False)]
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
            ).fit(cov_type="clustered", cluster_entity=True)
        except Exception as error:
            raise ResearchEngineError(f"面板模型估计失败：{error}") from error

        estimates: list[dict[str, Any]] = []
        for variable in model.treatments_or_exposures:
            if variable not in result.params.index:
                continue
            coefficient = float(result.params[variable])
            standard_error = float(result.std_errors[variable])
            estimates.append(
                {
                    "term": variable,
                    "coefficient": coefficient,
                    "standard_error": standard_error,
                    "t_statistic": float(result.tstats[variable]),
                    "p_value": float(result.pvalues[variable]),
                    "confidence_interval_95": [
                        coefficient - 1.96 * standard_error,
                        coefficient + 1.96 * standard_error,
                    ],
                    "nobs": int(result.nobs),
                }
            )
        if not estimates:
            raise ResearchEngineError("核心解释变量被固定效应完全吸收，未得到可报告估计。")

        diagnostics = {
            "rows_input": original_rows,
            "rows_used": int(result.nobs),
            "rows_dropped": original_rows - int(result.nobs),
            "entity_count": int(frame.index.get_level_values(0).nunique()),
            "time_period_count": int(frame.index.get_level_values(1).nunique()),
            "r_squared_within": _finite_or_none(result.rsquared_within),
            "entity_fixed_effects": True,
            "time_fixed_effects": True,
            "standard_errors": "clustered_by_entity",
        }
        return ExecutionRecord(
            execution_id=f"execution-{uuid4()}",
            run_type="baseline",
            plan_step_id=model.step_id,
            execution_status="succeeded",
            estimates=estimates,
            diagnostic_results=diagnostics,
            warnings=[],
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
