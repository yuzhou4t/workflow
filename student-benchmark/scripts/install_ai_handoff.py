#!/usr/bin/env python3
"""Install the AI-facing handoff layer into one curated frozen workspace."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any


OPS_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = OPS_ROOT / "package-template"
OPERATOR_PATH = OPS_ROOT / "scripts" / "case_operator.py"
POLICY_PATH = OPS_ROOT / "config" / "case-assignments.json"


def load_operator() -> Any:
    spec = importlib.util.spec_from_file_location("case_operator", OPERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load operator: {OPERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def relative_to_workspace(workspace: Path, path: Path, label: str) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError as exc:
        raise RuntimeError(f"{label} is outside the frozen workspace: {path}") from exc


def copy_file(source: Path, target: Path, *, replace: bool) -> None:
    if target.exists() and not replace:
        raise FileExistsError(f"refusing to overwrite existing handoff file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def assignment_payload(
    *,
    context: Any,
    rows: list[dict[str, Any]],
    workspace: Path,
    formal: bool,
) -> dict[str, Any]:
    if context.suite is None or context.output_root is None:
        raise RuntimeError("the release package does not contain an executable suite")
    suite_id = context.suite.get("suite_id")
    if not isinstance(suite_id, str) or not suite_id:
        raise RuntimeError("suite_id is missing")
    release_suffix = str(context.release["release_id"]).removeprefix(
        "benchmark-v3-pilot-"
    )
    runs = context.output_root / suite_id / "runs"
    contracts = context.output_root / f"contracts-{release_suffix}"
    orchestration = (
        context.output_root
        / suite_id
        / "student-orchestration"
        / context.assignment_name
    )
    return {
        "schema_version": "sixbench-student-package-v1",
        "package_id": (
            f"sixbench-{context.release['release_id']}-"
            f"case{context.case_number}-{context.assignment_name}"
        ),
        "execution_state": "formal" if formal else "pending_authorization",
        "release_id": context.release["release_id"],
        "case_number": context.case_number,
        "case_id": context.policy["case_id"],
        "case_title_zh": context.policy["title_zh"],
        "assignment": context.assignment_name,
        "assignment_title_zh": context.assignment["title_zh"],
        "systems": context.assignment["systems"],
        "expected_cell_count": context.assignment["expected_external_cells"],
        "paths": {
            "workspace": ".",
            "release_package": "release-package.json",
            "operator": "workflow/student-benchmark/scripts/case_operator.py",
            "output_root": relative_to_workspace(
                workspace, context.output_root, "output_root"
            ),
            "runs": relative_to_workspace(workspace, runs, "runs"),
            "contracts": relative_to_workspace(
                workspace, contracts, "contracts"
            ),
            "orchestration": relative_to_workspace(
                workspace, orchestration, "orchestration"
            ),
        },
        "expected_cells": [
            {
                "cell_id": str(row["cell_id"]),
                "system_id": str(row["system_id"]),
                "input_view": str(row["input_view"]),
                "leaderboard_id": str(row["leaderboard"]),
                "seed": row["seed"],
            }
            for row in rows
        ],
    }


def install(args: argparse.Namespace) -> dict[str, Any]:
    operator = load_operator()
    workspace = args.workspace.expanduser().resolve(strict=True)
    release = args.release.expanduser().resolve(strict=True)
    context = operator.build_context(
        args.case,
        args.assignment,
        workspace,
        release,
        require_python=False,
    )
    rows = operator.enumerate_case_cells(
        context,
        python_override=Path(sys.executable),
    )
    if args.formal:
        if not context.case_release.get("execution_enabled"):
            raise RuntimeError("formal handoff requires execution_enabled=true")
        if context.case_release.get("enabled_assignments") != [args.assignment]:
            raise RuntimeError(
                "formal handoff release must enable exactly this one assignment"
            )
    payload = assignment_payload(
        context=context,
        rows=rows,
        workspace=workspace,
        formal=args.formal,
    )
    windows_copies = tuple(
        (
            source,
            workspace / "tools" / "windows" / source.name,
        )
        for source in sorted((TEMPLATE_ROOT / "tools" / "windows").iterdir())
        if source.is_file()
    )
    copies = (
        (TEMPLATE_ROOT / "AGENTS.md", workspace / "AGENTS.md"),
        (
            TEMPLATE_ROOT / "START_HERE_FOR_AI.md",
            workspace / "START_HERE_FOR_AI.md",
        ),
        (
            TEMPLATE_ROOT / "README_FOR_STUDENT.md",
            workspace / "README_FOR_STUDENT.md",
        ),
        (TEMPLATE_ROOT / "SETUP.command", workspace / "SETUP.command"),
        (TEMPLATE_ROOT / "START.command", workspace / "START.command"),
        (
            TEMPLATE_ROOT / "CHECK_WINDOWS.cmd",
            workspace / "CHECK_WINDOWS.cmd",
        ),
        (
            TEMPLATE_ROOT / "SETUP_WINDOWS.cmd",
            workspace / "SETUP_WINDOWS.cmd",
        ),
        (
            TEMPLATE_ROOT / "START_WINDOWS.cmd",
            workspace / "START_WINDOWS.cmd",
        ),
        (
            TEMPLATE_ROOT / "tools" / "bootstrap.py",
            workspace / "tools" / "bootstrap.py",
        ),
        (
            TEMPLATE_ROOT / "tools" / "student_handoff.py",
            workspace / "tools" / "student_handoff.py",
        ),
        (
            TEMPLATE_ROOT / "schemas" / "student-result-v1.schema.json",
            workspace / "schemas" / "student-result-v1.schema.json",
        ),
        (
            OPERATOR_PATH,
            workspace
            / "workflow"
            / "student-benchmark"
            / "scripts"
            / "case_operator.py",
        ),
        (
            POLICY_PATH,
            workspace
            / "workflow"
            / "student-benchmark"
            / "config"
            / "case-assignments.json",
        ),
        (release, workspace / "release-package.json"),
        *windows_copies,
    )
    assignment_path = workspace / "ASSIGNMENT.json"
    pending_copies = [
        (source, target)
        for source, target in copies
        if source.resolve() != target.resolve()
    ]
    if not args.replace:
        existing = [
            target
            for _, target in pending_copies
            if target.exists()
        ]
        if assignment_path.exists():
            existing.append(assignment_path)
        if existing:
            raise FileExistsError(
                "refusing to overwrite existing handoff files: "
                + ", ".join(str(path) for path in existing)
            )
    for source, target in pending_copies:
        copy_file(source, target, replace=args.replace)
    assignment_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(workspace / "START.command", 0o755)
    os.chmod(workspace / "SETUP.command", 0o755)
    os.chmod(workspace / "tools" / "windows" / "run-in-wsl.sh", 0o755)
    os.chmod(
        workspace / "tools" / "windows" / "controller-entrypoint.sh",
        0o755,
    )
    os.chmod(workspace / "tools" / "bootstrap.py", 0o755)
    os.chmod(workspace / "tools" / "student_handoff.py", 0o755)
    os.chmod(assignment_path, 0o600)
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--case", choices=("005", "007"), required=True)
    result.add_argument(
        "--assignment",
        choices=("hypoweaver", "baselines"),
        required=True,
    )
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument("--release", type=Path, required=True)
    result.add_argument(
        "--formal",
        action="store_true",
        help="require the release to enable exactly this assignment",
    )
    result.add_argument("--replace", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    payload = install(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
