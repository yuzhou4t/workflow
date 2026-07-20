from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT.parent / "benchmark-cases"
DEFAULT_SOURCE_ROOT = (
    Path.home()
    / "Downloads"
    / "case_002_绿色信贷政策与高污染企业绿色转型"
)
DISCOVERY_CASE_ID = "case_002_green_credit_high_pollution_discovery_blind"
REPRODUCTION_CASE_ID = (
    "case_002_green_credit_high_pollution_reproduction_aligned"
)
SHARED_VISIBLE_FILES = (
    "main_data.csv",
    "data_dictionary.csv",
    "data_description.md",
)
VARIABLE_RENAMES = {
    "treat": "high_polluting_industry_current_year",
    "trend": "within_firm_observation_index",
}
DICTIONARY_COLUMNS = {
    "所在数据表": "dataset",
    "英文变量名": "variable",
    "中文名称": "label_zh",
    "变量角色": "role",
    "变量类型": "storage_type",
    "定义与计算方式": "definition",
    "单位": "unit",
    "数据来源": "source",
    "缺失值含义": "missing_value_meaning",
    "可用年份": "available_years",
    "处理状态": "processing_status",
    "缺失数": "missing_count",
    "缺失率": "missing_rate",
    "匹配键/备注": "notes",
}
PRIMARY_OUTCOME = "polint1"
SECONDARY_OUTCOMES = ("polint2", "polint3", "tfp_op", "tfp_lp")
CONTROLS = (
    "age",
    "size1",
    "size2",
    "capstr",
    "leverage",
    "roa",
    "wage",
    "growth",
)
REMOTE_PRE_YEARS = [1998, 1999, 2000, 2001]
EVENT_TERM_SCALING = "binary_group_year_contrast"


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


