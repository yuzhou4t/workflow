from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT.parent / "benchmark-cases-v3-pilot"
DEFAULT_PRIVATE_REFERENCE_ROOT = (
    PROJECT_ROOT.parent / "benchmark-private-references-v3-pilot"
)
DEFAULT_SOURCE_ROOT = (
    Path.home()
    / "Downloads"
    / "case_004_绿色金融地方政府竞争与工业绿色转型_FINAL"
)
DISCOVERY_CASE_ID = (
    "case_004_green_finance_local_competition_industrial_transition_"
    "discovery_blind"
)
REPRODUCTION_CASE_ID = (
    "case_004_green_finance_local_competition_industrial_transition_"
    "reproduction_aligned"
)
PRIVATE_CASE_ID = "case_004_green_finance_local_competition_industrial_transition"
MAIN_COLUMNS = (
    "city_id",
    "year",
    "ln_greentrans",
    "greenfin",
    "ers",
    "ers_squared",
    "pergdp",
    "perinv",
    "tour",
)
REGRESSORS = (
    "greenfin",
    "ers",
    "ers_squared",
    "pergdp",
    "perinv",
    "tour",
)
SHARED_VISIBLE_FILES = (
    "main_data.csv",
    "data_dictionary.csv",
    "data_description.md",
    "city_mapping.csv",
    "spatial_weights.csv",
    "spatial_weights_metadata.json",
)
SAFE_CASE_ROOT_ENTRIES = {
    "01_model_input",
    "agent_laboratory_config_v2.json",
    "case_manifest.json",
    "roundtrip_validation.json",
}
DISCOVERY_PROHIBITED_TOKENS = (
    "sdm",
    "空间杜宾",
    "xsmle",
    "code1.do",
    "run_baseline.py",
    "model_spec",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_source_contract(
    source_root: Path,
) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    hidden = source_root / "02_hidden_reference"
    manifest_path = source_input / "public_case_manifest.json"
    code_path = hidden / "original_code" / "code1.do"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("case_id") != "case_004":
        raise ValueError("case 004 public manifest case_id changed")
    if manifest.get("data_level") != "城市x年份":
        raise ValueError("case 004 public data level changed")
    if manifest.get("data_period") != "2000-2023":
        raise ValueError("case 004 public period changed")
    if manifest.get("model_types") != ["非空间面板FE"]:
        raise ValueError("case 004 public manifest contradiction changed")

    code = code_path.read_text(encoding="utf-8")
    required_fragments = (
        'spatwmat using "W.dta",name(Wmatrix) standardize',
        'spatwmat using "W1.dta", name(W1matrix) standardize',
        "gen ln_greentrans = ln(greentrans)",
        "gen ers_2 = ers^2",
        "drop if year<2003",
        "xsmle ln_greentrans greenfin ers ers_2 pergdp perinv tour",
        "wmatrix(Wmatrix) model(sdm) fe(individual time) vce(robust)",
    )
    missing_fragments = [item for item in required_fragments if item not in code]
    if missing_fragments:
        raise ValueError(
            "case 004 author contract changed: " + ", ".join(missing_fragments)
        )

    missing_region_assets = [
        name
        for name in ("W_east.dta", "W_west.dta", "W_middle.dta")
        if not (source_input / name).exists()
    ]
    if missing_region_assets != ["W_east.dta", "W_west.dta", "W_middle.dta"]:
        raise ValueError("case 004 regional-weight availability changed")

    return {
        "public_manifest_sha256": sha256(manifest_path),
        "author_code_sha256": sha256(code_path),
        "public_manifest_declared_model": "non_spatial_panel_fe",
        "author_code_main_model": "spatial_durbin_panel",
        "contract_resolution": (
            "author code1.do main-model command governs this freeze; the public "
            "manifest's non-spatial FE declaration is retained as a contradiction"
        ),
        "author_code_main_sample": "year >= 2003",
        "author_code_fixed_effects": ["city", "year"],
        "author_code_standard_errors": "vce(robust)",
        "author_code_regressors": list(REGRESSORS),
        "missing_region_weight_assets": missing_region_assets,
        "regional_heterogeneity_status": "unsupported_missing_required_weights",
    }


def load_main_frame(
    source_input: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    source_path = source_input / "data3.dta"
    source = pd.read_stata(source_path, convert_categoricals=False)
    if source.shape != (6816, 17):
        raise ValueError(f"unexpected case 004 source shape: {source.shape}")
    required = [
        "city_id",
        "year",
        "province",
        "greentrans",
        *[name for name in REGRESSORS if name != "ers_squared"],
    ]
    missing = [name for name in required if name not in source.columns]
    if missing:
        raise ValueError("case 004 source columns changed: " + ", ".join(missing))

    weight_labels = list(
        pd.read_stata(
            source_input / "W.dta", convert_categoricals=False
        ).columns
    )
    city_names = source["city_id"].drop_duplicates().tolist()
    if len(weight_labels) != 284 or len(city_names) != 284:
        raise ValueError("case 004 city or W label count changed")
    if len(set(weight_labels)) != 284 or len(set(city_names)) != 284:
        raise ValueError("case 004 city or W labels are not unique")
    if set(weight_labels) != set(city_names):
        missing_from_data = sorted(set(weight_labels) - set(city_names))
        missing_from_w = sorted(set(city_names) - set(weight_labels))
        raise ValueError(
            "case 004 city/W label sets differ: "
            f"missing_from_data={missing_from_data}; missing_from_w={missing_from_w}"
        )

    stable_attributes = source.groupby("city_id")[["province", "area"]].nunique()
    if not stable_attributes.eq(1).all().all():
        raise ValueError("case 004 city province/area mapping is not stable")
    attributes = (
        source[["city_id", "province", "area"]]
        .drop_duplicates()
        .set_index("city_id")
    )
    mapping = pd.DataFrame(
        {
            "city_id": range(1, 285),
            "matrix_position_1based": range(1, 285),
            "city_name_zh": weight_labels,
            "province_zh": [attributes.loc[name, "province"] for name in weight_labels],
        }
    )
    id_by_name = dict(zip(mapping["city_name_zh"], mapping["city_id"]))

    sample = source.loc[source["year"] >= 2003].copy()
    if sample.shape[0] != 5964:
        raise ValueError("case 004 author sample row count changed")
    if (sample["greentrans"] <= 0).any():
        raise ValueError("case 004 greentrans is not strictly positive")
    frame = pd.DataFrame(
        {
            "city_id": sample["city_id"].map(id_by_name).astype(int),
            "year": sample["year"].astype(int),
            "ln_greentrans": np.log(sample["greentrans"].astype(float)),
            "greenfin": sample["greenfin"].astype(float),
            "ers": sample["ers"].astype(float),
            "ers_squared": np.square(sample["ers"].astype(float)),
            "pergdp": sample["pergdp"].astype(float),
            "perinv": sample["perinv"].astype(float),
            "tour": sample["tour"].astype(float),
        }
    ).sort_values(["city_id", "year"], kind="stable").reset_index(drop=True)
    if tuple(frame.columns) != MAIN_COLUMNS:
        raise ValueError("case 004 canonical projection changed")
    if frame.duplicated(["city_id", "year"]).any():
        raise ValueError("case 004 city_id + year key is not unique")
    if frame.isna().any().any() or not np.isfinite(frame.to_numpy(float)).all():
        raise ValueError("case 004 canonical fields contain invalid values")
    if sorted(frame["year"].unique().tolist()) != list(range(2003, 2024)):
        raise ValueError("case 004 author sample year support changed")
    if not frame.groupby("city_id")["year"].nunique().eq(21).all():
        raise ValueError("case 004 must remain a balanced 284 x 21 panel")
    if frame["city_id"].drop_duplicates().tolist() != list(range(1, 285)):
        raise ValueError("case 004 panel is not sorted in frozen W order")

    source_first_order = source["city_id"].drop_duplicates().tolist()
    return frame, mapping, {
        "source_data_sha256": sha256(source_path),
        "source_shape": [6816, 17],
        "frozen_shape": [5964, len(MAIN_COLUMNS)],
        "source_first_appearance_order_matches_w": source_first_order == weight_labels,
        "source_lexical_order_matches_w": sorted(city_names) == weight_labels,
        "city_label_set_exact_match": True,
        "mapping_rule": (
            "city_id is reassigned 1..284 in the exact W.dta column-label order; "
            "panel rows are explicitly sorted by this id then year"
        ),
    }


def load_spatial_weights(
    source_input: Path,
    mapping: pd.DataFrame,
) -> tuple[np.ndarray, dict[str, Any]]:
    source_path = source_input / "W.dta"
    source = pd.read_stata(source_path, convert_categoricals=False)
    expected_labels = mapping["city_name_zh"].tolist()
    if source.shape != (284, 284) or list(source.columns) != expected_labels:
        raise ValueError("case 004 W.dta shape or label order changed")
    matrix = source.to_numpy(float)
    if not np.isfinite(matrix).all() or (matrix < 0).any():
        raise ValueError("case 004 W.dta contains invalid weights")
    if not np.allclose(matrix, matrix.T, atol=1e-12):
        raise ValueError("case 004 W.dta is no longer symmetric")
    if not np.allclose(np.diag(matrix), 0, atol=1e-12):
        raise ValueError("case 004 W.dta diagonal is not zero")
    off_diagonal = matrix.copy()
    np.fill_diagonal(off_diagonal, np.nan)
    if not (off_diagonal[~np.isnan(off_diagonal)] > 0).all():
        raise ValueError("case 004 W.dta contains ambiguous off-diagonal zeros")
    row_sums = matrix.sum(axis=1)
    if not (row_sums > 0).all():
        raise ValueError("case 004 W.dta contains an empty row")
    weights = matrix / row_sums[:, None]

    alternative_path = source_input / "W1.dta"
    alternative = pd.read_stata(alternative_path, convert_categoricals=False)
    if alternative.shape != (284, 284):
        raise ValueError("case 004 W1.dta shape changed")
    if list(alternative.columns) != expected_labels:
        raise ValueError("case 004 W1.dta label order changed")
    alternative_values = alternative.to_numpy(float)
    if np.allclose(alternative_values, matrix, rtol=0, atol=0):
        raise ValueError("case 004 W1.dta unexpectedly duplicates W.dta")

    return weights, {
        "source_spatial_matrix_sha256": sha256(source_path),
        "source_shape": [284, 284],
        "source_symmetric": True,
        "source_diagonal_zero": True,
        "source_off_diagonal_strictly_positive": True,
        "source_row_sum_min": float(row_sums.min()),
        "source_row_sum_max": float(row_sums.max()),
        "normalization": "each W.dta row divided by its visible 284-column row sum",
        "label_alignment": {
            "column_labels_unique": True,
            "column_label_set_equals_data_city_set": True,
            "frozen_row_identity_rule": "same ordered labels as columns",
            "frozen_numeric_id_rule": "matrix position + 1",
            "status": "passed_exact_set_and_explicit_reindex",
        },
        "row_identity_inference_gap": (
            "Stata W.dta stores 284 named columns but no separate row-label field; "
            "the same-as-column row order is frozen from the square symmetric matrix "
            "convention and remains pending independent source documentation"
        ),
        "alternative_weight": {
            "source_filename": "W1.dta",
            "source_sha256": sha256(alternative_path),
            "shape": [284, 284],
            "column_labels_match_primary": True,
            "copied_into_case": False,
            "status": "disclosed_not_packaged_semantics_undefined",
            "reason": (
                "author code invokes W1 as an endogeneity check, but no visible "
                "construction formula or scientific interpretation is provided"
            ),
        },
        "regional_weights": {
            "required_by_author_code": [
                "W_east.dta",
                "W_west.dta",
                "W_middle.dta",
            ],
            "available": [],
            "status": "unsupported_missing_required_weights",
        },
    }


def build_dictionary(frame: pd.DataFrame) -> pd.DataFrame:
    labels = {
        "city_id": "城市编码与空间单元标识",
        "year": "年份",
        "ln_greentrans": "工业绿色转型指标的自然对数",
        "greenfin": "绿色金融指标",
        "ers": "地方政府竞争相关指标",
        "ers_squared": "ers 的平方项",
        "pergdp": "人均 GDP 相关指标",
        "perinv": "人均投资相关指标",
        "tour": "旅游相关指标",
    }
    roles = {
        "city_id": "id + spatial_id",
        "year": "time",
        "ln_greentrans": "outcome",
        "greenfin": "primary exposure",
        "ers": "nonlinear competition term",
        "ers_squared": "nonlinear competition term",
        "pergdp": "covariate",
        "perinv": "covariate",
        "tour": "covariate",
    }
    definitions = {
        "city_id": (
            "为保证数据与空间矩阵显式对齐，按 W.dta 的列标签顺序重编为 "
            "1–284；映射见 city_mapping.csv。"
        ),
        "year": "日历年份；按作者主模型边界仅保留 2003–2023 年。",
        "ln_greentrans": "由源字段 greentrans 逐值取自然对数生成。",
        "greenfin": "源数据已构造的绿色金融指标；精确单位未在可见说明中冻结。",
        "ers": "源数据已构造的政府竞争相关指标；精确构造式待独立审计。",
        "ers_squared": "由 ers 逐值平方生成，与 ers 共同表示非线性项。",
        "pergdp": "源数据已构造的人均 GDP 相关指标；精确单位未报告。",
        "perinv": "源数据已构造的人均投资相关指标；精确单位未报告。",
        "tour": "源数据已构造的旅游相关指标；精确单位未报告。",
    }
    rows = []
    for name in frame.columns:
        rows.append(
            {
                "dataset": "main_data.csv",
                "variable": name,
                "label_zh": labels[name],
                "role": roles[name],
                "storage_type": str(frame[name].dtype),
                "definition": definitions[name],
                "unit": (
                    "冻结城市编码"
                    if name == "city_id"
                    else ("年" if name == "year" else "源数据单位")
                ),
                "source": "case 004 data3.dta and author-code main contract",
                "missing_value_meaning": "无缺失",
                "available_years": "2003–2023",
                "processing_status": (
                    "重编并按空间矩阵顺序排序"
                    if name == "city_id"
                    else (
                        "由 greentrans 取自然对数"
                        if name == "ln_greentrans"
                        else (
                            "由 ers 平方"
                            if name == "ers_squared"
                            else "数值未改写"
                        )
                    )
                ),
                "missing_count": int(frame[name].isna().sum()),
                "missing_rate": float(frame[name].isna().mean()),
            }
        )
    return pd.DataFrame(rows)


def build_data_description() -> str:
    return """# 中立数据说明

`main_data.csv` 是 284 个城市在 2003–2023 年的完整平衡面板，共 5,964 行、9 个字段。`city_id + year` 唯一，当前主表无缺失。

- `ln_greentrans` 是结果指标，由正值 `greentrans` 取自然对数。`greenfin` 是核心暴露，`ers` 与 `ers_squared` 共同表示政府竞争的非线性项，`pergdp`、`perinv`、`tour` 是可见协变量。
- `city_mapping.csv` 将每个城市显式映射到 `W.dta` 的列标签位置。数字 `city_id` 按该位置重编为 1–284，面板行也按同一顺序冻结。
- `spatial_weights.csv` 由源包 `W.dta` 的 284×284 非负对称矩阵逐行标准化生成；对角线为 0，每行和为 1。
- 源包还有一个与主 W 不同的矩阵资产，但没有可独立核验的构造语义，因此本冻结包只披露、不复制、不运行它。
- 地区异质性所需的东部、西部和中部权重文件在源包中缺失，因此该分析固定为 `unsupported_missing_required_weights`。
- 主矩阵行身份没有单独标签字段；本包依据方阵、对称性和列标签冻结“行与列同序”，并将这一点保留为待审计的参考推断缺口。
- 可见数据没有外生识别；结论上限是空间条件关联，不得转写为因果效应。

本说明不包含系数、方向、显著性、原论文文字、作者代码或运行结果。
"""


def build_evidence_bundle(*, aligned: bool) -> str:
    common = """# 冻结证据与评测边界

- 本案例资格为 `quasi_holdout_candidate_pending_freeze_audit`，当前不进入主分。
- 两个视图共用字节完全一致的主数据、字典、中立说明、城市映射和行标准化空间权重。
- 主目标是 `greenfin` 与 `ln_greentrans` 的本地直接、跨地区间接和总空间条件关联；方向为 `unspecified`。
- `ers` 与 `ers_squared` 必须联合解释，不得将单个一次项系数写成全局线性关系。
- 主 W 的行列标识已显式冻结，但源行没有独立标签；这一参考推断缺口必须在最终报告中披露。
- 另一权重资产的科学语义未冻结，不能当作自由切换的稳健性规格。
- 地区异质性缺少必需权重资产，状态固定为 `unsupported_missing_required_weights`。
- 当前没有与主执行器独立的可信空间面板复算，也没有可核验的直接、间接、总关联隐藏数值参考。
- 显著、不显著、反向、不稳定、推断失败和无可接纳主张都是合法终点。
"""
    if not aligned:
        return common + """

## 自主发现视图

本视图只给出空间面板资产、变量角色，以及需要区分本地直接、跨地区间接和总关联的研究目标。系统必须在读取结果前自主冻结可执行方法、面板结构处理、推断、诊断、停止条件和主张降级规则。
"""
    return common + """

## 方法对齐视图

主方法冻结为城市和年份双向固定效应的空间杜宾模型（SDM）。结果为 `ln_greentrans`；`greenfin`、`ers`、`ers_squared`、`pergdp`、`perinv`、`tour` 及其空间滞后进入主模型，并包含结果的空间滞后。推断规格按作者主命令冻结为 robust。

主报告必须给出 `greenfin` 的直接、间接和总空间条件关联；`ers` 与 `ers_squared` 必须联合报告非线性关系。主 W 为 `spatial_weights.csv`，以 `city_id` 为行列标识、对角线为 0 且已按行标准化。

不得将同一实现的重跑写成独立复算。缺少独立执行器、源行标签独立文档或可辩护的替代 W 语义时，必须降级或拒绝数值主张。
"""


def variable_specs() -> list[dict[str, Any]]:
    labels = {
        "city_id": "城市编码与空间单元标识",
        "year": "年份",
        "ln_greentrans": "工业绿色转型指标的自然对数",
        "greenfin": "绿色金融指标",
        "ers": "政府竞争相关指标",
        "ers_squared": "政府竞争指标平方项",
        "pergdp": "人均 GDP 相关指标",
        "perinv": "人均投资相关指标",
        "tour": "旅游相关指标",
    }
    roles = {
        "year": "time",
        "ln_greentrans": "outcome",
        "greenfin": "exposure",
        "ers": "control",
        "ers_squared": "control",
        "pergdp": "control",
        "perinv": "control",
        "tour": "control",
    }
    specs = [
        {
            "name": "city_id",
            "label": labels["city_id"],
            "role": "id",
            "definition": "见 data_dictionary.csv 与 city_mapping.csv。",
            "source": "case 004 data3.dta and W.dta",
        },
        {
            "name": "city_id",
            "label": labels["city_id"],
            "role": "spatial_id",
            "definition": "与 spatial_weights.csv 行列标识一一对齐。",
            "source": "case 004 W.dta column-label order",
        },
    ]
    for name in MAIN_COLUMNS[1:]:
        specs.append(
            {
                "name": name,
                "label": labels[name],
                "role": roles[name],
                "definition": f"见 data_dictionary.csv 中 {name} 的冻结定义。",
                "source": "case 004 data3.dta and author-code main contract",
            }
        )
    return specs


def build_case_profile(
    *,
    case_id: str,
    data_sha256: str,
    data_size: int,
    weights_sha256: str,
    weights_size: int,
    aligned: bool,
) -> dict[str, Any]:
    common_constraints = [
        "ln_greentrans 是结果，greenfin 是核心暴露；ers 与 ers_squared 必须联合解释。",
        "主目标是 greenfin 的本地直接、跨地区间接与总空间条件关联，方向未预设。",
        "必须将城市和年份面板结构、空间反馈、推断、诊断和停止规则在读取结果前冻结。",
        "源 W 行没有独立标签，须披露行列同序的参考推断缺口。",
        "替代权重语义不明，不得自由切换；地区异质性缺权重资产，不得伪装为已执行。",
        "只允许空间条件关联表述，无外生识别不得越界为因果。",
    ]
    if aligned:
        title = "绿色金融、地方政府竞争与工业绿色转型：SDM 对齐候选案例"
        benchmark_track = "reproduction_aligned"
        design_constraints = [
            "冻结主方法为城市和年份双向固定效应的空间杜宾模型（SDM）。",
            "主模型包含 W·ln_greentrans 以及 greenfin、ers、ers_squared、pergdp、perinv、tour 的空间滞后。",
            "主 W 冻结为 spatial_weights.csv；推断规格冻结为 robust。",
            *common_constraints,
        ]
    else:
        title = "绿色金融、地方政府竞争与工业绿色转型：空间方法自主路由"
        benchmark_track = "strict_blind"
        design_constraints = [
            "不公开主方法名称；必须在读取结果前根据空间面板资产和目标自主冻结方法。",
            *common_constraints,
        ]
    diagnostics = [
        "核验 284×21 平衡面板、city_id + year 唯一键、完整样本和变量类型",
        "核验 city_id 与 spatial_weights.csv 行列一一对齐、对角线为 0 且行和为 1",
        "报告空间反馈参数的可用性与 greenfin 直接、间接、总关联分解",
        "对 ers 与 ers_squared 做联合解释并报告相关数值稳定性",
        "显式披露源 W 行标签的参考推断缺口",
        "要求与主执行器独立的复算；无独立实现时记为未通过",
        "将替代 W 和地区异质性记为当前不支持，不得自由试验",
        "将结论严格校准为空间条件关联",
    ]
    return {
        "case_id": case_id,
        "title": title,
        "research_question": (
            "在 2003–2023 年 284 城市空间面板中，绿色金融 greenfin "
            "与工业绿色转型 ln_greentrans 的本地、跨地区和总空间条件关联是否可稳健复算？"
        ),
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "statement": (
                    "在冻结空间面板、权重与协变量条件下，greenfin 与 "
                    "ln_greentrans 存在可分解的空间条件关联。"
                ),
                "expected_direction": "unspecified",
                "mechanism": "不预设方向或因果机制；空间关联不等于空间因果溢出。",
            },
            {
                "hypothesis_id": "H2",
                "statement": "ers 与 ln_greentrans 之间可能存在非线性空间条件关联。",
                "expected_direction": "nonlinear",
                "mechanism": "必须联合解释 ers 与 ers_squared，不做单项因果解释。",
            },
        ],
        "unit_of_analysis": "城市—年度",
        "sample_period": "2003–2023",
        "data_structure_hint": "spatial_panel",
        "variables": variable_specs(),
        "dataset_refs": [
            {
                "dataset_id": "case_004_neutral_main_data",
                "role": "main",
                "filename": "main_data.csv",
                "mime_type": "text/csv",
                "sha256": data_sha256,
                "size_bytes": data_size,
            },
            {
                "dataset_id": "case_004_frozen_spatial_weights",
                "role": "supplementary",
                "filename": "spatial_weights.csv",
                "mime_type": "text/csv",
                "sha256": weights_sha256,
                "size_bytes": weights_size,
            },
        ],
        "design_envelope": {
            "benchmark_track": benchmark_track,
            "research_goal": "associational",
            "target_estimands": [
                "greenfin 与 ln_greentrans 的本地直接空间条件关联",
                "greenfin 与 ln_greentrans 的跨地区间接空间条件关联",
                "greenfin 与 ln_greentrans 的总空间条件关联",
                "ers 与 ers_squared 联合定义的非线性空间条件关联",
            ],
            "design_constraints": design_constraints,
            "required_diagnostics": diagnostics,
            "allowed_claim_strength": "associational",
        },
        "known_policy_facts": [],
        "constraints": [
            "main_data.csv 有 5,964 行、9 列、284 个城市和 21 个年份，city_id + year 唯一且无缺失。",
            "本案例是 quasi_holdout_candidate_pending_freeze_audit，当前不进入主分。",
            "两视图共用字节完全一致的数据、字典、说明、城市映射和空间权重。",
            "禁止读取或复制原论文、作者代码、结果、日志、run_baseline 或 02_hidden_reference。",
            "发现无可接纳主张是合法科学终点。",
        ],
    }


