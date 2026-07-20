from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT.parent / "benchmark-cases"
DEFAULT_SOURCE_ROOT = (
    Path.home() / "Downloads" / "case_005_农业绿色金融与农业绿色发展_FINAL"
)
DISCOVERY_CASE_ID = "case_005_agri_green_finance_discovery_blind"
REPRODUCTION_CASE_ID = "case_005_agri_green_finance_reproduction_aligned"
SHARED_VISIBLE_FILES = (
    "main_data.csv",
    "data_dictionary.csv",
    "data_description.md",
)
SOURCE_COLUMNS = (
    "area",
    "year",
    "Agricultural\n green development level（agd）",
    "Agricultural \ngreen finance development level（agf）",
    "Average years \nof schooling for rural residents（edu）",
    "Logarithm of real per capita disposable income of rural residents\xa0（lnrni）",
    "Logarithm of rural household fixed asset investment(lnfa)",
    "Fiscal expenditure to support agriculture(fis)",
    "Logarithm of traffic level\n\xa0(lntr)",
    "Number of agricultural green patent applications per capita(pagp)",
    "Environmental\n regulation(envi)",
)
COLUMN_RENAMES = dict(
    zip(
        SOURCE_COLUMNS,
        (
            "area",
            "year",
            "agd",
            "agf",
            "edu",
            "lnrni",
            "lnfa",
            "fis",
            "lntr",
            "pagp",
            "envi",
        ),
    )
)
KNOWN_CONTROLS = ("edu", "lnrni", "lnfa", "fis", "lntr")


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


def load_source_frame(source_input: Path) -> pd.DataFrame:
    source_path = source_input / "analysis_data.xlsx"
    frame = pd.read_excel(source_path, sheet_name="analysis_data")
    if tuple(frame.columns) != SOURCE_COLUMNS:
        raise ValueError("case 005 source column contract changed")
    frame = frame.rename(columns=COLUMN_RENAMES)
    if frame.shape != (330, 11):
        raise ValueError(f"unexpected case 005 source shape: {frame.shape}")
    if frame.duplicated(["area", "year"]).any():
        raise ValueError("case 005 area + year key is not unique")
    if frame.isna().any().any():
        raise ValueError("case 005 source unexpectedly contains missing values")
    if frame["area"].nunique() != 30:
        raise ValueError("case 005 must contain 30 province entities")
    if sorted(frame["year"].unique().tolist()) != list(range(2011, 2022)):
        raise ValueError("case 005 year support changed")
    if not frame.groupby("area")["year"].nunique().eq(11).all():
        raise ValueError("case 005 must remain a balanced 30 x 11 panel")
    return frame


