#!/usr/bin/env python3
"""Explain, run, summarize, and package one frozen SixBench assignment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any
import zipfile


SCHEMA_VERSION = "sixbench-student-package-v1"
RESULT_SCHEMA_VERSION = "sixbench-student-result-v1"
POINTER_SCHEMA_VERSION = "sixbench-student-return-pointer-v1"
REQUIRED_ARTIFACTS = (
    "cell_manifest.json",
    "normalized_result.json",
    "evidence_manifest.json",
)
OPTIONAL_RETURN_ARTIFACTS = (
    "benchmark_integrity_failure.json",
    "common-stage-failure.json",
    "common-execution-receipt.json",
)
SUCCESS_STATUSES = {"completed", "success", "succeeded"}
FAILURE_STATUSES = {"failed", "failure", "error"}


class PackageError(RuntimeError):
    """A package problem that the student or coordinator can act on."""


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackageError(f"缺少{label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PackageError(f"{label}不是有效 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{label}必须是 JSON 对象: {path}")
    return value


def resolve_inside(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise PackageError(f"ASSIGNMENT.json 缺少路径: {label}")
    raw = Path(relative)
    if raw.is_absolute():
        raise PackageError(f"{label}必须是包内相对路径")
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise PackageError(f"{label}越出测试包: {relative}") from exc
    return candidate


def load_assignment(root: Path) -> dict[str, Any]:
    assignment = load_object(root / "ASSIGNMENT.json", "ASSIGNMENT.json")
    if assignment.get("schema_version") != SCHEMA_VERSION:
        raise PackageError("不支持的 ASSIGNMENT.json schema_version")
    required = (
        "package_id",
        "release_id",
        "case_number",
        "case_id",
        "case_title_zh",
        "assignment",
        "assignment_title_zh",
        "systems",
        "expected_cell_count",
        "paths",
        "expected_cells",
    )
    missing = [key for key in required if key not in assignment]
    if missing:
        raise PackageError(f"ASSIGNMENT.json 缺少字段: {', '.join(missing)}")
    systems = assignment["systems"]
    cells = assignment["expected_cells"]
    expected_count = assignment["expected_cell_count"]
    if (
        not isinstance(systems, list)
        or not systems
        or not all(isinstance(item, str) and item for item in systems)
    ):
        raise PackageError("ASSIGNMENT.json systems 无效")
    if not isinstance(cells, list) or len(cells) != expected_count:
        raise PackageError("expected_cells 数量与 expected_cell_count 不一致")
    cell_ids: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            raise PackageError("expected_cells 中存在非对象项目")
        required_cell = ("cell_id", "system_id", "input_view", "leaderboard_id", "seed")
        if any(key not in cell for key in required_cell):
            raise PackageError("expected_cells 中存在字段不完整的单元")
        if cell["system_id"] not in systems:
            raise PackageError("expected_cells 包含未分配给本人的系统")
        cell_ids.append(str(cell["cell_id"]))
    if len(cell_ids) != len(set(cell_ids)):
        raise PackageError("expected_cells 包含重复 cell_id")
    paths = assignment["paths"]
    if not isinstance(paths, dict):
        raise PackageError("ASSIGNMENT.json paths 无效")
    for label in (
        "workspace",
        "release_package",
        "operator",
        "output_root",
        "runs",
        "contracts",
        "orchestration",
    ):
        resolve_inside(root, paths.get(label), label)
    return assignment


def resolved_paths(root: Path, assignment: dict[str, Any]) -> dict[str, Path]:
    return {
        key: resolve_inside(root, value, key)
        for key, value in assignment["paths"].items()
    }


def private_return_directory(root: Path) -> Path:
    path = root / "RETURN"
    if path.is_symlink():
        raise PackageError("RETURN 目录不得是符号链接")
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise PackageError("RETURN 目录越出测试包") from exc
    os.chmod(path, 0o700)
    return path


def reject_output_symlink(path: Path) -> None:
    if path.is_symlink():
        raise PackageError(f"输出文件不得是符号链接: {path}")


def explain_payload(root: Path) -> dict[str, Any]:
    assignment = load_assignment(root)
    paths = resolved_paths(root, assignment)
    return {
        "schema_version": "sixbench-student-explanation-v1",
        "package_root": str(root.resolve()),
        "workspace": str(paths["workspace"]),
        "package_id": assignment["package_id"],
        "execution_state": assignment.get("execution_state", "unspecified"),
        "release_id": assignment["release_id"],
        "case_number": assignment["case_number"],
        "case_id": assignment["case_id"],
        "case_title_zh": assignment["case_title_zh"],
        "assignment": assignment["assignment"],
        "assignment_title_zh": assignment["assignment_title_zh"],
        "systems": assignment["systems"],
        "expected_cell_count": assignment["expected_cell_count"],
        "release_package": str(paths["release_package"]),
        "result_root": str(paths["runs"]),
        "return_directory": str((root / "RETURN").resolve()),
        "execution_host": (
            "windows_wsl2_docker"
            if os.environ.get("SIXBENCH_WINDOWS_WSL_DOCKER") == "1"
            else platform.system().lower()
        ),
    }


def explain_text(payload: dict[str, Any]) -> str:
    systems = "、".join(str(item) for item in payload["systems"])
    warning = ""
    if payload["execution_state"] == "windows_pilot":
        warning = (
            "\n当前包状态：Windows 负责人验收版。隔离检查和正式预检通过后，"
            "仅允许项目负责人在自己的 Windows 电脑测试；尚不可分发给同学。"
        )
    elif payload["execution_state"] != "formal":
        warning = (
            "\n当前包状态：尚未标记为 formal。可以查看任务和做离线检查，"
            "但不得绕过授权门禁进行外部模型调用。"
        )
    return (
        "SixBench 单人测试包\n"
        f"- 当前绝对路径：{payload['package_root']}\n"
        f"- 工作区路径：{payload['workspace']}\n"
        f"- 任务：Case {payload['case_number']}（{payload['case_title_zh']}）/"
        f"{payload['assignment_title_zh']}\n"
        f"- 本人只负责：{systems}\n"
        f"- 应完成单元：{payload['expected_cell_count']}\n"
        f"- 正式结果目录：{payload['result_root']}\n"
        f"- 回传目录：{payload['return_directory']}\n"
        f"- 当前执行环境：{payload['execution_host']}\n"
        "\n执行顺序：初始化本机环境 → 配置 API → 离线预检 → 明确确认 → "
        "正式运行 → 生成回传包。"
        f"{warning}"
    )


def operator_command(root: Path, action: str) -> list[str]:
    assignment = load_assignment(root)
    paths = resolved_paths(root, assignment)
    operator = paths["operator"]
    release = paths["release_package"]
    workspace = paths["workspace"]
    if not operator.is_file():
        raise PackageError(f"缺少执行器: {operator}")
    if not release.is_file():
        raise PackageError(f"缺少正式 release package: {release}")
    command = [
        sys.executable,
        str(operator),
        action,
        "--case",
        str(assignment["case_number"]),
        "--assignment",
        str(assignment["assignment"]),
        "--workspace",
        str(workspace),
        "--release",
        str(release),
    ]
    if action == "preflight":
        report = private_return_directory(root) / "PREFLIGHT.json"
        reject_output_symlink(report)
        command.extend(("--report", str(report)))
    return command


def run_operator(root: Path, action: str) -> int:
    completed = subprocess.run(operator_command(root, action), cwd=root, check=False)
    return completed.returncode


def run_setup(root: Path, *, assume_yes: bool) -> int:
    bootstrap = root / "tools" / "bootstrap.py"
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise PackageError(f"缺少初始化工具: {bootstrap}")
    command = [sys.executable, str(bootstrap)]
    if assume_yes:
        command.append("--yes")
    completed = subprocess.run(command, cwd=root, check=False)
    return completed.returncode


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def optional_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    if path.is_symlink():
        return None, "symbolic links are not allowed"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "JSON root must be an object"
    return value, None


def identity_mismatches(
    source: dict[str, Any] | None,
    expected: dict[str, Any],
    source_name: str,
) -> list[dict[str, Any]]:
    if source is None:
        return []
    expected_identity = {
        "cell_id": expected["cell_id"],
        "case_id": expected["case_id"],
        "system_id": expected["system_id"],
        "input_view": expected["input_view"],
        "leaderboard_id": expected["leaderboard_id"],
        "seed": expected["seed"],
    }
    errors = []
    for key, wanted in expected_identity.items():
        observed = source.get(key)
        if observed != wanted:
            errors.append(
                {
                    "source": source_name,
                    "field": key,
                    "expected": wanted,
                    "observed": observed,
                }
            )
    return errors


def item_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (dict, list, tuple, set)):
        return len(value)
    return 1


def inspect_cell(
    *,
    assignment: dict[str, Any],
    expected: dict[str, Any],
    contracts_root: Path,
    runs_root: Path,
) -> dict[str, Any]:
    cell_id = str(expected["cell_id"])
    manifest_path = contracts_root / cell_id / "cell_manifest.json"
    run_dir = runs_root / cell_id
    result_path = run_dir / "normalized_result.json"
    evidence_path = run_dir / "evidence_manifest.json"
    artifact_paths = {
        "cell_manifest.json": manifest_path,
        "normalized_result.json": result_path,
        "evidence_manifest.json": evidence_path,
    }
    presence = {name: path.is_file() for name, path in artifact_paths.items()}
    loaded = {
        name: optional_object(path) for name, path in artifact_paths.items()
    }
    validity = {
        name: value is not None and error is None
        for name, (value, error) in loaded.items()
    }
    present_count = sum(presence.values())
    delivery_status = (
        "sealed"
        if present_count == len(REQUIRED_ARTIFACTS) and all(validity.values())
        else "partial"
        if present_count
        else "missing"
    )
    manifest = loaded["cell_manifest.json"][0]
    result = loaded["normalized_result.json"][0]
    evidence = loaded["evidence_manifest.json"][0]
    expected_identity = {
        **expected,
        "case_id": assignment["case_id"],
    }
    errors = [
        {
            "source": name,
            "field": "__json__",
            "expected": "valid JSON object",
            "observed": error,
        }
        for name, (_, error) in loaded.items()
        if error is not None
    ]
    errors.extend(
        identity_mismatches(manifest, expected_identity, "cell_manifest.json")
    )
    errors.extend(
        identity_mismatches(result, expected_identity, "normalized_result.json")
    )
    if evidence is not None and evidence.get("run_id") != cell_id:
        errors.append(
            {
                "source": "evidence_manifest.json",
                "field": "run_id",
                "expected": cell_id,
                "observed": evidence.get("run_id"),
            }
        )
    return {
        "cell_id": cell_id,
        "system_id": expected["system_id"],
        "input_view": expected["input_view"],
        "leaderboard_id": expected["leaderboard_id"],
        "seed": expected["seed"],
        "delivery_status": delivery_status,
        "artifact_presence": presence,
        "artifact_valid_json": validity,
        "artifact_sha256": {
            name: sha256_file(path)
            for name, path in artifact_paths.items()
            if path.is_file() and not path.is_symlink()
        },
        "run_status": result.get("run_status") if result else None,
        "failure_class": result.get("failure_class") if result else None,
        "score_eligibility": result.get("score_eligibility") if result else None,
        "claim_count": item_count(result.get("claims")) if result else 0,
        "execution_count": item_count(result.get("executions")) if result else 0,
        "completed_check_count": (
            item_count(result.get("reported_completed_check_ids")) if result else 0
        ),
        "false_evidence_flags": (
            result.get("false_evidence_flags") if result else None
        ),
        "false_evidence_flag_count": (
            item_count(result.get("false_evidence_flags")) if result else 0
        ),
        "identity_errors": errors,
    }


def unexpected_cell_ids(
    expected_ids: set[str],
    contracts_root: Path,
    runs_root: Path,
) -> list[str]:
    observed: set[str] = set()
    if contracts_root.is_dir():
        for child in contracts_root.iterdir():
            if child.is_dir() and (child / "cell_manifest.json").is_file():
                observed.add(child.name)
    if runs_root.is_dir():
        for child in runs_root.iterdir():
            if child.is_dir() and any(
                (child / name).exists()
                for name in (
                    "normalized_result.json",
                    "evidence_manifest.json",
                    "benchmark_output.json",
                    "common-stage-failure.json",
                )
            ):
                observed.add(child.name)
    return sorted(observed - expected_ids)


def scientific_outcome(cells: list[dict[str, Any]], collection_status: str) -> str:
    if collection_status != "complete":
        return "not_evaluated" if collection_status == "incomplete" else "needs_review"
    statuses = [str(cell["run_status"]).lower() for cell in cells]
    successful = sum(status in SUCCESS_STATUSES for status in statuses)
    failed = sum(status in FAILURE_STATUSES for status in statuses)
    if successful == len(cells):
        return "all_completed"
    if failed == len(cells):
        return "all_failed"
    if successful + failed == len(cells):
        return "mixed"
    return "needs_review"


def build_report(root: Path) -> dict[str, Any]:
    assignment = load_assignment(root)
    paths = resolved_paths(root, assignment)
    expected_cells = []
    for cell in assignment["expected_cells"]:
        expected_cells.append(
            {
                **cell,
                "case_id": assignment["case_id"],
            }
        )
    cells = [
        inspect_cell(
            assignment=assignment,
            expected=cell,
            contracts_root=paths["contracts"],
            runs_root=paths["runs"],
        )
        for cell in expected_cells
    ]
    missing = [
        cell["cell_id"] for cell in cells if cell["delivery_status"] == "missing"
    ]
    partial = [
        cell["cell_id"] for cell in cells if cell["delivery_status"] == "partial"
    ]
    identity_errors = [
        {"cell_id": cell["cell_id"], **error}
        for cell in cells
        for error in cell["identity_errors"]
    ]
    expected_ids = {str(cell["cell_id"]) for cell in expected_cells}
    unexpected = unexpected_cell_ids(
        expected_ids,
        paths["contracts"],
        paths["runs"],
    )
    if unexpected or identity_errors:
        collection_status = "needs_review"
    elif missing or partial:
        collection_status = "incomplete"
    else:
        collection_status = "complete"
    statuses = [
        str(cell["run_status"]).lower()
        for cell in cells
        if cell["delivery_status"] == "sealed" and cell["run_status"] is not None
    ]
    successful = sum(status in SUCCESS_STATUSES for status in statuses)
    failed = sum(status in FAILURE_STATUSES for status in statuses)
    sealed = sum(cell["delivery_status"] == "sealed" for cell in cells)
    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_id": assignment["package_id"],
        "release_id": assignment["release_id"],
        "case_number": assignment["case_number"],
        "case_id": assignment["case_id"],
        "assignment": assignment["assignment"],
        "systems": assignment["systems"],
        "execution_environment": {
            "package_root": str(root.resolve()),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "host_mode": (
                "windows_wsl2_docker"
                if os.environ.get("SIXBENCH_WINDOWS_WSL_DOCKER") == "1"
                else platform.system().lower()
            ),
            "runtime_image": os.environ.get("SIXBENCH_RUNTIME_IMAGE"),
        },
        "collection_status": collection_status,
        "scientific_outcome": scientific_outcome(cells, collection_status),
        "counts": {
            "expected": len(cells),
            "sealed": sealed,
            "successful": successful,
            "failed": failed,
            "other_terminal": len(statuses) - successful - failed,
            "partial": len(partial),
            "missing": len(missing),
            "unexpected": len(unexpected),
        },
        "missing_cells": missing,
        "partial_cells": partial,
        "unexpected_cells": unexpected,
        "identity_errors": identity_errors,
        "cells": cells,
    }
    return_dir = private_return_directory(root)
    json_path = return_dir / "RESULT_SUMMARY.json"
    markdown_path = return_dir / "RESULT_SUMMARY.md"
    reject_output_symlink(json_path)
    reject_output_symlink(markdown_path)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    os.chmod(json_path, 0o600)
    os.chmod(markdown_path, 0o600)
    return payload


def markdown_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    rows = [
        "# SixBench 单人测试结果",
        "",
        f"- Package：`{report['package_id']}`",
        f"- Case：`{report['case_number']}` / `{report['case_id']}`",
        f"- 分组：`{report['assignment']}`",
        f"- 系统：{', '.join(report['systems'])}",
        f"- 产物收集状态：`{report['collection_status']}`",
        f"- 运行结果概况：`{report['scientific_outcome']}`",
        (
            "- 数量："
            f"应有 {counts['expected']}，已封存 {counts['sealed']}，"
            f"成功 {counts['successful']}，失败 {counts['failed']}，"
            f"部分 {counts['partial']}，缺失 {counts['missing']}，"
            f"意外单元 {counts['unexpected']}"
        ),
        "",
        (
            "| Cell | 系统 | 视图 | 榜单 | 产物 | run_status | failure_class | "
            "claims | executions | completed checks | false-evidence flags |"
        ),
        "|---|---|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for cell in report["cells"]:
        rows.append(
            "| "
            + " | ".join(
                markdown_value(value)
                for value in (
                    cell["cell_id"],
                    cell["system_id"],
                    cell["input_view"],
                    cell["leaderboard_id"],
                    cell["delivery_status"],
                    cell["run_status"],
                    cell["failure_class"],
                    cell["claim_count"],
                    cell["execution_count"],
                    cell["completed_check_count"],
                    cell["false_evidence_flag_count"],
                )
            )
            + " |"
        )
    rows.extend(
        (
            "",
            "## 完整性问题",
            "",
            f"- 缺失单元：{markdown_value(report['missing_cells'])}",
            f"- 部分单元：{markdown_value(report['partial_cells'])}",
            f"- 意外单元：{markdown_value(report['unexpected_cells'])}",
            f"- 身份不匹配：{markdown_value(report['identity_errors'])}",
            "",
            "> 本表由工具从真实产物生成；失败单元仍计入已封存结果，不代表测试包不完整。",
            "",
        )
    )
    return "\n".join(rows)


def return_files(root: Path, assignment: dict[str, Any]) -> list[tuple[Path, str]]:
    paths = resolved_paths(root, assignment)
    files: list[tuple[Path, str]] = []

    def append_if_safe(path: Path, archive_name: str) -> None:
        if not path.is_file():
            return
        if path.is_symlink():
            raise PackageError(f"回传产物不得是符号链接: {path}")
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise PackageError(f"回传产物越出测试包: {path}") from exc
        files.append((path, archive_name))

    for name in (
        "RESULT_SUMMARY.json",
        "RESULT_SUMMARY.md",
        "PREFLIGHT.json",
        "SETUP_STATUS.json",
    ):
        path = root / "RETURN" / name
        append_if_safe(path, f"summary/{name}")
    progress = paths["orchestration"] / "progress.jsonl"
    append_if_safe(progress, "summary/progress.jsonl")
    for cell in assignment["expected_cells"]:
        cell_id = str(cell["cell_id"])
        candidates = [
            paths["contracts"] / cell_id / "cell_manifest.json",
            *[
                paths["runs"] / cell_id / name
                for name in (
                    "normalized_result.json",
                    "evidence_manifest.json",
                    *OPTIONAL_RETURN_ARTIFACTS,
                )
            ],
        ]
        for path in candidates:
            append_if_safe(path, f"artifacts/{cell_id}/{path.name}")
    return files


def build_return_bundle(root: Path) -> tuple[dict[str, Any], Path]:
    report = build_report(root)
    assignment = load_assignment(root)
    files = return_files(root, assignment)
    manifest = {
        "schema_version": "sixbench-student-return-manifest-v1",
        "package_id": assignment["package_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "path": archive_name,
                "sha256": sha256_file(source),
                "size_bytes": source.stat().st_size,
            }
            for source, archive_name in files
        ],
    }
    return_dir = private_return_directory(root)
    manifest_path = return_dir / "RETURN_MANIFEST.json"
    reject_output_symlink(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    archive = return_dir / f"{assignment['package_id']}-return.zip"
    temporary = archive.with_suffix(".zip.tmp")
    reject_output_symlink(archive)
    reject_output_symlink(temporary)
    if temporary.exists():
        temporary.unlink()
    prefix = f"{assignment['package_id']}-return"
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as bundle:
        for source, archive_name in files:
            bundle.write(source, f"{prefix}/{archive_name}")
        bundle.write(manifest_path, f"{prefix}/summary/RETURN_MANIFEST.json")
    os.replace(temporary, archive)
    os.chmod(archive, 0o600)
    pointer = {
        "schema_version": POINTER_SCHEMA_VERSION,
        "package_id": assignment["package_id"],
        "release_id": assignment["release_id"],
        "case_number": assignment["case_number"],
        "case_id": assignment["case_id"],
        "assignment": assignment["assignment"],
        "systems": assignment["systems"],
        "collection_status": report["collection_status"],
        "scientific_outcome": report["scientific_outcome"],
        "counts": report["counts"],
        "summary_json": str((return_dir / "RESULT_SUMMARY.json").resolve()),
        "summary_markdown": str((return_dir / "RESULT_SUMMARY.md").resolve()),
        "return_archive": str(archive.resolve()),
        "return_archive_sha256": sha256_file(archive),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    windows_sync_root = os.environ.get("SIXBENCH_WINDOWS_SYNC_ROOT")
    if (
        os.environ.get("SIXBENCH_WINDOWS_WSL_DOCKER") == "1"
        and windows_sync_root
    ):
        pointer["windows_return_directory"] = str(
            Path(windows_sync_root) / "RETURN"
        )
    pointer_path = return_dir / "RETURN_POINTER.json"
    reject_output_symlink(pointer_path)
    pointer_path.write_text(
        json.dumps(pointer, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(pointer_path, 0o600)
    return pointer, archive


def menu(root: Path) -> int:
    print(explain_text(explain_payload(root)))
    while True:
        print(
            "\n1. 查看任务\n"
            "2. 初始化本机环境\n"
            "3. 配置 API Key\n"
            "4. 运行离线预检\n"
            "5. 开始正式测试\n"
            "6. 生成结果和回传包\n"
            "0. 退出"
        )
        choice = input("请选择：").strip()
        if choice == "0":
            return 0
        if choice == "1":
            print(explain_text(explain_payload(root)))
        elif choice == "2":
            run_setup(root, assume_yes=False)
        elif choice == "3":
            run_operator(root, "configure-api")
        elif choice == "4":
            run_operator(root, "preflight")
        elif choice == "5":
            assignment = load_assignment(root)
            token = f"RUN {assignment['case_number']} {assignment['assignment']}"
            observed = input(f"正式运行会调用外部模型。请输入 `{token}` 确认：").strip()
            if observed != token:
                print("未确认，正式运行没有开始。")
                continue
            code = run_operator(root, "run")
            pointer, archive = build_return_bundle(root)
            print(json.dumps(pointer, ensure_ascii=False, indent=2))
            print(f"回传包：{archive}")
            if code:
                print(f"执行器返回非零状态 {code}；失败和部分产物已保留。")
        elif choice == "6":
            pointer, archive = build_return_bundle(root)
            print(json.dumps(pointer, ensure_ascii=False, indent=2))
            print(f"回传包：{archive}")
        else:
            print("无效选项。")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    explain_parser = subparsers.add_parser("explain")
    explain_parser.add_argument("--json", action="store_true")
    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument("--yes", action="store_true")
    subparsers.add_parser("configure-api")
    subparsers.add_parser("preflight")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the external formal execution",
    )
    subparsers.add_parser("report")
    subparsers.add_parser("bundle")
    subparsers.add_parser("menu")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = package_root()
    if args.command == "explain":
        payload = explain_payload(root)
        print(
            json.dumps(payload, ensure_ascii=False, indent=2)
            if args.json
            else explain_text(payload)
        )
        return 0
    if args.command == "configure-api":
        return run_operator(root, "configure-api")
    if args.command == "setup":
        return run_setup(root, assume_yes=args.yes)
    if args.command == "preflight":
        return run_operator(root, "preflight")
    if args.command == "run":
        if not args.yes:
            raise PackageError(
                "正式运行会调用外部模型；确认后请使用 `run --yes`，"
                "或通过 START.command 菜单执行"
            )
        code = run_operator(root, "run")
        pointer, archive = build_return_bundle(root)
        print(json.dumps(pointer, ensure_ascii=False, indent=2))
        print(f"回传包：{archive}")
        return code
    if args.command == "report":
        report = build_report(root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "bundle":
        pointer, archive = build_return_bundle(root)
        print(json.dumps(pointer, ensure_ascii=False, indent=2))
        print(f"回传包：{archive}")
        return 0
    if args.command == "menu":
        return menu(root)
    raise PackageError(f"不支持的命令: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PackageError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
