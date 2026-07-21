from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .models import FormalResearchContract
from .plot_agent.recipe_contracts import RecipeId


@dataclass(frozen=True)
class DerivedFigureInput:
    recipe_id: RecipeId
    data: list[dict[str, Any]] | dict[str, Any]


def derive_dataset_figure_inputs(
    contract: FormalResearchContract,
    dataset_path: Path,
) -> tuple[list[DerivedFigureInput], list[str]]:
    """Derive plot-only summaries without exposing raw rows to the renderer."""

    warnings: list[str] = []
    main_ref = next(
        (item for item in contract.dataset_refs if item.role == "main"),
        contract.dataset_refs[0] if contract.dataset_refs else None,
    )
    if main_ref is None:
        return [], ["冻结合同没有主数据资产，未派生描述类图形。"]
    _verify_sha256(dataset_path, main_ref.sha256)
    if not contract.approved_plan.baseline_models:
        return [], ["冻结计划没有基准模型，未派生描述类图形。"]

    model = contract.approved_plan.baseline_models[0]
    outcome = str(model.outcome or "").strip()
    exposures = [
        str(item).strip()
        for item in model.treatments_or_exposures
        if str(item).strip()
    ]
    controls = [str(item).strip() for item in model.controls if str(item).strip()]
    policy_design = model.parameters.get("policy_design", {})
    if not isinstance(policy_design, dict):
        policy_design = {}
    time_field = str(policy_design.get("time_field") or "").strip()
    group_field = str(policy_design.get("group_field") or "").strip()
    intervention_period = _policy_intervention_period(policy_design)
    if not time_field:
        time_field = _declared_time_field(model.fixed_effects)

    numeric_candidates = list(
        dict.fromkeys(
            field for field in [outcome, *exposures, *controls] if field
        )
    )
    required = list(
        dict.fromkeys(
            [
                *numeric_candidates,
                *([time_field] if time_field else []),
                *([group_field] if group_field else []),
            ]
        )
    )
    if not required:
        return [], ["冻结模型没有可用于描述类图形的字段。"]
    frame = _read_csv(dataset_path, required)
    missing = [field for field in required if field not in frame.columns]
    if missing:
        return [], ["数据缺少绘图所需冻结字段：" + "、".join(missing)]

    numeric: dict[str, pd.Series] = {
        field: pd.to_numeric(frame[field], errors="coerce").replace(
            [np.inf, -np.inf],
            np.nan,
        )
        for field in numeric_candidates
        if field in frame.columns
    }
    inputs: list[DerivedFigureInput] = []

    descriptive = _descriptive_records(numeric, len(frame))
    if descriptive:
        inputs.append(
            DerivedFigureInput("descriptive_statistics", descriptive)
        )

    correlation = _correlation_payload(numeric)
    if correlation is not None:
        inputs.append(
            DerivedFigureInput("correlation_heatmap", correlation)
        )

    for variable in list(dict.fromkeys([outcome, *(exposures[:1])])):
        series = numeric.get(variable)
        histogram = _histogram_payload(variable, series)
        if histogram is not None:
            inputs.append(
                DerivedFigureInput("distribution_histogram", histogram)
            )

    trend = _grouped_time_series_payload(
        frame,
        numeric.get(outcome),
        outcome,
        time_field,
        group_field,
        intervention_period,
    )
    if trend is not None:
        inputs.append(DerivedFigureInput("grouped_time_series", trend))

    resolved_group = group_field or _small_group_field(frame, exposures)
    box = _box_plot_payload(
        frame,
        numeric.get(outcome),
        outcome,
        resolved_group,
    )
    if box is not None:
        inputs.append(DerivedFigureInput("box_plot", box))

    if outcome and exposures:
        scatter = _scatter_payload(
            exposures[0],
            numeric.get(exposures[0]),
            outcome,
            numeric.get(outcome),
        )
        if scatter is not None:
            inputs.append(DerivedFigureInput("scatter_plot", scatter))

    generated = {item.recipe_id for item in inputs}
    for recipe_id, reason in (
        ("grouped_time_series", "时间点不足或缺少冻结时间字段"),
        ("correlation_heatmap", "可比数值字段不足"),
        ("box_plot", "缺少小规模冻结分组字段"),
        ("scatter_plot", "成对有效观测或横轴变化不足"),
    ):
        if recipe_id not in generated:
            warnings.append(f"{recipe_id} 未生成：{reason}。")
    return inputs, warnings