def verify_public_spec(source_input: Path) -> dict[str, Any]:
    spec_path = source_input / "model_specifications.jsonl"
    records = [
        json.loads(line)
        for line in spec_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(records) != 1:
        raise ValueError("case 005 public model specification changed")
    spec = records[0]
    if spec.get("fe") != "省份+年份双向固定效应":
        raise ValueError("case 005 public fixed-effect declaration changed")
    if spec.get("se") != "not_reported":
        raise ValueError("case 005 public standard-error declaration changed")
    if spec.get("controls") != "edu,lnrni,lnfa,fis,lnainex":
        raise ValueError("case 005 public control declaration changed")
    return {
        "public_spec_sha256": sha256(spec_path),
        "declared_fixed_effects": ["area", "year"],
        "declared_standard_errors": "not_reported",
        "unresolved_control_name": "lnainex",
        "observed_data_column": "lntr",
        "silent_aliasing_forbidden": True,
    }


def build_dictionary(frame: pd.DataFrame) -> pd.DataFrame:
    labels = {
        "area": "省份标识",
        "year": "年份",
        "agd": "农业绿色发展水平",
        "agf": "农业绿色金融发展水平",
        "edu": "农村居民平均受教育年限",
        "lnrni": "农村居民人均实际可支配收入对数",
        "lnfa": "农村家庭固定资产投资对数",
        "fis": "财政支农支出",
        "lntr": "交通水平对数",
        "pagp": "人均农业绿色专利申请量",
        "envi": "环境规制",
    }
    definitions = {
        "area": "源数据中的省份类别标识；不应当作连续数值解释。",
        "year": "日历年份。",
        "agd": "源数据已构造的农业绿色发展水平指标；底层构造流程未随本视图提供。",
        "agf": "源数据已构造的农业绿色金融发展水平指标；底层构造流程未随本视图提供。",
        "edu": "农村居民平均受教育年限。",
        "lnrni": "农村居民人均实际可支配收入的对数形式。",
        "lnfa": "农村家庭固定资产投资的对数形式。",
        "fis": "源数据中的财政支农支出指标；精确单位未提供。",
        "lntr": "源数据中交通水平指标的对数形式。",
        "pagp": "人均农业绿色专利申请量。",
        "envi": "源数据中的环境规制指标；精确构造方式未提供。",
    }
    roles = {
        "area": "id",
        "year": "time",
        "agd": "outcome",
        "agf": "exposure",
        **{name: "control" for name in KNOWN_CONTROLS},
        "pagp": "unknown",
        "envi": "unknown",
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
                "unit": "类别" if name == "area" else ("年" if name == "year" else "源数据单位"),
                "source": "案例包 analysis_data.xlsx；更底层来源未在当前可见合同中提供",
                "missing_value_meaning": "无缺失",
                "available_years": "2011—2021",
                "processing_status": "已整理分析字段，构造流水线未提供",
                "missing_count": int(frame[name].isna().sum()),
                "missing_rate": float(frame[name].isna().mean()),
                "notes": (
                    "为使当前 schema 可执行，edu/lnrni/lnfa/fis/lntr 被预标记为 control；"
                    "这是路由执行合同，不是结果驱动选取。"
                    if name in KNOWN_CONTROLS
                    else ""
                ),
            }
        )
    return pd.DataFrame(rows)


def build_data_description() -> str:
    return """# 中立数据说明

`main_data.csv` 是 30 个省份在 2011—2021 年的完整平衡面板，共 330 行、11 个字段。`area + year` 唯一，所有字段在当前表中均无缺失。

- `agd` 是农业绿色发展水平结果指标，`agf` 是农业绿色金融发展水平暴露指标。两者都是已构造指标，本视图不包含底层生成流水线。
- `edu`、`lnrni`、`lnfa`、`fis`、`lntr` 在两个视图中先标记为已知协变量，以满足当前可执行 schema。因此自主发现视图评测的是方法路由、实现、诊断和论述校准，不是完全自主的控制变量挑选。
- `pagp` 和 `envi` 保留为 `unknown`，不得根据结果临时改写为机制、门槛或控制变量。
- 可见数据和设计信息不足以识别因果效应；主目标是条件关联，结论不得越界为政策或因果影响。
- 只有 30 个省份聚类单元。若在省份层面聚类标准误，必须披露小聚类风险，不能仅依赖大样本渐近 p 值。
- 源包的公开规格把标准误记为 `not_reported`；两个系统都必须明确自己的推断规则。

本说明不包含回归结果、系数方向、显著性、论文文字、作者代码或隐藏参考。
"""


