from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from ..seal import canonical_sha256


# Adapted from carolzhu-jr/GreenFinance_Plot_Agent at commit
# 07820bd3aef18e84b8a4e2290e03d1b7ef666ade. The workflow-facing contract,
# deterministic IDs, neutral text, safe paths, and artifact URIs are local changes.
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FIGURE_ROOT = PROJECT_ROOT / "backend" / "var" / "figures"
SOURCE_COMMIT = "07820bd3aef18e84b8a4e2290e03d1b7ef666ade"
MIME_TYPES = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "csv": "text/csv",
}


def render_request(
    request: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    _configure_reproducible_process()
    recipe_id = str(request["recipe_id"])
    data = request["bindings"]["data"]
    request_id = str(request["request_id"])
    stage = str(request["stage"])
    workflow_run_id = str(request["source"]["artifact_id"]).removesuffix(
        ":research_run"
    )
    figure_id = f"figure-{canonical_sha256(request)[:24]}"
    output_root = (artifact_root or DEFAULT_FIGURE_ROOT).resolve()
    output_dir = output_root / _safe_component(workflow_run_id) / _safe_component(stage)
    output_dir.mkdir(parents=True, exist_ok=True)

    title, caption, alt_text = _neutral_metadata(recipe_id, stage)
    if recipe_id == "coefficient_forest":
        if not isinstance(data, list) or not data:
            raise ValueError("coefficient_forest requires non-empty record data")
        result = _render_coefficient_forest(
            data,
            output_dir,
            figure_id,
            title,
            caption,
        )
        data_snapshot: dict[str, Any] = {"records": data}
    elif recipe_id == "sample_flow":
        if not isinstance(data, dict):
            raise ValueError("sample_flow requires one data record")
        result = _render_sample_flow(
            data,
            output_dir,
            figure_id,
            title,
            caption,
        )
        data_snapshot = data
    else:
        raise ValueError(f"unknown recipe_id: {recipe_id}")

    import matplotlib
    import pandas as pd

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
                "files": files,
                "data_snapshot": data_snapshot,
                "warnings": [],
            }
        ],
        "renderer": {
            "name": "GreenFinance_Plot_Agent",
            "version": "in-process-1.0",
            "upstream_commit": SOURCE_COMMIT,
            "backend": "matplotlib",
            "dependencies": {
                "matplotlib": matplotlib.__version__,
                "pandas": pd.__version__,
            },
            "font_family": matplotlib.rcParams["font.sans-serif"][0],
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
            frame["coefficient"],
            y_positions,
            xerr=[
                frame["coefficient"] - frame["ci_lower"],
                frame["ci_upper"] - frame["coefficient"],
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


def _save_all(
    figure: Any,
    frame: Any,
    output_dir: Path,
    base_name: str,
) -> dict[str, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "svg": output_dir / f"{base_name}.svg",
        "png": output_dir / f"{base_name}.png",
        "pdf": output_dir / f"{base_name}.pdf",
        "csv": output_dir / f"{base_name}.csv",
    }
    figure.savefig(
        paths["svg"],
        format="svg",
        dpi=300,
        bbox_inches="tight",
        metadata={"Date": None, "Creator": "GreenFinance_Plot_Agent"},
    )
    figure.savefig(
        paths["png"],
        format="png",
        dpi=300,
        bbox_inches="tight",
        metadata={"Software": "GreenFinance_Plot_Agent"},
    )
    figure.savefig(
        paths["pdf"],
        format="pdf",
        dpi=300,
        bbox_inches="tight",
        metadata={
            "CreationDate": None,
            "ModDate": None,
            "Creator": "GreenFinance_Plot_Agent",
        },
    )
    frame.to_csv(paths["csv"], index=False, lineterminator="\n")
    return {
        file_format: {"path": path, "sha256": _file_sha256(path)}
        for file_format, path in paths.items()
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
            return FontProperties(family=[selected], size=11, weight="bold")
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


def _neutral_metadata(recipe_id: str, stage: str) -> tuple[str, str, str]:
    stage_label = "论文" if stage == "publication" else "证据审计"
    if recipe_id == "coefficient_forest":
        return (
            f"{stage_label}：系数与置信区间",
            "点表示系数估计，线段表示 95% 置信区间；图形不代替 Claim 授权。",
            "各执行记录的系数点估计、95% 置信区间和零参考线。",
        )
    return (
        f"{stage_label}：样本筛选流程",
        "依次展示输入记录、剔除记录与最终估计样本；三项数量闭合。",
        "输入记录、剔除记录和最终估计样本的横向条形图。",
    )


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