def text_value(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def load_source_frame(source_input: Path) -> pd.DataFrame:
    source_path = source_input / "main_data.dta"
    frame = pd.read_stata(source_path, convert_categoricals=False)
    if frame.shape != (249_504, 59):
        raise ValueError(f"unexpected source shape: {frame.shape}")
    if frame.duplicated(["idcode", "year"]).any():
        raise ValueError("source idcode + year key is not unique")
    if sorted(frame["year"].unique().tolist()) != [
        1998,
        1999,
        2000,
        2001,
        2002,
        2003,
        2004,
        2005,
        2006,
        2007,
        2008,
        2009,
        2011,
        2012,
        2013,
    ]:
        raise ValueError("source year support changed")
    if set(frame["treat"].dropna().unique().tolist()) != {0, 1}:
        raise ValueError("source high-pollution classification is not binary")

    chronological_index = frame.groupby("idcode")["year"].rank(method="dense")
    if not chronological_index.eq(frame["trend"]).all():
        raise ValueError(
            "source trend is not the within-firm chronological observation index"
        )
    return frame.rename(columns=VARIABLE_RENAMES)


def build_dictionary(source_input: Path, columns: list[str]) -> pd.DataFrame:
    raw = pd.read_excel(
        source_input / "data_dictionary.xlsx",
        header=1,
        dtype=object,
    )
    raw["所在数据表"] = raw["所在数据表"].astype(str).str.strip()
    dictionary = raw.loc[raw["所在数据表"].eq("gqwr.dta")].copy()
    dictionary["英文变量名"] = (
        dictionary["英文变量名"].astype(str).str.strip().replace(VARIABLE_RENAMES)
    )
    dictionary["所在数据表"] = "main_data.csv"

    group_mask = dictionary["英文变量名"].eq(
        "high_polluting_industry_current_year"
    )
    dictionary.loc[group_mask, "中文名称"] = "当年高污染行业分类标记"
    dictionary.loc[group_mask, "变量角色"] = "分组字段候选"
    dictionary.loc[group_mask, "定义与计算方式"] = (
        "依据企业当年行业分类形成的二元标记：1 表示当年属于高污染行业，"
        "0 表示当年不属于；该字段不表示随机分配，也不编码政策发生时间。"
    )
    dictionary.loc[group_mask, "匹配键/备注"] = (
        "该分类可能随企业年度变化；可见数据中有 2,333 家企业出现过分类变化。"
    )

    index_mask = dictionary["英文变量名"].eq(
        "within_firm_observation_index"
    )
    dictionary.loc[index_mask, "中文名称"] = "企业内观测序号"
    dictionary.loc[index_mask, "变量角色"] = "样本序位字段"
    dictionary.loc[index_mask, "定义与计算方式"] = (
        "企业在当前不平衡面板中按观测年份排序后的序号；它不是连续日历年编码，"
        "年份缺口不会自动占位。"
    )
    dictionary.loc[index_mask, "单位"] = "企业内观测序号"
    dictionary.loc[index_mask, "匹配键/备注"] = (
        "必须与 year 分开解释；2010 年不在主表中。"
    )

    dictionary = dictionary.rename(columns=DICTIONARY_COLUMNS)
    dictionary = dictionary[list(DICTIONARY_COLUMNS.values())]
    variables = dictionary["variable"].tolist()
    if variables != columns:
        raise ValueError("neutral dictionary rows do not match main-data column order")
    if len(dictionary) != 59:
        raise ValueError("neutral dictionary must contain exactly 59 rows")
    return dictionary


def build_data_description(frame: pd.DataFrame) -> str:
    changing_firms = int(
        frame.groupby("idcode")["high_polluting_industry_current_year"]
        .nunique()
        .gt(1)
        .sum()
    )
    return f"""# 中立数据说明

## 主表与分析边界

`main_data.csv` 是企业—年度不平衡面板，共 {len(frame):,} 行、{len(frame.columns)} 个字段、{frame['idcode'].nunique():,} 家企业。`idcode + year` 非缺失且唯一。可见年份为 1998—2009、2011—2013；2010 年没有观测，不能自动补齐，也不能把 2009 与 2011 误当作相邻年度。

本轮只提供这张主表，不提供或合并扩展数据。主表来自已经整理的分析数据，对数、比率、生产率和污染强度等派生字段的逐步生成日志并不完整；系统不得声称重新完成了底层数据清洗。

## 字段中立化

- `high_polluting_industry_current_year` 是按企业当年行业分类形成的二元事实字段。它不编码政策发生时间，也不应在设计前被称为“处理组”。可见样本中有 {changing_firms:,} 家企业的该字段曾随年份变化，因此任何要求永久固定分组的识别方法都必须先检查适用性。
- `within_firm_observation_index` 是企业在当前不平衡面板内按年份排序的观测序号，不是连续日历时间，也不是预先指定的模型趋势项。
- `polint1` 是预先构造的二氧化硫排放量/工业总产值强度，作为首轮主结果候选；`polint2`、`polint3`、`tfp_op`、`tfp_lp` 是次要或稳健性结果候选。

## 数据质量事实

- `sewcharge` 缺失 235,900 条，`accpay` 缺失 47,973 条；缺失不得填为零。
- 行业与地区代码是分类标识，不应作为连续数值解释。
- 首轮不得新增、外采或按行序拼接其他数据。
- 所有模型选择、变量构造、样本规则、标准误、诊断和停止条件都必须在查看结果前冻结，并完整保留不显著、反向、矛盾与失败结果。

本说明不提供最终估计方法、系数方向、显著性或论文结论。
"""


def build_evidence_bundle(*, reproduction_aligned: bool) -> str:
    policy_only = """# 冻结背景证据包

本文件只提供当前输入视图中所有参评系统共同可见的背景，不包含目标论文、作者代码、目标结果或目标结论。运行期间不得联网补充文献或数据。

## 1. 政策原文

- 标题：关于落实环保政策法规防范信贷风险的意见
- 文号：环发〔2007〕108号
- 日期：2007-07-18
- 发布主体：原国家环境保护总局、中国人民银行、中国银行业监督管理委员会
- 官方链接：https://www.mee.gov.cn/gkml/zj/wj/200910/t20091022_172469.htm
- 中立释义：文件要求把企业环境守法情况纳入信贷管理，控制对不符合环保要求项目和企业的授信风险。该事实给出政策时间与制度背景，但不决定统计方法，也不预告经验结果。
"""
    if not reproduction_aligned:
        return policy_only + """

## 自主发现边界

本视图不提供方法专属论文或估计规格。系统须根据研究问题、政策时点、面板结构和分组变化自主选择方法，并在结果不可见时冻结诊断与结论边界。
"""
    return policy_only + """

## 2. 政策评估中的序列相关

- Bertrand, M., Duflo, E., & Mullainathan, S. (2002), *How Much Should We Trust Differences-In-Differences Estimates?*, NBER Working Paper 8841.
- 链接：https://www.nber.org/papers/w8841
- 中立释义：政策面板中的序列相关会使常规推断偏乐观，应预先选择与处理层级和数据结构相符的聚类或其他稳健推断，并使用安慰剂或置换检查。该文不替本案例指定唯一实现。

## 3. 预趋势检验的局限

- Roth, J. (2022), *Pretest with Caution: Event-Study Estimates after Testing for Parallel Trends*, American Economic Review: Insights, 4(3), 305–322.
- 链接：https://www.aeaweb.org/articles?id=10.1257/aeri.20210236
- 中立释义：未拒绝预趋势不等于证明平行趋势成立；预检功效与基于预检筛选后的偏误都应进入结论校准。

## 4. 多期差分方法的适用边界

- Callaway, B., & Sant'Anna, P. H. C., *Difference-in-Differences with Multiple Time Periods*.
- 链接：https://arxiv.org/abs/1803.09015
- 中立释义：多期、分期处理需要明确组别和处理时点。本案例的高污染行业分类按企业当年行业代码形成，并非对所有企业永久固定，因此不能机械套用要求固定组别或吸收处理的估计量；必须先说明组别变化如何进入目标估计量。
"""


def variable_specs(
    dictionary: pd.DataFrame,
    *,
    reproduction_aligned: bool,
) -> list[dict[str, Any]]:
    outcome_names = {PRIMARY_OUTCOME, *SECONDARY_OUTCOMES}
    specs: list[dict[str, Any]] = []
    for row in dictionary.to_dict(orient="records"):
        name = str(row["variable"])
        role = "unknown"
        if name == "idcode":
            role = "id"
        elif name == "year":
            role = "time"
        elif name in outcome_names:
            role = "outcome"
        elif name in CONTROLS:
            role = "control"
        elif name == "within_firm_observation_index" and reproduction_aligned:
            role = "control"
        elif name == "high_polluting_industry_current_year" and reproduction_aligned:
            role = "treatment"
        elif name in {"indcode2", "areacode2"} and reproduction_aligned:
            role = "fixed_effect"
        elif name == "indcode" and reproduction_aligned:
            role = "cluster"
        specs.append(
            {
                "name": name,
                "label": text_value(row["label_zh"]),
                "role": role,
                "definition": text_value(row["definition"]),
                "source": text_value(row["source"]),
            }
        )
    by_name = {item["name"]: item for item in specs}
    ordered_outcomes = [
        by_name[name]
        for name in (PRIMARY_OUTCOME, *SECONDARY_OUTCOMES)
    ]
    return [
        *ordered_outcomes,
        *[item for item in specs if item["name"] not in outcome_names],
    ]


def build_case_profile(
    *,
    case_id: str,
    dictionary: pd.DataFrame,
    data_sha256: str,
    data_size: int,
    reproduction_aligned: bool,
) -> dict[str, Any]:
    if reproduction_aligned:
        title = "绿色信贷政策与高污染行业企业污染强度：方法对齐盲测"
        benchmark_track = "reproduction_aligned"
        design_constraints = [
            "基准识别路径使用差分中的差分（DID），不得根据结果切换主方法。",
            "政策暴露由当年高污染行业分类与政策时间强度相乘：2007 年强度为 0.42，2008 年及以后为 1，2007 年以前为 0。",
            "首轮主结果为 polint1；polint2、polint3、tfp_op、tfp_lp 只能按预先冻结的次要结果或稳健性顺序报告。",
            "基准控制变量为 age、size1、size2、capstr、leverage、roa、wage、growth，并包含 within_firm_observation_index。",
            "吸收 idcode、year、areacode2 和 indcode2 固定效应；标准误按 indcode × areacode2 × year 的交叉单元聚类。",
            "事件研究以 2006 年为参照；1998—2001 合并为远期提前项，其他年份按实际观测年份报告，不得虚构 2010 年。",
            (
                "事件研究年度项冻结为 treated×year 二元年度差异"
                "（event_term_scaling=binary_group_year_contrast）；2007 年项不乘 0.42，"
                "因而不是基准单位 policy_exposure 系数，不得直接比较系数量级。"
            ),
            "运行平行趋势联合检验、替代结果、提前效应和冻结随机种子的 500 次安慰剂检验；失败或不支持项必须显式输出。",
        ]
        required_diagnostics = [
            "核验政策前后及两类行业分类均有观测",
            "披露 2,333 家分类变化企业如何进入估计量",
            "报告事件研究全部提前项及联合检验，不以未显著作为假设已证明",
            "核验固定效应、聚类单元、样本量和缺失处理",
            "执行独立复算并比较核心系数、标准误和有效样本",
        ]
    else:
        title = "绿色信贷政策与高污染行业企业污染强度：自主发现盲测"
        benchmark_track = "strict_blind"
        design_constraints = [
            "系统必须在读取任何结果前，根据政策时点、面板结构和分组字段的时间变化自主选择主方法与备选方法。",
            "首轮主结果候选固定为 polint1；其他污染强度和生产率字段只能按预先冻结顺序作为次要结果或稳健性结果。",
            "不得从目标论文、作者代码、隐藏表格或运行后显著性反推变量构造、样本、固定效应、标准误或诊断。",
            "若当前数据与假设不足以支持因果识别，必须降级为关联或描述性结论。",
            (
                "若自主选择事件研究，必须在查看结果前冻结年度项缩放口径，"
                "并披露事件年系数能否与基准暴露系数直接比较。"
            ),
        ]
        required_diagnostics = [
            "核验政策前后及两类行业分类均有观测",
            "检查企业行业分类随时间变化对识别假设的影响",
            "检查政策前可比趋势、提前变化与伪政策时点",
            "报告样本、固定效应或分层结构、推断层级和独立复算",
        ]

    policy_design: dict[str, Any] = {
        "policy_date": "2007-07",
        "group_field": "high_polluting_industry_current_year",
        "time_field": "year",
        "post_start_weight": 1.0,
        "exposure_name": "policy_exposure",
        "fixed_effects": [],
        "cluster_fields": [],
        "cluster_composition": "interaction",
        "event_years": [],
    }
    if reproduction_aligned:
        policy_design.update(
            {
                "policy_start_weight": 0.42,
                "fixed_effects": ["idcode", "year", "indcode2", "areacode2"],
                "cluster_fields": ["indcode", "areacode2", "year"],
                "event_reference_year": 2006,
                "event_remote_pre_years": REMOTE_PRE_YEARS,
                "event_years": [
                    2002,
                    2003,
                    2004,
                    2005,
                    2007,
                    2008,
                    2009,
                    2011,
                    2012,
                    2013,
                ],
                "event_term_scaling": EVENT_TERM_SCALING,
                "placebo_start_year": 2004,
                "placebo_repetitions": 500,
                "random_seed": 12345,
            }
        )

    known_policy_facts = [
        (
            "环发〔2007〕108号文件于 2007-07-18 发布；冻结官方来源为"
            "https://www.mee.gov.cn/gkml/zj/wj/200910/t20091022_172469.htm。"
        ),
        "政策面向全国银行信贷活动，并重点关注高污染、高耗能行业的环境合规风险。",
        "政策发布主体为原国家环境保护总局、中国人民银行和中国银行业监督管理委员会。",
    ]
    if reproduction_aligned:
        known_policy_facts.extend(
            [
                (
                    "冻结背景证据：Bertrand、Duflo 与 Mullainathan（NBER W8841，"
                    "https://www.nber.org/papers/w8841）指出 DID 面板中的序列相关会使常规推断偏乐观；"
                    "本案例因此必须预先冻结与处理层级和数据结构相符的推断及安慰剂检查。"
                ),
                (
                    "冻结背景证据：Roth（AER: Insights 2022，"
                    "https://www.aeaweb.org/articles?id=10.1257/aeri.20210236）说明未拒绝预趋势"
                    "不等于证明平行趋势，预检功效和预检后偏误必须进入结论校准。"
                ),
                (
                    "冻结背景证据：Callaway 与 Sant'Anna（"
                    "https://arxiv.org/abs/1803.09015）要求多期 DID 明确组别和处理时点；"
                    "本案例的当年行业分类并非永久企业分组，不能机械套用固定组别解释。"
                ),
            ]
        )

    return {
        "case_id": case_id,
        "title": title,
        "research_question": (
            "2007 年绿色信贷政策实施前后，当年被归类为高污染行业的企业与"
            "其他制造业企业之间，污染排放强度是否出现可归因于政策的差异变化？"
        ),
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "statement": (
                    "政策实施后，当年被归类为高污染行业的企业与其他制造业企业"
                    "之间，污染排放强度存在差异变化。"
                ),
                "expected_direction": "unspecified",
                "mechanism": (
                    "不预设变化方向或传导机制；主设计、竞争解释和证伪条件须在"
                    "查看统计结果前冻结。"
                ),
            }
        ],
        "unit_of_analysis": "企业—年度",
        "sample_period": "1998—2009、2011—2013（2010 年无观测）",
        "data_structure_hint": "panel",
        "variables": variable_specs(
            dictionary,
            reproduction_aligned=reproduction_aligned,
        ),
        "dataset_refs": [
            {
                "dataset_id": "case_002_neutral_main_data",
                "role": "main",
                "filename": "main_data.csv",
                "mime_type": "text/csv",
                "sha256": data_sha256,
                "size_bytes": data_size,
            }
        ],
        "design_envelope": {
            "benchmark_track": benchmark_track,
            "research_goal": "causal",
            "target_estimands": [
                "政策发布后高污染行业分类企业相对于其他制造业企业的 polint1 平均差异变化",
                "预先排序的替代污染强度与生产率结果上的对应差异变化",
            ],
            "design_constraints": design_constraints,
            "required_diagnostics": required_diagnostics,
            "allowed_claim_strength": "causal",
        },
        "policy_design": policy_design,
        "known_policy_facts": known_policy_facts,
        "constraints": [
            "main_data.csv 有 249,504 条观测、59 个字段、81,857 家企业，idcode + year 唯一。",
            "2010 年无观测，不得补齐，也不得在动态分析中伪造该年份。",
            "high_polluting_industry_current_year 按当年行业分类形成；2,333 家企业在样本期内出现过分类变化。",
            "within_firm_observation_index 是企业内实际观测序号，不是连续日历年，也不能未经论证自动视为模型趋势。",
            "首轮只使用主表，不合并扩展数据，不联网采集文献或数据。",
            "主表是已整理分析数据，底层清洗与派生流水线不完整；不得声称复现了全部数据工程。",
            "目标论文、作者代码、隐藏结果、系数方向和显著性只能在两个系统的运行均封存后用于评测。",
        ],
    }