def profile_markdown(profile: dict[str, Any], *, aligned: bool) -> str:
    envelope = profile["design_envelope"]
    constraints = "\n".join(f"- {item}" for item in envelope["design_constraints"])
    diagnostics = "\n".join(f"- {item}" for item in envelope["required_diagnostics"])
    method_note = (
        "冻结为 SDM、双向固定效应、robust 推断与主 W。"
        if aligned
        else "只公开空间资产和估计目标，不公开主方法名称。"
    )
    return f"""# {profile['title']}

## 轨道与评测资格

- 轨道：`{envelope['benchmark_track']}`
- 资格：`quasi_holdout_candidate_pending_freeze_audit`，当前不进入主分。
- {method_note}

## 研究问题与假设

{profile['research_question']}

- H1：{profile['hypotheses'][0]['statement']}
- H2：{profile['hypotheses'][1]['statement']}

方向为 `unspecified`，主张上限是空间条件关联。

## 结果不可见时冻结的设计边界

{constraints}

## 必须报告的诊断

{diagnostics}

## 共同数据事实

- 284 个城市、2003–2023 年、5,964 行的完整平衡面板。
- `city_id + year` 唯一，当前主表无缺失。
- 空间矩阵 284×284，行列使用同一冻结标识，对角线为 0，行和为 1。
- 不提供论文、代码、结果、日志、run_baseline 或隐藏参考。
"""


