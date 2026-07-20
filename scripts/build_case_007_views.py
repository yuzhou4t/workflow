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
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT.parent / "benchmark-cases"
DEFAULT_SOURCE_ROOT = (
    Path.home() / "Downloads" / "case_007_数字经济绿色金融与经济韧性_FINAL"
)
DISCOVERY_CASE_ID = (
    "case_007_digital_green_finance_resilience_discovery_blind"
)
REPRODUCTION_CASE_ID = (
    "case_007_digital_green_finance_resilience_reproduction_aligned"
)
MAIN_COLUMNS = (
    "province_id",
    "year",
    "Resi",
    "D",
    "gf",
    "ind",
    "lngdp",
    "popdensity",
    "open",
    "std",
)
CONTROLS = ("ind", "lngdp", "popdensity", "open", "std")
SHARED_VISIBLE_FILES = (
    "main_data.csv",
    "data_dictionary.csv",
    "data_description.md",
    "province_mapping.csv",
    "spatial_weights.csv",
    "spatial_weights_metadata.json",
)
PROVINCE_MAPPING = (
    (1, "Beijing", "北京"),
    (2, "Tianjin", "天津"),
    (3, "Hebei", "河北"),
    (4, "Shanxi", "山西"),
    (5, "Inner Mongolia", "内蒙古"),
    (6, "Liaoning", "辽宁"),
    (7, "Jilin", "吉林"),
    (8, "Heilongjiang", "黑龙江"),
    (9, "Shanghai", "上海"),
    (10, "Jiangsu", "江苏"),
    (11, "Zhejiang", "浙江"),
    (12, "Anhui", "安徽"),
    (13, "Fujian", "福建"),
    (14, "Jiangxi", "江西"),
    (15, "Shandong", "山东"),
    (16, "Henan", "河南"),
    (17, "Hubei", "湖北"),
    (18, "Hunan", "湖南"),
    (19, "Guangdong", "广东"),
    (20, "Guangxi", "广西"),
    (21, "Hainan", "海南"),
    (22, "Chongqing", "重庆"),
    (23, "Sichuan", "四川"),
    (24, "Guizhou", "贵州"),
    (25, "Yunnan", "云南"),
    (27, "Shaanxi", "陕西"),
    (28, "Gansu", "甘肃"),
    (29, "Qinghai", "青海"),
    (30, "Ningxia", "宁夏"),
    (31, "Xinjiang", "新疆"),
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


def verify_public_contract(source_input: Path) -> dict[str, Any]:
    manifest_path = source_input / "public_case_manifest.json"
    specification_path = source_input / "model_specifications.jsonl"
    hypothesis_path = source_input / "hypothesis_cards.jsonl"
    lineage_path = source_input / "data_lineage_public.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("case_id") != "case_007":
        raise ValueError("case 007 public manifest case_id changed")
    if manifest.get("data_level") != "省份x年份":
        raise ValueError("case 007 public data level changed")
    if manifest.get("data_period") != "2011-2023":
        raise ValueError("case 007 public period changed")
    if manifest.get("model_types") != ["面板FE+空间杜宾模型(SDM)"]:
        raise ValueError("case 007 public model declaration changed")

    specifications = [
        json.loads(line)
        for line in specification_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(specifications) != 1:
        raise ValueError("case 007 public model specification changed")
    specification = specifications[0]
    expected = {
        "dv": "Resi(经济韧性)",
        "iv": "D(数字经济)+gf(绿色金融)",
        "controls": "ind,lngdp,popdensity,open,std",
        "fe": "省份+年份双向固定效应",
        "se": "not_reported",
        "model": "面板FE+空间杜宾模型(SDM)",
    }
    for key, value in expected.items():
        if specification.get(key) != value:
            raise ValueError(f"case 007 public model field changed: {key}")

    hypotheses = [
        json.loads(line)
        for line in hypothesis_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if [item.get("id") for item in hypotheses] != ["H1", "H2"]:
        raise ValueError("case 007 public hypotheses changed")
    if hypotheses[1].get("type") != "heterogeneity":
        raise ValueError("case 007 H2 is no longer the disclosed heterogeneity target")

    lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    if lineage.get("data_file") != "sorted.dta":
        raise ValueError("case 007 public lineage main file changed")
    if lineage.get("dimensions") != "390x81":
        raise ValueError("case 007 public lineage dimensions changed")

    return {
        "public_manifest_sha256": sha256(manifest_path),
        "public_specification_sha256": sha256(specification_path),
        "public_hypotheses_sha256": sha256(hypothesis_path),
        "public_lineage_sha256": sha256(lineage_path),
        "declared_model": "spatial_durbin_panel",
        "declared_fixed_effects": ["province_id", "year"],
        "declared_standard_errors": "not_reported",
        "declared_controls": list(CONTROLS),
        "heterogeneity_contract": "unsupported_by_visible_contract",
    }


def load_main_frame(source_input: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = pd.read_stata(source_input / "sorted.dta", convert_categoricals=False)
    if source.shape != (390, 81):
        raise ValueError(f"unexpected case 007 source shape: {source.shape}")
    required = [
        "city_id",
        "district",
        "year",
        "Resi",
        "D",
        "gf",
        "greenfinance",
        *CONTROLS,
    ]
    missing = [name for name in required if name not in source.columns]
    if missing:
        raise ValueError("case 007 source columns changed: " + ", ".join(missing))
    if not np.allclose(
        source["gf"].to_numpy(float),
        source["greenfinance"].to_numpy(float),
        rtol=0,
        atol=0,
        equal_nan=True,
    ):
        raise ValueError("greenfinance is no longer an exact duplicate of gf")

    mapping = (
        source[["city_id", "district"]]
        .drop_duplicates()
        .sort_values("city_id")
        .reset_index(drop=True)
    )
    expected_pairs = [(item[0], item[1]) for item in PROVINCE_MAPPING]
    observed_pairs = list(mapping.itertuples(index=False, name=None))
    if observed_pairs != expected_pairs:
        raise ValueError("case 007 public city_id/district mapping changed")

    frame = source[["city_id", "year", "Resi", "D", "gf", *CONTROLS]].rename(
        columns={"city_id": "province_id"}
    )
    if tuple(frame.columns) != MAIN_COLUMNS or frame.shape != (390, 10):
        raise ValueError("case 007 canonical projection changed")
    if frame.duplicated(["province_id", "year"]).any():
        raise ValueError("case 007 province_id + year key is not unique")
    if frame.isna().any().any():
        raise ValueError("case 007 canonical fields unexpectedly contain missing values")
    if sorted(frame["year"].unique().tolist()) != list(range(2011, 2024)):
        raise ValueError("case 007 year support changed")
    if not frame.groupby("province_id")["year"].nunique().eq(13).all():
        raise ValueError("case 007 must remain a balanced 30 x 13 panel")

    mapping_frame = pd.DataFrame(
        PROVINCE_MAPPING,
        columns=["province_id", "district_en", "matrix_label_zh"],
    )
    return frame, mapping_frame


def load_spatial_weights(
    source_input: Path,
    mapping: pd.DataFrame,
) -> tuple[np.ndarray, dict[str, Any]]:
    source_path = source_input / "eco.dta"
    source_frame = pd.read_stata(source_path, convert_categoricals=False)
    expected_labels = mapping["matrix_label_zh"].tolist()
    if source_frame.shape != (30, 30):
        raise ValueError("case 007 eco.dta is no longer 30 x 30")
    if list(source_frame.columns) != expected_labels:
        raise ValueError("case 007 eco.dta province-column order changed")

    source_matrix = source_frame.to_numpy(float)
    if not np.isfinite(source_matrix).all() or (source_matrix < 0).any():
        raise ValueError("case 007 eco.dta contains invalid weights")
    if not np.allclose(np.diag(source_matrix), 0, atol=1e-12):
        raise ValueError("case 007 eco.dta diagonal is not zero")
    off_diagonal = source_matrix.copy()
    np.fill_diagonal(off_diagonal, np.nan)
    if not (off_diagonal[~np.isnan(off_diagonal)] > 0).all():
        raise ValueError("case 007 eco.dta contains ambiguous off-diagonal zeros")
    row_sums = source_matrix.sum(axis=1)
    if not (row_sums > 0).all():
        raise ValueError("case 007 eco.dta contains an empty row")
    weights = source_matrix / row_sums[:, None]

    expanded_path = source_input / "ecoxt.dta"
    expanded = pd.read_stata(expanded_path, convert_categoricals=False).to_numpy(float)
    if expanded.shape != (390, 390):
        raise ValueError("case 007 ecoxt.dta is no longer 390 x 390")
    block_errors = []
    off_block_max = 0.0
    for block in range(13):
        start = block * 30
        stop = start + 30
        block_errors.append(float(np.max(np.abs(expanded[start:stop, start:stop] - weights))))
        if start:
            off_block_max = max(
                off_block_max,
                float(np.max(np.abs(expanded[start:stop, :start]))),
            )
        if stop < 390:
            off_block_max = max(
                off_block_max,
                float(np.max(np.abs(expanded[start:stop, stop:]))),
            )
    max_block_error = max(block_errors)
    if max_block_error > 5e-8 or off_block_max > 1e-12:
        raise ValueError("ecoxt.dta does not confirm the row-standardized eco.dta matrix")

    return weights, {
        "source_spatial_matrix_sha256": sha256(source_path),
        "expanded_matrix_sha256": sha256(expanded_path),
        "unresolved_alternative_matrix_sha256": sha256(source_input / "eco2.dta"),
        "source_shape": [30, 30],
        "source_diagonal_zero": True,
        "source_off_diagonal_strictly_positive": True,
        "source_row_sum_min": float(row_sums.min()),
        "source_row_sum_max": float(row_sums.max()),
        "normalization": "each eco.dta row divided by its visible 30-column row sum",
        "expanded_matrix_confirmation": {
            "shape": [390, 390],
            "block_count": 13,
            "max_abs_block_difference": max_block_error,
            "max_abs_off_block_value": off_block_max,
            "status": "passed",
        },
        "alternative_weight_status": (
            "unsupported_by_visible_contract: eco2.dta is distinct, but the public "
            "files do not define its semantics or a prespecified alternative-W test"
        ),
    }


def build_dictionary(frame: pd.DataFrame) -> pd.DataFrame:
    labels = {
        "province_id": "省份编码与空间单元标识",
        "year": "年份",
        "Resi": "经济韧性",
        "D": "数字经济",
        "gf": "绿色金融",
        "ind": "产业结构",
        "lngdp": "经济发展水平",
        "popdensity": "人口密度",
        "open": "对外开放度",
        "std": "可见数据中的 std 协变量",
    }
    roles = {
        "province_id": "id + spatial_id",
        "year": "time",
        "Resi": "outcome",
        "D": "co-primary exposure",
        "gf": "co-primary exposure",
        **{name: "control" for name in CONTROLS},
    }
    definitions = {
        "province_id": (
            "来源 city_id；同时用作面板实体键和空间矩阵行列标识。"
            "与中英文省份标签的冻结映射见 province_mapping.csv。"
        ),
        "year": "日历年份。",
        "Resi": "来源包已构造的经济韧性指标；底层指标构造流水线未在本视图复算。",
        "D": "来源包已构造的数字经济指标。",
        "gf": (
            "来源包已构造的绿色金融指标；原数据 greenfinance 与 gf "
            "逐值完全相同，本视图只保留 gf，避免重复构念入模。"
        ),
        "ind": "公开规格列出的产业结构协变量；精确单位未报告。",
        "lngdp": "公开规格列出的经济发展协变量；精确底数与价格口径未报告。",
        "popdensity": "公开规格列出的人口密度协变量；精确单位未报告。",
        "open": "公开规格列出的对外开放度协变量；精确构造式未报告。",
        "std": (
            "公开规格列出的 std 协变量；当前公开说明未给出足以"
            "安全扩展其经济语义的定义。"
        ),
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
                "unit": "省份编码" if name == "province_id" else ("年" if name == "year" else "源数据单位"),
                "source": "case 007 公开 sorted.dta 与公开规格",
                "missing_value_meaning": "无缺失",
                "available_years": "2011—2023",
                "processing_status": "仅列投影与重复列删除，数值未改写",
                "missing_count": int(frame[name].isna().sum()),
                "missing_rate": float(frame[name].isna().mean()),
            }
        )
    return pd.DataFrame(rows)


def build_data_description() -> str:
    return """# 中立数据说明

`main_data.csv` 是 30 个省级空间单元在 2011—2023 年的完整平衡面板，共 390 行、10 个字段。`province_id + year` 唯一，当前主表无缺失。

- `Resi` 是经济韧性结果指标，`D` 和 `gf` 是共同核心暴露，`ind`、`lngdp`、`popdensity`、`open`、`std` 是公开规格列出的协变量。
- 原表 `greenfinance` 与 `gf` 逐值完全相同，因此只保留 `gf`，防止同一构念重复入模。其余非冻结主表字段也未纳入。
- `province_mapping.csv` 冻结了公开 `city_id/district` 与公开空间矩阵中文列标签的一一对应。
- `spatial_weights.csv` 由公开 `eco.dta` 的 30×30 非负矩阵确定性按行标准化而来；行列标签完全一致、对角线为 0、每行和为 1。该矩阵可由公开 `ecoxt.dta` 的 13 个重复块交叉核验。
- 公开材料不提供标准误策略，也没有为异质性 H2 冻结分组变量、分组阈值或多重检验规则。
- 当前可见资产不提供外生识别；所有结论上限是空间条件关联，不得写成因果效应。

本说明不包含系数、方向、显著性、原论文文字、作者代码、运行结果或隐藏参考。
"""


def build_evidence_bundle(*, aligned: bool) -> str:
    common = """# 冻结证据与评测边界

- 本案例是 `seen/results-blind validation_spatial`，不是私有 holdout，不能单独支撑通用 AI Scientist 能力结论。
- 两个视图共用字节完全一致的主数据、字典、中立说明、省份映射和行标准化空间权重。
- `D` 和 `gf` 是共同核心暴露；不预设两者系数、直接、间接或总关联的方向。
- 公开规格的标准误为 `not_reported`；benchmark-owned 执行器会独立复算 SDM 点估计和直接/间接/总效应，但当前条件协方差不具备主张准入资格。必须将空间面板推断和替代 W 灵敏性的缺口与数值估计分开报告。
- 异质性 H2 缺少预先指定的子样本、切分规则和多重检验契约，状态固定为 `unsupported_by_visible_contract`。
- 显著、不显著、反向、不稳定、推断失败和无可接纳主张都是合法终点。
"""
    if not aligned:
        return common + """

## 自主发现视图

本视图只给出空间面板资产、共同核心暴露和需要区分本地、跨地区与总关联的研究目标，不公开主方法名称。系统必须在读取结果前自主冻结可执行方法、双向面板结构的处理、推断策略、诊断、停止条件和主张降级规则。
"""
    return common + """

## 方法对齐视图

可执行的主方法冻结为省份和年份双向固定效应的空间杜宾模型（SDM）。结果为 `Resi`，`D` 和 `gf` 是共同核心暴露，`ind`、`lngdp`、`popdensity`、`open`、`std` 是控制变量；空间模型同时包含结果的空间滞后和全部解释变量的空间滞后。主报告对 `D` 与 `gf` 分别给出直接、间接和总关联。

主 W 是 `spatial_weights.csv`，以 `province_id` 为行列标识、对角线为 0 且已按行标准化。它由公开 `eco.dta` 确定性生成，并由公开 `ecoxt.dta` 的 13 个空间块交叉核验。

benchmark evaluator 使用与系统运行隔离的 profile-QML SDM 实现，对点估计及直接、间接和总效应作独立复算；该复算不向系统提供结果。当前仍未冻结可辩护的空间面板标准误或重抽样方案，复算的条件协方差不具备主张准入资格。公开 `eco2.dta` 与主 W 不同，但可见文件未定义它的语义或替代 W 检验规则，因此不得自由切换。在推断和替代 W 缺口补齐前，数值执行成功不等于科学主张可被接纳。
"""


def variable_specs() -> list[dict[str, Any]]:
    labels = {
        "province_id": "省份编码与空间单元标识",
        "year": "年份",
        "Resi": "经济韧性",
        "D": "数字经济",
        "gf": "绿色金融",
        "ind": "产业结构",
        "lngdp": "经济发展水平",
        "popdensity": "人口密度",
        "open": "对外开放度",
        "std": "std 协变量",
    }
    roles = {
        "year": "time",
        "Resi": "outcome",
        "D": "exposure",
        "gf": "exposure",
        **{name: "control" for name in CONTROLS},
    }
    specs = [
        {
            "name": "province_id",
            "label": labels["province_id"],
            "role": "id",
            "definition": "见 data_dictionary.csv；该字段同时承担面板实体键。",
            "source": "case 007 public sorted.dta",
        },
        {
            "name": "province_id",
            "label": labels["province_id"],
            "role": "spatial_id",
            "definition": "见 province_mapping.csv；该字段同时对齐 spatial_weights.csv 行列。",
            "source": "case 007 public sorted.dta and eco.dta",
        },
    ]
    for name in MAIN_COLUMNS[1:]:
        specs.append(
            {
                "name": name,
                "label": labels[name],
                "role": roles[name],
                "definition": f"见 data_dictionary.csv 中 {name} 的冻结定义。",
                "source": "case 007 public sorted.dta and public specification",
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
    if aligned:
        title = "数字经济、绿色金融与经济韧性：SDM 对齐验证"
        track = "reproduction_aligned"
        constraints = [
            "冻结主方法为省份和年份双向固定效应的空间杜宾模型（SDM）。",
            "Resi 是结果，D 和 gf 是共同核心暴露；主模型包含 W·Resi 与 D、gf 及五个控制变量的空间滞后。",
            "主 W 冻结为公开 eco.dta 按可见 30 列行和标准化后的 spatial_weights.csv。",
            "分别报告 D 和 gf 的直接、间接和总关联；方向为 unspecified。",
            "公开规格未报告标准误；空间面板推断缺口必须阻断显著性主张。",
            "缺少与主执行器独立的可信 SDM 复算以及预注册的替代 W；不得把同一实现的重跑当作独立复算。",
            "H2 异质性状态为 unsupported_by_visible_contract；不得自由选子样本。",
            "只允许空间条件关联表述，不得使用导致、政策效应或因果影响表述。",
        ]
    else:
        title = "数字经济、绿色金融与经济韧性：空间方法自主路由验证"
        track = "strict_blind"
        constraints = [
            "不公开主方法名称；必须在读取结果前根据空间面板资产和估计目标冻结方法。",
            "Resi 是结果，D 和 gf 是共同核心暴露，ind、lngdp、popdensity、open、std 因公开 schema 预标记为控制变量。",
            "必须区分本地直接、跨地区间接与总关联，不得用一个非空间系数冒充三类目标。",
            "必须冻结省份和年份面板结构的处理、空间反馈来源、解释变量空间项、推断策略和停止规则。",
            "必须将空间推断、真独立复算和替代 W 灵敏性作为主张接纳条件，缺失时降级或拒绝主张。",
            "H2 异质性缺少可见分组契约，状态为 unsupported_by_visible_contract。",
            "只允许空间条件关联表述，无外生识别不得越界为因果。",
        ]
    diagnostics = [
        "核验 30×13 平衡面板、province_id + year 唯一键、完整样本和变量类型",
        "核验 province_id 与 spatial_weights.csv 行列标签一一对齐、对角线为 0 且行和为 1",
        "报告空间反馈参数的可用性与 D/gf 直接、间接、总关联分解",
        "显式报告空间面板推断缺口，不得仅凭默认大样本 p 值接纳主张",
        "要求与主执行器独立的复算；无独立实现时将其记为未通过而非伪装复现",
        "将替代 W 灵敏性记为当前未预注册，不得自由试验 eco2.dta",
        "将 H2 明确记为 unsupported_by_visible_contract",
    ]
    target_estimands = [
        "D 与 Resi 的本地直接、跨地区间接和总空间条件关联",
        "gf 与 Resi 的本地直接、跨地区间接和总空间条件关联",
    ]
    return {
        "case_id": case_id,
        "title": title,
        "research_question": (
            "在 2011—2023 年 30 省级空间面板中，数字经济 D 和绿色金融 gf "
            "与经济韧性 Resi 的本地、跨地区和总空间条件关联是否可稳健复算？"
        ),
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "statement": (
                    "在冻结的空间面板、权重与协变量条件下，D 和 gf "
                    "与 Resi 存在可区分为直接、间接和总部分的空间条件关联。"
                ),
                "expected_direction": "unspecified",
                "mechanism": "不预设方向或因果机制；空间关联不等于空间因果溢出。",
            },
            {
                "hypothesis_id": "H2",
                "statement": "D 和 gf 与 Resi 的空间条件关联可能在子样本间存在异质性。",
                "expected_direction": "heterogeneous",
                "mechanism": "当前可见合同未冻结分组变量与切分规则，因此 H2 不可执行。",
            },
        ],
        "unit_of_analysis": "省份—年度",
        "sample_period": "2011—2023",
        "data_structure_hint": "spatial_panel",
        "variables": variable_specs(),
        "dataset_refs": [
            {
                "dataset_id": "case_007_neutral_main_data",
                "role": "main",
                "filename": "main_data.csv",
                "mime_type": "text/csv",
                "sha256": data_sha256,
                "size_bytes": data_size,
            },
            {
                "dataset_id": "case_007_frozen_spatial_weights",
                "role": "supplementary",
                "filename": "spatial_weights.csv",
                "mime_type": "text/csv",
                "sha256": weights_sha256,
                "size_bytes": weights_size,
            },
        ],
        "design_envelope": {
            "benchmark_track": track,
            "research_goal": "associational",
            "target_estimands": target_estimands,
            "design_constraints": constraints,
            "required_diagnostics": diagnostics,
            "allowed_claim_strength": "associational",
        },
        "known_policy_facts": [],
        "constraints": [
            "main_data.csv 有 390 行、10 列、30 个省级空间单元和 13 个年份，province_id + year 唯一且无缺失。",
            "本案例是 seen/results-blind validation_spatial，不是私有 holdout。",
            "两视图共用字节完全一致的主数据、字典、中立说明、省份映射和空间权重。",
            "禁止读取或复制原论文、作者代码、结果、日志、run_baseline 或 02_hidden_reference。",
            "发现无可接纳主张是合法科学终点。",
        ],
    }


def profile_markdown(profile: dict[str, Any], *, aligned: bool) -> str:
    envelope = profile["design_envelope"]
    constraints = "\n".join(f"- {item}" for item in envelope["design_constraints"])
    diagnostics = "\n".join(f"- {item}" for item in envelope["required_diagnostics"])
    note = (
        "冻结 SDM、双向固定效应、D/gf 共同核心暴露与主 W。"
        if aligned
        else "公开空间资产和估计目标，不公开主方法名称。"
    )
    return f"""# {profile['title']}

## 轨道与评测资格

- 轨道：`{envelope['benchmark_track']}`
- 资格：`seen/results-blind validation_spatial`，不是私有 holdout。
- {note}

## 研究问题与假设

{profile['research_question']}

- H1：{profile['hypotheses'][0]['statement']}
- H2：{profile['hypotheses'][1]['statement']} 当前状态为 `unsupported_by_visible_contract`。

H1 方向为 `unspecified`，主张上限是空间条件关联。

## 结果不可见时冻结的设计边界

{constraints}

## 必须报告的诊断

{diagnostics}

## 共同数据事实

- 30 个省级空间单元、2011—2023 年、390 行的完整平衡面板。
- `province_id + year` 唯一，当前主表无缺失。
- 空间矩阵 30×30，行列标签一致、对角线为 0、行和为 1。
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
                    "province_mapping": "province_mapping.csv",
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
            "output_dir": "../../benchmark-results/agent-laboratory-v2",
            "upstream_repo_root": "../../Agent Laboratory",
            "execution_timeout_seconds": 600,
            "max_steps": 5,
            "max_llm_calls": 40,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
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
    labels = [str(value) for value in mapping["province_id"].tolist()]
    if restored.columns[0] != "spatial_id":
        raise ValueError("spatial_weights.csv first column changed")
    if [str(value) for value in restored.columns[1:]] != labels:
        raise ValueError("spatial_weights.csv column labels changed")
    if restored.iloc[:, 0].astype(str).tolist() != labels:
        raise ValueError("spatial_weights.csv row labels changed")
    matrix = restored.iloc[:, 1:].to_numpy(float)
    if matrix.shape != (30, 30):
        raise ValueError("spatial_weights.csv shape changed")
    if not np.allclose(np.diag(matrix), 0, atol=1e-12):
        raise ValueError("spatial_weights.csv diagonal changed")
    row_sum_error = float(np.max(np.abs(matrix.sum(axis=1) - 1)))
    if row_sum_error > 1e-8:
        raise ValueError("spatial_weights.csv is not row standardized")
    return {
        "status": "passed",
        "shape": [30, 30],
        "row_column_labels_identical": True,
        "diagonal_zero": True,
        "nonnegative": bool((matrix >= 0).all()),
        "row_sum_max_error": row_sum_error,
        "csv_sha256": sha256(path),
    }


def manifest_entry(path: Path, case_root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(case_root)),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def build_manifest(
    *,
    case_root: Path,
    case_id: str,
    input_view: str,
    benchmark_track: str,
    counterpart_case_id: str,
    source_input: Path,
    public_contract: dict[str, Any],
    data_roundtrip: dict[str, Any],
    spatial_roundtrip: dict[str, Any],
    spatial_provenance: dict[str, Any],
) -> dict[str, Any]:
    input_dir = case_root / "01_model_input"
    visible_files = sorted(path for path in input_dir.iterdir() if path.is_file())
    return {
        "manifest_version": 2,
        "case_id": case_id,
        "input_view": input_view,
        "benchmark_track": benchmark_track,
        "evaluation_class": "seen_results_blind_validation_spatial",
        "primary_ai_scientist_claim_eligible": False,
        "benchmark_contract": "same_neutral_data_two_method_information_views",
        "research_target": "spatial_associational",
        "hypothesis_direction": "unspecified",
        "visible_input": {
            "directory": "01_model_input",
            "row_count": 390,
            "column_count": 10,
            "entity_count": 30,
            "time_period_count": 13,
            "spatial_weight_shape": [30, 30],
            "files": [manifest_entry(path, case_root) for path in visible_files],
        },
        "shared_visible_asset_contract": {
            "counterpart_case_id": counterpart_case_id,
            "required_byte_identical_files": list(SHARED_VISIBLE_FILES),
            "sha256": {name: sha256(input_dir / name) for name in SHARED_VISIBLE_FILES},
        },
        "scope_disclosures": {
            "two_co_primary_exposures": ["D", "gf"],
            "known_covariates_prelabelled_controls": list(CONTROLS),
            "discovery_control_selection_fully_autonomous": False,
            "heterogeneity_h2": "unsupported_by_visible_contract",
            "standard_error_source_declaration": "not_reported",
            "spatial_inference_contract": "not_frozen_claim_admission_gap",
            "independent_spatial_reproducer": "not_available_claim_admission_gap",
            "alternative_weight_sensitivity": "not_prespecified_claim_admission_gap",
            "causal_identification": "not_available",
        },
        "hidden_reference": {
            "copied_into_case": False,
            "accessed_during_build": False,
            "allowed_during_system_run": False,
            "paper_code_results_logs_or_run_baseline_accessed": False,
        },
        "source_provenance": {
            "source_sorted_dta_sha256": sha256(source_input / "sorted.dta"),
            **public_contract,
            "neutralization": {
                "column_projection": list(MAIN_COLUMNS),
                "column_renames": {"city_id": "province_id"},
                "exact_duplicate_removed": {"greenfinance": "gf"},
                "data_values_changed": False,
                "coefficients_estimated_during_build": False,
                "paper_code_results_logs_hidden_or_run_baseline_copied": False,
            },
            "province_mapping": {
                "source_fields": ["sorted.dta:city_id", "sorted.dta:district", "eco.dta:column_labels"],
                "mapping_sha256": sha256(input_dir / "province_mapping.csv"),
                "status": "passed_exact_public_label_contract",
            },
            "spatial_weights": spatial_provenance,
            "csv_round_trip": data_roundtrip,
            "spatial_csv_validation": spatial_roundtrip,
        },
        "agent_laboratory_v2_budget": {
            "provider_attempt_ceiling": 40,
            "max_steps": 5,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
            "execution_timeout_seconds": 600,
        },
    }


def ensure_no_prohibited_files(case_roots: list[Path]) -> None:
    prohibited_suffixes = {".pdf", ".doc", ".docx", ".py", ".ipynb", ".log", ".dta"}
    prohibited_names = {"02_hidden_reference", "run_baseline.py", "original_paper.pdf"}
    for case_root in case_roots:
        for path in case_root.rglob("*"):
            if path.name in prohibited_names:
                raise ValueError(f"prohibited case asset created: {path}")
            if path.is_file() and path.suffix.lower() in prohibited_suffixes:
                raise ValueError(f"prohibited file type created: {path}")


def build_cases(source_root: Path, benchmark_root: Path) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    if not source_input.is_dir():
        raise FileNotFoundError(f"invalid case 007 source package: {source_root}")
    targets = [
        benchmark_root / DISCOVERY_CASE_ID,
        benchmark_root / REPRODUCTION_CASE_ID,
    ]
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise FileExistsError(
            "non-destructive builder refuses to overwrite existing targets: "
            + ", ".join(existing)
        )

    public_contract = verify_public_contract(source_input)
    frame, mapping = load_main_frame(source_input)
    weights, spatial_provenance = load_spatial_weights(source_input, mapping)
    dictionary = build_dictionary(frame)
    description = build_data_description()
    benchmark_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=benchmark_root, prefix=".case007-build-") as temporary:
        stage_root = Path(temporary)
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
        (discovery_input / "data_description.md").write_text(description, encoding="utf-8")
        mapping.to_csv(
            discovery_input / "province_mapping.csv",
            index=False,
            lineterminator="\n",
        )
        weight_frame = pd.DataFrame(
            weights,
            columns=[str(value) for value in mapping["province_id"].tolist()],
        )
        weight_frame.insert(0, "spatial_id", mapping["province_id"].tolist())
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
            "asset_status": "deterministically_derived_from_visible_public_assets",
            "matrix_scope": "30 province-level units in main_data.csv",
            "row_column_identifier": "province_id",
            "row_column_labels_identical": True,
            "diagonal_zero": True,
            "row_standardized": True,
            "weight_formula": spatial_provenance["normalization"],
            "province_mapping_file": "province_mapping.csv",
            "province_mapping_sha256": sha256(discovery_input / "province_mapping.csv"),
            "source_main_data_sha256": sha256(source_input / "sorted.dta"),
            **spatial_provenance,
            "spatial_weights_sha256": spatial_roundtrip["csv_sha256"],
            "row_sum_max_error": spatial_roundtrip["row_sum_max_error"],
            "scientific_boundaries": {
                "causal_claims": "not_supported",
                "spatial_inference": "not_frozen_in_visible_public_contract",
                "independent_reproduction": "benchmark_owned_profile_qml_point_estimate_recompute",
                "recompute_inference_status": "conditional_covariance_not_claim_admissible",
                "alternative_weight": "eco2.dta_semantics_and_test_rule_not_defined",
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
                    "source_format": "Stata .dta public input assets",
                    "target_format": "UTF-8 CSV",
                    "main_data": data_roundtrip,
                    "spatial_weights": spatial_roundtrip,
                    "province_mapping_status": "passed_exact_public_label_contract",
                    "coefficients_estimated": False,
                },
            )
            (root / "README.md").write_text(
                f"""# {case_id}

这是 Case007 的 `{input_view}` seen/results-blind `validation_spatial` 视图。

- 仅 `01_model_input` 可作为模型输入。
- 本目录不包含论文、代码、结果、日志、run_baseline 或 hidden reference。
- 主张上限是空间条件关联，方向为 `unspecified`。
- H2 异质性是 `unsupported_by_visible_contract`。
- benchmark-owned profile-QML 会独立复算点估计与空间效应；空间推断和替代 W 灵敏性仍是显式待补缺口。
- Agent Laboratory v2 预算冻结为 `40/5/1/0`，单次代码执行超时 600 秒。
""",
                encoding="utf-8",
            )
            manifest = build_manifest(
                case_root=root,
                case_id=case_id,
                input_view=input_view,
                benchmark_track=track,
                counterpart_case_id=counterpart,
                source_input=source_input,
                public_contract=public_contract,
                data_roundtrip=data_roundtrip,
                spatial_roundtrip=spatial_roundtrip,
                spatial_provenance=spatial_provenance,
            )
            write_json(root / "case_manifest.json", manifest)

        for name in SHARED_VISIBLE_FILES:
            if (
                staged_cases[0] / "01_model_input" / name
            ).read_bytes() != (
                staged_cases[1] / "01_model_input" / name
            ).read_bytes():
                raise ValueError(f"shared visible asset differs across views: {name}")
        ensure_no_prohibited_files(staged_cases)
        for staged, target in zip(staged_cases, targets):
            staged.replace(target)

    return {
        "status": "passed",
        "evaluation_class": "seen_results_blind_validation_spatial",
        "case_roots": [str(path) for path in targets],
        "rows": 390,
        "columns": 10,
        "entities": 30,
        "periods": 13,
        "neutral_csv_sha256": sha256(targets[0] / "01_model_input" / "main_data.csv"),
        "spatial_weights_sha256": sha256(
            targets[0] / "01_model_input" / "spatial_weights.csv"
        ),
        "hidden_reference_accessed": False,
        "paper_code_results_logs_or_run_baseline_accessed": False,
        "prohibited_assets_copied": False,
        "data_roundtrip": data_roundtrip,
        "spatial_roundtrip": spatial_roundtrip,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build two non-destructive Case007 spatial validation views."
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = build_cases(
        source_root=args.source_root.resolve(),
        benchmark_root=args.benchmark_root.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