def build_evidence_bundle(*, aligned: bool) -> str:
    common = """# 冻结证据与评测边界

- 本案例已被开发者看过，仅用作 `seen validation`，不能进入私有 holdout 或支撑通用 AI Scientist 能力结论。
- 关联目标是在可见协变量和面板结构下衡量 `agf` 与 `agd` 的条件关联，方向预先设为 `unspecified`。
- 不允许联网增补数据，不得使用原论文、作者代码、回归结果或隐藏材料来选规格。
- 显著、不显著、反向、不稳定、推断失败和无可接纳主张都是合法终点。
"""
    if not aligned:
        return common + """

## 自主发现视图

本视图不指定主估计量、固定效应、标准误或灵敏性形式。系统必须在读取结果前根据 30×11 面板结构冻结设计，并处理序列相关、省份聚类数较少以及关联不等于因果的边界。
"""
    return common + """

## 方法对齐视图

为保留一条可执行、可复算的基准，本视图冻结为省份和年份双向固定效应模型：`agd ~ agf + edu + lnrni + lnfa + fis + lntr + area FE + year FE`。主目标仍只是条件关联。

推断合同冻结为按 `area` 聚类标准误，同时必须报告只有 30 个聚类的风险，并用小聚类稳健推断（如 wild-cluster bootstrap 或等价的小样本校正）作灵敏性检查。原公开规格对标准误仅记为 `not_reported`，因此这是 benchmark 的可执行化规则，不得声称为原研究推断的完全复现。

源包公开规格使用了数据中不存在的 `lnainex`，实际可见列是 `lntr`。本基准为可执行性使用 `lntr`，但必须把它记为未解决的命名冲突，不得静默宣称两者已被验证为同义字段。

公开源规格还标注了“面板门槛”，但当前可见合同未冻结门槛变量、门槛数、搜索范围、重抽样和多重检验规则。因此门槛部分的状态是 `unsupported_by_visible_contract`：系统应明确拒绝猜测，而不是自由搜索出有利阈值。
"""


def variable_specs(*, aligned: bool) -> list[dict[str, Any]]:
    definitions = {
        "area": ("省份标识", "id"),
        "year": ("年份", "time"),
        "agd": ("农业绿色发展水平", "outcome"),
        "agf": ("农业绿色金融发展水平", "exposure"),
        "edu": ("农村居民平均受教育年限", "control"),
        "lnrni": ("农村居民人均实际可支配收入对数", "control"),
        "lnfa": ("农村家庭固定资产投资对数", "control"),
        "fis": ("财政支农支出", "control"),
        "lntr": ("交通水平对数", "control"),
        "pagp": ("人均农业绿色专利申请量", "unknown"),
        "envi": ("环境规制", "unknown"),
    }
    specs = []
    for name, (label, role) in definitions.items():
        specs.append(
            {
                "name": name,
                "label": label,
                "role": role,
                "definition": f"见 data_dictionary.csv 中 {name} 的冻结定义。",
                "source": "case 005 visible analysis_data.xlsx",
            }
        )
    return specs


