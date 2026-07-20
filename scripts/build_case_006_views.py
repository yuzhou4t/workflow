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
    / "case_006_绿色金融与企业环境投资_FINAL_修正版"
)
DISCOVERY_CASE_ID = (
    "case_006_green_finance_environmental_investment_discovery_blind"
)
REPRODUCTION_CASE_ID = (
    "case_006_green_finance_environmental_investment_reproduction_aligned"
)
PRIVATE_CASE_ID = "case_006_green_finance_environmental_investment"
SOURCE_DATA_FILENAME = "data information.dta"
PUBLIC_LINEAGE_FILENAME = "data_information.dta"
OUTCOMES = ("EPI", "EPIE")
CONTROLS = ("Size", "Debt", "ROA", "Cost", "Cashflow", "FIXED", "TobinQ")
MAIN_COLUMNS = (
    "stkcd",
    "year",
    "province",
    "EPI",
    "EPIE",
    "did",
    "treat",
    "post",
    *CONTROLS,
    "event",
)
SOURCE_COLUMNS = (
    "stkcd",
    "year",
    "province",
    "EPI",
    "Size",
    "Debt",
    "ROA",
    "Cost",
    "treat",
    "post",
    "did",
    "Cashflow",
    "FIXED",
    "TobinQ",
    "GQ",
    "QY",
    "lnexp",
    "Lninve",
    "YF",
    "Patent",
    "Ifpatent",
    "Age",
    "Ifage",
    "EPIE",
    "Industry",
    "event",
    "eventz2",
    "eventz3",
    "eventz4",
    "eventz5",
    "eventz6",
    "eventz7",
    "eventz8",
    "eventz9",
    "eventz10",
    "eventz11",
)
SHARED_VISIBLE_FILES = (
    "main_data.csv",
    "data_dictionary.csv",
    "data_description.md",
)
SAFE_CASE_ROOT_ENTRIES = {
    "01_model_input",
    "agent_laboratory_config_v2.json",
    "case_manifest.json",
    "roundtrip_validation.json",
}
INITIAL_PILOT_PROVINCES = (
    "广东省",
    "贵州省",
    "江西省",
    "浙江省",
    "新疆维吾尔自治区",
)
LATER_PILOT_PROVINCE = "甘肃省"
EXPECTED_FIRST_EXPOSURE_YEAR = {
    **{province: 2018 for province in INITIAL_PILOT_PROVINCES},
    LATER_PILOT_PROVINCE: 2020,
}


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
    expected_manifest = {
        "case_id": "case_006",
        "data_level": "企业x年份",
        "data_period": "2010-2020",
        "data_source": "PLOS ONE S1 Data",
        "model_types": ["交错DID(Staggered DID)"],
    }
    for key, value in expected_manifest.items():
        if manifest.get(key) != value:
            raise ValueError(f"case 006 public manifest field changed: {key}")

    specifications = [
        json.loads(line)
        for line in specification_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(specifications) != 1:
        raise ValueError("case 006 public model specification changed")
    specification = specifications[0]
    expected_specification = {
        "dv": "EPI(环境投资规模)/EPIE(环境投资效率)",
        "iv": "did(treat*post,GFRI试点区DID交互项)",
        "controls": "Size,Debt,ROA,Cost,Cashflow,FIXED,TobinQ",
        "fe": "企业+年份双向固定效应",
        "se": "robust",
        "model": "交错DID(Staggered DID)",
    }
    for key, value in expected_specification.items():
        if specification.get(key) != value:
            raise ValueError(f"case 006 public model field changed: {key}")

    hypotheses = [
        json.loads(line)
        for line in hypothesis_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if [item.get("id") for item in hypotheses] != ["H1", "H2"]:
        raise ValueError("case 006 public hypotheses changed")
    if hypotheses[1].get("type") != "heterogeneity":
        raise ValueError("case 006 H2 is no longer the disclosed heterogeneity target")

    lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    if lineage.get("data_file") != PUBLIC_LINEAGE_FILENAME:
        raise ValueError("case 006 public lineage data filename changed")
    if lineage.get("dimensions") != "9009x36":
        raise ValueError("case 006 public lineage dimensions changed")

    return {
        "public_manifest_sha256": sha256(manifest_path),
        "public_specification_sha256": sha256(specification_path),
        "public_hypotheses_sha256": sha256(hypothesis_path),
        "public_lineage_sha256": sha256(lineage_path),
        "public_lineage_filename": PUBLIC_LINEAGE_FILENAME,
        "actual_visible_filename": SOURCE_DATA_FILENAME,
        "public_lineage_filename_matches_actual": False,
        "declared_model": "staggered_did",
        "declared_fixed_effects": ["stkcd", "year"],
        "declared_standard_errors": "robust",
        "declared_controls": list(CONTROLS),
        "heterogeneity_contract": "unsupported_by_visible_contract",
    }


def load_main_frame(source_input: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    source_path = source_input / SOURCE_DATA_FILENAME
    source = pd.read_stata(source_path, convert_categoricals=False)
    if source.shape != (9009, 36):
        raise ValueError(f"unexpected case 006 source shape: {source.shape}")
    if tuple(source.columns) != SOURCE_COLUMNS:
        raise ValueError("case 006 source column contract changed")

    frame = source[list(MAIN_COLUMNS)].copy()
    if frame.shape != (9009, 16):
        raise ValueError("case 006 canonical projection changed")
    if frame.duplicated(["stkcd", "year"]).any():
        raise ValueError("case 006 stkcd + year key is not unique")
    non_event_columns = [name for name in MAIN_COLUMNS if name != "event"]
    if frame[non_event_columns].isna().any().any():
        raise ValueError("case 006 canonical non-event fields contain missing values")
    if frame["stkcd"].nunique() != 819:
        raise ValueError("case 006 must contain 819 firms")
    if sorted(frame["year"].unique().tolist()) != list(range(2010, 2021)):
        raise ValueError("case 006 year support changed")
    if not frame.groupby("stkcd")["year"].nunique().eq(11).all():
        raise ValueError("case 006 must remain a balanced 819 x 11 panel")
    if not (frame["did"] == frame["treat"] * frame["post"]).all():
        raise ValueError("case 006 did is no longer exactly treat * post")
    if (frame.groupby(["province", "year"])["did"].nunique() > 1).any():
        raise ValueError("case 006 did is not a region-year exposure")
    if (frame.groupby(["province", "year"])["treat"].nunique() > 1).any():
        raise ValueError("case 006 treat is not uniquely defined by region-year")
    if (frame.groupby(["province", "year"])["post"].nunique() > 1).any():
        raise ValueError("case 006 post is not uniquely defined by region-year")

    first_exposure = (
        frame.loc[frame["did"].eq(1)]
        .groupby("province")["year"]
        .min()
        .sort_index()
        .to_dict()
    )
    if first_exposure != dict(sorted(EXPECTED_FIRST_EXPOSURE_YEAR.items())):
        raise ValueError(f"case 006 policy onset changed: {first_exposure}")
    province_change_firms = int(
        frame.groupby("stkcd")["province"].nunique().gt(1).sum()
    )
    treatment_status_change_firms = int(
        frame.groupby("stkcd")["treat"].nunique().gt(1).sum()
    )
    if province_change_firms != 27:
        raise ValueError("case 006 province-change count changed")
    if treatment_status_change_firms != 9:
        raise ValueError("case 006 treatment-status-change count changed")
    if not ((frame["treat"].eq(1)) == frame["event"].notna()).all():
        raise ValueError("case 006 event availability contract changed")
    gansu_2020_event = frame.loc[
        frame["province"].eq(LATER_PILOT_PROVINCE) & frame["year"].eq(2020),
        "event",
    ].unique()
    if len(gansu_2020_event) != 1 or float(gansu_2020_event[0]) != 3.0:
        raise ValueError("case 006 Gansu event-timing mismatch changed")

    exposure_facts = {
        "did_equals_treat_times_post": True,
        "did_constant_within_province_year": True,
        "first_exposure_year_by_province": first_exposure,
        "firms_with_any_province_change": province_change_firms,
        "firms_whose_treat_status_changes": treatment_status_change_firms,
        "firm_invariant_treatment_assumption_valid": False,
        "event_nonmissing_exactly_when_treat_equals_one": True,
        "gansu_2020_event_value": 3.0,
        "gansu_policy_onset_year": 2020,
        "event_matches_staggered_timing": False,
        "source_cost_mean": stable_float(frame["Cost"].mean()),
        "cost_rescaled_by_builder": False,
    }
    return frame, exposure_facts


def build_dictionary(frame: pd.DataFrame) -> pd.DataFrame:
    labels = {
        "stkcd": "企业证券代码",
        "year": "年份",
        "province": "当年注册地区",
        "EPI": "环境投资规模",
        "EPIE": "环境投资效率",
        "did": "GFRI 地区—年份政策暴露",
        "treat": "当年注册地是否属于试点地区",
        "post": "该地区当年是否进入政策后时段",
        "Size": "企业规模",
        "Debt": "负债特征",
        "ROA": "总资产收益率",
        "Cost": "Cost 控制变量",
        "Cashflow": "现金流",
        "FIXED": "固定资产特征",
        "TobinQ": "Tobin Q",
        "event": "源数据事件时间字段",
    }
    roles = {
        "stkcd": "id",
        "year": "time",
        "province": "time-varying geographic assignment",
        "EPI": "co-primary outcome",
        "EPIE": "co-primary outcome",
        "did": "region-year exposure",
        "treat": "exposure component",
        "post": "exposure component",
        **{name: "control" for name in CONTROLS},
        "event": "diagnostic-only event field",
    }
    definitions = {
        "stkcd": "源数据中的企业证券代码；作为面板实体键。",
        "year": "2010—2020 年日历年份。",
        "province": (
            "企业当年注册地区。该字段可随企业和时间变化，"
            "不得默认固化为企业永久属性。"
        ),
        "EPI": "源数据已构造的企业环境投资规模指标。",
        "EPIE": "源数据已构造的企业环境投资效率指标。",
        "did": (
            "源数据提供的地区—年份政策暴露，逐行等于 treat×post；"
            "不得重构为企业不变的处理组。"
        ),
        "treat": "当年注册地区是否属于六个可见试点地区。",
        "post": (
            "地区对应的政策后时段标记；首批地区从 2018 年起，"
            "甘肃从 2020 年起。未试点地区的 post 不会单独产生暴露。"
        ),
        "Size": "源数据提供的企业规模控制变量。",
        "Debt": "源数据提供的负债特征控制变量。",
        "ROA": "源数据提供的总资产收益率控制变量。",
        "Cost": (
            "主回归保留源数据原列和原尺度。已知冻结审计提示该列与论文"
            "描述统计存在约 100 倍尺度矛盾；在未解决前不得自动缩放。"
        ),
        "Cashflow": "源数据提供的现金流控制变量。",
        "FIXED": "源数据提供的固定资产特征控制变量。",
        "TobinQ": "源数据提供的 Tobin Q 控制变量。",
        "event": (
            "源数据中仅 treat=1 行非缺失的事件字段。它统一按日历年"
            "锚定，对 2020 年才暴露的甘肃队列并非正确的相对政策时间。"
        ),
    }
    rows: list[dict[str, Any]] = []
    for name in MAIN_COLUMNS:
        missing_count = int(frame[name].isna().sum())
        rows.append(
            {
                "dataset": "main_data.csv",
                "variable": name,
                "label_zh": labels[name],
                "role": roles[name],
                "storage_type": str(frame[name].dtype),
                "definition": definitions[name],
                "unit": (
                    "类别"
                    if name == "province"
                    else ("年" if name == "year" else "源数据单位")
                ),
                "source": "PLOS ONE S1 Data 公开案例包；本构建器仅投影可见数据",
                "missing_value_meaning": (
                    "当年注册地不在试点地区，不是随机数据缺失"
                    if name == "event"
                    else "无缺失"
                ),
                "available_years": "2010—2020",
                "processing_status": "原列投影，未缩放、填补或结果驱动改写",
                "missing_count": missing_count,
                "missing_rate": float(frame[name].isna().mean()),
                "notes": (
                    "EPI 与 EPIE 是同等权重的共同主结果。"
                    if name in OUTCOMES
                    else (
                        "尺度矛盾未解决；主规格使用原始列，尺度替代只能作敏感性分析。"
                        if name == "Cost"
                        else ""
                    )
                ),
            }
        )
    return pd.DataFrame(rows)


def build_data_description() -> str:
    return """# 中立数据说明

`main_data.csv` 是 819 家 A 股上市企业在 2010—2020 年的完整平衡面板，共 9,009 行、16 个投影字段。`stkcd + year` 唯一。除 `event` 的结构性缺失外，所有投影字段均无缺失。

- `EPI` 和 `EPIE` 分别是环境投资规模与环境投资效率，是同等权重的两个共同主结果；不允许根据显著性只选其一。
- `did` 是数据中已提供的地区—年份暴露，逐行等于 `treat * post`。首批五个地区从 2018 年暴露，甘肃从 2020 年暴露。
- `province` 是企业当年注册地。数据中 27 家企业的地区字段发生变化，其中 9 家跨越试点/非试点边界而导致 `treat` 状态变化。因此不得虚构 firm-invariant treatment。
- `Size`、`Debt`、`ROA`、`Cost`、`Cashflow`、`FIXED`、`TobinQ` 是公开规格指定的七个控制变量。这种预标记是可执行 schema 的边界，因此自主视图不测试完全自主的控制变量挑选。
- `Cost` 原始列与论文描述统计存在已知约 100 倍尺度矛盾。主回归保留原列，不进行隐式缩放；矛盾未解决前必须披露。
- 源 `event` 字段统一锚定在日历时间：甘肃 2020 年真实首次暴露时 `event=3`，因此它不是完全对齐交错政策时点的相对时间。事件研究必须从 `did`、`province` 和 `year` 重建队列时点，不得直接把 `event` 当作正确 staggered event time。

本说明不包含论文文字、回归结果、系数方向、显著性、作者代码或隐藏参考。
"""


def build_evidence_bundle(*, aligned: bool) -> str:
    common = """# 冻结证据与评测边界

- 案例评测资格为 `quasi_holdout_candidate_pending_freeze_audit`；冻结审计通过前不进入主分。
- 只能使用 `01_model_input` 中的可见资产；禁止读取原论文、结果、日志、`run_baseline.py` 或任何隐藏参考。
- `EPI` 和 `EPIE` 是同等权重的共同主结果，不得根据结果只报告其一。
- `did` 是已提供的地区—年份暴露；不得用企业首期或末期注册地将其改写为企业不变处理。
- 首批广东、贵州、江西、浙江和新疆从 2018 年暴露；甘肃从 2020 年暴露。
- 27 家企业的可见地区字段变化，其中 9 家的 `treat` 状态变化；必须披露这一暴露定义边界。
- `Cost` 主规格保留原始列。已知约 100 倍尺度矛盾必须披露；未预注册缩放不得替代主规格。
- 源 `event` 与甘肃 2020 队列时点不完全对齐；直接使用它的事件研究不能作为已通过的平行趋势证据。
- 显著、不显著、方向相反、识别诊断失败和无可接纳主张均是合法终点。
"""
    if not aligned:
        return common + """

## 自主发现视图

本视图不指定主估计量、固定效应、标准误、事件研究实现或主张强度。系统必须在读取任何回归结果前，根据地区—年份暴露、企业双向面板、注册地变化和交错时点冻结设计。如平行趋势、序列相关或交错处理异质性不能受到可信处理，必须主动降级为关联或拒绝因果主张。
"""
    return common + """

## 方法对齐视图

两个共同主模型分别冻结为：

- `EPI ~ did + Size + Debt + ROA + Cost + Cashflow + FIXED + TobinQ + stkcd FE + year FE`
- `EPIE ~ did + Size + Debt + ROA + Cost + Cashflow + FIXED + TobinQ + stkcd FE + year FE`

该规格使用数据中原始 `did` 和原始 `Cost`，不重构处理组，不根据结果改变控制变量。两个结果的权重相同。

系数复现与不确定性口径必须分开：

1. 首先复现相同双向固定效应设计下的 `did` 系数、样本量、企业数和模型内 R²。
2. `homoskedastic` 标准误只作算术对齐通道，不得冒充处理序列相关后的可信推断。
3. 公开可见规格声明的 `heteroskedastic robust` 必须单独报告。
4. 按 `stkcd` 实体聚类的标准误是必做的序列相关敏感性通道，不得写成作者原始报告口径。

事件研究不得直接沿用源 `event`。必须由各地区首次 `did=1` 的年份重建 cohort-relative time，并明确甘肃 2020 的队列边界。如无法完成对交错处理有效的识别与推断，最终主张必须降级。
"""


def variable_specs() -> list[dict[str, Any]]:
    labels = {
        "stkcd": "企业证券代码",
        "year": "年份",
        "province": "当年注册地区",
        "EPI": "环境投资规模",
        "EPIE": "环境投资效率",
        "did": "GFRI 地区—年份政策暴露",
        "treat": "当年试点地区标记",
        "post": "地区政策后时段标记",
        "Size": "企业规模",
        "Debt": "负债特征",
        "ROA": "总资产收益率",
        "Cost": "Cost 控制变量",
        "Cashflow": "现金流",
        "FIXED": "固定资产特征",
        "TobinQ": "Tobin Q",
        "event": "源数据事件字段",
    }
    roles = {
        "stkcd": "id",
        "year": "time",
        "province": "unknown",
        "EPI": "outcome",
        "EPIE": "outcome",
        "did": "exposure",
        "treat": "unknown",
        "post": "unknown",
        **{name: "control" for name in CONTROLS},
        "event": "unknown",
    }
    return [
        {
            "name": name,
            "label": labels[name],
            "role": roles[name],
            "definition": f"见 data_dictionary.csv 中 {name} 的冻结定义。",
            "source": "case 006 visible PLOS ONE S1 data projection",
        }
        for name in MAIN_COLUMNS
    ]


def build_case_profile(
    *,
    case_id: str,
    data_sha256: str,
    data_size: int,
    aligned: bool,
) -> dict[str, Any]:
    if aligned:
        title = "绿色金融与企业环境投资：双向固定效应对齐复现"
        track = "reproduction_aligned"
        constraints = [
            "分别对 EPI 和 EPIE 执行相同权重的企业和年份双向固定效应主回归。",
            "核心暴露使用原始 did，控制 Size、Debt、ROA、Cost、Cashflow、FIXED 和 TobinQ。",
            "不得把企业首期或末期地区改写为企业不变处理组；必须披露 9 家处理状态变化企业。",
            "Cost 保留原始列与原尺度；约 100 倍描述统计矛盾必须披露。",
            "分开报告系数复现、homoskedastic SE、heteroskedastic robust SE 和按 stkcd 实体聚类的灵敏性 SE。",
            "事件研究必须从地区首次暴露年份重建；源 event 不得直接用作交错时点。",
            "平行趋势、交错处理异质性或序列相关处理不充分时，必须降级主张。",
        ]
        diagnostics = [
            "核验 819×11 平衡面板、stkcd + year 唯一键与两个结果的完整样本",
            "核验 did 为地区—年份暴露，首批 2018 年、甘肃 2020 年",
            "披露 27 家地区变化企业及其中 9 家 treat 变化企业如何进入估计量",
            "对交错处理有效地评估政策前趋势、处理效应异质性和事件时点",
            "将源 event 与重建的 cohort-relative time 分开，披露甘肃不对齐",
            "分开报告系数、homoskedastic、heteroskedastic robust 与实体聚类推断",
            "对 EPI 与 EPIE 执行与主估计状态独立的双向固定效应复算",
            "披露 Cost 尺度矛盾，不把未冻结的缩放写成主规格",
            "H2 异质性因未冻结分组契约而记为 unsupported_by_visible_contract",
        ]
    else:
        title = "绿色金融与企业环境投资：自主方法路由"
        track = "strict_blind"
        constraints = [
            "在读取结果前，根据地区—年份暴露、交错时点和平衡企业面板自主冻结主方法。",
            "EPI 和 EPIE 是同等权重的共同主结果，不得事后择一。",
            "did 是已提供的地区—年份暴露；禁止虚构 firm-invariant treatment。",
            "七个公开控制变量因当前 schema 预标记；本视图不测试完全自主控制变量选择。",
            "必须处理注册地变化、序列相关、交错处理异质性与可信的政策前诊断。",
            "Cost 主规格保留原始尺度，并披露约 100 倍尺度矛盾。",
            "若因果识别条件不足，必须降级为关联或描述性结论。",
        ]
        diagnostics = [
            "核验 819×11 平衡面板、stkcd + year 唯一键与两个结果的完整样本",
            "核验 did 为地区—年份暴露，首批 2018 年、甘肃 2020 年",
            "披露 27 家地区变化企业及其中 9 家 treat 变化企业如何进入所选估计量",
            "在读取结果前冻结并执行适合交错时点的政策前诊断、处理异质性检查和停止规则",
            "将源 event 与重建的 cohort-relative time 分开，披露甘肃不对齐",
            "为所选主估计量冻结与面板依赖、序列相关和异方差相匹配的推断规则及敏感性分析",
            "对 EPI 与 EPIE 的所选主估计执行与原运行状态独立的复算",
            "披露 Cost 尺度矛盾，不把未冻结的缩放写成主规格",
            "H2 异质性因未冻结分组契约而记为 unsupported_by_visible_contract",
        ]
    return {
        "case_id": case_id,
        "title": title,
        "research_question": (
            "在 2010—2020 年 819 家 A 股上市企业面板中，GFRI 地区—年份"
            "暴露与企业环境投资规模 EPI 和环境投资效率 EPIE 的差异变化"
            "是否能在同等权重下得到可复算、经诊断校准的证据？"
        ),
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "statement": (
                    "GFRI 地区—年份暴露后，EPI 与 EPIE 的差异变化"
                    "在冻结设计下可被分别复算。"
                ),
                "expected_direction": "unspecified",
                "mechanism": "不预设系数方向；因果强度取决于识别诊断。",
            },
            {
                "hypothesis_id": "H2",
                "statement": "政策暴露与 EPI/EPIE 的差异变化可能在子样本间存在异质性。",
                "expected_direction": "heterogeneous",
                "mechanism": "当前可见契约未冻结分组变量和切分规则，因此 H2 不可执行。",
            },
        ],
        "unit_of_analysis": "企业—年度",
        "sample_period": "2010—2020",
        "data_structure_hint": "panel",
        "variables": variable_specs(),
        "dataset_refs": [
            {
                "dataset_id": "case_006_neutral_main_data",
                "role": "main",
                "filename": "main_data.csv",
                "mime_type": "text/csv",
                "sha256": data_sha256,
                "size_bytes": data_size,
            }
        ],
        "design_envelope": {
            "benchmark_track": track,
            "research_goal": "causal",
            "target_estimands": [
                "GFRI 地区—年份暴露对 EPI 的平均差异变化",
                "GFRI 地区—年份暴露对 EPIE 的平均差异变化",
            ],
            "design_constraints": constraints,
            "required_diagnostics": diagnostics,
            "allowed_claim_strength": "causal",
        },
        "known_policy_facts": [
            "可见数据中广东、贵州、江西、浙江和新疆首次 did=1 为 2018 年。",
            "可见数据中甘肃首次 did=1 为 2020 年。",
            "did 在每个 province—year 单元内一致，且逐行等于 treat×post。",
            "处理由企业当年注册地区决定；9 家企业因地区变化而跨越试点/非试点边界。",
        ],
        "constraints": [
            "main_data.csv 有 9,009 行、16 列、819 家企业和 11 个年份，stkcd + year 唯一。",
            "评测资格为 quasi_holdout_candidate_pending_freeze_audit；审计通过前不进入主分。",
            "两视图共用字节完全一致的主数据、字典和中立数据说明。",
            "禁止读取原论文、结果、日志、run_baseline.py 或 02_hidden_reference。",
            "无可接纳的因果主张是合法科学终点。",
        ],
    }


def profile_markdown(profile: dict[str, Any], *, aligned: bool) -> str:
    envelope = profile["design_envelope"]
    constraints = "\n".join(f"- {item}" for item in envelope["design_constraints"])
    diagnostics = "\n".join(f"- {item}" for item in envelope["required_diagnostics"])
    note = (
        "冻结两个同等权重的企业+年份 TWFE 复现，不同推断口径分开。"
        if aligned
        else "公开暴露与数据事实，不公开主估计量。"
    )
    return f"""# {profile['title']}

## 轨道与评测资格

- 轨道：`{envelope['benchmark_track']}`
- 资格：`quasi_holdout_candidate_pending_freeze_audit`，审计通过前不进入主分。
- {note}

## 研究问题与假设

{profile['research_question']}

- H1：{profile['hypotheses'][0]['statement']}
- H2：{profile['hypotheses'][1]['statement']} 当前状态为 `unsupported_by_visible_contract`。

两个主结果权重相同，系数方向为 `unspecified`。因果强度取决于识别诊断，不是默认许可。

## 结果不可见时冻结的设计边界

{constraints}

## 必须报告的诊断

{diagnostics}

## 共同数据事实

- 819 家企业、2010—2020 年、9,009 行的完整平衡面板。
- `did` 是地区—年份暴露；首批 2018 年，甘肃 2020 年。
- 27 家企业注册地变化，其中 9 家 `treat` 状态变化。
- `Cost` 保留原列并披露约 100 倍尺度矛盾。
- 源 `event` 对甘肃 staggered timing 不完全对齐。
- 不提供论文、作者代码、结果、日志或隐藏参考。
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
            "execution_timeout_seconds": 1_200,
            "max_steps": 10,
            "max_llm_calls": 80,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 3,
            "papersolver_max_steps": 5,
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


def two_way_demean(frame: pd.DataFrame, columns: list[str]) -> np.ndarray:
    values = frame[columns].astype(float)
    entity_mean = values.groupby(frame["stkcd"]).transform("mean")
    time_mean = values.groupby(frame["year"]).transform("mean")
    transformed = values - entity_mean - time_mean + values.mean()
    return transformed.to_numpy(float)


def stable_float(value: float) -> float:
    """Make benchmark-owned audit JSON stable across BLAS-level roundoff."""

    return float(f"{float(value):.12g}")


def build_twfe_freeze_audit(frame: pd.DataFrame) -> dict[str, Any]:
    x_names = ["did", *CONTROLS]
    x = two_way_demean(frame, x_names)
    xtx_inverse = np.linalg.inv(x.T @ x)
    nobs = len(frame)
    entity_count = int(frame["stkcd"].nunique())
    time_count = int(frame["year"].nunique())
    df_resid = nobs - len(x_names) - entity_count - time_count + 1
    if df_resid != 8172:
        raise ValueError("case 006 TWFE residual degrees of freedom changed")

    results: dict[str, Any] = {}
    for outcome in OUTCOMES:
        y = two_way_demean(frame, [outcome])[:, 0]
        coefficients = np.linalg.lstsq(x, y, rcond=None)[0]
        residuals = y - x @ coefficients
        scale = nobs / df_resid
        homoskedastic_covariance = (
            float(residuals @ residuals) / df_resid * xtx_inverse
        )
        robust_meat = x.T @ ((residuals * residuals)[:, None] * x)
        robust_covariance = xtx_inverse @ robust_meat @ xtx_inverse * scale
        cluster_meat = np.zeros((len(x_names), len(x_names)), dtype=float)
        for indices in frame.groupby("stkcd", sort=False).indices.values():
            score = x[indices].T @ residuals[indices]
            cluster_meat += np.outer(score, score)
        clustered_covariance = xtx_inverse @ cluster_meat @ xtx_inverse * scale
        total_sum_squares = float(y @ y)
        within_r_squared = 1.0 - float(residuals @ residuals) / total_sum_squares
        results[outcome] = {
            "did_coefficient": stable_float(coefficients[0]),
            "homoskedastic_standard_error": stable_float(
                np.sqrt(homoskedastic_covariance[0, 0])
            ),
            "heteroskedastic_robust_standard_error": stable_float(
                np.sqrt(robust_covariance[0, 0])
            ),
            "entity_clustered_standard_error_sensitivity": stable_float(
                np.sqrt(clustered_covariance[0, 0])
            ),
            "nobs": nobs,
            "entities": entity_count,
            "time_periods": time_count,
            "df_resid": df_resid,
            "within_r_squared": stable_float(within_r_squared),
        }

    expected = {
        "EPI": {
            "coefficient": -0.8215069838066829,
            "homoskedastic": 0.11466639175397025,
            "robust": 0.10529676450155705,
            "clustered": 0.19726630325420716,
        },
        "EPIE": {
            "coefficient": 0.004769262974922498,
            "homoskedastic": 0.0012049808144021087,
            "robust": 0.0011144613806986568,
            "clustered": 0.0014831563142377014,
        },
    }
    for outcome, target in expected.items():
        observed = results[outcome]
        checks = {
            "coefficient": observed["did_coefficient"],
            "homoskedastic": observed["homoskedastic_standard_error"],
            "robust": observed["heteroskedastic_robust_standard_error"],
            "clustered": observed["entity_clustered_standard_error_sensitivity"],
        }
        for name, value in checks.items():
            if not np.isclose(value, target[name], rtol=0, atol=5e-13):
                raise ValueError(
                    f"case 006 frozen TWFE {outcome}/{name} changed: {value}"
                )

    return {
        "schema_version": "case006-twfe-freeze-audit-v1",
        "status": "passed",
        "model_access": "denied_not_in_01_model_input",
        "purpose": "benchmark-owned arithmetic replay; not a model-visible result",
        "outcomes_equal_weight": list(OUTCOMES),
        "formula": {
            "outcomes": list(OUTCOMES),
            "exposure": "did",
            "controls": list(CONTROLS),
            "fixed_effects": ["stkcd", "year"],
            "cost_column": "source_scale_unchanged",
            "treatment_assignment": "provided_region_year_did_not_firm_invariant",
        },
        "covariance_channels": {
            "homoskedastic": "arithmetic-alignment only",
            "heteroskedastic_robust": "public visible specification compatibility",
            "entity_clustered": "mandatory serial-correlation sensitivity; not author-reported",
        },
        "implementation": (
            "NumPy two-way within transformation and explicit sandwich covariance; "
            "does not execute or import the supplied run_baseline.py"
        ),
        "results": results,
        "independence_scope": "estimator_algebra_reimplemented_from_visible_contract",
        "shared_components": [
            "same projected main_data.csv values",
            "same visible model specification",
        ],
        "claim_limit": (
            "coefficient replay does not validate staggered-DID identification, "
            "parallel trends, cohort heterogeneity, or causal interpretation"
        ),
    }


def manifest_entry(path: Path, case_root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(case_root)),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def build_private_reference(
    *,
    private_case_root: Path,
    source_input: Path,
    public_contract: dict[str, Any],
    exposure_facts: dict[str, Any],
    freeze_audit: dict[str, Any],
) -> None:
    """Write evaluator-only facts outside every system-visible case root."""

    private_case_root.mkdir(parents=True, exist_ok=False)
    audit_path = private_case_root / "freeze_audit.json"
    write_json(audit_path, freeze_audit)
    write_json(
        private_case_root / "private_manifest.json",
        {
            "schema_version": "case006-private-reference-v1",
            "case_id": PRIVATE_CASE_ID,
            "access": "benchmark_evaluator_only",
            "system_case_root_contains_private_reference": False,
            "evaluation_class": (
                "quasi_holdout_candidate_pending_freeze_audit"
            ),
            "primary_score_eligible": False,
            "source_data": {
                "filename": SOURCE_DATA_FILENAME,
                "sha256": sha256(source_input / SOURCE_DATA_FILENAME),
            },
            "public_contract": public_contract,
            "exposure_audit": exposure_facts,
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
    source_input: Path,
    exposure_facts: dict[str, Any],
    roundtrip: dict[str, Any],
) -> dict[str, Any]:
    input_dir = case_root / "01_model_input"
    visible_files = sorted(path for path in input_dir.iterdir() if path.is_file())
    return {
        "manifest_version": 3,
        "case_id": case_id,
        "input_view": input_view,
        "benchmark_track": benchmark_track,
        "evaluation_class": "quasi_holdout_candidate_pending_freeze_audit",
        "primary_ai_scientist_claim_eligible": False,
        "primary_score_eligible": False,
        "outbound_status": "requires_explicit_authorization",
        "hidden_reference_access": "denied",
        "benchmark_contract": "same_neutral_data_two_method_information_views",
        "research_target": "staggered_policy_causal_candidate",
        "hypothesis_direction": "unspecified",
        "visible_input": {
            "directory": "01_model_input",
            "row_count": 9009,
            "column_count": 16,
            "entity_count": 819,
            "time_period_count": 11,
            "files": [manifest_entry(path, case_root) for path in visible_files],
        },
        "shared_visible_asset_contract": {
            "counterpart_case_id": counterpart_case_id,
            "required_byte_identical_files": list(SHARED_VISIBLE_FILES),
            "sha256": {name: sha256(input_dir / name) for name in SHARED_VISIBLE_FILES},
        },
        "scope_disclosures": {
            "co_primary_outcomes_equal_weight": list(OUTCOMES),
            "known_controls_prelabelled": list(CONTROLS),
            "discovery_control_selection_fully_autonomous": False,
            "provided_exposure": "did is region-year and equals treat * post",
            "first_exposure_year_by_province": exposure_facts[
                "first_exposure_year_by_province"
            ],
            "firms_with_any_province_change": exposure_facts[
                "firms_with_any_province_change"
            ],
            "firms_whose_treat_status_changes": exposure_facts[
                "firms_whose_treat_status_changes"
            ],
            "firm_invariant_treatment_assumption_valid": False,
            "source_event_matches_staggered_timing": False,
            "cost_scale_conflict": {
                "known_approximate_factor": 100,
                "main_regression_resolution": "retain_source_column_without_rescaling",
                "must_disclose": True,
            },
            "heterogeneity_h2": "unsupported_by_visible_contract",
            "causal_claim_requires_identification_diagnostics": True,
        },
        "hidden_reference": {
            "access": "denied",
            "directory_present": False,
            "copied_into_case": False,
            "accessed_during_build": False,
            "allowed_during_system_run": False,
            "paper_results_logs_or_hidden_copied": False,
            "private_reference_present_in_case_root": False,
        },
        "source_provenance": {
            "source_data_filename": SOURCE_DATA_FILENAME,
            "source_data_sha256": sha256(source_input / SOURCE_DATA_FILENAME),
            "neutralization": {
                "column_projection": list(MAIN_COLUMNS),
                "data_values_changed": False,
                "cost_rescaled": False,
                "coefficients_inserted_into_model_input": False,
                "paper_results_logs_hidden_or_run_baseline_copied": False,
                "builder_runtime_executes_or_imports_run_baseline": False,
            },
            "exposure_audit": exposure_facts,
            "csv_round_trip": roundtrip,
        },
        "budget_2x": {
            "provider_attempt_limit": 80,
            "cell_wall_time_seconds": 5400,
            "statistical_phase_wall_time_seconds": 3600,
            "generated_code_timeout_seconds": 1200,
            "identical_infrastructure_retries": 2,
        },
    }


def ensure_no_prohibited_files(case_roots: list[Path]) -> None:
    prohibited_suffixes = {
        ".pdf",
        ".doc",
        ".docx",
        ".py",
        ".ipynb",
        ".log",
        ".dta",
    }
    prohibited_names = {"02_hidden_reference", "run_baseline.py", "original_paper.pdf"}
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
            "case 006 system root contains non-whitelisted entries: "
            f"{sorted(actual_entries - SAFE_CASE_ROOT_ENTRIES)}"
        )
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    forbidden = (
        "benchmark_owned_freeze_audit",
        "freeze_audit.json",
        "did_coefficient",
        "homoskedastic_standard_error",
        "heteroskedastic_robust_standard_error",
        "entity_clustered_standard_error_sensitivity",
        "declared_model",
        "declared_fixed_effects",
        "declared_standard_errors",
    )
    leaked = [token for token in forbidden if token in serialized]
    if leaked:
        raise ValueError(f"case 006 safe manifest leaks private fields: {leaked}")


def validate_case_roots(
    case_roots: list[Path],
    private_case_root: Path,
    source_input: Path,
    source_frame: pd.DataFrame,
    expected_public_contract: dict[str, Any],
    expected_exposure_facts: dict[str, Any],
    expected_freeze_audit: dict[str, Any],
) -> dict[str, Any]:
    if len(case_roots) != 2 or not all(path.is_dir() for path in case_roots):
        raise FileNotFoundError("both case 006 views must exist before validation")
    expected_views = ("discovery_blind", "reproduction_aligned")
    manifests: list[dict[str, Any]] = []
    for case_root, expected_view in zip(case_roots, expected_views):
        manifest = json.loads(
            (case_root / "case_manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("input_view") != expected_view:
            raise ValueError(f"case 006 view mismatch: {case_root}")
        if manifest.get("evaluation_class") != (
            "quasi_holdout_candidate_pending_freeze_audit"
        ):
            raise ValueError("case 006 evaluation class changed")
        if manifest.get("primary_score_eligible") is not False:
            raise ValueError("case 006 became primary-score eligible before audit")
        if manifest.get("hidden_reference_access") != "denied":
            raise ValueError("case 006 hidden-reference access is not denied")
        ensure_safe_case_root(case_root, manifest)
        visible_entries = manifest["visible_input"]["files"]
        declared_paths = set()
        for entry in visible_entries:
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
            raise ValueError("case 006 visible directory differs from manifest")
        verify_round_trip(source_frame, case_root / "01_model_input" / "main_data.csv")
        manifests.append(manifest)

    for name in SHARED_VISIBLE_FILES:
        if (
            case_roots[0] / "01_model_input" / name
        ).read_bytes() != (
            case_roots[1] / "01_model_input" / name
        ).read_bytes():
            raise ValueError(f"shared visible asset differs across views: {name}")
    ensure_no_prohibited_files(case_roots)
    private_entries = {path.name for path in private_case_root.iterdir()}
    if private_entries != {"freeze_audit.json", "private_manifest.json"}:
        raise ValueError("case 006 private reference directory changed")
    observed_freeze = json.loads(
        (private_case_root / "freeze_audit.json").read_text(encoding="utf-8")
    )
    if observed_freeze != expected_freeze_audit:
        raise ValueError("case 006 frozen TWFE audit changed")
    private_manifest = json.loads(
        (private_case_root / "private_manifest.json").read_text(encoding="utf-8")
    )
    if private_manifest.get("access") != "benchmark_evaluator_only":
        raise ValueError("case 006 private reference access changed")
    if private_manifest.get("source_data") != {
        "filename": SOURCE_DATA_FILENAME,
        "sha256": sha256(source_input / SOURCE_DATA_FILENAME),
    }:
        raise ValueError("case 006 private source-data contract changed")
    if private_manifest.get("public_contract") != expected_public_contract:
        raise ValueError("case 006 private public contract changed")
    if private_manifest.get("exposure_audit") != expected_exposure_facts:
        raise ValueError("case 006 private exposure audit changed")
    if sha256(private_case_root / "freeze_audit.json") != private_manifest[
        "freeze_audit"
    ]["sha256"]:
        raise ValueError("case 006 private freeze-audit hash changed")
    return {
        "status": "passed",
        "evaluation_class": "quasi_holdout_candidate_pending_freeze_audit",
        "primary_score_eligible": False,
        "case_roots": [str(path) for path in case_roots],
        "views": list(expected_views),
        "rows": 9009,
        "columns": 16,
        "entities": 819,
        "periods": 11,
        "neutral_csv_sha256": sha256(
            case_roots[0] / "01_model_input" / "main_data.csv"
        ),
        "private_freeze_audit_sha256": sha256(
            private_case_root / "freeze_audit.json"
        ),
        "private_manifest_sha256": sha256(
            private_case_root / "private_manifest.json"
        ),
        "private_reference_present_in_case_root": False,
        "hidden_reference_accessed": False,
        "prohibited_assets_copied": False,
        "external_model_or_api_called": False,
        "manifest_ids": [manifest["case_id"] for manifest in manifests],
    }


def build_cases(
    source_root: Path,
    benchmark_root: Path,
    private_reference_root: Path,
) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    if not source_input.is_dir():
        raise FileNotFoundError(f"invalid case 006 source package: {source_root}")
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

    public_contract = verify_public_contract(source_input)
    frame, exposure_facts = load_main_frame(source_input)
    dictionary = build_dictionary(frame)
    description = build_data_description()
    freeze_audit = build_twfe_freeze_audit(frame)
    benchmark_root.mkdir(parents=True, exist_ok=True)
    private_reference_root.mkdir(parents=True, exist_ok=True)

    with (
        tempfile.TemporaryDirectory(
            dir=benchmark_root, prefix=".case006-build-"
        ) as temporary,
        tempfile.TemporaryDirectory(
            dir=private_reference_root, prefix=".case006-private-build-"
        ) as private_temporary,
    ):
        stage_root = Path(temporary)
        private_stage = Path(private_temporary) / PRIVATE_CASE_ID
        staged_cases = [stage_root / target.name for target in targets]
        for root in staged_cases:
            (root / "01_model_input").mkdir(parents=True)

        discovery_input = staged_cases[0] / "01_model_input"
        csv_path = discovery_input / "main_data.csv"
        frame.to_csv(
            csv_path,
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
        (discovery_input / "evidence_bundle.md").write_text(
            build_evidence_bundle(aligned=False), encoding="utf-8"
        )
        roundtrip = verify_round_trip(frame, csv_path)

        aligned_input = staged_cases[1] / "01_model_input"
        for name in SHARED_VISIBLE_FILES:
            shutil.copyfile(discovery_input / name, aligned_input / name)
        (aligned_input / "evidence_bundle.md").write_text(
            build_evidence_bundle(aligned=True), encoding="utf-8"
        )

        data_sha256 = roundtrip["csv_sha256"]
        data_size = csv_path.stat().st_size
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
                    "source_format": "Stata .dta",
                    "target_format": "UTF-8 CSV",
                    **roundtrip,
                },
            )
            manifest = build_manifest(
                case_root=root,
                case_id=case_id,
                input_view=input_view,
                benchmark_track=track,
                counterpart_case_id=counterpart,
                source_input=source_input,
                exposure_facts=exposure_facts,
                roundtrip=roundtrip,
            )
            write_json(root / "case_manifest.json", manifest)

        build_private_reference(
            private_case_root=private_stage,
            source_input=source_input,
            public_contract=public_contract,
            exposure_facts=exposure_facts,
            freeze_audit=freeze_audit,
        )

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
        private_stage.replace(private_target)

    return validate_case_roots(
        targets,
        private_target,
        source_input,
        frame,
        public_contract,
        exposure_facts,
        freeze_audit,
    )


def validate_existing(
    source_root: Path,
    benchmark_root: Path,
    private_reference_root: Path,
) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    public_contract = verify_public_contract(source_input)
    frame, exposure_facts = load_main_frame(source_input)
    freeze_audit = build_twfe_freeze_audit(frame)
    targets = [
        benchmark_root / DISCOVERY_CASE_ID,
        benchmark_root / REPRODUCTION_CASE_ID,
    ]
    return validate_case_roots(
        targets,
        private_reference_root / PRIVATE_CASE_ID,
        source_input,
        frame,
        public_contract,
        exposure_facts,
        freeze_audit,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build or validate two non-destructive Case006 quasi-holdout-candidate views."
        )
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT
    )
    parser.add_argument(
        "--private-reference-root",
        type=Path,
        default=DEFAULT_PRIVATE_REFERENCE_ROOT,
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate existing Case006 views without modifying them.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    source_root = args.source_root.resolve()
    benchmark_root = args.benchmark_root.resolve()
    private_reference_root = args.private_reference_root.resolve()
    result = (
        validate_existing(source_root, benchmark_root, private_reference_root)
        if args.validate_only
        else build_cases(source_root, benchmark_root, private_reference_root)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