def agent_laboratory_config(
    *, case_id: str, input_view: str, benchmark_track: str
) -> dict[str, Any]:
    return {
        "case": {
            "case_id": case_id,
            "input_view": input_view,
            "benchmark_track": benchmark_track,
            "model_input_dir": "01_model_input",
            "files": {
                "case_profile": "case_profile.md",
                "main_data": "main_data.csv",
                "data_dictionary": "data_dictionary.csv",
                "data_description": "data_description.md",
                "evidence_bundle": "evidence_bundle.md",
                "additional_data": {
                    "spatial_weights": "spatial_weights.csv",
                    "city_mapping": "city_mapping.csv",
                    "spatial_weights_metadata": "spatial_weights_metadata.json",
                },
            },
        },
        "model": {
            "name": "qwen3.7-plus",
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "timeout_seconds": 360,
            "max_tokens": 12_288,
        },
        "workflow": {
            "output_dir": "../../benchmark-results/agent-laboratory-v3",
            "upstream_repo_root": "../../Agent Laboratory",
            "execution_timeout_seconds": 600,
            "max_steps": 5,
            "max_llm_calls": 40,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
            "formal_budget_override": (
                "suite-owned 2x envelope: 80 provider attempts, 5400s cell, "
                "3600s statistics, 1200s generated code"
            ),
        },
    }