def build_case_profile(
    *,
    case_id: str,
    data_sha256: str,
    data_size: int,
    aligned: bool,
) -> dict[str, Any]:
    if aligned:
        title = "农业绿色金融与农业绿色发展：双向固定效应对齐验证"
        track = "reproduction_aligned"
        constraints = [
            "冻结可执行基准为 agd 对 agf、edu、lnrni、lnfa、fis、lntr 的省份和年份双向固定效应回归。",
            "主推断按 area 聚类；必须披露仅 30 个聚类的渐近风险，并执行小聚类稳健灵敏性分析。",
            "公开规格将标准误记为 not_reported；当前聚类规则是 benchmark 可执行化合同，不是对原研究推断的忠实声称。",
            "公开规格中 lnainex 与实际数据列 lntr 存在未解决命名冲突；为可执行性使用 lntr，但不得声称已验证两者同义。",
            "面板门槛部分因门槛变量、搜索空间和重抽样规则未冻结而不支持执行；禁止猜测或结果驱动搜索。",
            "主目标为条件关联，不得使用导致、影响、政策效应等因果表述。",
        ]
        diagnostics = [
            "核验 30×11 平衡面板、唯一键、完整样本和变量类型",
            "报告双向固定效应的样本量、实体数、时间期和吸收结构",
            "报告省份聚类数、小聚类风险与稳健推断灵敏性",
            "执行不依赖原始运行状态的独立复算，对照核心系数、标准误和样本",
            "将门槛部分显式记为 unsupported_by_visible_contract",
        ]
    else:
        title = "农业绿色金融与农业绿色发展：自主路由验证"
        track = "strict_blind"
        constraints = [
            "在读取任何结果前，根据 30×11 省份面板自主冻结主方法、推断层级、诊断和停止条件。",
            "agd 是结果、agf 是暴露；edu、lnrni、lnfa、fis、lntr 因当前 schema 预标记为控制变量。",
            "这个自主视图测试方法路由和执行，不测试完全自主的控制变量选择。",
            "pagp 和 envi 保留为 unknown；若要改变其角色，必须在结果不可见时说明理由和证伪边界。",
            "只允许关联性主张；无可见外生冲击、随机化或有效工具变量支持因果识别。",
        ]
        diagnostics = [
            "核验面板唯一键、平衡性、时间内变异与完整样本",
            "说明未观测的省份不变因素、年份共同冲击和时变混杂如何处理",
            "显式冻结标准误，并处理只有 30 个可能聚类单元的风险",
            "报告方法切换、规格敏感性、完整样本和独立复算",
            "将结论严格校准为条件关联",
        ]

    return {
        "case_id": case_id,
        "title": title,
        "research_question": (
            "在 2011—2021 年 30 省面板中，农业绿色金融发展水平 agf "
            "与农业绿色发展水平 agd 之间是否存在稳健的条件关联？"
        ),
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "statement": (
                    "在冻结的面板结构与可见协变量条件下，agf 与 agd "
                    "之间存在可复算的条件关联。"
                ),
                "expected_direction": "unspecified",
                "mechanism": (
                    "不预设方向或机制；可见数据不足以将关联解释为因果效应。"
                ),
            }
        ],
        "unit_of_analysis": "省份—年度",
        "sample_period": "2011—2021",
        "data_structure_hint": "panel",
        "variables": variable_specs(aligned=aligned),
        "dataset_refs": [
            {
                "dataset_id": "case_005_neutral_main_data",
                "role": "main",
                "filename": "main_data.csv",
                "mime_type": "text/csv",
                "sha256": data_sha256,
                "size_bytes": data_size,
            }
        ],
        "design_envelope": {
            "benchmark_track": track,
            "research_goal": "associational",
            "target_estimands": [
                "冻结面板与协变量条件下 agf 与 agd 的平均条件关联"
            ],
            "design_constraints": constraints,
            "required_diagnostics": diagnostics,
            "allowed_claim_strength": "associational",
        },
        "known_policy_facts": [],
        "constraints": [
            "main_data.csv 有 330 行、11 列、30 个省份、11 个年份，area + year 唯一且无缺失。",
            "本案例是 seen validation，只可用于工程回归和路由校验，不能作为最终 holdout。",
            "两个视图共用字节完全一致的主数据、字典和中立数据说明。",
            "源数据是已整理指标，本视图不包含底层构造流水线。",
            "禁止读取原论文、作者代码、结果、日志或 02_hidden_reference。",
        ],
    }