def _descriptive_records(
    numeric: dict[str, pd.Series],
    row_count: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for variable, source in numeric.items():
        values = source.dropna()
        if len(values) < 2:
            continue
        records.append(
            {
                "variable": variable,
                "sample_scope": "frozen_source_rows",
                "n": int(len(values)),
                "missing": int(row_count - len(values)),
                "mean": float(values.mean()),
                "std": float(values.std(ddof=1)),
                "min": float(values.min()),
                "q1": float(values.quantile(0.25)),
                "median": float(values.median()),
                "q3": float(values.quantile(0.75)),
                "max": float(values.max()),
            }
        )
    return records


def _correlation_payload(
    numeric: dict[str, pd.Series],
) -> dict[str, Any] | None:
    eligible = {
        name: values
        for name, values in numeric.items()
        if values.notna().sum() >= 3 and values.nunique(dropna=True) > 1
    }
    if len(eligible) < 2:
        return None
    frame = pd.DataFrame(eligible).dropna()
    if len(frame) < 3:
        return None
    matrix = frame.corr(method="pearson")
    if matrix.isna().to_numpy().any():
        return None
    variables = list(matrix.columns)
    return {
        "variables": variables,
        "matrix": [
            [float(matrix.loc[row, column]) for column in variables]
            for row in variables
        ],
        "method": "pearson",
        "sample_policy": "listwise_complete",
        "sample_scope": "frozen_source_rows",
        "n": int(len(frame)),
    }


def _histogram_payload(
    variable: str,
    source: pd.Series | None,
) -> dict[str, Any] | None:
    if not variable or source is None:
        return None
    values = source.dropna().to_numpy(dtype=float)
    if len(values) < 8 or float(np.min(values)) == float(np.max(values)):
        return None
    bin_count = min(20, max(5, int(math.ceil(math.sqrt(len(values))))))
    counts, edges = np.histogram(values, bins=bin_count)
    return {
        "variable": variable,
        "sample_scope": "frozen_source_rows",
        "bins": [
            {
                "lower": float(edges[index]),
                "upper": float(edges[index + 1]),
                "count": int(counts[index]),
            }
            for index in range(len(counts))
        ],
        "binning_rule": f"equal_width_{bin_count}",
        "n": int(len(values)),
    }


def _grouped_time_series_payload(
    frame: pd.DataFrame,
    outcome: pd.Series | None,
    outcome_name: str,
    time_field: str,
    group_field: str,
    intervention_period: float | None,
) -> dict[str, Any] | None:
    if outcome is None or not outcome_name or not time_field:
        return None
    time = pd.to_numeric(frame[time_field], errors="coerce")
    working = pd.DataFrame({"period": time, "value": outcome})
    if group_field:
        working["series"] = frame[group_field].astype("string")
    else:
        working["series"] = "overall"
    working = working.dropna(subset=["period", "value", "series"])
    if working["period"].nunique() < 8:
        return None
    if working["series"].nunique() > 5:
        return None
    if (
        intervention_period is not None
        and intervention_period not in set(working["period"])
    ):
        return None
    grouped = (
        working.groupby(["period", "series"], sort=True, observed=True)["value"]
        .agg(["mean", "count"])
        .reset_index()
    )
    series_values = sorted(str(value) for value in working["series"].unique())
    series_labels = (
        {"0": "Control (0)", "1": "Treated (1)"}
        if set(series_values) == {"0", "1"}
        else {value: value for value in series_values}
    )
    return {
        "value_name": outcome_name,
        "time_variable": time_field,
        "series_variable": group_field or "series",
        "series_labels": series_labels,
        "sample_scope": "frozen_source_rows",
        "intervention_period": intervention_period,
        "records": [
            {
                "period": float(row["period"]),
                "period_label": f"{row['period']:g}",
                "series": str(row["series"]),
                "value": float(row["mean"]),
                "n": int(row["count"]),
            }
            for row in grouped.to_dict(orient="records")
        ],
    }


def _small_group_field(frame: pd.DataFrame, exposures: list[str]) -> str:
    for field in exposures:
        if field not in frame.columns:
            continue
        count = frame[field].nunique(dropna=True)
        if 2 <= count <= 8:
            return field
    return ""


def _box_plot_payload(
    frame: pd.DataFrame,
    outcome: pd.Series | None,
    outcome_name: str,
    group_field: str,
) -> dict[str, Any] | None:
    if outcome is None or not outcome_name or not group_field:
        return None
    working = pd.DataFrame(
        {
            "group": frame[group_field].astype("string"),
            "value": outcome,
        }
    ).dropna()
    if not 2 <= working["group"].nunique() <= 8:
        return None
    group_values = {str(value) for value in working["group"].unique()}
    if group_values == {"0", "1"}:
        working["group"] = working["group"].map(
            {"0": "Control (0)", "1": "Treated (1)"}
        )
    groups: list[dict[str, Any]] = []
    for label, values in working.groupby("group", sort=True, observed=True)["value"]:
        if len(values) < 2:
            continue
        q1 = float(values.quantile(0.25))
        median = float(values.median())
        q3 = float(values.quantile(0.75))
        iqr = q3 - q1
        within = values[(values >= q1 - 1.5 * iqr) & (values <= q3 + 1.5 * iqr)]
        groups.append(
            {
                "group": str(label),
                "whisker_low": float(within.min()),
                "q1": q1,
                "median": median,
                "q3": q3,
                "whisker_high": float(within.max()),
                "n": int(len(values)),
            }
        )
    if len(groups) < 2:
        return None
    return {
        "variable": outcome_name,
        "group_variable": group_field,
        "whisker_rule": "tukey_1_5_iqr",
        "sample_scope": "frozen_source_rows",
        "groups": groups,
    }


def _scatter_payload(
    x_name: str,
    x: pd.Series | None,
    y_name: str,
    y: pd.Series | None,
) -> dict[str, Any] | None:
    if x is None or y is None or not x_name or not y_name:
        return None
    working = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(working) < 20 or working["x"].nunique() < 8:
        return None
    bins = min(20, int(working["x"].nunique()))
    try:
        working["bin"] = pd.qcut(working["x"], bins, duplicates="drop")
    except ValueError:
        return None
    grouped = working.groupby("bin", observed=True, sort=True).agg(
        x=("x", "mean"),
        y=("y", "mean"),
        n=("y", "size"),
    )
    if len(grouped) < 8:
        return None
    return {
        "x_variable": x_name,
        "y_variable": y_name,
        "sample_scope": "frozen_source_rows",
        "grain": "bin",
        "points": [
            {
                "x": float(row.x),
                "y": float(row.y),
                "n": int(row.n),
                "label": f"bin-{index + 1}",
            }
            for index, row in enumerate(grouped.itertuples())
        ],
    }


def _declared_time_field(fixed_effects: list[str]) -> str:
    markers = {"year", "time", "年份", "年度"}
    return next(
        (
            name
            for name in fixed_effects
            if name.replace("_", "").casefold() in markers
        ),
        "",
    )


def _policy_intervention_period(policy_design: dict[str, Any]) -> float | None:
    value = policy_design.get("policy_start_year")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    policy_date = policy_design.get("policy_date")
    if isinstance(policy_date, str) and len(policy_date) >= 4:
        try:
            return float(int(policy_date[:4]))
        except ValueError:
            return None
    return None


def _read_csv(path: Path, required: list[str]) -> pd.DataFrame:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(
                path,
                encoding=encoding,
                usecols=lambda name: name in required,
            )
        except UnicodeDecodeError as error:
            last_error = error
    raise ValueError("CSV 编码必须是 UTF-8 或 GB18030。") from last_error


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("绘图数据资产 SHA256 与冻结合同不一致。")