def verify_round_trip(source: pd.DataFrame, csv_path: Path) -> dict[str, Any]:
    restored = pd.read_csv(csv_path, float_precision="round_trip")
    pd.testing.assert_frame_equal(
        source.reset_index(drop=True),
        restored.reset_index(drop=True),
        check_dtype=False,
        check_exact=True,
        check_column_type=False,
    )
    return {
        "status": "passed",
        "row_order_preserved": True,
        "column_order_preserved": list(source.columns) == list(restored.columns),
        "values_exact_after_dtype_normalization": True,
        "rows": len(restored),
        "columns": len(restored.columns),
        "csv_sha256": sha256(csv_path),
    }


def verify_spatial_csv(path: Path, mapping: pd.DataFrame) -> dict[str, Any]:
    restored = pd.read_csv(path, encoding="utf-8-sig")
    labels = [str(value) for value in mapping["city_id"].tolist()]
    if restored.columns[0] != "spatial_id":
        raise ValueError("spatial_weights.csv first column changed")
    if [str(value) for value in restored.columns[1:]] != labels:
        raise ValueError("spatial_weights.csv column labels changed")
    if restored.iloc[:, 0].astype(str).tolist() != labels:
        raise ValueError("spatial_weights.csv row labels changed")
    matrix = restored.iloc[:, 1:].to_numpy(float)
    if matrix.shape != (284, 284):
        raise ValueError("spatial_weights.csv shape changed")
    if not np.allclose(np.diag(matrix), 0, atol=1e-12):
        raise ValueError("spatial_weights.csv diagonal changed")
    row_sum_error = float(np.max(np.abs(matrix.sum(axis=1) - 1)))
    if row_sum_error > 1e-8:
        raise ValueError("spatial_weights.csv is not row standardized")
    return {
        "status": "passed",
        "shape": [284, 284],
        "row_column_labels_identical": True,
        "diagonal_zero": True,
        "nonnegative": bool((matrix >= 0).all()),
        "row_sum_max_error": row_sum_error,
        "csv_sha256": sha256(path),
    }


