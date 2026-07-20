from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT.parent / "benchmark-cases"
PROTOCOL_VERSION = "case-benchmark-v2-dev"
BENCHMARK_TASK_TRACK = "given_input_method_experiment_write"
SHARED_VISIBLE_FILES = (
    "main_data.csv",
    "data_dictionary.csv",
    "data_description.md",
)
VISIBLE_FILES = (
    *SHARED_VISIBLE_FILES,
    "evidence_bundle.md",
)
PERMUTATION_FREEZE = {
    "permutation_scheme": "assignment_unit_label",
    "permutation_unit_field": "idcode",
    "placebo_repetitions": 199,
    "random_seed": 12345,
}
ALIGNED_EVENT_STUDY_FREEZE = {
    "event_remote_pre_years": [1998, 1999, 2000, 2001],
    "event_term_scaling": "binary_group_year_contrast",
}
COMMON_PERMUTATION_BOUNDARY = (
    "若进入政策面板置换诊断路径，敏感性固定按 idcode 分配单元标签重排、"
    "199 次、随机种子 12345；该约束不替系统指定主方法。"
)
CASES = (
    {
        "source_case_id": "case_002_green_credit_high_pollution_discovery_blind",
        "case_id": "case_002_green_credit_high_pollution_discovery_blind_v2_dev",
        "input_view": "discovery_blind",
        "benchmark_track": "strict_blind",
    },
    {
        "source_case_id": "case_002_green_credit_high_pollution_reproduction_aligned",
        "case_id": "case_002_green_credit_high_pollution_reproduction_aligned_v2_dev",
        "input_view": "reproduction_aligned",
        "benchmark_track": "reproduction_aligned",
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def hardlink_or_copy(source: Path, destination: Path) -> str:
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        return "copy"


def v2_profile(
    source_path: Path,
    *,
    case_id: str,
    input_view: str,
) -> dict[str, Any]:
    profile = json.loads(source_path.read_text(encoding="utf-8"))
    profile["case_id"] = case_id
    policy_design = profile.get("policy_design")
    if not isinstance(policy_design, dict):
        raise ValueError(f"case profile has no policy_design: {source_path}")
    policy_design.update(PERMUTATION_FREEZE)
    if input_view == "reproduction_aligned":
        policy_design.update(ALIGNED_EVENT_STUDY_FREEZE)
    elif any(key in policy_design for key in ALIGNED_EVENT_STUDY_FREEZE):
        raise ValueError(f"discovery profile leaks event-study freeze: {source_path}")

    envelope = profile.get("design_envelope")
    if not isinstance(envelope, dict):
        raise ValueError(f"case profile has no design_envelope: {source_path}")
    constraints = envelope.get("design_constraints")
    if not isinstance(constraints, list):
        raise ValueError(f"case profile has no design constraints: {source_path}")
    calibrated = [
        str(item).replace(
            "冻结随机种子的 500 次安慰剂检验",
            "按 idcode 分配单元标签重排的 199 次安慰剂检验（随机种子 12345）",
        )
        for item in constraints
    ]
    if COMMON_PERMUTATION_BOUNDARY not in calibrated:
        calibrated.append(COMMON_PERMUTATION_BOUNDARY)
    envelope["design_constraints"] = calibrated
    return profile


def v2_profile_markdown(source_path: Path, *, input_view: str) -> str:
    text = source_path.read_text(encoding="utf-8")
    aligned_old = (
        "- 运行平行趋势联合检验、替代结果、提前效应和冻结随机种子的 "
        "500 次安慰剂检验；失败或不支持项必须显式输出。"
    )
    aligned_new = (
        "- 运行平行趋势联合检验、替代结果、提前效应和按 `idcode` "
        "分配单元标签重排的 199 次安慰剂检验（随机种子 `12345`）；"
        "失败或不支持项必须显式输出。"
    )
    if input_view == "reproduction_aligned":
        if aligned_old not in text:
            raise ValueError(f"aligned placebo narrative changed: {source_path}")
        return text.replace(aligned_old, aligned_new)

    marker = "\n## 必须报告的诊断\n"
    if marker not in text:
        raise ValueError(f"discovery diagnostic heading changed: {source_path}")
    boundary = (
        "\n- 若进入政策面板置换诊断路径，敏感性固定按 `idcode` 分配单元"
        "标签重排、199 次、随机种子 `12345`；该约束不替系统指定主方法。\n"
    )
    return text.replace(marker, boundary + marker)


def agent_config(case: dict[str, str]) -> dict[str, Any]:
    return {
        "case": {
            "case_id": case["case_id"],
            "input_view": case["input_view"],
            "benchmark_track": case["benchmark_track"],
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
            "max_tokens": 12288,
        },
        "workflow": {
            "benchmark_task_track": BENCHMARK_TASK_TRACK,
            "output_dir": "../../benchmark-results-v2-dev/agent-laboratory",
            "upstream_repo_root": "../../Agent Laboratory",
            "execution_timeout_seconds": 600,
            "max_steps": 5,
            "max_llm_calls": 40,
            "num_papers_lit_review": 0,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
        },
    }


def manifest_entry(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def build_one(
    *,
    benchmark_root: Path,
    stage_root: Path,
    case: dict[str, str],
) -> dict[str, Any]:
    source_root = benchmark_root / case["source_case_id"]
    source_input = source_root / "01_model_input"
    if not source_input.is_dir():
        raise FileNotFoundError(f"source visible input is missing: {source_input}")
    target_input = stage_root / "01_model_input"
    target_input.mkdir(parents=True)

    materialization: dict[str, str] = {}
    for filename in VISIBLE_FILES:
        source = source_input / filename
        if not source.is_file():
            raise FileNotFoundError(f"source visible asset is missing: {source}")
        materialization[filename] = hardlink_or_copy(
            source,
            target_input / filename,
        )

    write_json(
        target_input / "case_profile.json",
        v2_profile(
            source_input / "case_profile.json",
            case_id=case["case_id"],
            input_view=case["input_view"],
        ),
    )
    (target_input / "case_profile.md").write_text(
        v2_profile_markdown(
            source_input / "case_profile.md",
            input_view=case["input_view"],
        ),
        encoding="utf-8",
    )
    write_json(stage_root / "agent_laboratory_config_v2.json", agent_config(case))

    counterpart = next(
        item["case_id"] for item in CASES if item["case_id"] != case["case_id"]
    )
    visible_files = sorted(path for path in target_input.iterdir() if path.is_file())
    policy_design_freeze = dict(PERMUTATION_FREEZE)
    if case["input_view"] == "reproduction_aligned":
        policy_design_freeze.update(ALIGNED_EVENT_STUDY_FREEZE)
    manifest = {
        "manifest_version": 2,
        "protocol_version": PROTOCOL_VERSION,
        "case_id": case["case_id"],
        "source_case_id": case["source_case_id"],
        "input_view": case["input_view"],
        "benchmark_track": case["benchmark_track"],
        "benchmark_contract": (
            "same_neutral_data_two_method_information_views_then_hidden_evaluation"
        ),
        "benchmark_task_track": BENCHMARK_TASK_TRACK,
        "visible_input": {
            "directory": "01_model_input",
            "files": [manifest_entry(path, stage_root) for path in visible_files],
            "materialization": materialization,
        },
        "shared_visible_asset_contract": {
            "counterpart_case_id": counterpart,
            "required_byte_identical_files": list(SHARED_VISIBLE_FILES),
            "sha256": {
                filename: sha256(target_input / filename)
                for filename in SHARED_VISIBLE_FILES
            },
        },
        "policy_design_freeze": policy_design_freeze,
        "agent_laboratory_budget": {
            "provider_attempt_cap": 40,
            "max_steps": 5,
            "num_papers_lit_review": 0,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
        },
        "hidden_reference_access": "denied",
        "hidden_reference": {
            "copied_into_case": False,
            "directory_present": False,
            "access": "denied",
        },
    }
    write_json(stage_root / "case_manifest.json", manifest)
    return manifest


def validate_sources(benchmark_root: Path) -> None:
    for filename in SHARED_VISIBLE_FILES:
        hashes = {
            sha256(
                benchmark_root
                / case["source_case_id"]
                / "01_model_input"
                / filename
            )
            for case in CASES
        }
        if len(hashes) != 1:
            raise ValueError(f"source views differ for shared asset: {filename}")


def build_cases(benchmark_root: Path) -> dict[str, Any]:
    benchmark_root = benchmark_root.resolve(strict=True)
    destinations = [benchmark_root / case["case_id"] for case in CASES]
    existing = [str(path) for path in destinations if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing v2 dev case directories: "
            + ", ".join(existing)
        )
    validate_sources(benchmark_root)

    stages: list[Path] = []
    manifests: list[dict[str, Any]] = []
    try:
        for case in CASES:
            stage = Path(
                tempfile.mkdtemp(
                    prefix=f".{case['case_id']}.",
                    dir=benchmark_root,
                )
            )
            stages.append(stage)
            manifests.append(
                build_one(
                    benchmark_root=benchmark_root,
                    stage_root=stage,
                    case=case,
                )
            )

        for filename in SHARED_VISIBLE_FILES:
            hashes = {
                sha256(stage / "01_model_input" / filename) for stage in stages
            }
            if len(hashes) != 1:
                raise ValueError(f"v2 views differ for shared asset: {filename}")
        if any((stage / "02_hidden_reference").exists() for stage in stages):
            raise ValueError("v2 dev package must not contain hidden references")

        for stage, destination in zip(stages, destinations):
            stage.rename(destination)
    finally:
        for stage in stages:
            if stage.exists():
                shutil.rmtree(stage)

    return {
        "status": "passed",
        "protocol_version": PROTOCOL_VERSION,
        "case_roots": [str(path) for path in destinations],
        "case_manifest_sha256": {
            path.name: sha256(path / "case_manifest.json") for path in destinations
        },
        "shared_visible_sha256": manifests[0]["shared_visible_asset_contract"][
            "sha256"
        ],
        "hidden_reference_access": "denied",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build non-destructive Case 002 v2 development views."
    )
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=DEFAULT_BENCHMARK_ROOT,
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(
        json.dumps(
            build_cases(args.benchmark_root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
