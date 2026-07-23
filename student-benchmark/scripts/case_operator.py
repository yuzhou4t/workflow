#!/usr/bin/env python3
"""Fail-closed operator for delegated SixBench case work."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


OPS_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = OPS_ROOT / "config" / "case-assignments.json"
CASE_NUMBERS = ("005", "007")
VIEWS = ("discovery_blind", "reproduction_aligned")
SYSTEMS = (
    "hypoweaver",
    "agent_laboratory",
    "data_to_paper",
    "direct_qwen",
    "qwen_code_agent_writer",
    "deep_scientist",
)
BOARDS = ("native_system_package", "common_executor_reasoning_control")
NATIVE_COMMANDS = {
    "hypoweaver": ("run-hypoweaver",),
    "agent_laboratory": ("run-agent-laboratory",),
    "data_to_paper": ("run-data-to-paper",),
    "direct_qwen": ("run-qwen", "--system", "direct_qwen"),
    "qwen_code_agent_writer": (
        "run-qwen",
        "--system",
        "qwen_code_agent_writer",
    ),
    "deep_scientist": ("run-deep-scientist",),
}


class OperatorError(RuntimeError):
    """A user-actionable, fail-closed operator error."""


@dataclass(frozen=True)
class Context:
    case_number: str
    policy: dict[str, Any]
    release: dict[str, Any]
    case_release: dict[str, Any]
    workspace: Path
    harness: Path
    python: Path
    protocol: Path
    suite_path: Path | None
    suite: dict[str, Any] | None
    executor_contract: Path | None
    runtime_config: Path | None
    output_root: Path | None


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OperatorError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OperatorError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OperatorError(f"expected a JSON object: {path}")
    return value


def load_policies() -> dict[str, dict[str, Any]]:
    payload = load_json(POLICY_PATH)
    if payload.get("schema_version") != "sixbench-student-assignments-v1":
        raise OperatorError("unsupported assignment policy schema")
    cases = payload.get("cases")
    if not isinstance(cases, dict) or tuple(sorted(cases)) != CASE_NUMBERS:
        raise OperatorError("assignment policy must contain exactly Cases 005/007")
    return cases


def resolve_beneath(
    workspace: Path,
    relative: str,
    label: str,
    *,
    follow_symlinks: bool = True,
) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise OperatorError(f"{label} must be relative to the frozen workspace")
    root = workspace.expanduser().resolve()
    lexical = Path(os.path.abspath(root / raw))
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise OperatorError(f"{label} escapes the frozen workspace: {relative}") from exc
    candidate = lexical.resolve() if follow_symlinks else lexical
    if follow_symlinks:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise OperatorError(f"{label} resolves outside the frozen workspace") from exc
    return candidate


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode != 0:
        tail = "\n".join(output.splitlines()[-30:])
        raise OperatorError(
            f"command failed ({completed.returncode}): {' '.join(command)}"
            + (f"\n{tail}" if tail else "")
        )
    return output


def harness_env(harness: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def build_context(
    case_number: str,
    workspace: Path,
    release_path: Path,
) -> Context:
    policies = load_policies()
    policy = policies[case_number]
    release = load_json(release_path.expanduser().resolve())
    if release.get("schema_version") != "sixbench-student-release-v1":
        raise OperatorError("unsupported release package schema")
    release_id = release.get("release_id")
    commit = release.get("harness_commit")
    if not isinstance(release_id, str) or not release_id:
        raise OperatorError("release package is missing release_id")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise OperatorError("release package must pin a full 40-character harness commit")

    workspace = workspace.expanduser().resolve()
    harness = resolve_beneath(workspace, str(release.get("harness_repo", "")), "harness_repo")
    python = resolve_beneath(
        workspace,
        str(release.get("python", "")),
        "python",
        follow_symlinks=False,
    )
    protocol = resolve_beneath(workspace, str(release.get("protocol", "")), "protocol")
    for path, label in (
        (harness, "harness repository"),
        (python, "frozen Python"),
        (protocol, "frozen protocol"),
    ):
        if not path.exists():
            raise OperatorError(f"{label} does not exist: {path}")

    actual_commit = run_checked(["git", "rev-parse", "HEAD"], cwd=harness).strip()
    if actual_commit != commit:
        raise OperatorError(
            f"harness commit mismatch: expected {commit}, observed {actual_commit}"
        )
    dirty = run_checked(["git", "status", "--porcelain"], cwd=harness)
    if dirty:
        raise OperatorError("frozen harness worktree is not clean")

    release_cases = release.get("cases")
    if not isinstance(release_cases, dict) or case_number not in release_cases:
        raise OperatorError(f"release package does not declare Case {case_number}")
    case_release = release_cases[case_number]
    if not isinstance(case_release, dict):
        raise OperatorError(f"invalid release entry for Case {case_number}")

    suite_path: Path | None = None
    suite: dict[str, Any] | None = None
    executor_contract: Path | None = None
    runtime_config: Path | None = None
    output_root: Path | None = None
    suite_value = case_release.get("suite")
    if suite_value is not None:
        if not isinstance(suite_value, str):
            raise OperatorError("suite path must be a string")
        suite_path = resolve_beneath(workspace, suite_value, "suite")
        suite = load_json(suite_path)
        if suite.get("independent_case_id") != policy["case_id"]:
            raise OperatorError("suite case identity does not match the assignment")
        paths = suite.get("paths")
        if not isinstance(paths, dict):
            raise OperatorError("suite is missing paths")
        runtime_config = (suite_path.parent / str(paths.get("runtime_config", ""))).resolve()
        output_root = (suite_path.parent / str(paths.get("output_root", ""))).resolve()
        for path, label in ((runtime_config, "runtime_config"), (output_root, "output_root")):
            try:
                path.relative_to(workspace)
            except ValueError as exc:
                raise OperatorError(f"suite {label} escapes the frozen workspace") from exc

    contract_value = case_release.get("executor_contract")
    if contract_value is not None:
        if not isinstance(contract_value, str):
            raise OperatorError("executor_contract must be a string")
        executor_contract = resolve_beneath(workspace, contract_value, "executor_contract")
        if not executor_contract.is_file():
            raise OperatorError(f"missing executor contract: {executor_contract}")

    return Context(
        case_number=case_number,
        policy=policy,
        release=release,
        case_release=case_release,
        workspace=workspace,
        harness=harness,
        python=python,
        protocol=protocol,
        suite_path=suite_path,
        suite=suite,
        executor_contract=executor_contract,
        runtime_config=runtime_config,
        output_root=output_root,
    )


def write_runtime_config(
    path: Path,
    *,
    api_key: str,
    model: str,
    base_url: str,
    replace: bool,
) -> None:
    if not api_key.strip():
        raise OperatorError("API key cannot be empty")
    if path.is_symlink():
        raise OperatorError("runtime config must not be a symbolic link")
    if path.exists() and not replace:
        raise OperatorError(f"runtime config already exists: {path}; use --replace only after approval")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "qwen_api_key": api_key.strip(),
        "qwen_model": model,
        "qwen_base_url": base_url,
        "research_engine_url": "http://127.0.0.1:9000",
        "research_engine_token": secrets.token_urlsafe(32),
    }
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def configure_api(context: Context, *, replace: bool) -> None:
    if context.policy["execution_mode"] != "validation_matrix":
        raise OperatorError(f"Case {context.case_number} is audit-only and does not need an API key")
    if context.suite is None or context.runtime_config is None:
        raise OperatorError("release package is missing the executable suite")
    provider = context.suite.get("provider")
    if not isinstance(provider, dict):
        raise OperatorError("suite is missing provider configuration")
    model = provider.get("model")
    base_url = provider.get("default_base_url")
    if not isinstance(model, str) or not isinstance(base_url, str):
        raise OperatorError("suite provider model/base URL is invalid")
    api_key = getpass.getpass("DashScope API Key（输入不会回显）: ")
    write_runtime_config(
        context.runtime_config,
        api_key=api_key,
        model=model,
        base_url=base_url,
        replace=replace,
    )
    print(f"API configuration written with mode 0600: {context.runtime_config}")


def validate_runtime_config(context: Context) -> None:
    if context.runtime_config is None or context.suite is None:
        raise OperatorError("suite does not declare a runtime config")
    payload = load_json(context.runtime_config)
    provider = context.suite["provider"]
    if not isinstance(payload.get("qwen_api_key"), str) or not payload["qwen_api_key"].strip():
        raise OperatorError("runtime config does not contain a non-empty qwen_api_key")
    if payload.get("qwen_model") != provider.get("model"):
        raise OperatorError("runtime config model differs from the frozen suite")
    configured_url = payload.get("qwen_base_url")
    if configured_url not in (None, provider.get("default_base_url")):
        raise OperatorError("runtime config endpoint differs from the frozen suite")
    permissions = stat.S_IMODE(context.runtime_config.stat().st_mode)
    if permissions & 0o077:
        raise OperatorError(
            f"runtime config permissions are too broad: {oct(permissions)}; expected 0o600"
        )


def cli(context: Context, *arguments: str) -> list[str]:
    if context.suite_path is None:
        raise OperatorError("this assignment has no suite config")
    return [
        str(context.python),
        "-m",
        "sixbench.cli",
        "--config",
        str(context.suite_path),
        *arguments,
    ]


def validate_suite(context: Context) -> None:
    if context.suite_path is None:
        return
    run_checked(
        cli(context, "validate"),
        cwd=context.harness,
        env=harness_env(context.harness),
        timeout=900,
    )


def validate_authorization(context: Context) -> None:
    if context.suite_path is None:
        raise OperatorError("authorization validation requires a suite")
    source = (
        "from pathlib import Path\n"
        "from sixbench.config import load_suite_config\n"
        "from sixbench.cli import _validate_release_lock_for_suite, _assert_outbound_authorized\n"
        "config = load_suite_config(Path(__import__('sys').argv[1]))\n"
        "protocol = Path(__import__('sys').argv[2])\n"
        "_validate_release_lock_for_suite(config, protocol_path=protocol)\n"
        "_assert_outbound_authorized(config)\n"
        "print('release lock and outbound authorization are valid')\n"
    )
    run_checked(
        [str(context.python), "-c", source, str(context.suite_path), str(context.protocol)],
        cwd=context.harness,
        env=harness_env(context.harness),
        timeout=900,
    )


def enumerate_case_cells(context: Context) -> list[dict[str, Any]]:
    if context.policy["execution_mode"] != "validation_matrix":
        raise OperatorError(f"Case {context.case_number} has no external execution plan")
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"sixbench-case{context.case_number}-") as temp:
        for board in BOARDS:
            output = Path(temp) / f"{board}.json"
            run_checked(
                [
                    str(context.python),
                    "-m",
                    "sixbench.protocol_v3",
                    "--protocol",
                    str(context.protocol),
                    "--plan",
                    "validation",
                    "--leaderboard",
                    board,
                    "--output",
                    str(output),
                ],
                cwd=context.harness,
                env=harness_env(context.harness),
                timeout=900,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise OperatorError("protocol plan is not a list")
            selected = [row for row in payload if row.get("case_id") == context.policy["case_id"]]
            if len(selected) != 12:
                raise OperatorError(
                    f"expected 12 {board} cells for Case {context.case_number}, found {len(selected)}"
                )
            rows.extend(selected)
    system_rank = {value: index for index, value in enumerate(SYSTEMS)}
    view_rank = {value: index for index, value in enumerate(VIEWS)}
    board_rank = {value: index for index, value in enumerate(BOARDS)}
    rows.sort(
        key=lambda row: (
            board_rank[str(row["leaderboard"])],
            view_rank[str(row["input_view"])],
            system_rank[str(row["system_id"])],
        )
    )
    identities = {str(row["cell_id"]) for row in rows}
    if len(rows) != 24 or len(identities) != 24:
        raise OperatorError("the Case plan must contain 24 unique cells")
    return rows


def run_local_tests(context: Context) -> None:
    tests = context.policy.get("local_tests")
    if not isinstance(tests, list) or not tests:
        raise OperatorError("assignment does not declare local tests")
    for relative in tests:
        path = context.harness / str(relative)
        if not path.is_file():
            raise OperatorError(f"missing declared test: {path}")
    run_checked(
        [str(context.python), "-m", "pytest", "-q", *[str(item) for item in tests]],
        cwd=context.harness,
        env=harness_env(context.harness),
        timeout=1800,
    )


def preflight(context: Context, *, report_path: Path | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def checked(name: str, action: Any) -> bool:
        try:
            action()
        except (OperatorError, subprocess.TimeoutExpired, OSError) as exc:
            checks.append({"name": name, "status": "failed", "message": str(exc)})
            return False
        checks.append({"name": name, "status": "passed"})
        return True

    checked("local_contract_tests", lambda: run_local_tests(context))
    if context.suite_path is not None:
        checked("suite_validation", lambda: validate_suite(context))

    plan: list[dict[str, Any]] = []
    if context.policy["execution_mode"] == "validation_matrix":
        checked("matrix_shape_24", lambda: plan.extend(enumerate_case_cells(context)))

    local_passed = all(row["status"] == "passed" for row in checks)
    external_ready = False
    blockers: list[str] = []
    if not context.policy["external_execution_allowed"]:
        blockers.append("assignment policy marks this Case as local audit only")
    elif not bool(context.case_release.get("execution_enabled")):
        blockers.append("release package has execution_enabled=false")
    elif local_passed:
        runtime_ok = checked("runtime_config", lambda: validate_runtime_config(context))
        authorization_ok = checked("release_and_authorization", lambda: validate_authorization(context))
        external_ready = runtime_ok and authorization_ok
        if not runtime_ok:
            blockers.append("runtime API configuration is missing or invalid")
        if not authorization_ok:
            blockers.append("release lock or hash-bound authorization receipt is invalid")
    else:
        blockers.append("one or more offline checks failed")

    payload = {
        "schema_version": "sixbench-student-preflight-v1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "case_number": context.case_number,
        "case_id": context.policy["case_id"],
        "release_id": context.release["release_id"],
        "preflight_passed": local_passed,
        "external_execution_ready": external_ready,
        "expected_external_cells": context.policy["expected_external_cells"],
        "planned_cells": len(plan),
        "checks": checks,
        "blockers": blockers,
    }
    if report_path is not None:
        report_path = report_path.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(report_path, 0o600)
    return payload


def emit(log_dir: Path, event: str, **fields: object) -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_step(
    context: Context,
    log_dir: Path,
    *,
    cell_id: str,
    step: str,
    command: list[str],
    timeout: int,
) -> int:
    log_path = log_dir / f"{cell_id}.{step}.log"
    emit(log_dir, "step_started", cell_id=cell_id, step=step, log=str(log_path))
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as handle:
        try:
            completed = subprocess.run(
                command,
                cwd=context.harness,
                env=harness_env(context.harness),
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            return_code = completed.returncode
        except subprocess.TimeoutExpired:
            return_code = 124
            handle.write(f"\nouter operator timeout after {timeout} seconds\n")
    emit(
        log_dir,
        "step_finished",
        cell_id=cell_id,
        step=step,
        return_code=return_code,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return return_code


def assert_no_integrity_failure(run_dir: Path, cell_id: str) -> None:
    failure = run_dir / "benchmark_integrity_failure.json"
    if failure.is_file():
        payload = load_json(failure)
        raise OperatorError(
            f"{cell_id}: benchmark integrity failure: "
            f"{payload.get('reason_code')}: {payload.get('message')}"
        )


def verify_adapted(run_dir: Path) -> None:
    missing = [
        str(path)
        for path in (
            run_dir / "normalized_result.json",
            run_dir / "evidence_manifest.json",
        )
        if not path.is_file()
    ]
    if missing:
        raise OperatorError(f"adapter did not create required artifacts: {missing}")


def seal_manifest(
    context: Context,
    *,
    row: dict[str, Any],
    contracts_root: Path,
) -> Path:
    cell_id = str(row["cell_id"])
    output_dir = contracts_root / cell_id
    manifest = output_dir / "cell_manifest.json"
    if not manifest.is_file():
        run_checked(
            cli(
                context,
                "seal-cell-manifest-v3",
                "--protocol",
                str(context.protocol),
                "--phase",
                "validation",
                "--leaderboard",
                str(row["leaderboard"]),
                "--cell-id",
                cell_id,
                "--output-dir",
                str(output_dir),
            ),
            cwd=context.harness,
            env=harness_env(context.harness),
            timeout=900,
        )
    payload = load_json(manifest)
    expected = {
        "cell_id": cell_id,
        "run_id": cell_id,
        "case_id": context.policy["case_id"],
        "input_view": row["input_view"],
        "system_id": row["system_id"],
        "leaderboard_id": row["leaderboard"],
    }
    mismatches = {
        key: {"expected": value, "observed": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise OperatorError(f"sealed manifest identity mismatch: {mismatches}")
    return manifest


def run_native_cell(
    context: Context,
    row: dict[str, Any],
    manifest: Path,
    run_root: Path,
    log_dir: Path,
) -> None:
    cell_id = str(row["cell_id"])
    system = str(row["system_id"])
    run_dir = run_root / cell_id
    output = run_dir / "benchmark_output.json"
    emit(log_dir, "cell_started", board=BOARDS[0], cell_id=cell_id)
    assert_no_integrity_failure(run_dir, cell_id)
    if (run_dir / "normalized_result.json").is_file() and (
        run_dir / "evidence_manifest.json"
    ).is_file():
        emit(log_dir, "cell_skipped_already_adapted", cell_id=cell_id)
        return
    if run_dir.exists() and not output.is_file():
        raise OperatorError(
            f"{cell_id}: partial run exists without benchmark_output.json; refusing rerun"
        )
    runtime = context.suite["runtime"] if context.suite is not None else {}
    outer_timeout = int(runtime.get("cell_wall_time_limit_seconds", 5400)) + 600
    run_rc: int | None = None
    if not output.is_file():
        run_rc = run_step(
            context,
            log_dir,
            cell_id=cell_id,
            step="run",
            command=cli(
                context,
                *NATIVE_COMMANDS[system],
                "--protocol",
                str(context.protocol),
                "--cell-manifest",
                str(manifest),
            ),
            timeout=outer_timeout,
        )
        assert_no_integrity_failure(run_dir, cell_id)
        if not output.is_file():
            raise OperatorError(
                f"{cell_id}: runner returned {run_rc} without benchmark_output.json"
            )
    adapt_rc = run_step(
        context,
        log_dir,
        cell_id=cell_id,
        step="adapt",
        command=cli(
            context,
            "adapt-native-v3",
            "--protocol",
            str(context.protocol),
            "--cell-manifest",
            str(manifest),
        ),
        timeout=900,
    )
    if adapt_rc != 0:
        raise OperatorError(f"{cell_id}: native adapter returned {adapt_rc}")
    verify_adapted(run_dir)
    status = load_json(output).get("run_status")
    emit(
        log_dir,
        "cell_finished",
        board=BOARDS[0],
        cell_id=cell_id,
        runner_return_code=run_rc,
        run_status=status,
    )


def adapt_common_failure(
    context: Context,
    *,
    cell_id: str,
    manifest: Path,
    run_dir: Path,
    log_dir: Path,
) -> None:
    failure = run_dir / "common-stage-failure.json"
    if not failure.is_file():
        raise OperatorError(f"{cell_id}: common failure has no sealed failure artifact")
    payload = load_json(failure)
    details = payload.get("failure")
    message = str(details.get("message", "")).lower() if isinstance(details, dict) else ""
    integrity_markers = (
        "effective provider model",
        "model binding",
        "requested models are not registered",
        "interrupted model request",
        "model receipts differ",
    )
    if any(marker in message for marker in integrity_markers):
        raise OperatorError(f"{cell_id}: model integrity failure: {message}")
    return_code = run_step(
        context,
        log_dir,
        cell_id=cell_id,
        step="adapt-failure",
        command=cli(
            context,
            "adapt-common-failure-v3",
            "--protocol",
            str(context.protocol),
            "--cell-manifest",
            str(manifest),
            "--failure-artifact",
            str(failure),
        ),
        timeout=900,
    )
    if return_code != 0:
        raise OperatorError(f"{cell_id}: common failure adapter returned {return_code}")
    verify_adapted(run_dir)
    emit(
        log_dir,
        "cell_finished",
        board=BOARDS[1],
        cell_id=cell_id,
        run_status="failed",
        failure_reason_code=payload.get("failure_reason_code"),
    )


def run_common_cell(
    context: Context,
    row: dict[str, Any],
    manifest: Path,
    run_root: Path,
    log_dir: Path,
) -> None:
    if context.executor_contract is None:
        raise OperatorError("common board requires an executor contract")
    cell_id = str(row["cell_id"])
    run_dir = run_root / cell_id
    request = run_dir / "analysis-request.json"
    result = run_dir / "common-execution-result.json"
    receipt = run_dir / "common-execution-receipt.json"
    claim = run_dir / "claim-decision.json"
    failure = run_dir / "common-stage-failure.json"
    emit(log_dir, "cell_started", board=BOARDS[1], cell_id=cell_id)
    if (run_dir / "normalized_result.json").is_file() and (
        run_dir / "evidence_manifest.json"
    ).is_file():
        emit(log_dir, "cell_skipped_already_adapted", cell_id=cell_id)
        return
    if failure.is_file():
        adapt_common_failure(
            context,
            cell_id=cell_id,
            manifest=manifest,
            run_dir=run_dir,
            log_dir=log_dir,
        )
        return
    if run_dir.exists():
        raise OperatorError(
            f"{cell_id}: an incomplete common run already exists; preserve it and "
            "request a coordinator disposition instead of resuming"
        )
    runtime = context.suite["runtime"] if context.suite is not None else {}
    outer_timeout = int(runtime.get("cell_wall_time_limit_seconds", 5400)) + 600
    executor_timeout = int(runtime.get("statistical_phase_wall_time_limit_seconds", 3600)) + 600
    if not request.is_file():
        pre_rc = run_step(
            context,
            log_dir,
            cell_id=cell_id,
            step="pre-result",
            command=cli(
                context,
                "run-common-stage-v3",
                "--stage",
                "pre_result",
                "--protocol",
                str(context.protocol),
                "--cell-manifest",
                str(manifest),
            ),
            timeout=outer_timeout,
        )
        if pre_rc != 0:
            adapt_common_failure(
                context,
                cell_id=cell_id,
                manifest=manifest,
                run_dir=run_dir,
                log_dir=log_dir,
            )
            return
        if not request.is_file():
            raise OperatorError(f"{cell_id}: pre-result stage omitted analysis-request.json")
    if result.is_file() != receipt.is_file():
        raise OperatorError(
            f"{cell_id}: common result/receipt pair is incomplete; refusing execution"
        )
    if not result.is_file():
        case_root = Path(str(row["case_root"])).resolve()
        try:
            case_root.relative_to(context.workspace)
        except ValueError as exc:
            raise OperatorError("protocol case root escapes the frozen workspace") from exc
        validate_rc = run_step(
            context,
            log_dir,
            cell_id=cell_id,
            step="executor-validate",
            command=[
                str(context.python),
                "-m",
                "sixbench.common_executor",
                "--request",
                str(request),
                "--case",
                str(case_root),
                "--contract",
                str(context.executor_contract),
                "--validate-only",
            ],
            timeout=900,
        )
        if validate_rc != 0:
            raise OperatorError(f"{cell_id}: common executor validation returned {validate_rc}")
        execute_rc = run_step(
            context,
            log_dir,
            cell_id=cell_id,
            step="executor-run",
            command=[
                str(context.python),
                "-m",
                "sixbench.common_executor",
                "--request",
                str(request),
                "--case",
                str(case_root),
                "--contract",
                str(context.executor_contract),
                "--output",
                str(result),
                "--protocol",
                str(context.protocol),
                "--cell-manifest",
                str(manifest),
                "--receipt",
                str(receipt),
            ],
            timeout=executor_timeout,
        )
        if execute_rc != 0 or not result.is_file() or not receipt.is_file():
            raise OperatorError(f"{cell_id}: common executor failed to seal result and receipt")
    if not claim.is_file():
        post_rc = run_step(
            context,
            log_dir,
            cell_id=cell_id,
            step="post-result",
            command=cli(
                context,
                "run-common-stage-v3",
                "--stage",
                "post_result",
                "--protocol",
                str(context.protocol),
                "--cell-manifest",
                str(manifest),
                "--execution-result",
                str(result),
                "--execution-receipt",
                str(receipt),
            ),
            timeout=outer_timeout,
        )
        if post_rc != 0:
            adapt_common_failure(
                context,
                cell_id=cell_id,
                manifest=manifest,
                run_dir=run_dir,
                log_dir=log_dir,
            )
            return
        if not claim.is_file():
            raise OperatorError(f"{cell_id}: post-result stage omitted claim-decision.json")
    adapt_rc = run_step(
        context,
        log_dir,
        cell_id=cell_id,
        step="adapt",
        command=cli(
            context,
            "adapt-common-v3",
            "--protocol",
            str(context.protocol),
            "--cell-manifest",
            str(manifest),
            "--execution-result",
            str(result),
            "--execution-receipt",
            str(receipt),
            "--claim-decision",
            str(claim),
        ),
        timeout=900,
    )
    if adapt_rc != 0:
        raise OperatorError(f"{cell_id}: common adapter returned {adapt_rc}")
    verify_adapted(run_dir)
    emit(log_dir, "cell_finished", board=BOARDS[1], cell_id=cell_id, run_status="completed")


def run_matrix(context: Context) -> None:
    report = preflight(context)
    if not report["external_execution_ready"]:
        raise OperatorError(
            "external execution is not ready: " + "; ".join(report["blockers"])
        )
    if context.suite is None or context.output_root is None:
        raise OperatorError("release package is missing suite output information")
    plan = enumerate_case_cells(context)
    suite_id = context.suite.get("suite_id")
    if not isinstance(suite_id, str) or not suite_id:
        raise OperatorError("suite is missing suite_id")
    release_suffix = str(context.release["release_id"]).removeprefix("benchmark-v3-pilot-")
    contracts_root = context.output_root / f"contracts-{release_suffix}"
    run_root = context.output_root / suite_id / "runs"
    log_dir = context.output_root / suite_id / "student-orchestration"
    emit(log_dir, "batch_started", case_number=context.case_number, suite_id=suite_id)
    for row in plan:
        manifest = seal_manifest(context, row=row, contracts_root=contracts_root)
        if row["leaderboard"] == BOARDS[0]:
            run_native_cell(context, row, manifest, run_root, log_dir)
        else:
            run_common_cell(context, row, manifest, run_root, log_dir)
    emit(log_dir, "batch_finished", case_number=context.case_number, suite_id=suite_id)


def status(case_number: str) -> None:
    policy = load_policies()[case_number]
    print(
        json.dumps(
            {
                "case_number": case_number,
                "case_id": policy["case_id"],
                "title_zh": policy["title_zh"],
                "track": policy["track"],
                "execution_mode": policy["execution_mode"],
                "external_execution_allowed": policy["external_execution_allowed"],
                "expected_external_cells": policy["expected_external_cells"],
                "assignment": policy["assignment"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="show the immutable assignment policy")
    status_parser.add_argument("--case", choices=CASE_NUMBERS, required=True)

    for name in ("configure-api", "preflight", "plan", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("--case", choices=CASE_NUMBERS, required=True)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--release", type=Path, required=True)
        if name == "configure-api":
            command.add_argument("--replace", action="store_true")
        if name == "preflight":
            command.add_argument("--report", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "status":
        status(args.case)
        return 0
    context = build_context(args.case, args.workspace, args.release)
    if args.command == "configure-api":
        configure_api(context, replace=args.replace)
        return 0
    if args.command == "preflight":
        payload = preflight(context, report_path=args.report)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["preflight_passed"] else 1
    if args.command == "plan":
        rows = enumerate_case_cells(context)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        run_matrix(context)
        return 0
    raise OperatorError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OperatorError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