def verify_discovery_blinding(input_dir: Path) -> dict[str, Any]:
    findings = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".csv"}:
            continue
        text = path.read_text(encoding="utf-8-sig").lower()
        for token in DISCOVERY_PROHIBITED_TOKENS:
            if token.lower() in text or token.lower() in path.name.lower():
                findings.append({"file": path.name, "token": token})
    if findings:
        raise ValueError(f"case 004 discovery view leaks method information: {findings}")
    return {
        "status": "passed",
        "checked_tokens": list(DISCOVERY_PROHIBITED_TOKENS),
        "findings": [],
    }


def manifest_entry(path: Path, case_root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(case_root)),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def public_spatial_provenance(
    spatial_provenance: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        key: spatial_provenance[key]
        for key in (
            "source_shape",
            "source_symmetric",
            "source_diagonal_zero",
            "source_off_diagonal_strictly_positive",
            "source_row_sum_min",
            "source_row_sum_max",
            "normalization",
            "label_alignment",
            "row_identity_inference_gap",
        )
    }
    payload["alternative_weight"] = {
        "copied_into_case": False,
        "status": "disclosed_not_packaged_semantics_undefined",
    }
    payload["regional_weights"] = {
        "available": [],
        "status": "unsupported_missing_required_weights",
    }
    return payload


def build_private_reference(
    *,
    private_case_root: Path,
    source_root: Path,
    source_contract: dict[str, Any],
    data_provenance: dict[str, Any],
    spatial_provenance: dict[str, Any],
) -> None:
    """Write hidden author-contract facts outside system-visible case roots."""

    private_case_root.mkdir(parents=True, exist_ok=False)
    audit_path = private_case_root / "freeze_audit.json"
    write_json(
        audit_path,
        {
            "schema_version": "case004-freeze-audit-v1",
            "case_id": PRIVATE_CASE_ID,
            "access": "benchmark_evaluator_only",
            "evaluation_class": (
                "quasi_holdout_candidate_pending_freeze_audit"
            ),
            "primary_score_eligible": False,
            "source_contract": source_contract,
            "data_provenance": data_provenance,
            "spatial_provenance": spatial_provenance,
            "source_assets": {
                "main_data_sha256": sha256(
                    source_root / "01_model_input" / "data3.dta"
                ),
                "primary_weight_sha256": sha256(
                    source_root / "01_model_input" / "W.dta"
                ),
                "alternative_weight_sha256": sha256(
                    source_root / "01_model_input" / "W1.dta"
                ),
            },
            "reference_inference_gap": {
                "public_manifest_author_code_model_conflict": True,
                "w_rows_have_independent_labels": False,
                "independent_spatial_panel_reproducer_available": False,
                "direct_indirect_total_numeric_reference_available": False,
                "primary_score_status": "pending_freeze_audit",
            },
            "builder_access_scope": (
                "public manifest plus hidden author code1.do contract only; no "
                "paper, result table, execution log, or run_baseline output copied"
            ),
        },
    )
    write_json(
        private_case_root / "private_manifest.json",
        {
            "schema_version": "case004-private-reference-v1",
            "case_id": PRIVATE_CASE_ID,
            "access": "benchmark_evaluator_only",
            "system_case_root_contains_private_reference": False,
            "freeze_audit": {
                "path": "freeze_audit.json",
                "sha256": sha256(audit_path),
                "size_bytes": audit_path.stat().st_size,
            },
        },
    )


