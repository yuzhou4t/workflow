#!/usr/bin/env python3
"""Rebuild relocatable runtimes and the machine-local release lock."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

import student_handoff


class SetupError(RuntimeError):
    """A local setup problem with a concrete remediation."""


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    print("+", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=cwd, env=env, check=False)
    if completed.returncode:
        raise SetupError(
            f"初始化命令失败（状态 {completed.returncode}）: {' '.join(command)}"
        )


def python_version(executable: str) -> tuple[int, int, int] | None:
    try:
        completed = subprocess.run(
            [
                executable,
                "-c",
                "import sys; print('.'.join(map(str, sys.version_info[:3])))",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    try:
        major, minor, patch = completed.stdout.strip().split(".")
        return int(major), int(minor), int(patch)
    except ValueError:
        return None


def select_python() -> str:
    candidates = [
        os.environ.get("SIXBENCH_PYTHON"),
        shutil.which("python3.11"),
        shutil.which("python3"),
    ]
    for candidate in candidates:
        if candidate:
            version = python_version(candidate)
            if version is not None and version[:2] == (3, 11):
                return candidate
    raise SetupError(
        "需要 Python 3.11。请先安装，并可用 SIXBENCH_PYTHON 指向其可执行文件"
    )


def resolve_from(base: Path, relative: object, root: Path, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise SetupError(f"冻结配置缺少路径: {label}")
    raw = Path(relative)
    if raw.is_absolute():
        raise SetupError(f"冻结配置中的 {label} 必须是相对路径")
    candidate = (base / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SetupError(f"冻结配置中的 {label} 越出测试包") from exc
    return candidate


def load_setup_context(root: Path) -> dict[str, Any]:
    assignment = student_handoff.load_assignment(root)
    paths = student_handoff.resolved_paths(root, assignment)
    release = student_handoff.load_object(
        paths["release_package"],
        "release-package.json",
    )
    harness = resolve_from(
        root,
        release.get("harness_repo"),
        root,
        "harness_repo",
    )
    protocol = resolve_from(
        root,
        release.get("protocol"),
        root,
        "protocol",
    )
    case_release = release.get("cases", {}).get(assignment["case_number"])
    if not isinstance(case_release, dict):
        raise SetupError("release package 没有本人的 Case")
    suite = resolve_from(
        root,
        case_release.get("suite"),
        root,
        "suite",
    )
    suite_payload = student_handoff.load_object(suite, "suite")
    suite_root = suite.parent.parent
    suite_paths = suite_payload.get("paths")
    outbound = suite_payload.get("outbound_authorization")
    provider = suite_payload.get("provider")
    if not all(
        isinstance(value, dict) for value in (suite_paths, outbound, provider)
    ):
        raise SetupError("suite 缺少 paths、provider 或 outbound_authorization")
    protocol_payload = student_handoff.load_object(protocol, "protocol")
    inventory_record = (
        protocol_payload.get("release_requirements", {}).get("inventory", {})
    )
    inventory = resolve_from(
        protocol.parent.parent,
        inventory_record.get("path"),
        root,
        "release inventory",
    )
    inventory_payload = student_handoff.load_object(inventory, "release inventory")
    environment_freeze = resolve_from(
        protocol.parent.parent,
        inventory_payload.get("frozen_files", {}).get(
            "harness_environment_freeze"
        ),
        root,
        "harness environment freeze",
    )
    return {
        "assignment": assignment,
        "release": release,
        "harness": harness,
        "harness_python": resolve_from(
            root,
            release.get("python"),
            root,
            "harness Python",
        ),
        "protocol": protocol,
        "inventory": inventory,
        "environment_freeze": environment_freeze,
        "suite": suite,
        "data_to_paper": resolve_from(
            suite_root,
            suite_paths.get("data_to_paper_repo"),
            root,
            "data_to_paper_repo",
        ),
        "deep_scientist": resolve_from(
            suite_root,
            suite_paths.get("deep_scientist_repo"),
            root,
            "deep_scientist_repo",
        ),
        "release_lock": resolve_from(
            suite_root,
            outbound.get("release_lock_path"),
            root,
            "release lock",
        ),
        "provider_id": outbound.get("provider_id"),
        "provider_base_url": provider.get("default_base_url"),
        "model": provider.get("model"),
    }


def create_venv(system_python: str, target_python: Path) -> None:
    if target_python.is_file():
        return
    venv_root = target_python.parent.parent
    if venv_root.exists():
        raise SetupError(
            f"发现不完整的虚拟环境，请保留现场并联系负责人: {venv_root}"
        )
    run_checked([system_python, "-m", "venv", str(venv_root)], cwd=venv_root.parent)
    if not target_python.is_file():
        raise SetupError(f"没有生成预期的 Python: {target_python}")


def install_runtimes(root: Path, context: dict[str, Any]) -> None:
    if platform.system() != "Darwin":
        raise SetupError("当前冻结执行包只验证了 macOS，不能在其他系统正式运行")
    system_python = select_python()
    systems = set(context["assignment"]["systems"])
    harness_python = context["harness_python"]
    create_venv(system_python, harness_python)
    run_checked(
        [
            str(harness_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(context["environment_freeze"]),
        ],
        cwd=context["harness"],
    )

    # The release lock validates the complete common-executor board even when
    # this package delegates only HypoWeaver cells.
    data_repo = context["data_to_paper"]
    data_python = data_repo / ".venv" / "bin" / "python"
    create_venv(system_python, data_python)
    run_checked(
        [
            str(data_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-e",
            str(data_repo),
        ],
        cwd=data_repo,
    )

    if "deep_scientist" in systems:
        npm = shutil.which("npm")
        if npm is None:
            raise SetupError(
                "五基线运行需要 Node.js/npm；请先安装 Node.js 18.18 或更高版本"
            )
        deep_repo = context["deep_scientist"]
        deep_python = deep_repo / ".venv" / "bin" / "python"
        create_venv(system_python, deep_python)
        run_checked(
            [
                str(deep_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-e",
                str(deep_repo),
            ],
            cwd=deep_repo,
        )
        run_checked([npm, "ci"], cwd=deep_repo)


def generate_release_lock(context: dict[str, Any]) -> None:
    lock = context["release_lock"]
    if lock.exists():
        raise SetupError(
            f"release lock 已存在，不能静默覆盖: {lock}。请让 AI 先运行预检；"
            "若它来自另一台机器，请联系负责人重新发放干净包"
        )
    lock.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    run_checked(
        [
            str(context["harness_python"]),
            "-m",
            "sixbench.release_freeze",
            "--release-id",
            str(context["release"]["release_id"]),
            "--protocol",
            str(context["protocol"]),
            "--inventory",
            str(context["inventory"]),
            "--provider-id",
            str(context["provider_id"]),
            "--provider-base-url",
            str(context["provider_base_url"]),
            "--model",
            str(context["model"]),
            "--output",
            str(lock),
        ],
        cwd=context["harness"],
        env=env,
    )


def status(root: Path, context: dict[str, Any]) -> dict[str, Any]:
    targets = {
        "harness_python": context["harness_python"],
        "data_to_paper_python": (
            context["data_to_paper"] / ".venv" / "bin" / "python"
        ),
        "release_lock": context["release_lock"],
    }
    systems = set(context["assignment"]["systems"])
    if "deep_scientist" in systems:
        targets["deep_scientist_python"] = (
            context["deep_scientist"] / ".venv" / "bin" / "python"
        )
        targets["deep_scientist_opencode"] = (
            context["deep_scientist"] / "node_modules" / ".bin" / "opencode"
        )
    payload = {
        "schema_version": "sixbench-student-setup-status-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_root": str(root.resolve()),
        "ready": all(path.is_file() for path in targets.values()),
        "checks": {
            name: {
                "path": str(path),
                "present": path.is_file(),
            }
            for name, path in targets.items()
        },
    }
    return_dir = root / "RETURN"
    if return_dir.is_symlink():
        raise SetupError("RETURN 目录不得是符号链接")
    return_dir.mkdir(parents=True, exist_ok=True)
    output = return_dir / "SETUP_STATUS.json"
    if output.is_symlink():
        raise SetupError("SETUP_STATUS.json 不得是符号链接")
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(output, 0o600)
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--check", action="store_true")
    result.add_argument("--yes", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = student_handoff.package_root()
    context = load_setup_context(root)
    if args.check:
        print(json.dumps(status(root, context), ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        print(
            "初始化会联网下载冻结依赖，并在包内创建本机虚拟环境和 release lock。"
        )
        confirmation = input("请输入 SETUP 确认：").strip()
        if confirmation != "SETUP":
            print("未确认，初始化没有开始。")
            return 1
    install_runtimes(root, context)
    generate_release_lock(context)
    payload = status(root, context)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ready"]:
        raise SetupError("初始化结束，但仍有必需组件缺失")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SetupError, student_handoff.PackageError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