def profile_markdown(profile: dict[str, Any], *, aligned: bool) -> str:
    envelope = profile["design_envelope"]
    constraints = "\n".join(
        f"- {item}" for item in envelope["design_constraints"]
    )
    diagnostics = "\n".join(
        f"- {item}" for item in envelope["required_diagnostics"]
    )
    note = (
        "冻结可执行的双向固定效应基准；门槛部分明确不支持。"
        if aligned
        else "不公开主方法；五个已知协变量因 schema 预标记为 control。"
    )
    return f"""# {profile['title']}

## 轨道与评测资格

- 轨道：`{envelope['benchmark_track']}`
- 资格：`seen validation`，不是私有 holdout。
- {note}

## 研究问题与假设

{profile['research_question']}

H1：{profile['hypotheses'][0]['statement']}

方向为 `unspecified`，主张上限是条件关联。

## 结果不可见时冻结的设计边界

{constraints}

## 必须报告的诊断

{diagnostics}

## 共同数据事实

- 30 个省份、2011—2021 年、330 行的完整平衡面板。
- `area + year` 唯一，当前可见表无缺失。
- 只有 30 个省份层聚类单元，小聚类风险必须进入结论校准。
- 不提供论文、代码、结果、日志或隐藏参考。
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
    public_spec: dict[str, Any],
    roundtrip: dict[str, Any],
) -> dict[str, Any]:
    input_dir = case_root / "01_model_input"
    visible_files = sorted(path for path in input_dir.iterdir() if path.is_file())
    return {
        "manifest_version": 2,
        "case_id": case_id,
        "input_view": input_view,
        "benchmark_track": benchmark_track,
        "evaluation_class": "seen_validation",
        "primary_ai_scientist_claim_eligible": False,
        "benchmark_contract": "same_neutral_data_two_method_information_views",
        "research_target": "associational",
        "hypothesis_direction": "unspecified",
        "visible_input": {
            "directory": "01_model_input",
            "row_count": 330,
            "column_count": 11,
            "entity_count": 30,
            "time_period_count": 11,
            "files": [manifest_entry(path, case_root) for path in visible_files],
        },
        "shared_visible_asset_contract": {
            "counterpart_case_id": counterpart_case_id,
            "required_byte_identical_files": list(SHARED_VISIBLE_FILES),
            "sha256": {
                name: sha256(input_dir / name) for name in SHARED_VISIBLE_FILES
            },
        },
        "scope_disclosures": {
            "five_known_covariates_prelabelled_controls": list(KNOWN_CONTROLS),
            "discovery_control_selection_fully_autonomous": False,
            "threshold_component": "unsupported_by_visible_contract",
            "standard_error_source_declaration": "not_reported",
            "province_cluster_count": 30,
            "small_cluster_risk_required": True,
            "unresolved_control_name_conflict": {
                "public_specification": "lnainex",
                "observed_data_column": "lntr",
                "treated_as_verified_synonyms": False,
            },
        },
        "hidden_reference": {
            "copied_into_case": False,
            "accessed_during_build": False,
            "allowed_during_system_run": False,
        },
        "source_provenance": {
            "source_analysis_data_xlsx_sha256": sha256(
                source_input / "analysis_data.xlsx"
            ),
            **public_spec,
            "neutralization": {
                "column_renames": COLUMN_RENAMES,
                "data_values_changed": False,
                "additional_data_included": False,
                "paper_code_results_logs_or_hidden_copied": False,
            },
            "csv_round_trip": roundtrip,
        },
        "agent_laboratory_v2_budget": {
            "provider_attempt_ceiling": 40,
            "max_steps": 5,
            "num_papers_lit_review": 1,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
        },
    }


def ensure_no_prohibited_files(case_roots: list[Path]) -> None:
    prohibited_suffixes = {".pdf", ".doc", ".docx", ".py", ".ipynb", ".log"}
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
        raise FileNotFoundError(f"invalid case 005 source package: {source_root}")
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

    frame = load_source_frame(source_input)
    public_spec = verify_public_spec(source_input)
    dictionary = build_dictionary(frame)
    description = build_data_description()
    benchmark_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        dir=benchmark_root, prefix=".case005-build-"
    ) as temporary:
        stage_root = Path(temporary)
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
                    "source_format": "Excel .xlsx",
                    "target_format": "UTF-8 CSV",
                    **roundtrip,
                },
            )
            (root / "README.md").write_text(
                f"""# {case_id}

这是 Case005 的 `{input_view}` seen-validation 视图。

- 仅 `01_model_input` 可作为模型输入。
- 本目录不包含论文、代码、结果、日志或 hidden reference。
- 主张上限是条件关联，方向为 `unspecified`。
- 门槛部分是 `unsupported_by_visible_contract`，不得猜测。
- Agent Laboratory v2 预算冻结为 `40/5/1/0`（provider attempts / max steps / MLE steps / paper refinement steps）。
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
                public_spec=public_spec,
                roundtrip=roundtrip,
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
        "evaluation_class": "seen_validation",
        "case_roots": [str(path) for path in targets],
        "rows": 330,
        "columns": 11,
        "entities": 30,
        "periods": 11,
        "neutral_csv_sha256": sha256(
            targets[0] / "01_model_input" / "main_data.csv"
        ),
        "hidden_reference_accessed": False,
        "prohibited_assets_copied": False,
        "roundtrip": roundtrip,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build two non-destructive Case005 seen-validation views."
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT
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