def build_manifest(
    *,
    case_root: Path,
    case_id: str,
    input_view: str,
    benchmark_track: str,
    counterpart_case_id: str,
    source_root: Path,
    data_provenance: dict[str, Any],
    data_roundtrip: dict[str, Any],
    spatial_roundtrip: dict[str, Any],
    spatial_provenance: dict[str, Any],
    discovery_blinding: dict[str, Any] | None,
) -> dict[str, Any]:
    input_dir = case_root / "01_model_input"
    visible_files = sorted(path for path in input_dir.iterdir() if path.is_file())
    safe_spatial_provenance = public_spatial_provenance(spatial_provenance)
    return {
        "manifest_version": 3,
        "case_id": case_id,
        "input_view": input_view,
        "benchmark_track": benchmark_track,
        "evaluation_class": "quasi_holdout_candidate_pending_freeze_audit",
        "primary_ai_scientist_claim_eligible": False,
        "primary_score_eligible": False,
        "hidden_reference_access": "denied",
        "outbound_status": "requires_explicit_authorization",
        "benchmark_contract": "same_neutral_data_two_method_information_views",
        "research_target": "spatial_associational",
        "hypothesis_direction": "unspecified",
        "visible_input": {
            "directory": "01_model_input",
            "row_count": 5964,
            "column_count": len(MAIN_COLUMNS),
            "entity_count": 284,
            "time_period_count": 21,
            "spatial_weight_shape": [284, 284],
            "files": [manifest_entry(path, case_root) for path in visible_files],
        },
        "shared_visible_asset_contract": {
            "counterpart_case_id": counterpart_case_id,
            "required_byte_identical_files": list(SHARED_VISIBLE_FILES),
            "sha256": {name: sha256(input_dir / name) for name in SHARED_VISIBLE_FILES},
        },
        "scope_disclosures": {
            "primary_exposure": "greenfin",
            "nonlinear_competition_terms": ["ers", "ers_squared"],
            "other_covariates": ["pergdp", "perinv", "tour"],
            "regional_heterogeneity": "unsupported_missing_required_weights",
            "source_row_identity": "pending_independent_documentation",
            "independent_spatial_reproducer": "not_available_claim_admission_gap",
            "alternative_weight_sensitivity": "disclosed_not_packaged_semantics_undefined",
            "causal_identification": "not_available",
        },
        "hidden_reference": {
            "access": "denied",
            "copied_into_case": False,
            "directory_present": False,
            "allowed_during_system_run": False,
            "accessed_during_build": True,
            "private_reference_present_in_case_root": False,
        },
        "source_provenance": {
            **data_provenance,
            "source_w_sha256": sha256(source_root / "01_model_input" / "W.dta"),
            "neutralization": {
                "sample_filter": "year >= 2003",
                "column_projection": list(MAIN_COLUMNS),
                "column_renames": {"source city_id string": "frozen numeric city_id"},
                "derived_fields": {
                    "ln_greentrans": "natural log of greentrans",
                    "ers_squared": "ers squared",
                },
                "data_values_changed_beyond_declared_derivations": False,
                "coefficients_estimated_during_build": False,
                "paper_code_results_logs_or_run_baseline_copied": False,
            },
            "city_w_alignment": spatial_provenance["label_alignment"],
            "reference_inference_gap": {
                "w_rows_have_independent_labels": False,
                "independent_spatial_panel_reproducer_available": False,
                "direct_indirect_total_numeric_reference_available": False,
                "primary_score_status": "pending_freeze_audit",
            },
            "spatial_weights": safe_spatial_provenance,
            "csv_round_trip": data_roundtrip,
            "spatial_csv_validation": spatial_roundtrip,
            "discovery_blinding": (
                {"status": discovery_blinding["status"], "findings": []}
                if discovery_blinding is not None
                else None
            ),
        },
        "baseline_agent_config": {
            "provider_attempt_ceiling": 40,
            "max_steps": 5,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
            "execution_timeout_seconds": 600,
            "suite_2x_override_required_for_formal_run": True,
        },
    }


def ensure_no_prohibited_files(case_roots: list[Path]) -> None:
    prohibited_suffixes = {".pdf", ".doc", ".docx", ".py", ".ipynb", ".log", ".dta"}
    prohibited_names = {
        "02_hidden_reference",
        "run_baseline.py",
        "model_specifications.jsonl",
        "original_paper.pdf",
        "code1.do",
    }
    for case_root in case_roots:
        for path in case_root.rglob("*"):
            if path.name in prohibited_names:
                raise ValueError(f"prohibited case asset created: {path}")
            if path.is_file() and path.suffix.lower() in prohibited_suffixes:
                raise ValueError(f"prohibited file type created: {path}")


def ensure_safe_case_root(case_root: Path, manifest: dict[str, Any]) -> None:
    actual_entries = {path.name for path in case_root.iterdir()}
    if actual_entries != SAFE_CASE_ROOT_ENTRIES:
        raise ValueError(
            "case 004 system root contains non-whitelisted entries: "
            f"{sorted(actual_entries - SAFE_CASE_ROOT_ENTRIES)}"
        )
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True).lower()
    forbidden = (
        "freeze_audit.json",
        "author_code",
        "hidden_main",
        "public_manifest_declared_model",
        "contract_resolution",
        "vce(robust)",
        "spatial_durbin_panel",
        "xsmle",
        "source_w1",
        "standard_error_contract",
    )
    leaked = [token for token in forbidden if token in serialized]
    if leaked:
        raise ValueError(f"case 004 safe manifest leaks private fields: {leaked}")