def profile_markdown(profile: dict[str, Any], *, reproduction_aligned: bool) -> str:
    envelope = profile["design_envelope"]
    constraints = "\n".join(
        f"- {item}" for item in envelope["design_constraints"]
    )
    diagnostics = "\n".join(
        f"- {item}" for item in envelope["required_diagnostics"]
    )
    track_note = (
        "本视图公开冻结的 DID 基准规格和诊断要求，但不公开任何结果。"
        if reproduction_aligned
        else "本视图不公开目标论文的方法设定；系统须在结果不可见时自主选法。"
    )
    return f"""# {profile['title']}

## 输入视图

- 运行轨道：`{envelope['benchmark_track']}`
- {track_note}
- 两个视图共用字节完全相同的 59 列主数据、中立数据字典和中立数据说明；自主发现视图只给政策来源，方法对齐视图另给冻结的方法背景。

## 研究问题

{profile['research_question']}

## 方向未指定的首轮假设

H1：{profile['hypotheses'][0]['statement']}

不预设正向或负向，也不预设机制；不显著、反向、矛盾和失败结果必须保留。

## 结果不可见时冻结的设计边界

{constraints}

## 必须报告的诊断

{diagnostics}

## 共同数据事实

- 分析单位是企业—年度；样本覆盖 1998—2009、2011—2013，2010 年没有观测。
- 主键为 `idcode + year`；首轮主结果候选固定为 `polint1`。
- `high_polluting_industry_current_year` 可能随企业年度变化，不能静默改写成永久固定分组。
- 只有两个系统的运行都完成封存后，评测程序才能读取 `02_hidden_reference` 对应的外部源包。
"""


