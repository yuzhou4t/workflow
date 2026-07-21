from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..seal import canonical_sha256
from .recipe_contracts import recipe_data_snapshot, validate_recipe_data


# Adapted from carolzhu-jr/GreenFinance_Plot_Agent at commit
# 07820bd3aef18e84b8a4e2290e03d1b7ef666ade. The workflow-facing contract,
# deterministic IDs, neutral text, safe paths, and artifact URIs are local changes.
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FIGURE_ROOT = PROJECT_ROOT / "backend" / "var" / "figures"
SOURCE_COMMIT = "07820bd3aef18e84b8a4e2290e03d1b7ef666ade"
RENDERER_VERSION = "in-process-1.2"
MIME_TYPES = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "csv": "text/csv",
}
_RENDER_LOCK = threading.Lock()


def render_request(
    request: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    recipe_id = str(request["recipe_id"])
    data = validate_recipe_data(recipe_id, request["bindings"]["data"])
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    data_snapshot = recipe_data_snapshot(recipe_id, data)
    request_id = str(request["request_id"])
    stage = str(request["stage"])
    workflow_run_id = str(request["source"]["artifact_id"]).removesuffix(
        ":research_run"
    )
    output_root = (artifact_root or DEFAULT_FIGURE_ROOT).resolve()

    renderer = _RECIPE_RENDERERS.get(recipe_id)
    if renderer is None:
        raise ValueError(f"unknown recipe_id: {recipe_id}")
    title, caption, alt_text = _neutral_metadata(recipe_id, stage, data)
    with _RENDER_LOCK:
        _configure_reproducible_process()
        rendering_fingerprint = _rendering_fingerprint()
        figure_id = "figure-" + canonical_sha256(
            {
                "request": request,
                "rendering_fingerprint": rendering_fingerprint,
            }
        )[:24]
        output_dir = (
            output_root
            / _safe_component(workflow_run_id)
            / _safe_component(stage)
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        result = renderer(
            data,
            output_dir,
            figure_id,
            title,
            caption,
        )

    files = [
        {
            "format": file_format,
            "mime_type": MIME_TYPES[file_format],
            "artifact_uri": (
                "artifact://figures/"
                f"{_safe_component(workflow_run_id)}/{_safe_component(stage)}/"
                f"{info['path'].name}"
            ),
            "sha256": info["sha256"],
        }
        for file_format, info in result.items()
        if file_format in request["formats"]
    ]
    identity = {
        "request_id": request_id,
        "figure_id": figure_id,
        "files": [file["sha256"] for file in files],
    }
    return {
        "schema_version": request["schema_version"],
        "bundle_id": f"figure-bundle-{canonical_sha256(identity)[:24]}",
        "stage": stage,
        "status": "succeeded",
        "figures": [
            {
                "figure_id": figure_id,
                "recipe_id": recipe_id,
                "recipe_version": request["recipe_version"],
                "title": title,
                "caption": caption,
                "alt_text": alt_text,
                "execution_ids": request["execution_ids"],
                "claim_ids": request["claim_ids"],
                "sources": [
                    request["source"],
                    *request.get("data_sources", []),
                ],
                "files": files,
                "data_snapshot": data_snapshot,
                "warnings": [],
            }
        ],
        "renderer": {
            "name": "HypoWeaver_Plot_Engine",
            "version": RENDERER_VERSION,
            "upstream_project": "carolzhu-jr/GreenFinance_Plot_Agent",
            "upstream_commit": SOURCE_COMMIT,
            "backend": "matplotlib",
            "dependencies": {
                "matplotlib": rendering_fingerprint["matplotlib"],
                "pandas": rendering_fingerprint["pandas"],
            },
            "font_family": rendering_fingerprint["font_family"],
            "rendering_fingerprint": rendering_fingerprint,
        },
        "warnings": [],
    }


def resolve_artifact_uri(
    artifact_uri: str,
    *,
    artifact_root: Path | None = None,
    expected_sha256: str | None = None,
) -> Path:
    prefix = "artifact://figures/"
    if not artifact_uri.startswith(prefix):
        raise ValueError("unsupported figure artifact URI")
    root = (artifact_root or DEFAULT_FIGURE_ROOT).resolve()
    candidate = (root / artifact_uri.removeprefix(prefix)).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("figure artifact does not exist inside the figure store")
    if expected_sha256 is not None and _file_sha256(candidate) != expected_sha256:
        raise ValueError("figure artifact sha256 does not match FigureBundle")
    return candidate


def _render_coefficient_forest(
    data_records: list[dict[str, Any]],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    frame = pd.DataFrame(data_records).sort_values(
        ["coefficient", "execution_id", "term"],
        ascending=[True, True, True],
        kind="mergesort",
    )
    source_frame = frame.copy()
    duplicate_terms = frame["term"].duplicated(keep=False)
    frame["display_label"] = frame["term"]
    frame.loc[duplicate_terms, "display_label"] = (
        frame.loc[duplicate_terms, "term"]
        + " · "
        + frame.loc[duplicate_terms, "execution_id"]
    )
    height = max(3.6, min(8.5, 2.4 + 0.45 * len(frame)))
    figure, axis = plt.subplots(figsize=(8, height))
    try:
        y_positions = range(len(frame))
        axis.errorbar(
            frame["coefficient"].tolist(),
            y_positions,
            xerr=[
                (frame["coefficient"] - frame["ci_lower"]).tolist(),
                (frame["ci_upper"] - frame["coefficient"]).tolist(),
            ],
            fmt="o",
            color="#333333",
            ecolor="#666666",
            elinewidth=1.5,
            capsize=4,
            capthick=1.5,
        )
        axis.axvline(x=0, color="#999999", linestyle="--", linewidth=1)
        axis.set_yticks(list(y_positions))
        axis.set_yticklabels(frame["display_label"], fontproperties=font("label"))
        axis.set_xlabel("Coefficient", fontproperties=font("axis"))
        axis.set_ylabel("Term", fontproperties=font("axis"))
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="x", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            source_frame,
            output_dir,
            f"{prefix}_coefficient_forest",
        )
    finally:
        plt.close(figure)


def _render_sample_flow(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    rows_input = int(data_record["rows_input"])
    rows_dropped = int(data_record["rows_dropped"])
    rows_used = int(data_record["rows_used"])
    if rows_input != rows_dropped + rows_used:
        raise ValueError(
            f"sample flow not closed: {rows_input} != {rows_dropped} + {rows_used}"
        )
    figure, axis = plt.subplots(figsize=(8, 4))
    try:
        categories = ["Initial Sample", "Dropped", "Final Sample"]
        values = [rows_input, rows_dropped, rows_used]
        bars = axis.barh(categories, values, color=["#333333", "#999999", "#666666"])
        offset = max(values) * 0.01 if max(values) else 0.1
        for bar, value in zip(bars, values, strict=True):
            axis.text(
                bar.get_width() + offset,
                bar.get_y() + bar.get_height() / 2,
                f"{value:,}",
                va="center",
                fontproperties=font("label"),
            )
        axis.set_xlabel("Number of Records", fontproperties=font("axis"))
        axis.set_yticks(range(len(categories)))
        axis.set_yticklabels(categories, fontproperties=font("label"))
        axis.invert_yaxis()
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="x", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        frame = pd.DataFrame([data_record])
        return _save_all(figure, frame, output_dir, f"{prefix}_sample_flow")
    finally:
        plt.close(figure)


def _render_event_study(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    points = sorted(
        data_record["points"],
        key=lambda item: (item["relative_time"], item["execution_id"]),
    )
    frame = pd.DataFrame(points)
    frame["reference_period"] = data_record.get("reference_period")
    frame["joint_pretrend_p_value"] = data_record.get(
        "joint_pretrend_p_value"
    )
    figure, axis = plt.subplots(figsize=(8, 4.8))
    try:
        x_values = [item["relative_time"] for item in points]
        coefficients = [item["coefficient"] for item in points]
        axis.errorbar(
            x_values,
            coefficients,
            yerr=[
                [
                    item["coefficient"] - item["ci_lower"]
                    for item in points
                ],
                [
                    item["ci_upper"] - item["coefficient"]
                    for item in points
                ],
            ],
            fmt="o-",
            color="#333333",
            ecolor="#777777",
            linewidth=1.2,
            elinewidth=1.2,
            capsize=3,
        )
        axis.axhline(0, color="#999999", linestyle="--", linewidth=1)
        axis.axvline(0, color="#555555", linestyle=":", linewidth=1)
        reference_period = data_record.get("reference_period")
        if reference_period is not None:
            axis.scatter(
                [reference_period],
                [0],
                marker="D",
                facecolors="white",
                edgecolors="#333333",
                zorder=4,
                label="Reference period",
            )
        axis.set_xlabel("Relative Time", fontproperties=font("axis"))
        axis.set_ylabel("Coefficient", fontproperties=font("axis"))
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="both", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        if reference_period is not None:
            axis.legend(frameon=False, prop=font("label"), loc="best")
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_event_study",
        )
    finally:
        plt.close(figure)


def _render_grouped_time_series(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    records = sorted(
        data_record["records"],
        key=lambda item: (
            item["series"],
            _period_sort_key(item["period"]),
            item.get("period_label") or str(item["period"]),
        ),
    )
    frame = pd.DataFrame(records)
    frame["value_name"] = data_record["value_name"]
    frame["time_variable"] = data_record["time_variable"]
    frame["series_variable"] = data_record["series_variable"]
    frame["series_label"] = frame["series"].map(
        data_record["series_labels"]
    )
    frame["sample_scope"] = data_record["sample_scope"]
    frame["intervention_period"] = data_record.get("intervention_period")
    periods = sorted(
        {item["period"] for item in records},
        key=_period_sort_key,
    )
    positions = {period: index for index, period in enumerate(periods)}
    period_labels = {
        item["period"]: item.get("period_label") or str(item["period"])
        for item in records
    }
    figure, axis = plt.subplots(figsize=(8, 4.8))
    try:
        series_names = sorted({item["series"] for item in records})
        markers = ["o", "s", "^", "D", "v", "P", "X"]
        linestyles = ["-", "--", "-.", ":"]
        for index, series_name in enumerate(series_names):
            series = [
                item for item in records if item["series"] == series_name
            ]
            axis.plot(
                [positions[item["period"]] for item in series],
                [item["value"] for item in series],
                marker=markers[index % len(markers)],
                linestyle=linestyles[index % len(linestyles)],
                color=str(0.18 + 0.62 * index / max(1, len(series_names) - 1)),
                linewidth=1.4,
                markersize=4,
                label=data_record["series_labels"][series_name],
            )
        intervention_period = data_record.get("intervention_period")
        if intervention_period is not None:
            axis.axvline(
                positions[intervention_period],
                color="#555555",
                linestyle=":",
                linewidth=1,
                label="Intervention",
            )
        axis.set_xticks(range(len(periods)))
        axis.set_xticklabels(
            [period_labels[period] for period in periods],
            rotation=45 if len(periods) > 8 else 0,
            ha="right" if len(periods) > 8 else "center",
            fontproperties=font("label"),
        )
        axis.set_xlabel(data_record["time_variable"], fontproperties=font("axis"))
        axis.set_ylabel(data_record["value_name"], fontproperties=font("axis"))
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="y", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        if len(series_names) > 1 or intervention_period is not None:
            axis.legend(
                frameon=False,
                prop=font("label"),
                title=data_record["series_variable"],
                loc="best",
            )
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_grouped_time_series",
        )
    finally:
        plt.close(figure)


def _render_heterogeneity_forest(
    data_records: list[dict[str, Any]],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    records = sorted(
        data_records,
        key=lambda item: (
            item["subgroup_variable"],
            item["subgroup"],
            item["execution_id"],
        ),
    )
    frame = pd.DataFrame(records)
    height = max(3.8, min(9.0, 2.5 + 0.48 * len(records)))
    figure, axis = plt.subplots(figsize=(8, height))
    try:
        y_positions = list(range(len(records)))
        axis.errorbar(
            [item["coefficient"] for item in records],
            y_positions,
            xerr=[
                [item["coefficient"] - item["ci_lower"] for item in records],
                [item["ci_upper"] - item["coefficient"] for item in records],
            ],
            fmt="o",
            color="#333333",
            ecolor="#666666",
            capsize=4,
            elinewidth=1.4,
        )
        axis.axvline(0, color="#999999", linestyle="--", linewidth=1)
        axis.set_yticks(y_positions)
        axis.set_yticklabels(
            [item["subgroup"] for item in records],
            fontproperties=font("label"),
        )
        axis.set_xlabel(
            f"Coefficient ({records[0]['term']})",
            fontproperties=font("axis"),
        )
        axis.set_ylabel("Subgroup", fontproperties=font("axis"))
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="x", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_heterogeneity_forest",
        )
    finally:
        plt.close(figure)


def _render_specification_curve(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    run_order = {"baseline": 0, "robustness": 1}
    points = sorted(
        data_record["points"],
        key=lambda item: (
            run_order.get(item["run_type"], 9),
            item["specification"],
            item["execution_id"],
        ),
    )
    frame = pd.DataFrame(points)
    frame.insert(0, "term", data_record["term"])
    figure, axis = plt.subplots(figsize=(max(8, 0.65 * len(points) + 3), 4.8))
    try:
        x_positions = list(range(len(points)))
        axis.errorbar(
            x_positions,
            [item["coefficient"] for item in points],
            yerr=[
                [item["coefficient"] - item["ci_lower"] for item in points],
                [item["ci_upper"] - item["coefficient"] for item in points],
            ],
            fmt="o-",
            color="#333333",
            ecolor="#777777",
            linewidth=1,
            capsize=3,
        )
        axis.axhline(0, color="#999999", linestyle="--", linewidth=1)
        axis.set_xticks(x_positions)
        axis.set_xticklabels(
            [item["specification"] for item in points],
            rotation=35,
            ha="right",
            fontproperties=font("label"),
        )
        axis.set_xlabel("Specification", fontproperties=font("axis"))
        axis.set_ylabel(
            f"Coefficient ({data_record['term']})",
            fontproperties=font("axis"),
        )
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="y", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_specification_curve",
        )
    finally:
        plt.close(figure)


def _render_descriptive_statistics(
    data_records: list[dict[str, Any]],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    frame = pd.DataFrame(data_records).sort_values("variable", kind="mergesort")
    columns = [
        "variable",
        "n",
        "missing",
        "mean",
        "std",
        "min",
        "q1",
        "median",
        "q3",
        "max",
    ]
    display = frame[columns].copy()
    for column in columns[3:]:
        display[column] = display[column].map(_format_number)
    height = max(3.0, min(10.0, 1.8 + 0.42 * len(display)))
    figure, axis = plt.subplots(figsize=(11, height))
    try:
        axis.axis("off")
        table = axis.table(
            cellText=display.values.tolist(),
            colLabels=[
                "Variable",
                "N",
                "Missing",
                "Mean",
                "SD",
                "Min",
                "Q1",
                "Median",
                "Q3",
                "Max",
            ],
            cellLoc="center",
            colLoc="center",
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 1.3)
        for (row, _), cell in table.get_celld().items():
            cell.set_edgecolor("#bdbdbd")
            cell.set_linewidth(0.5)
            cell.set_facecolor("#eeeeee" if row == 0 else "white")
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_descriptive_statistics",
        )
    finally:
        plt.close(figure)


def _render_correlation_heatmap(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    variables = data_record["variables"]
    matrix = data_record["matrix"]
    rows = [
        {
            "row_variable": row_name,
            "column_variable": column_name,
            "correlation": matrix[row_index][column_index],
            "method": data_record["method"],
            "sample_policy": data_record["sample_policy"],
            "sample_scope": data_record["sample_scope"],
            "n": data_record.get("n"),
        }
        for row_index, row_name in enumerate(variables)
        for column_index, column_name in enumerate(variables)
    ]
    frame = pd.DataFrame(rows)
    size = max(5.2, min(11.0, 2.2 + 0.65 * len(variables)))
    figure, axis = plt.subplots(figsize=(size, size))
    try:
        image = axis.imshow(matrix, cmap="RdBu_r", vmin=-1, vmax=1)
        axis.set_xticks(range(len(variables)))
        axis.set_yticks(range(len(variables)))
        axis.set_xticklabels(
            variables,
            rotation=45,
            ha="right",
            fontproperties=font("label"),
        )
        axis.set_yticklabels(variables, fontproperties=font("label"))
        if len(variables) <= 15:
            for row_index, row in enumerate(matrix):
                for column_index, value in enumerate(row):
                    axis.text(
                        column_index,
                        row_index,
                        f"{value:.2f}",
                        ha="center",
                        va="center",
                        color="white" if abs(value) >= 0.55 else "#222222",
                        fontproperties=font("label"),
                    )
        colorbar = figure.colorbar(image, ax=axis, fraction=0.046, pad=0.04)
        colorbar.set_label("Correlation", fontproperties=font("axis"))
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_correlation_heatmap",
        )
    finally:
        plt.close(figure)


def _render_distribution_histogram(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    bins = sorted(data_record["bins"], key=lambda item: item["lower"])
    frame = pd.DataFrame(bins)
    frame.insert(0, "variable", data_record["variable"])
    frame["binning_rule"] = data_record["binning_rule"]
    frame["sample_scope"] = data_record["sample_scope"]
    frame["n"] = data_record["n"]
    figure, axis = plt.subplots(figsize=(8, 4.8))
    try:
        axis.bar(
            [item["lower"] for item in bins],
            [item["count"] for item in bins],
            width=[item["upper"] - item["lower"] for item in bins],
            align="edge",
            color="#666666",
            edgecolor="white",
            linewidth=0.6,
        )
        axis.set_xlabel(data_record["variable"], fontproperties=font("axis"))
        axis.set_ylabel("Count", fontproperties=font("axis"))
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="y", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_distribution_histogram",
        )
    finally:
        plt.close(figure)


def _render_box_plot(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    groups = sorted(data_record["groups"], key=lambda item: item["group"])
    frame = pd.DataFrame(groups)
    frame.insert(0, "variable", data_record["variable"])
    frame["group_variable"] = data_record["group_variable"]
    frame["whisker_rule"] = data_record["whisker_rule"]
    frame["sample_scope"] = data_record["sample_scope"]
    statistics = [
        {
            "label": item["group"],
            "whislo": item["whisker_low"],
            "q1": item["q1"],
            "med": item["median"],
            "q3": item["q3"],
            "whishi": item["whisker_high"],
            "fliers": [],
        }
        for item in groups
    ]
    figure, axis = plt.subplots(figsize=(max(7, 1.1 * len(groups) + 3), 4.8))
    try:
        artists = axis.bxp(
            statistics,
            showfliers=False,
            patch_artist=True,
        )
        for box in artists["boxes"]:
            box.set_facecolor("#dddddd")
            box.set_edgecolor("#444444")
        for key in ("whiskers", "caps", "medians"):
            for artist in artists[key]:
                artist.set_color("#444444")
        axis.set_xlabel(data_record["group_variable"], fontproperties=font("axis"))
        axis.set_ylabel(data_record["variable"], fontproperties=font("axis"))
        axis.tick_params(axis="x", rotation=30 if len(groups) > 5 else 0)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(axis="y", color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_box_plot",
        )
    finally:
        plt.close(figure)


def _render_scatter_plot(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    points = sorted(
        data_record["points"],
        key=lambda item: (item["x"], item["y"], item.get("label") or ""),
    )
    frame = pd.DataFrame(points)
    frame["x_variable"] = data_record["x_variable"]
    frame["y_variable"] = data_record["y_variable"]
    frame["sample_scope"] = data_record["sample_scope"]
    frame["grain"] = data_record["grain"]
    figure, axis = plt.subplots(figsize=(7.5, 5.2))
    try:
        sizes = [
            max(14.0, min(90.0, 8.0 + 4.0 * (item.get("n") or 1) ** 0.5))
            for item in points
        ]
        axis.scatter(
            [item["x"] for item in points],
            [item["y"] for item in points],
            s=sizes,
            facecolors="#777777",
            edgecolors="#333333",
            linewidths=0.4,
            alpha=0.75,
        )
        if len(points) <= 30:
            for item in points:
                label = item.get("label")
                if label:
                    axis.annotate(
                        label,
                        (item["x"], item["y"]),
                        xytext=(4, 4),
                        textcoords="offset points",
                        fontproperties=font("label"),
                    )
        axis.set_xlabel(data_record["x_variable"], fontproperties=font("axis"))
        axis.set_ylabel(data_record["y_variable"], fontproperties=font("axis"))
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.grid(color="#e5e5e5", linewidth=0.6)
        axis.set_axisbelow(True)
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_scatter_plot",
        )
    finally:
        plt.close(figure)


def _render_spatial_choropleth(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    matplotlib, _, pd, plt, font = _plot_dependencies()
    from matplotlib.patches import Polygon

    regions = sorted(
        data_record["regions"], key=lambda item: item["region_id"]
    )
    positions = [
        position
        for item in regions
        for position in item["polygon"][:-1]
    ]
    reference_latitude = sum(position[1] for position in positions) / len(
        positions
    )
    longitude_scale = math.cos(math.radians(reference_latitude))

    def project(position: list[float]) -> tuple[float, float]:
        return position[0] * longitude_scale, position[1]

    frame = pd.DataFrame(
        [
            {
                **{key: value for key, value in item.items() if key != "polygon"},
                "polygon": _canonical_json(item["polygon"]),
                "crs": data_record["crs"],
                "value_name": data_record["value_name"],
                "display_projection": "local_equirectangular",
                "reference_latitude": reference_latitude,
                "geometry_source_sha256": data_record[
                    "geometry_source_sha256"
                ],
                "value_source_sha256": data_record["value_source_sha256"],
            }
            for item in regions
        ]
    )
    values = [item["value"] for item in regions]
    lower, upper = min(values), max(values)
    if lower == upper:
        lower -= 0.5
        upper += 0.5
    normalization = matplotlib.colors.Normalize(vmin=lower, vmax=upper)
    color_map = matplotlib.colormaps["Greys"]
    figure, axis = plt.subplots(figsize=(8, 6))
    try:
        for item in regions:
            projected_polygon = [project(position) for position in item["polygon"]]
            polygon = Polygon(
                projected_polygon,
                closed=True,
                facecolor=color_map(normalization(item["value"])),
                edgecolor="#333333",
                linewidth=0.6,
            )
            axis.add_patch(polygon)
            if len(regions) <= 40:
                vertices = projected_polygon[:-1]
                longitude = sum(point[0] for point in vertices) / len(vertices)
                latitude = sum(point[1] for point in vertices) / len(vertices)
                axis.text(
                    longitude,
                    latitude,
                    item["label"],
                    ha="center",
                    va="center",
                    color=(
                        "white"
                        if normalization(item["value"]) >= 0.62
                        else "#222222"
                    ),
                    fontproperties=font("label"),
                )
        axis.autoscale_view()
        axis.set_aspect("equal", adjustable="datalim")
        axis.axis("off")
        scalar_map = matplotlib.cm.ScalarMappable(
            norm=normalization,
            cmap=color_map,
        )
        scalar_map.set_array([])
        colorbar = figure.colorbar(
            scalar_map,
            ax=axis,
            fraction=0.035,
            pad=0.02,
        )
        colorbar.set_label(data_record["value_name"], fontproperties=font("axis"))
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_spatial_choropleth",
        )
    finally:
        plt.close(figure)


def _render_mechanism_evidence_graph(
    data_record: dict[str, Any],
    output_dir: Path,
    prefix: str,
    title: str,
    caption: str,
) -> dict[str, dict[str, Any]]:
    _, _, pd, plt, font = _plot_dependencies()
    nodes = sorted(data_record["nodes"], key=lambda item: item["node_id"])
    edges = sorted(data_record["edges"], key=lambda item: item["edge_id"])
    source_rows = [
        {
            "record_type": "node",
            "id": item["node_id"],
            "source": None,
            "target": None,
            "kind": None,
            "label": item["label"],
        }
        for item in nodes
    ] + [
        {
            "record_type": "edge",
            "id": item["edge_id"],
            "source": item["source"],
            "target": item["target"],
            "kind": item["edge_kind"],
            "label": item["label"],
        }
        for item in edges
    ]
    frame = pd.DataFrame(source_rows)
    positions = _mechanism_positions(nodes, edges)
    figure, axis = plt.subplots(figsize=(9, 5.5))
    try:
        axis.set_xlim(-0.1, 1.1)
        axis.set_ylim(-0.15, 1.15)
        axis.axis("off")
        node_labels = {item["node_id"]: item["label"] for item in nodes}
        for edge in edges:
            source = positions[edge["source"]]
            target = positions[edge["target"]]
            axis.annotate(
                "",
                xy=target,
                xytext=source,
                arrowprops={
                    "arrowstyle": "->",
                    "color": "#555555",
                    "linewidth": 1.2,
                    "linestyle": "--",
                    "shrinkA": 25,
                    "shrinkB": 25,
                },
            )
            label = edge["label"]
            midpoint = (
                (source[0] + target[0]) / 2,
                (source[1] + target[1]) / 2 + 0.035,
            )
            axis.text(
                *midpoint,
                label,
                ha="center",
                va="center",
                fontproperties=font("label"),
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5},
            )
        for node_id, position in positions.items():
            axis.text(
                *position,
                node_labels[node_id],
                ha="center",
                va="center",
                fontproperties=font("axis"),
                bbox={
                    "boxstyle": "round,pad=0.45",
                    "facecolor": "#eeeeee",
                    "edgecolor": "#444444",
                    "linewidth": 0.9,
                },
                zorder=3,
            )
        _add_figure_text(figure, axis, title, caption, font)
        return _save_all(
            figure,
            frame,
            output_dir,
            f"{prefix}_mechanism_evidence_graph",
        )
    finally:
        plt.close(figure)


def _period_sort_key(value: Any) -> tuple[int, float | str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, float(value))
    return (1, str(value))


def _format_number(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value):.4g}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _mechanism_positions(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, tuple[float, float]]:
    node_ids = [item["node_id"] for item in nodes]
    incoming = {node_id: 0 for node_id in node_ids}
    outgoing = {node_id: [] for node_id in node_ids}
    for edge in edges:
        incoming[edge["target"]] += 1
        outgoing[edge["source"]].append(edge["target"])

    levels: dict[str, int] = {node_id: 0 for node_id in node_ids}
    ready = sorted(node_id for node_id, count in incoming.items() if count == 0)
    visited: list[str] = []
    while ready:
        node_id = ready.pop(0)
        visited.append(node_id)
        for target in sorted(outgoing[node_id]):
            levels[target] = max(levels[target], levels[node_id] + 1)
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if len(visited) != len(node_ids):
        ordered = sorted(node_ids)
        return {
            node_id: (
                0.5 + 0.4 * math.cos(2 * math.pi * index / len(ordered)),
                0.5 + 0.4 * math.sin(2 * math.pi * index / len(ordered)),
            )
            for index, node_id in enumerate(ordered)
        }

    max_level = max(levels.values(), default=0)
    grouped: dict[int, list[str]] = {}
    for node_id in node_ids:
        grouped.setdefault(levels[node_id], []).append(node_id)
    positions: dict[str, tuple[float, float]] = {}
    for level in sorted(grouped):
        members = sorted(grouped[level])
        x_position = 0.5 if max_level == 0 else level / max_level
        for index, node_id in enumerate(members):
            y_position = (index + 1) / (len(members) + 1)
            positions[node_id] = (x_position, y_position)
    return positions


_RECIPE_RENDERERS = {
    "coefficient_forest": _render_coefficient_forest,
    "sample_flow": _render_sample_flow,
    "event_study": _render_event_study,
    "grouped_time_series": _render_grouped_time_series,
    "heterogeneity_forest": _render_heterogeneity_forest,
    "specification_curve": _render_specification_curve,
    "descriptive_statistics": _render_descriptive_statistics,
    "correlation_heatmap": _render_correlation_heatmap,
    "distribution_histogram": _render_distribution_histogram,
    "box_plot": _render_box_plot,
    "scatter_plot": _render_scatter_plot,
    "spatial_choropleth": _render_spatial_choropleth,
    "mechanism_evidence_graph": _render_mechanism_evidence_graph,
}


def _save_all(
    figure: Any,
    frame: Any,
    output_dir: Path,
    base_name: str,
) -> dict[str, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination_paths = {
        "svg": output_dir / f"{base_name}.svg",
        "png": output_dir / f"{base_name}.png",
        "pdf": output_dir / f"{base_name}.pdf",
        "csv": output_dir / f"{base_name}.csv",
    }
    with tempfile.TemporaryDirectory(
        dir=output_dir,
        prefix=".render-",
    ) as tempdir:
        temporary_root = Path(tempdir)
        temporary_paths = {
            file_format: temporary_root / path.name
            for file_format, path in destination_paths.items()
        }
        figure.savefig(
            temporary_paths["svg"],
            format="svg",
            dpi=300,
            bbox_inches="tight",
            metadata={"Date": None, "Creator": "HypoWeaver_Plot_Engine"},
        )
        figure.savefig(
            temporary_paths["png"],
            format="png",
            dpi=300,
            bbox_inches="tight",
            metadata={"Software": "HypoWeaver_Plot_Engine"},
        )
        figure.savefig(
            temporary_paths["pdf"],
            format="pdf",
            dpi=300,
            bbox_inches="tight",
            metadata={
                "CreationDate": None,
                "ModDate": None,
                "Creator": "HypoWeaver_Plot_Engine",
            },
        )
        frame.to_csv(
            temporary_paths["csv"],
            index=False,
            lineterminator="\n",
        )
        temporary_hashes = {
            file_format: _file_sha256(path)
            for file_format, path in temporary_paths.items()
        }
        for file_format, destination in destination_paths.items():
            if (
                destination.exists()
                and _file_sha256(destination) != temporary_hashes[file_format]
            ):
                raise RuntimeError(
                    "immutable figure artifact collision for "
                    f"{destination.name}"
                )
        for file_format, destination in destination_paths.items():
            if not destination.exists():
                os.replace(temporary_paths[file_format], destination)
    return {
        file_format: {
            "path": path,
            "sha256": _file_sha256(path),
        }
        for file_format, path in destination_paths.items()
    }


def _rendering_fingerprint() -> dict[str, str]:
    matplotlib, FontProperties, pd, _, _ = _plot_dependencies()
    from matplotlib import font_manager

    font_family = str(matplotlib.rcParams["font.sans-serif"][0])
    font_path = Path(
        font_manager.findfont(FontProperties(family=[font_family]))
    ).resolve()
    return {
        "renderer_version": RENDERER_VERSION,
        "renderer_code_sha256": _file_sha256(Path(__file__).resolve()),
        "recipe_contracts_sha256": _file_sha256(
            Path(__file__).with_name("recipe_contracts.py").resolve()
        ),
        "matplotlib": str(matplotlib.__version__),
        "pandas": str(pd.__version__),
        "font_family": font_family,
        "font_sha256": _file_sha256(font_path),
    }


def _plot_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import pandas as pd
        from matplotlib.font_manager import FontProperties, fontManager
    except ImportError as error:
        raise RuntimeError(
            "绘图模块缺少 matplotlib；请重新安装 backend/requirements.txt"
        ) from error

    chinese_families = [
        "Noto Sans CJK SC",
        "PingFang SC",
        "Microsoft YaHei",
        "SimHei",
        "DejaVu Sans",
    ]
    available = {item.name for item in fontManager.ttflist}
    selected = next(
        (family for family in chinese_families if family in available),
        "DejaVu Sans",
    )
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["svg.hashsalt"] = "hypoweaver-fixed-salt"
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = [selected, *chinese_families]

    def font(kind: str) -> Any:
        if kind == "title":
            return FontProperties(family=[selected], size=11, weight=600)
        return FontProperties(
            family=[selected],
            size=9 if kind in {"caption", "label"} else 10,
        )

    return matplotlib, FontProperties, pd, plt, font


def _add_figure_text(
    figure: Any,
    axis: Any,
    title: str,
    caption: str,
    font: Any,
) -> None:
    axis.set_title(title, fontproperties=font("title"), pad=14)
    figure.subplots_adjust(bottom=0.18)
    figure.text(
        0.5,
        0.025,
        caption,
        fontproperties=font("caption"),
        ha="center",
        va="center",
    )


def _neutral_metadata(
    recipe_id: str,
    stage: str,
    data: list[dict[str, Any]] | dict[str, Any],
) -> tuple[str, str, str]:
    stage_label = "论文" if stage == "publication" else "证据审计"
    metadata = {
        "coefficient_forest": (
            "系数与置信区间",
            "点表示系数估计，线段表示 95% 置信区间；图形不代替 Claim 授权。",
            "各执行记录的系数点估计、95% 置信区间和零参考线。",
        ),
        "sample_flow": (
            "样本筛选流程",
            "依次展示输入记录、剔除记录与最终估计样本；三项数量闭合。",
            "输入记录、剔除记录和最终估计样本的横向条形图。",
        ),
        "event_study": (
            "事件研究估计",
            "点线展示相对时期估计及 95% 置信区间；零线、政策时点与参考期仅作定位。",
            "相对时期估计、95% 置信区间、零参考线、政策时点和参考期。",
        ),
        "grouped_time_series": (
            "分组时间序列",
            "折线直接展示上游已聚合的分组时期数值，不在绘图阶段重新聚合。",
            "一个或多个分组随时期变化的折线图。",
        ),
        "heterogeneity_forest": (
            "异质性估计",
            "点表示各预先定义子组的估计，线段表示 95% 置信区间。",
            "各预定义子组的系数点估计、95% 置信区间和零参考线。",
        ),
        "specification_curve": (
            "规格估计序列",
            "按稳定的规格名称顺序展示基准与稳健性估计和 95% 置信区间。",
            "不同模型规格的系数点估计、95% 置信区间和零参考线。",
        ),
        "descriptive_statistics": (
            "描述性统计",
            "表中数值来自上游已计算的统计摘要，绘图阶段不重新计算。",
            "变量样本量、缺失数、均值、标准差和分位数的统计表。",
        ),
        "correlation_heatmap": (
            "相关矩阵",
            "颜色与数字展示上游已计算的相关系数；相关关系不表示方向性解释。",
            "变量两两相关系数的对称热力矩阵。",
        ),
        "distribution_histogram": (
            "分布直方图",
            "柱形展示上游预先确定分箱的频数，绘图阶段不改变分箱规则。",
            "预计算分箱边界及其频数的直方图。",
        ),
        "box_plot": (
            "分组箱线图",
            "箱体与须展示上游预计算的分位数和范围，不在绘图阶段重新估计。",
            "各组的下须、第一四分位数、中位数、第三四分位数和上须。",
        ),
        "scatter_plot": (
            "散点关系",
            "点展示上游授权的数值对；图中未在绘图阶段拟合趋势线。",
            "两个变量的授权数值对散点图。",
        ),
        "spatial_choropleth": (
            "空间分布（局部等距圆柱示意）",
            "颜色展示与 H2 冻结空间输入完全一致的区域数值；边界使用局部等距圆柱投影显示，不用于面积或距离比较。",
            "按冻结输入多边形区域着色的局部等距圆柱投影空间数值示意图。",
        ),
        "mechanism_evidence_graph": (
            "机制假设关系图",
            "虚线箭头仅表示冻结机制步骤声明的假设关系；不承载估计值，不能据此作因果解释。",
            "冻结机制步骤声明的假设节点与有向关系，不提供因果结论。",
        ),
    }
    try:
        label, caption, alt_text = metadata[recipe_id]
    except KeyError as error:
        raise ValueError(f"unknown recipe_id: {recipe_id}") from error
    scope = _sample_scope_from_data(data)
    if recipe_id == "grouped_time_series" and isinstance(data, dict):
        label = f"分组时间序列 · {data['value_name']}"
        if data.get("intervention_period") is not None:
            caption = (
                f"折线展示 {data['series_variable']} 分组的上游聚合值；"
                f"竖向点线标记干预期 {data['intervention_period']:g}。"
            )
    if recipe_id == "scatter_plot" and isinstance(data, dict):
        grain = data["grain"]
        if grain == "bin":
            caption = (
                "每个点是横轴分位箱内的 x/y 均值，点大小表示箱内样本量；"
                "不是原始行散点，也未在绘图阶段拟合趋势线。"
            )
            alt_text = "两个变量按横轴分位箱聚合后的均值散点图。"
        else:
            caption = (
                f"点展示上游授权的 {grain} 粒度数值对；"
                "图中未在绘图阶段拟合趋势线。"
            )
    if recipe_id == "box_plot" and isinstance(data, dict):
        caption = (
            "箱体展示上游预计算的分位数，须线采用 Tukey 1.5×IQR 规则；"
            "绘图阶段不重新估计。"
        )
    if scope is not None:
        caption = f"{caption} 样本口径：{scope}。"
    return f"{stage_label}：{label}", caption, alt_text


def _sample_scope_from_data(
    data: list[dict[str, Any]] | dict[str, Any],
) -> str | None:
    if isinstance(data, dict):
        value = data.get("sample_scope")
    else:
        value = data[0].get("sample_scope") if data else None
    return {
        "frozen_source_rows": "冻结源数据行，未冒充估计样本",
        "prepared_estimation_sample": "已登记的估计样本",
        "upstream_aggregate": "上游已登记聚合结果",
    }.get(value)


def _configure_reproducible_process() -> None:
    os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")
    cache_dir = PROJECT_ROOT / "backend" / "var" / "matplotlib-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".-")
    if not sanitized:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return sanitized[:120]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