def validate_case_roots(
    *,
    case_roots: list[Path],
    private_case_root: Path,
    source_frame: pd.DataFrame,
    source_mapping: pd.DataFrame,
    source_weights: np.ndarray,
    source_contract: dict[str, Any],
    data_provenance: dict[str, Any],
    spatial_provenance: dict[str, Any],
) -> dict[str, Any]:
    if len(case_roots) != 2 or not all(path.is_dir() for path in case_roots):
        raise FileNotFoundError("both case 004 views must exist before validation")
    expected_views = ("discovery_blind", "reproduction_aligned")
    manifests: list[dict[str, Any]] = []
    for case_root, expected_view in zip(case_roots, expected_views):
        manifest = json.loads(
            (case_root / "case_manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("input_view") != expected_view:
            raise ValueError(f"case 004 view mismatch: {case_root}")
        if manifest.get("evaluation_class") != (
            "quasi_holdout_candidate_pending_freeze_audit"
        ):
            raise ValueError("case 004 evaluation class changed")
        if manifest.get("primary_score_eligible") is not False:
            raise ValueError("case 004 became primary-score eligible before audit")
        if manifest.get("hidden_reference_access") != "denied":
            raise ValueError("case 004 hidden-reference access is not denied")
        ensure_safe_case_root(case_root, manifest)
        declared_paths: set[Path] = set()
        for entry in manifest["visible_input"]["files"]:
            path = case_root / str(entry["path"])
            if not path.is_file():
                raise FileNotFoundError(f"declared visible file missing: {path}")
            if path.stat().st_size != int(entry["size_bytes"]):
                raise ValueError(f"declared visible size changed: {path}")
            if sha256(path) != str(entry["sha256"]):
                raise ValueError(f"declared visible hash changed: {path}")
            declared_paths.add(path.resolve())
        actual_paths = {
            path.resolve()
            for path in (case_root / "01_model_input").iterdir()
            if path.is_file()
        }
        if declared_paths != actual_paths:
            raise ValueError("case 004 visible directory differs from manifest")
        verify_round_trip(
            source_frame, case_root / "01_model_input" / "main_data.csv"
        )
        observed_mapping = pd.read_csv(
            case_root / "01_model_input" / "city_mapping.csv"
        )
        pd.testing.assert_frame_equal(
            source_mapping.reset_index(drop=True),
            observed_mapping.reset_index(drop=True),
            check_dtype=False,
            check_exact=True,
        )
        verify_spatial_csv(
            case_root / "01_model_input" / "spatial_weights.csv",
            source_mapping,
        )
        observed_weights = pd.read_csv(
            case_root / "01_model_input" / "spatial_weights.csv",
            encoding="utf-8-sig",
        ).iloc[:, 1:].to_numpy(float)
        if not np.allclose(
            observed_weights, source_weights, rtol=0, atol=5e-16
        ):
            raise ValueError("case 004 visible weights differ from source derivation")
        manifests.append(manifest)

    for name in SHARED_VISIBLE_FILES:
        if (case_roots[0] / "01_model_input" / name).read_bytes() != (
            case_roots[1] / "01_model_input" / name
        ).read_bytes():
            raise ValueError(f"shared visible asset differs across views: {name}")
    verify_discovery_blinding(case_roots[0] / "01_model_input")
    ensure_no_prohibited_files(case_roots)

    private_entries = {path.name for path in private_case_root.iterdir()}
    if private_entries != {"freeze_audit.json", "private_manifest.json"}:
        raise ValueError("case 004 private reference directory changed")
    private_audit_path = private_case_root / "freeze_audit.json"
    private_audit = json.loads(private_audit_path.read_text(encoding="utf-8"))
    if private_audit.get("access") != "benchmark_evaluator_only":
        raise ValueError("case 004 private reference access changed")
    if private_audit.get("source_contract") != source_contract:
        raise ValueError("case 004 private source contract changed")
    if private_audit["data_provenance"]["source_data_sha256"] != (
        data_provenance["source_data_sha256"]
    ):
        raise ValueError("case 004 private data hash changed")
    if private_audit["spatial_provenance"]["source_spatial_matrix_sha256"] != (
        spatial_provenance["source_spatial_matrix_sha256"]
    ):
        raise ValueError("case 004 private W hash changed")
    if private_audit["spatial_provenance"]["alternative_weight"][
        "source_sha256"
    ] != spatial_provenance["alternative_weight"]["source_sha256"]:
        raise ValueError("case 004 private W1 hash changed")
    if private_audit.get("source_assets") != {
        "main_data_sha256": data_provenance["source_data_sha256"],
        "primary_weight_sha256": spatial_provenance[
            "source_spatial_matrix_sha256"
        ],
        "alternative_weight_sha256": spatial_provenance[
            "alternative_weight"
        ]["source_sha256"],
    }:
        raise ValueError("case 004 private source-asset contract changed")
    private_manifest_path = private_case_root / "private_manifest.json"
    private_manifest = json.loads(private_manifest_path.read_text(encoding="utf-8"))
    if sha256(private_audit_path) != private_manifest["freeze_audit"]["sha256"]:
        raise ValueError("case 004 private freeze-audit hash changed")
    return {
        "status": "passed",
        "evaluation_class": "quasi_holdout_candidate_pending_freeze_audit",
        "primary_score_eligible": False,
        "case_roots": [str(path) for path in case_roots],
        "views": list(expected_views),
        "rows": 5964,
        "columns": len(MAIN_COLUMNS),
        "entities": 284,
        "periods": 21,
        "neutral_csv_sha256": sha256(
            case_roots[0] / "01_model_input" / "main_data.csv"
        ),
        "spatial_weights_sha256": sha256(
            case_roots[0] / "01_model_input" / "spatial_weights.csv"
        ),
        "city_mapping_sha256": sha256(
            case_roots[0] / "01_model_input" / "city_mapping.csv"
        ),
        "private_freeze_audit_sha256": sha256(private_audit_path),
        "private_manifest_sha256": sha256(private_manifest_path),
        "private_reference_present_in_case_root": False,
        "external_model_or_api_called": False,
        "regional_heterogeneity_status": "unsupported_missing_required_weights",
        "alternative_w_status": "disclosed_not_packaged_semantics_undefined",
        "reference_inference_gap": "pending_freeze_audit",
        "manifest_ids": [manifest["case_id"] for manifest in manifests],
    }


def build_cases(
    source_root: Path,
    benchmark_root: Path,
    private_reference_root: Path,
) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    if not source_input.is_dir():
        raise FileNotFoundError(f"invalid case 004 source package: {source_root}")
    targets = [
        benchmark_root / DISCOVERY_CASE_ID,
        benchmark_root / REPRODUCTION_CASE_ID,
    ]
    private_target = private_reference_root / PRIVATE_CASE_ID
    existing = [str(path) for path in [*targets, private_target] if path.exists()]
    if existing:
        raise FileExistsError(
            "non-destructive builder refuses to overwrite existing targets: "
            + ", ".join(existing)
        )

    source_contract = verify_source_contract(source_root)
    frame, mapping, data_provenance = load_main_frame(source_input)
    weights, spatial_provenance = load_spatial_weights(source_input, mapping)
    dictionary = build_dictionary(frame)
    description = build_data_description()
    benchmark_root.mkdir(parents=True, exist_ok=True)
    private_reference_root.mkdir(parents=True, exist_ok=True)

    with (
        tempfile.TemporaryDirectory(
            dir=benchmark_root, prefix=".case004-build-"
        ) as temporary,
        tempfile.TemporaryDirectory(
            dir=private_reference_root, prefix=".case004-private-build-"
        ) as private_temporary,
    ):
        stage_root = Path(temporary)
        private_stage = Path(private_temporary) / PRIVATE_CASE_ID
        staged_cases = [stage_root / target.name for target in targets]
        for root in staged_cases:
            (root / "01_model_input").mkdir(parents=True)

        discovery_input = staged_cases[0] / "01_model_input"
        main_csv = discovery_input / "main_data.csv"
        frame.to_csv(
            main_csv,
            index=False,
            float_format="%.17g",
            lineterminator="\n",
            na_rep="",
        )
        dictionary.to_csv(
            discovery_input / "data_dictionary.csv",
            index=False,
            lineterminator="\n",
            na_rep="",
        )
        (discovery_input / "data_description.md").write_text(
            description, encoding="utf-8"
        )
        mapping.to_csv(
            discovery_input / "city_mapping.csv",
            index=False,
            lineterminator="\n",
        )
        weight_frame = pd.DataFrame(
            weights,
            columns=[str(value) for value in mapping["city_id"].tolist()],
        )
        weight_frame.insert(0, "spatial_id", mapping["city_id"].tolist())
        weight_frame.to_csv(
            discovery_input / "spatial_weights.csv",
            index=False,
            float_format="%.17g",
            lineterminator="\n",
        )

        data_roundtrip = verify_round_trip(frame, main_csv)
        spatial_roundtrip = verify_spatial_csv(
            discovery_input / "spatial_weights.csv", mapping
        )
        metadata = {
            "asset_status": "deterministically_derived_from_frozen_source_asset",
            "matrix_scope": "284 city-level units in main_data.csv",
            "row_column_identifier": "city_id",
            "row_column_labels_identical": True,
            "diagonal_zero": True,
            "row_standardized": True,
            "weight_formula": spatial_provenance["normalization"],
            "city_mapping_file": "city_mapping.csv",
            "city_mapping_sha256": sha256(discovery_input / "city_mapping.csv"),
            "source_main_data_sha256": sha256(source_input / "data3.dta"),
            **public_spatial_provenance(spatial_provenance),
            "spatial_weights_sha256": spatial_roundtrip["csv_sha256"],
            "row_sum_max_error": spatial_roundtrip["row_sum_max_error"],
            "scientific_boundaries": {
                "causal_claims": "not_supported",
                "source_row_identity": "pending_independent_documentation",
                "independent_reproduction": "trusted_independent_spatial_panel_reproducer_not_available",
                "alternative_weight": "semantics_not_defined_not_packaged",
                "regional_heterogeneity": "unsupported_missing_required_weights",
            },
        }
        write_json(discovery_input / "spatial_weights_metadata.json", metadata)
        (discovery_input / "evidence_bundle.md").write_text(
            build_evidence_bundle(aligned=False), encoding="utf-8"
        )

        aligned_input = staged_cases[1] / "01_model_input"
        for name in SHARED_VISIBLE_FILES:
            shutil.copyfile(discovery_input / name, aligned_input / name)
        (aligned_input / "evidence_bundle.md").write_text(
            build_evidence_bundle(aligned=True), encoding="utf-8"
        )

        data_sha256 = sha256(main_csv)
        data_size = main_csv.stat().st_size
        weights_path = discovery_input / "spatial_weights.csv"
        weights_sha256 = sha256(weights_path)
        weights_size = weights_path.stat().st_size
        case_specs = [
            (
                DISCOVERY_CASE_ID,
                "discovery_blind",
                "strict_blind",
                False,
                REPRODUCTION_CASE_ID,
            ),
            (
                REPRODUCTION_CASE_ID,
                "reproduction_aligned",
                "reproduction_aligned",
                True,
                DISCOVERY_CASE_ID,
            ),
        ]
        for root, (case_id, input_view, track, aligned, counterpart) in zip(
            staged_cases, case_specs
        ):
            input_dir = root / "01_model_input"
            profile = build_case_profile(
                case_id=case_id,
                data_sha256=data_sha256,
                data_size=data_size,
                weights_sha256=weights_sha256,
                weights_size=weights_size,
                aligned=aligned,
            )
            write_json(input_dir / "case_profile.json", profile)
            (input_dir / "case_profile.md").write_text(
                profile_markdown(profile, aligned=aligned), encoding="utf-8"
            )
            write_json(
                root / "agent_laboratory_config_v2.json",
                agent_laboratory_config(
                    case_id=case_id,
                    input_view=input_view,
                    benchmark_track=track,
                ),
            )
            write_json(
                root / "roundtrip_validation.json",
                {
                    "case_id": case_id,
                    "source_format": "Stata .dta source assets",
                    "target_format": "UTF-8 CSV",
                    "main_data": data_roundtrip,
                    "spatial_weights": spatial_roundtrip,
                    "city_mapping_status": "passed_exact_set_and_explicit_reindex",
                    "source_row_identity_status": "pending_independent_documentation",
                    "coefficients_estimated": False,
                },
            )

        discovery_blinding = verify_discovery_blinding(discovery_input)
        for root, (case_id, input_view, track, _aligned, counterpart) in zip(
            staged_cases, case_specs
        ):
            manifest = build_manifest(
                case_root=root,
                case_id=case_id,
                input_view=input_view,
                benchmark_track=track,
                counterpart_case_id=counterpart,
                source_root=source_root,
                data_provenance=data_provenance,
                data_roundtrip=data_roundtrip,
                spatial_roundtrip=spatial_roundtrip,
                spatial_provenance=spatial_provenance,
                discovery_blinding=(
                    discovery_blinding if input_view == "discovery_blind" else None
                ),
            )
            write_json(root / "case_manifest.json", manifest)

        build_private_reference(
            private_case_root=private_stage,
            source_root=source_root,
            source_contract=source_contract,
            data_provenance=data_provenance,
            spatial_provenance=spatial_provenance,
        )

        for name in SHARED_VISIBLE_FILES:
            if (staged_cases[0] / "01_model_input" / name).read_bytes() != (
                staged_cases[1] / "01_model_input" / name
            ).read_bytes():
                raise ValueError(f"shared visible asset differs across views: {name}")
        ensure_no_prohibited_files(staged_cases)
        for staged, target in zip(staged_cases, targets):
            staged.replace(target)
        private_stage.replace(private_target)

    return validate_case_roots(
        case_roots=targets,
        private_case_root=private_target,
        source_frame=frame,
        source_mapping=mapping,
        source_weights=weights,
        source_contract=source_contract,
        data_provenance=data_provenance,
        spatial_provenance=spatial_provenance,
    )


def validate_existing(
    source_root: Path,
    benchmark_root: Path,
    private_reference_root: Path,
) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    source_contract = verify_source_contract(source_root)
    frame, mapping, data_provenance = load_main_frame(source_input)
    weights, spatial_provenance = load_spatial_weights(source_input, mapping)
    targets = [
        benchmark_root / DISCOVERY_CASE_ID,
        benchmark_root / REPRODUCTION_CASE_ID,
    ]
    return validate_case_roots(
        case_roots=targets,
        private_case_root=private_reference_root / PRIVATE_CASE_ID,
        source_frame=frame,
        source_mapping=mapping,
        source_weights=weights,
        source_contract=source_contract,
        data_provenance=data_provenance,
        spatial_provenance=spatial_provenance,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build or validate two non-destructive Case004 quasi-holdout "
            "candidate views."
        )
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT)
    parser.add_argument(
        "--private-reference-root",
        type=Path,
        default=DEFAULT_PRIVATE_REFERENCE_ROOT,
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate existing Case004 views without modifying them.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    source_root = args.source_root.resolve()
    benchmark_root = args.benchmark_root.resolve()
    private_reference_root = args.private_reference_root.resolve()
    result = (
        validate_existing(
            source_root, benchmark_root, private_reference_root
        )
        if args.validate_only
        else build_cases(
            source_root, benchmark_root, private_reference_root
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