def agent_laboratory_config(
    *,
    case_id: str,
    input_view: str,
    benchmark_track: str,
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
            "timeout_seconds": 180,
        },
        "workflow": {
            "output_dir": "../../benchmark-results/agent-laboratory",
            "upstream_repo_root": "../../Agent Laboratory",
            "execution_timeout_seconds": 600,
            "max_steps": 3,
            "max_llm_calls": 40,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 1,
        },
    }


def hidden_reference_manifest(source_hidden: Path) -> dict[str, Any]:
    files = []
    for path in sorted(item for item in source_hidden.rglob("*") if item.is_file()):
        files.append(
            {
                "path": str(path.relative_to(source_hidden)),
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    tree_digest = hashlib.sha256()
    for item in files:
        tree_digest.update(
            (
                f"{item['path']}\0{item['sha256']}\0{item['size_bytes']}\n"
            ).encode("utf-8")
        )
    return {
        "allowed_use": "only_after_both_system_runs_are_sealed",
        "copied_into_case": False,
        "source_package_directory_name": source_hidden.parent.name,
        "source_subdirectory": source_hidden.name,
        "source_tree_sha256": tree_digest.hexdigest(),
        "file_count": len(files),
        "files": files,
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
    source_hidden_tree_sha256: str,
    roundtrip: dict[str, Any],
) -> dict[str, Any]:
    visible_dir = case_root / "01_model_input"
    visible_files = sorted(path for path in visible_dir.iterdir() if path.is_file())
    shared_hashes = {
        filename: sha256(visible_dir / filename)
        for filename in SHARED_VISIBLE_FILES
    }
    return {
        "manifest_version": 1,
        "case_id": case_id,
        "input_view": input_view,
        "benchmark_track": benchmark_track,
        "benchmark_contract": "same_neutral_data_two_method_information_views_then_hidden_evaluation",
        "visible_input": {
            "directory": "01_model_input",
            "row_count": 249_504,
            "column_count": 59,
            "entity_count": 81_857,
            "time_period_count": 15,
            "files": [manifest_entry(path, case_root) for path in visible_files],
        },
        "shared_visible_asset_contract": {
            "counterpart_case_id": counterpart_case_id,
            "required_byte_identical_files": list(SHARED_VISIBLE_FILES),
            "sha256": shared_hashes,
        },
        "hidden_reference": {
            "directory": "02_hidden_reference",
            "allowed_use": "only_after_both_system_runs_are_sealed",
            "source_tree_sha256": source_hidden_tree_sha256,
            "reference_pointer_sha256": sha256(
                case_root / "02_hidden_reference" / "reference_source.json"
            ),
        },
        "source_provenance": {
            "source_main_data_dta_sha256": sha256(source_input / "main_data.dta"),
            "source_data_dictionary_xlsx_sha256": sha256(
                source_input / "data_dictionary.xlsx"
            ),
            "source_case_profile_json_sha256": sha256(
                source_input / "case_profile.json"
            ),
            "neutralization": {
                "column_renames": VARIABLE_RENAMES,
                "data_values_changed": False,
                "additional_data_included": False,
            },
            "csv_round_trip": roundtrip,
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


def ensure_discovery_is_method_blind(case_root: Path) -> None:
    visible_profile = (
        (case_root / "01_model_input" / "case_profile.json").read_text(
            encoding="utf-8"
        )
        + (case_root / "01_model_input" / "case_profile.md").read_text(
            encoding="utf-8"
        )
    )
    prohibited = (
        "0.42",
        "差分中的差分",
        "DID",
        "indcode × areacode2 × year",
        "吸收 idcode",
        "2006 年为参照",
    )
    leaked = [token for token in prohibited if token in visible_profile]
    if leaked:
        raise ValueError(f"discovery profile leaks aligned method constraints: {leaked}")


def ensure_no_hidden_results(case_roots: list[Path]) -> None:
    result_tokens = (
        "-0.4751",
        "-0.4772",
        "-1.2537",
        "-0.3361",
        "-0.0915",
        "-0.0793",
        "-0.0547",
    )
    for case_root in case_roots:
        for path in (case_root / "01_model_input").iterdir():
            if path.suffix.lower() not in {".json", ".md", ".csv"}:
                continue
            if path.name == "main_data.csv":
                continue
            text = path.read_text(encoding="utf-8")
            leaked = [token for token in result_tokens if token in text]
            if leaked:
                raise ValueError(f"hidden result leakage in {path}: {leaked}")


def build_cases(source_root: Path, benchmark_root: Path) -> dict[str, Any]:
    source_input = source_root / "01_model_input"
    source_hidden = source_root / "02_hidden_reference"
    if not source_input.is_dir() or not source_hidden.is_dir():
        raise FileNotFoundError(f"invalid case 002 source package: {source_root}")

    frame = load_source_frame(source_input)
    dictionary = build_dictionary(source_input, list(frame.columns))
    description = build_data_description(frame)
    hidden_manifest = hidden_reference_manifest(source_hidden)

    cases = [
        {
            "case_id": DISCOVERY_CASE_ID,
            "input_view": "discovery_blind",
            "benchmark_track": "strict_blind",
            "reproduction_aligned": False,
            "counterpart": REPRODUCTION_CASE_ID,
        },
        {
            "case_id": REPRODUCTION_CASE_ID,
            "input_view": "reproduction_aligned",
            "benchmark_track": "reproduction_aligned",
            "reproduction_aligned": True,
            "counterpart": DISCOVERY_CASE_ID,
        },
    ]
    case_roots = [benchmark_root / str(case["case_id"]) for case in cases]
    for root in case_roots:
        (root / "01_model_input").mkdir(parents=True, exist_ok=True)
        (root / "02_hidden_reference").mkdir(parents=True, exist_ok=True)

    discovery_input = case_roots[0] / "01_model_input"
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
        description,
        encoding="utf-8",
    )
    (discovery_input / "evidence_bundle.md").write_text(
        build_evidence_bundle(reproduction_aligned=False),
        encoding="utf-8",
    )
    roundtrip = verify_round_trip(frame, csv_path)

    reproduction_input = case_roots[1] / "01_model_input"
    for filename in SHARED_VISIBLE_FILES:
        shutil.copyfile(discovery_input / filename, reproduction_input / filename)
    (reproduction_input / "evidence_bundle.md").write_text(
        build_evidence_bundle(reproduction_aligned=True),
        encoding="utf-8",
    )

    data_sha256 = roundtrip["csv_sha256"]
    data_size = csv_path.stat().st_size
    for case, root in zip(cases, case_roots):
        input_dir = root / "01_model_input"
        profile = build_case_profile(
            case_id=str(case["case_id"]),
            dictionary=dictionary,
            data_sha256=data_sha256,
            data_size=data_size,
            reproduction_aligned=bool(case["reproduction_aligned"]),
        )
        write_json(input_dir / "case_profile.json", profile)
        (input_dir / "case_profile.md").write_text(
            profile_markdown(
                profile,
                reproduction_aligned=bool(case["reproduction_aligned"]),
            ),
            encoding="utf-8",
        )
        write_json(
            root / "agent_laboratory_config.json",
            agent_laboratory_config(
                case_id=str(case["case_id"]),
                input_view=str(case["input_view"]),
                benchmark_track=str(case["benchmark_track"]),
            ),
        )
        write_json(
            root / "02_hidden_reference" / "reference_source.json",
            hidden_manifest,
        )
        write_json(
            root / "roundtrip_validation.json",
            {
                "case_id": case["case_id"],
                "source_format": "Stata .dta",
                "target_format": "UTF-8 CSV",
                **roundtrip,
            },
        )
        (root / "README.md").write_text(
            f"""# {case['case_id']}

这是 case 002 的 `{case['input_view']}` 输入视图。

- 模型只可读取 `01_model_input` 与其中列入配置的文件。
- `02_hidden_reference/reference_source.json` 仅记录外部隐藏源包的冻结哈希；两个系统运行封存前不得读取。
- `main_data.csv` 只做两项中立字段改名，数值和行列顺序不变；未加入扩展数据。
- Agent Laboratory 预算冻结为 `max_steps=3`、`max_llm_calls=40`，并使用与 HypoWeaver 当前输入视图语义一致的冻结背景证据。
""",
            encoding="utf-8",
        )

    ensure_discovery_is_method_blind(case_roots[0])
    ensure_no_hidden_results(case_roots)

    shared_hashes: dict[str, str] = {}
    for filename in SHARED_VISIBLE_FILES:
        first = case_roots[0] / "01_model_input" / filename
        second = case_roots[1] / "01_model_input" / filename
        if first.read_bytes() != second.read_bytes():
            raise ValueError(f"shared visible file differs across views: {filename}")
        shared_hashes[filename] = sha256(first)

    for case, root in zip(cases, case_roots):
        manifest = build_manifest(
            case_root=root,
            case_id=str(case["case_id"]),
            input_view=str(case["input_view"]),
            benchmark_track=str(case["benchmark_track"]),
            counterpart_case_id=str(case["counterpart"]),
            source_input=source_input,
            source_hidden_tree_sha256=hidden_manifest["source_tree_sha256"],
            roundtrip=roundtrip,
        )
        write_json(root / "case_manifest.json", manifest)

    return {
        "status": "passed",
        "case_roots": [str(root) for root in case_roots],
        "source_main_data_sha256": sha256(source_input / "main_data.dta"),
        "neutral_csv_sha256": data_sha256,
        "shared_visible_sha256": shared_hashes,
        "view_specific_evidence_sha256": {
            root.name: sha256(root / "01_model_input" / "evidence_bundle.md")
            for root in case_roots
        },
        "hidden_reference_tree_sha256": hidden_manifest["source_tree_sha256"],
        "roundtrip": roundtrip,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the two frozen case 002 benchmark input views."
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=DEFAULT_BENCHMARK_ROOT,
    )
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
