from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .engine import DESIGN_RETRY_MODEL, REVIEWER_MODEL, WRITER_ESCALATION_MODEL
from .models import ContractBudget
from .research_api import registry_path_sha256, runtime_identity
from .runtime_config import EffectiveRuntimeConfig, RuntimeConfigStore
from .system_comparison_v2 import (
    SystemComparisonRunOutputV2,
    SystemResourceUsageV2,
    SystemRuntimeEnvelopeV2,
    derive_system_resource_usage_v2,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_OUTPUT_ROOT = WORKSPACE_ROOT / "benchmark-results-v2-dev"
_DEFAULT_CONTRACT_BUDGET = ContractBudget()
CELL_WALL_TIME_LIMIT_SECONDS = (
    _DEFAULT_CONTRACT_BUDGET.max_end_to_end_wall_time_seconds
)
HYPOWEAVER_V2_BUDGET_FLAG = "--v2-model-budget"
INDEPENDENT_CASE_ID = "case_002_green_credit_high_pollution"
BENCHMARK_TASK_TRACK = "given_input_method_experiment_write"
COMPARISON_ESTIMAND = "system_package_capability"
MODEL_CONTROL = "native_role_routing_disclosed_not_equalized"
FROZEN_DEFAULT_MODEL = "qwen3.7-plus"
FROZEN_ESCALATION_MODEL = "qwen3.7-max"
_MODEL_DISCLOSURE_KEYS = (
    "model",
    "default_model",
    "comparison_estimand",
    "model_control",
    "system_model_routing",
)
ALIGNED_EVENT_STUDY_FREEZE = {
    "event_remote_pre_years": [1998, 1999, 2000, 2001],
    "event_term_scaling": "binary_group_year_contrast",
}
FROZEN_AGENT_UPSTREAM_FILES = (
    "agents.py",
    "ai_lab_repo.py",
    "mlesolver.py",
    "papersolver.py",
)
CASE_DIRECTORIES = {
    "discovery_blind": (
        "case_002_green_credit_high_pollution_discovery_blind_v2_dev"
    ),
    "reproduction_aligned": (
        "case_002_green_credit_high_pollution_reproduction_aligned_v2_dev"
    ),
}


@dataclass(frozen=True)
class Cell:
    system_id: Literal["hypoweaver", "agent_laboratory"]
    input_view: Literal["discovery_blind", "reproduction_aligned"]


CELLS = (
    Cell("agent_laboratory", "discovery_blind"),
    Cell("hypoweaver", "discovery_blind"),
    Cell("hypoweaver", "reproduction_aligned"),
    Cell("agent_laboratory", "reproduction_aligned"),
)


def _model_disclosure(default_model: str) -> dict[str, Any]:
    if default_model != FROZEN_DEFAULT_MODEL:
        raise ValueError(
            f"Case002 v2 dev is frozen to {FROZEN_DEFAULT_MODEL}"
        )
    if {
        REVIEWER_MODEL,
        DESIGN_RETRY_MODEL,
        WRITER_ESCALATION_MODEL,
    } != {FROZEN_ESCALATION_MODEL}:
        raise ValueError(
            "HypoWeaver role-model routing no longer matches Case002 v2"
        )
    return {
        # Compatibility alias retained for existing parsers.
        "model": default_model,
        "default_model": default_model,
        "comparison_estimand": COMPARISON_ESTIMAND,
        "model_control": MODEL_CONTROL,
        "system_model_routing": {
            "agent_laboratory": {
                "default_model": default_model,
                "all_roles_model": default_model,
                "role_overrides": {},
            },
            "hypoweaver": {
                "default_model": default_model,
                "role_overrides": {
                    "reviewer": REVIEWER_MODEL,
                    "scientific_audit": REVIEWER_MODEL,
                    "design_retry": DESIGN_RETRY_MODEL,
                    "writer_escalation": WRITER_ESCALATION_MODEL,
                },
            },
        },
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_file_group(root: Path, paths: list[Path]) -> dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    files: dict[str, str] = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"frozen source must be a regular file: {path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(f"frozen source escapes its root: {path}")
        files[str(resolved.relative_to(resolved_root))] = _sha256(resolved)
    if not files:
        raise ValueError(f"frozen source group is empty: {root}")
    return {"sha256": _canonical_sha256(files), "files": files}


def _source_snapshot() -> dict[str, Any]:
    """Hash explicit source groups without including mutable git metadata."""

    hypoweaver_root = PROJECT_ROOT / "backend" / "src" / "hypoweaver"
    harness_names = {"case002_v2_dev_runner.py", "system_comparison_v2.py"}
    hypoweaver = _hash_file_group(
        hypoweaver_root,
        [
            path
            for path in hypoweaver_root.glob("*.py")
            if path.name not in harness_names
        ],
    )
    harness = _hash_file_group(
        hypoweaver_root,
        [hypoweaver_root / name for name in sorted(harness_names)],
    )
    agent_root = WORKSPACE_ROOT / "Agent Laboratory"
    adapter_root = agent_root / "benchmark_adapter"
    agent_adapter = _hash_file_group(
        adapter_root,
        list(adapter_root.glob("*.py")),
    )
    frozen_upstream = _hash_file_group(
        agent_root,
        [agent_root / name for name in FROZEN_AGENT_UPSTREAM_FILES],
    )
    agent_combined_sha256 = _canonical_sha256(
        {
            "adapter": agent_adapter["sha256"],
            "frozen_upstream": frozen_upstream["sha256"],
        }
    )
    return {
        "source_sha256": {
            "hypoweaver": hypoweaver["sha256"],
            "agent_laboratory": agent_combined_sha256,
            "benchmark_harness": harness["sha256"],
        },
        "source_component_sha256": {
            "hypoweaver": hypoweaver["sha256"],
            "agent_laboratory_adapter": agent_adapter["sha256"],
            "agent_laboratory_frozen_upstream": frozen_upstream["sha256"],
            "benchmark_harness": harness["sha256"],
        },
        "source_files": {
            "hypoweaver": hypoweaver["files"],
            "agent_laboratory_adapter": agent_adapter["files"],
            "agent_laboratory_frozen_upstream": frozen_upstream["files"],
            "benchmark_harness": harness["files"],
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _build_hypoweaver_command(
    *,
    case_root: Path,
    output_root: Path,
    registry_path: Path,
    run_id: str,
) -> list[str]:
    """Return the frozen v2 HypoWeaver command for one comparison cell."""

    return [
        sys.executable,
        "-m",
        "hypoweaver.case_benchmark_cli",
        "--case-root",
        str(case_root),
        "--output-root",
        str(output_root),
        "--registry-path",
        str(registry_path),
        "--run-id",
        run_id,
        HYPOWEAVER_V2_BUDGET_FLAG,
    ]


def _validate_agent_code_runner(config_path: Path) -> dict[str, Any]:
    agent_root = WORKSPACE_ROOT / "Agent Laboratory"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(agent_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmark_adapter.cli",
            "--config",
            str(config_path),
            "--validate-code-runner",
        ],
        cwd=agent_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1_000:]
        raise ValueError(f"Agent Laboratory code-runner smoke failed: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Agent Laboratory code-runner smoke returned invalid JSON") from error
    if (
        not isinstance(result, dict)
        or result.get("status") != "valid"
        or result.get("code_runner_smoke") != "AGENT_LAB_CODE_RUNNER_SMOKE_OK"
        or result.get("network_called") is not False
    ):
        raise ValueError("Agent Laboratory code-runner smoke did not seal success")
    return result


def _validate_visible_manifest(case_root: Path) -> str:
    manifest_path = case_root / "case_manifest.json"
    manifest = _load_json(manifest_path)
    if manifest.get("hidden_reference_access") != "denied":
        raise ValueError(f"hidden reference access is not denied: {case_root.name}")
    visible = manifest.get("visible_input") or {}
    entries = visible.get("files") or []
    if not isinstance(entries, list):
        raise ValueError(f"invalid visible manifest entries: {case_root.name}")
    recorded: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"invalid visible manifest entry: {case_root.name}")
        recorded[entry["path"]] = entry
    input_root = case_root / "01_model_input"
    actual_paths = sorted(path for path in input_root.iterdir() if path.is_file())
    actual_names = {str(path.relative_to(case_root)) for path in actual_paths}
    if set(recorded) != actual_names:
        raise ValueError(f"visible manifest file set mismatch: {case_root.name}")
    for path in actual_paths:
        relative = str(path.relative_to(case_root))
        entry = recorded[relative]
        if entry.get("sha256") != _sha256(path):
            raise ValueError(f"visible manifest SHA256 mismatch: {relative}")
        if entry.get("size_bytes") != path.stat().st_size:
            raise ValueError(f"visible manifest size mismatch: {relative}")
    return _sha256(manifest_path)


def _validate_suite_id(suite_id: str) -> str:
    value = suite_id.strip()
    if not value or value in {".", ".."} or Path(value).name != value:
        raise ValueError("suite_id must be one non-empty path-safe name")
    return value


def _validate_research_engine_health(
    runtime: EffectiveRuntimeConfig,
    *,
    expected_registry_path: Path,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Verify the frozen executor code and suite-specific registry binding."""

    if not runtime.research_engine_url:
        raise ValueError("Research Engine URL is not configured")
    endpoint = f"{runtime.research_engine_url.rstrip('/')}/v1/health"
    headers = (
        {"Authorization": f"Bearer {runtime.research_engine_token}"}
        if runtime.research_engine_token
        else {}
    )
    trust_env = urlsplit(endpoint).hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    try:
        with httpx.Client(
            timeout=10,
            transport=transport,
            trust_env=trust_env,
        ) as client:
            response = client.get(endpoint, headers=headers)
            response.raise_for_status()
            health = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("Research Engine preflight failed: unavailable") from error
    if not isinstance(health, dict):
        raise ValueError("Research Engine preflight failed: invalid health payload")
    if health.get("status") != "ok":
        raise ValueError("Research Engine preflight failed: unhealthy status")

    local_identity = runtime_identity()
    identity_keys = set(local_identity)
    actual_identity = {key: health.get(key) for key in identity_keys}
    if actual_identity != local_identity:
        raise ValueError("Research Engine runtime identity mismatch")

    expected_registry_sha256 = registry_path_sha256(expected_registry_path)
    if health.get("dataset_registry_path_sha256") != expected_registry_sha256:
        raise ValueError("Research Engine dataset registry identity mismatch")
    expected_keys = {
        "status",
        "dataset_registry_path_sha256",
        *identity_keys,
    }
    if set(health) != expected_keys:
        raise ValueError("Research Engine runtime identity fields mismatch")
    return {
        "status": "ok",
        "runtime_identity": local_identity,
        "dataset_registry_path_sha256": expected_registry_sha256,
    }


def _preflight(
    output_root: Path,
    *,
    suite_id: str,
    runtime: EffectiveRuntimeConfig,
    research_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    suite_id = _validate_suite_id(suite_id)
    if not runtime.qwen_api_key:
        raise ValueError("Qwen API key is not configured")
    if runtime.qwen_model != FROZEN_DEFAULT_MODEL:
        raise ValueError(
            f"Case002 v2 dev is frozen to {FROZEN_DEFAULT_MODEL}"
        )
    expected_registry_path = (
        output_root / "suites" / suite_id / "hypoweaver-datasets.json"
    )
    research_engine_health = _validate_research_engine_health(
        runtime,
        expected_registry_path=expected_registry_path,
        transport=research_transport,
    )

    cases: dict[str, Any] = {}
    common_hashes: dict[str, dict[str, str]] = {}
    configuration_files: dict[str, Any] = {}
    for view, directory_name in CASE_DIRECTORIES.items():
        case_root = WORKSPACE_ROOT / "benchmark-cases" / directory_name
        profile_path = case_root / "01_model_input" / "case_profile.json"
        config_path = case_root / "agent_laboratory_config_v2.json"
        manifest_sha256 = _validate_visible_manifest(case_root)
        case_manifest = _load_json(case_root / "case_manifest.json")
        profile = _load_json(profile_path)
        config = _load_json(config_path)
        workflow = config.get("workflow") or {}
        model = config.get("model") or {}
        if (
            workflow.get("max_llm_calls") != 40
            or workflow.get("execution_timeout_seconds") != 600
            or workflow.get("max_steps") != 5
            or workflow.get("num_papers_lit_review") != 0
            or workflow.get("mlesolver_max_steps") != 1
            or workflow.get("papersolver_max_steps") != 0
            or workflow.get("benchmark_task_track") != BENCHMARK_TASK_TRACK
        ):
            raise ValueError(f"Agent Laboratory v2 schedule mismatch: {view}")
        manifest_budget = case_manifest.get("agent_laboratory_budget") or {}
        if (
            case_manifest.get("benchmark_task_track") != BENCHMARK_TASK_TRACK
            or manifest_budget
            != {
                "provider_attempt_cap": 40,
                "max_steps": 5,
                "num_papers_lit_review": 0,
                "mlesolver_max_steps": 1,
                "papersolver_max_steps": 0,
            }
        ):
            raise ValueError(f"Case002 task-track manifest mismatch: {view}")
        if (
            model.get("name") != runtime.qwen_model
            or model.get("base_url") != runtime.qwen_base_url
            or model.get("max_tokens") != 12_288
            or model.get("timeout_seconds") != 360
        ):
            raise ValueError(f"Agent Laboratory model envelope mismatch: {view}")
        policy = profile.get("policy_design") or {}
        if (
            policy.get("permutation_scheme") != "assignment_unit_label"
            or policy.get("permutation_unit_field") != "idcode"
            or policy.get("placebo_repetitions") != 199
            or policy.get("random_seed") != 12345
        ):
            raise ValueError(f"Case002 v2 sensitivity contract mismatch: {view}")
        expected_policy_freeze = {
            "permutation_scheme": "assignment_unit_label",
            "permutation_unit_field": "idcode",
            "placebo_repetitions": 199,
            "random_seed": 12345,
        }
        if view == "reproduction_aligned":
            expected_policy_freeze.update(ALIGNED_EVENT_STUDY_FREEZE)
            if any(
                policy.get(key) != value
                for key, value in ALIGNED_EVENT_STUDY_FREEZE.items()
            ):
                raise ValueError(
                    "Case002 aligned event-study contract mismatch"
                )
        elif any(key in policy for key in ALIGNED_EVENT_STUDY_FREEZE):
            raise ValueError("Case002 discovery view leaks event-study contract")
        if case_manifest.get("policy_design_freeze") != expected_policy_freeze:
            raise ValueError(f"Case002 policy-design manifest mismatch: {view}")
        visible = case_root / "01_model_input"
        hashes = {
            name: _sha256(visible / name)
            for name in (
                "main_data.csv",
                "data_dictionary.csv",
                "data_description.md",
            )
        }
        common_hashes[view] = hashes
        visible_file_hashes = {
            str(path.relative_to(case_root)): _sha256(path)
            for path in sorted(visible.iterdir())
            if path.is_file()
        }
        configuration_files[view] = {
            "case_manifest.json": manifest_sha256,
            "agent_laboratory_config_v2.json": _sha256(config_path),
            "visible_input": visible_file_hashes,
        }
        cases[view] = {
            "case_id": profile.get("case_id"),
            "independent_case_id": INDEPENDENT_CASE_ID,
            "case_root": str(case_root),
            "case_profile_sha256": _sha256(profile_path),
            "agent_config_sha256": _sha256(config_path),
            "case_manifest_sha256": manifest_sha256,
            "shared_visible_asset_sha256": hashes,
            "hidden_reference_access": "denied",
        }
    if len({json.dumps(value, sort_keys=True) for value in common_hashes.values()}) != 1:
        raise ValueError("the two Case002 v2 views do not share byte-identical data assets")
    agent_smoke = _validate_agent_code_runner(
        WORKSPACE_ROOT
        / "benchmark-cases"
        / CASE_DIRECTORIES["discovery_blind"]
        / "agent_laboratory_config_v2.json"
    )
    source_snapshot = _source_snapshot()
    model_disclosure = _model_disclosure(runtime.qwen_model)
    public_runtime_envelope = {
        "benchmark_task_track": BENCHMARK_TASK_TRACK,
        **model_disclosure,
        "base_url": runtime.qwen_base_url,
        "provider_attempt_limit_per_system_per_view": 40,
        "hypoweaver_logical_call_limit": 20,
        "max_attempts_per_logical_call": 3,
        "agent_laboratory_schedule": {
            "max_steps": 5,
            "num_papers_lit_review": 0,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
        },
        "cell_wall_time_limit_seconds": CELL_WALL_TIME_LIMIT_SECONDS,
        "statistical_phase_wall_time_limit_seconds": (
            _DEFAULT_CONTRACT_BUDGET.max_wall_time_seconds
        ),
        "frozen_dag_step_limit_per_implementation": (
            _DEFAULT_CONTRACT_BUDGET.max_executions
        ),
        "research_engine_url": runtime.research_engine_url,
        "research_engine_runtime_identity": research_engine_health[
            "runtime_identity"
        ],
        "dataset_registry_path_sha256": research_engine_health[
            "dataset_registry_path_sha256"
        ],
    }
    configuration_sha256 = _canonical_sha256(
        {
            "independent_case_id": INDEPENDENT_CASE_ID,
            "independent_case_count": 1,
            "suite_id": suite_id,
            "cases": configuration_files,
            "runtime_envelope": public_runtime_envelope,
        }
    )
    protocol_sha256 = _canonical_sha256(
        {
            "schema_version": "case002-v2-dev-protocol-v1",
            "classification": "development_calibration_only",
            "split": "dev",
            "independent_case_id": INDEPENDENT_CASE_ID,
            "source_sha256": source_snapshot["source_sha256"],
            "configuration_sha256": configuration_sha256,
            "model_disclosure": model_disclosure,
        }
    )
    output_root.mkdir(parents=True, exist_ok=True)
    return {
        "status": "ready",
        "suite_id": suite_id,
        "benchmark_task_track": BENCHMARK_TASK_TRACK,
        "protocol_sha256": protocol_sha256,
        "source_sha256": source_snapshot["source_sha256"],
        "source_component_sha256": source_snapshot["source_component_sha256"],
        "source_files": source_snapshot["source_files"],
        "configuration_sha256": configuration_sha256,
        "configuration_files": configuration_files,
        "public_runtime_envelope": public_runtime_envelope,
        "independent_case_id": INDEPENDENT_CASE_ID,
        "independent_case_count": 1,
        **model_disclosure,
        "base_url": runtime.qwen_base_url,
        "provider_attempt_limit_per_system_per_view": 40,
        "hypoweaver_native_provider_attempt_limit": 40,
        "hypoweaver_logical_call_limit": 20,
        "hypoweaver_group_counting_unit": "logical_call",
        "max_attempts_per_logical_call": 3,
        "cell_wall_time_limit_seconds": CELL_WALL_TIME_LIMIT_SECONDS,
        "statistical_phase_wall_time_limit_seconds": (
            _DEFAULT_CONTRACT_BUDGET.max_wall_time_seconds
        ),
        "frozen_dag_step_limit_per_implementation": (
            _DEFAULT_CONTRACT_BUDGET.max_executions
        ),
        "agent_laboratory_schedule": "5/0/0",
        "agent_laboratory_generated_code_timeout_seconds": 600,
        "agent_laboratory_code_runner_smoke": agent_smoke,
        "research_engine_health": research_engine_health,
        "cases": cases,
    }


def _derive_usage_audit(
    system_id: Literal["hypoweaver", "agent_laboratory"],
    native_output: dict[str, Any],
) -> tuple[SystemResourceUsageV2 | None, str | None]:
    usage_payload = native_output.get("execution_cost")
    if not isinstance(usage_payload, dict) or "call_receipts" not in usage_payload:
        return None, "usage_parse_failed"
    try:
        usage = derive_system_resource_usage_v2(system_id, usage_payload)
    except (TypeError, ValueError) as error:
        message = str(error).casefold()
        receipt_contract_markers = (
            "receipt",
            "provider attempts",
            "logical_call_id",
        )
        if any(marker in message for marker in receipt_contract_markers):
            return None, "budget_noncompliant"
        return None, "usage_parse_failed"
    return usage, None


def _native_failure_reason(native_output: dict[str, Any]) -> str | None:
    failure = native_output.get("failure")
    if isinstance(failure, dict) and failure.get("reason_code"):
        return str(failure["reason_code"])
    research_run = native_output.get("research_run")
    if isinstance(research_run, dict) and research_run.get("failure_reason_code"):
        return str(research_run["failure_reason_code"])
    return None


_TRANSPORT_ERROR_TYPES = {
    "apiconnectionerror",
    "apitimeouterror",
    "connectionerror",
    "connectionreseterror",
    "connecterror",
    "connecttimeout",
    "gaierror",
    "networkerror",
    "proxyerror",
    "readtimeout",
    "remotedisconnected",
    "remoteprotocolerror",
    "sslerror",
    "timeouterror",
    "urlerror",
}
_TRANSPORT_ERROR_CATEGORIES = {
    "connect_timeout",
    "connection_reset",
    "dns",
    "proxy",
    "read_timeout",
    "tls",
    "unknown_transport",
}


def _is_sha256_value(value: Any) -> bool:
    candidate = str(value or "")
    return (
        len(candidate) == 64
        and candidate != "0" * 64
        and all(character in "0123456789abcdef" for character in candidate)
    )


def _is_frozen_transport_receipt(receipt: dict[str, Any]) -> bool:
    outcome = str(
        receipt.get("outcome") or receipt.get("status") or ""
    ).casefold()
    if outcome not in {"failed", "failure", "transport_failure"}:
        return False
    category = str(receipt.get("error_category") or "").casefold()
    if category:
        return (
            outcome == "transport_failure"
            and category in _TRANSPORT_ERROR_CATEGORIES
        )
    error_type = str(receipt.get("error_type") or "").casefold()
    return error_type in _TRANSPORT_ERROR_TYPES


def _has_terminal_transport_receipts(native_output: dict[str, Any]) -> bool:
    usage_payload = native_output.get("execution_cost")
    if not isinstance(usage_payload, dict):
        return False
    raw_receipts = usage_payload.get("call_receipts")
    if not isinstance(raw_receipts, list) or len(raw_receipts) < 3:
        return False
    if any(not isinstance(receipt, dict) for receipt in raw_receipts):
        return False
    receipts = list(raw_receipts)
    terminal_logical_id = str(receipts[-1].get("logical_call_id") or "")
    if not terminal_logical_id:
        return False
    terminal_receipts = [
        receipt
        for receipt in receipts
        if str(receipt.get("logical_call_id") or "") == terminal_logical_id
    ]
    if len(terminal_receipts) != 3 or receipts[-3:] != terminal_receipts:
        return False
    try:
        attempt_indexes = [
            int(receipt["attempt_index"]) for receipt in terminal_receipts
        ]
    except (KeyError, TypeError, ValueError):
        return False
    if attempt_indexes != [1, 2, 3]:
        return False
    if [
        str(receipt.get("attempt_type") or "")
        for receipt in terminal_receipts
    ] != ["primary", "transport_retry", "transport_retry"]:
        return False
    input_hashes = {
        str(receipt.get("request_sha256") or receipt.get("input_sha256") or "")
        for receipt in terminal_receipts
    }
    if len(input_hashes) != 1 or not _is_sha256_value(next(iter(input_hashes))):
        return False
    for identity_key in ("provider", "model"):
        values = {
            str(receipt.get(identity_key) or "")
            for receipt in terminal_receipts
        }
        if len(values) != 1 or not next(iter(values)):
            return False
    return all(
        _is_frozen_transport_receipt(receipt)
        for receipt in terminal_receipts
    )


def _is_provider_transport_terminal(native_output: dict[str, Any]) -> bool:
    if native_output.get("run_status") != "failed":
        return False
    reason_code = (_native_failure_reason(native_output) or "").casefold()
    if reason_code == "model_transport_exhausted":
        return True
    # r6 compatibility is intentionally receipt-driven: its legacy
    # model_technical_failure reason is accepted only because the final logical
    # call has three identical, frozen RemoteDisconnected transport receipts.
    return _has_terminal_transport_receipts(native_output)


def _is_research_engine_unavailable(
    cell: Cell,
    native_output: dict[str, Any],
) -> bool:
    """Classify executor transport/health failures without reading run files."""

    if cell.system_id != "hypoweaver" or native_output.get("run_status") != "failed":
        return False
    reason_code = (_native_failure_reason(native_output) or "").casefold()
    if reason_code in {
        "external_executor_unavailable",
        "research_engine_health_lost",
        "research_engine_unavailable",
    }:
        return True
    failure_text = str(native_output.get("failure") or "").casefold()
    explicit_markers = (
        "external_executor",
        "research engine unavailable",
        "research_engine_unavailable",
        "research engine connection",
        "research engine health",
    )
    if any(marker in failure_text for marker in explicit_markers):
        return True
    return (
        "did not reach h3" in failure_text
        and "all connection attempts failed" in failure_text
        and str(native_output.get("execution_status")) == "not_started"
        and str(native_output.get("scientific_status")) == "not_evaluated"
    )


def _native_statuses(native_output: dict[str, Any]) -> tuple[str, str]:
    research_run = native_output.get("research_run")
    nested = research_run if isinstance(research_run, dict) else {}
    execution_status = str(
        native_output.get("execution_status")
        or nested.get("execution_status")
        or "not_available"
    )
    scientific_status = str(
        native_output.get("scientific_status")
        or nested.get("scientific_status")
        or "not_available"
    )
    return execution_status, scientific_status


def _normalize_native_output(
    cell: Cell,
    *,
    protocol_sha256: str,
    native_output: dict[str, Any],
    native_output_sha256: str,
    return_code: int | None,
    timed_out: bool,
    suite_id: str | None = None,
    run_id: str | None = None,
    cell_elapsed_seconds: float | None = None,
) -> SystemComparisonRunOutputV2:
    usage, usage_issue = _derive_usage_audit(cell.system_id, native_output)
    native_run_status = str(native_output.get("run_status") or "")
    normalized_run_status: Literal["completed", "failed"] = (
        "completed" if native_run_status == "completed" else "failed"
    )
    execution_status, scientific_status = _native_statuses(native_output)
    cell_within_wall_time_budget = (
        False
        if timed_out
        else None
        if cell_elapsed_seconds is None
        else cell_elapsed_seconds <= CELL_WALL_TIME_LIMIT_SECONDS
    )

    failure_class: Literal[
        "none",
        "provider_transport",
        "benchmark_infrastructure",
        "system_capability",
    ]
    failure_reason_code: str | None
    if timed_out:
        failure_class = "system_capability"
        failure_reason_code = "cell_wall_time_budget_exhausted"
    elif native_run_status not in {"completed", "failed"}:
        failure_class = "benchmark_infrastructure"
        failure_reason_code = "native_run_status_invalid"
    elif return_code != (0 if native_run_status == "completed" else 2):
        failure_class = "benchmark_infrastructure"
        failure_reason_code = "return_code_native_status_mismatch"
    elif _is_research_engine_unavailable(cell, native_output):
        failure_class = "benchmark_infrastructure"
        failure_reason_code = "research_engine_unavailable"
    elif usage_issue == "usage_parse_failed":
        failure_class = "benchmark_infrastructure"
        failure_reason_code = "usage_parse_failed"
    elif usage_issue == "budget_noncompliant" or (
        usage is not None and not usage.within_budget
    ):
        failure_class = "system_capability"
        failure_reason_code = "budget_noncompliant"
    elif _is_provider_transport_terminal(native_output):
        failure_class = "provider_transport"
        failure_reason_code = "provider_transport_terminal"
    elif native_run_status == "failed":
        failure_class = "system_capability"
        failure_reason_code = "native_system_failure"
    else:
        failure_class = "none"
        failure_reason_code = None

    score_eligibility = (
        "excluded_infrastructure_failure"
        if failure_class in {"provider_transport", "benchmark_infrastructure"}
        else "development_only"
    )
    if failure_class == "provider_transport":
        scientific_status = "not_evaluated"
    return SystemComparisonRunOutputV2(
        protocol_sha256=protocol_sha256,
        case_id=INDEPENDENT_CASE_ID,
        suite_id=suite_id,
        run_id=run_id,
        input_view=cell.input_view,
        independent_case_id=INDEPENDENT_CASE_ID,
        hidden_reference_access="denied",
        cell_elapsed_seconds=cell_elapsed_seconds,
        timed_out=timed_out,
        cell_within_wall_time_budget=cell_within_wall_time_budget,
        split="dev",
        system_id=cell.system_id,
        runtime_envelope=SystemRuntimeEnvelopeV2(
            system_id=cell.system_id,
            logical_call_limit=20 if cell.system_id == "hypoweaver" else None,
        ),
        run_status=normalized_run_status,
        execution_status=execution_status,
        scientific_status=scientific_status,
        failure_class=failure_class,
        failure_reason_code=failure_reason_code,
        score_eligibility=score_eligibility,
        usage=usage,
        native_output_sha256=native_output_sha256,
        budget_compliant=bool(
            usage is not None
            and usage.within_budget
            and cell_within_wall_time_budget is not False
        ),
    )


def _run_cell(
    cell: Cell,
    *,
    suite_id: str,
    suite_root: Path,
    output_root: Path,
    runtime: EffectiveRuntimeConfig,
    protocol_sha256: str,
) -> dict[str, Any]:
    case_root = (
        WORKSPACE_ROOT / "benchmark-cases" / CASE_DIRECTORIES[cell.input_view]
    )
    profile = _load_json(case_root / "01_model_input" / "case_profile.json")
    case_id = str(profile["case_id"])
    run_id = f"{suite_id}-{cell.input_view}-{cell.system_id}"
    environment = dict(os.environ)
    environment["DASHSCOPE_API_KEY"] = str(runtime.qwen_api_key)
    environment["QWEN_BASE_URL"] = runtime.qwen_base_url
    environment["QWEN_MODEL"] = runtime.qwen_model

    if cell.system_id == "agent_laboratory":
        config_path = case_root / "agent_laboratory_config_v2.json"
        command = [
            sys.executable,
            "-m",
            "benchmark_adapter.cli",
            "--config",
            str(config_path),
            "--execute-generated-code",
            "--run-id",
            run_id,
        ]
        cwd = WORKSPACE_ROOT / "Agent Laboratory"
        environment["PYTHONPATH"] = str(cwd)
        config = _load_json(config_path)
        native_root = (
            config_path.parent
            / str(config["workflow"]["output_dir"])
            / case_id
            / run_id
        ).resolve()
    else:
        registry_path = suite_root / "hypoweaver-datasets.json"
        environment["HYPOWEAVER_DATASET_REGISTRY_PATH"] = str(registry_path)
        native_output_root = output_root / "hypoweaver"
        command = _build_hypoweaver_command(
            case_root=case_root,
            output_root=native_output_root,
            registry_path=registry_path,
            run_id=run_id,
        )
        cwd = PROJECT_ROOT
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "backend" / "src")
        native_root = native_output_root / case_id / run_id
    timeout_seconds = CELL_WALL_TIME_LIMIT_SECONDS

    started = time.monotonic()
    timed_out = False
    return_code: int | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout = str(error.stdout or "")
        stderr = str(error.stderr or "")
    elapsed = time.monotonic() - started
    cell_slug = f"{cell.input_view}-{cell.system_id}"
    (suite_root / "logs").mkdir(parents=True, exist_ok=True)
    (suite_root / "logs" / f"{cell_slug}.stdout.log").write_text(
        stdout,
        encoding="utf-8",
    )
    (suite_root / "logs" / f"{cell_slug}.stderr.log").write_text(
        stderr,
        encoding="utf-8",
    )

    output_path = native_root / "benchmark_output.json"
    native_output_sha256 = _sha256(output_path) if output_path.is_file() else None
    native_output: dict[str, Any] | None = None
    native_load_failed = False
    if output_path.is_file():
        try:
            native_output = _load_json(output_path)
        except (OSError, TypeError, ValueError):
            native_load_failed = True
    normalized: SystemComparisonRunOutputV2 | None = None
    if native_output is not None and native_output_sha256 is not None:
        normalized = _normalize_native_output(
            cell,
            protocol_sha256=protocol_sha256,
            native_output=native_output,
            native_output_sha256=native_output_sha256,
            return_code=return_code,
            timed_out=timed_out,
            suite_id=suite_id,
            run_id=run_id,
            cell_elapsed_seconds=round(elapsed, 6),
        )
    normalization_reason_code = (
        normalized.failure_reason_code
        if normalized is not None
        else (
            "cell_wall_time_budget_exhausted"
            if timed_out
            else (
                "native_output_parse_failed"
                if native_load_failed
                else "native_output_missing"
            )
        )
    )
    failure_class = (
        normalized.failure_class
        if normalized is not None
        else "system_capability" if timed_out else "benchmark_infrastructure"
    )
    execution_status, scientific_status = (
        _native_statuses(native_output)
        if native_output is not None
        else ("not_available", "not_available")
    )
    if normalized is not None:
        execution_status = normalized.execution_status
        scientific_status = normalized.scientific_status
    elif timed_out:
        scientific_status = "not_evaluated"
    normalized_payload = (
        normalized.model_dump(mode="json") if normalized is not None else None
    )
    normalized_output_path = (
        suite_root / "normalized" / f"{cell_slug}.json"
        if normalized_payload is not None
        else None
    )
    result = {
        "system_id": cell.system_id,
        "input_view": cell.input_view,
        "case_id": case_id,
        "independent_case_id": INDEPENDENT_CASE_ID,
        "run_id": run_id,
        "return_code": return_code,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 6),
        "native_output_path": str(output_path),
        "native_output_sha256": native_output_sha256,
        "native_output_present": output_path.is_file(),
        "normalized_output": normalized_payload,
        "normalized_output_path": (
            str(normalized_output_path) if normalized_output_path is not None else None
        ),
        "normalization_reason_code": normalization_reason_code,
        "failure_class": failure_class,
        "run_status": (
            normalized.run_status if normalized is not None else "infrastructure_failed"
        ),
        "execution_status": execution_status,
        "scientific_status": scientific_status,
        "failure": (
            native_output.get("failure")
            if native_output is not None
            else normalization_reason_code
        ),
        "usage": (
            normalized.usage.model_dump(mode="json")
            if normalized is not None and normalized.usage is not None
            else None
        ),
        "budget_compliant": (
            normalized.budget_compliant if normalized is not None else False
        ),
        "hidden_reference_access": "denied",
    }
    if normalized_output_path is not None:
        _write_json(normalized_output_path, normalized_payload)
    _write_json(suite_root / "cells" / f"{cell_slug}.json", result)
    return result


def _pair_eligibility(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    infrastructure_classes = {"provider_transport", "benchmark_infrastructure"}
    views = list(dict.fromkeys(cell.input_view for cell in CELLS))
    pairs: list[dict[str, Any]] = []
    for input_view in views:
        by_system = {
            str(result.get("system_id")): result
            for result in results
            if result.get("input_view") == input_view
        }
        missing_systems = [
            system_id
            for system_id in ("hypoweaver", "agent_laboratory")
            if system_id not in by_system
        ]
        pair_results = list(by_system.values())
        has_infrastructure_failure = any(
            result.get("failure_class") in infrastructure_classes
            for result in pair_results
        )
        has_system_capability_failure = any(
            result.get("failure_class") == "system_capability"
            for result in pair_results
        )

        if missing_systems:
            completion = {
                "eligible": False,
                "reason_code": "missing_system_cell",
            }
        elif has_infrastructure_failure:
            completion = {
                "eligible": False,
                "reason_code": "infrastructure_failure_in_pair",
            }
        else:
            completion = {
                "eligible": True,
                "reason_code": "eligible_non_infrastructure_pair",
            }

        scientific_artifacts_available = bool(pair_results) and all(
            result.get("failure_class") == "none"
            and result.get("run_status") == "completed"
            and result.get("scientific_status")
            not in {None, "invalid", "not_available", "not_evaluated"}
            for result in pair_results
        )
        if missing_systems:
            artifact_quality = {
                "eligible": False,
                "reason_code": "missing_system_cell",
            }
        elif has_infrastructure_failure:
            artifact_quality = {
                "eligible": False,
                "reason_code": "infrastructure_failure_in_pair",
            }
        elif has_system_capability_failure:
            artifact_quality = {
                "eligible": False,
                "reason_code": "one_or_more_system_capability_failures",
            }
        elif scientific_artifacts_available:
            artifact_quality = {
                "eligible": True,
                "reason_code": "eligible_completed_scientific_artifacts",
            }
        else:
            artifact_quality = {
                "eligible": False,
                "reason_code": "one_or_more_scientific_artifacts_unavailable",
            }

        pairs.append(
            {
                "input_view": input_view,
                "independent_case_id": INDEPENDENT_CASE_ID,
                "missing_systems": missing_systems,
                "completion": completion,
                "artifact_quality": artifact_quality,
                "systems": {
                    system_id: {
                        key: result.get(key)
                        for key in (
                            "failure_class",
                            "run_status",
                            "execution_status",
                            "scientific_status",
                        )
                    }
                    for system_id, result in by_system.items()
                },
            }
        )
    return pairs


def run_suite(output_root: Path, suite_id: str) -> Path:
    suite_id = _validate_suite_id(suite_id)
    suite_root = output_root / "suites" / suite_id
    if suite_root.exists():
        raise FileExistsError(f"suite already exists: {suite_id}")
    runtime = RuntimeConfigStore().resolve()
    preflight = _preflight(output_root, suite_id=suite_id, runtime=runtime)
    suite_root.mkdir(parents=True, exist_ok=False)
    model_disclosure = {key: preflight[key] for key in _MODEL_DISCLOSURE_KEYS}
    manifest = {
        "schema_version": "case002-v2-dev-suite-v1",
        "suite_id": suite_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": "development_calibration_only",
        "benchmark_task_track": BENCHMARK_TASK_TRACK,
        "include_in_primary_score": False,
        "case_is_statistical_unit": True,
        "independent_case_id": INDEPENDENT_CASE_ID,
        "independent_case_count": 1,
        **model_disclosure,
        "public_runtime_envelope": preflight["public_runtime_envelope"],
        "cell_wall_time_limit_seconds": CELL_WALL_TIME_LIMIT_SECONDS,
        "cells": [cell.__dict__ for cell in CELLS],
        "preflight": preflight,
        "data_egress_authorization": (
            "Case002 visible contents authorized by the user; hidden references denied"
        ),
    }
    _write_json(suite_root / "suite_manifest.json", manifest)

    results = []
    for index, cell in enumerate(CELLS, start=1):
        print(
            f"[{index}/{len(CELLS)}] {cell.system_id} {cell.input_view}",
            flush=True,
        )
        result = _run_cell(
            cell,
            suite_id=suite_id,
            suite_root=suite_root,
            output_root=output_root,
            runtime=runtime,
            protocol_sha256=str(preflight["protocol_sha256"]),
        )
        results.append(result)
        print(
            f"[{index}/{len(CELLS)}] terminal={result['failure_class']}",
            flush=True,
        )
    terminal_counts = {
        failure_class: sum(
            result["failure_class"] == failure_class for result in results
        )
        for failure_class in (
            "none",
            "provider_transport",
            "benchmark_infrastructure",
            "system_capability",
        )
    }
    pair_eligibility = _pair_eligibility(results)
    non_infrastructure_terminal_cells = (
        terminal_counts["none"] + terminal_counts["system_capability"]
    )
    _write_json(
        suite_root / "suite_summary.json",
        {
            "schema_version": "case002-v2-dev-summary-v1",
            "suite_id": suite_id,
            "classification": "development_calibration_only",
            "independent_case_id": INDEPENDENT_CASE_ID,
            "independent_case_count": 1,
            "normalized_terminal_counts": terminal_counts,
            "successful_cells": terminal_counts["none"],
            "non_infrastructure_terminal_cells": non_infrastructure_terminal_cells,
            "scientifically_comparable_terminal_cells": (
                non_infrastructure_terminal_cells
            ),
            "legacy_field_aliases": {
                "scientifically_comparable_terminal_cells": (
                    "non_infrastructure_terminal_cells"
                )
            },
            "pair_eligibility": pair_eligibility,
            "paired_completion_eligible_views": sum(
                pair["completion"]["eligible"] for pair in pair_eligibility
            ),
            "paired_artifact_quality_eligible_views": sum(
                pair["artifact_quality"]["eligible"] for pair in pair_eligibility
            ),
            "excluded_infrastructure_cells": (
                terminal_counts["provider_transport"]
                + terminal_counts["benchmark_infrastructure"]
            ),
            "results": results,
        },
    )
    return suite_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the four frozen Case002 v2 development-calibration cells."
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--suite-id", default=None)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_root = args.output_root.resolve()
    suite_id = args.suite_id or datetime.now(timezone.utc).strftime(
        "case002-v2-dev-%Y%m%dT%H%M%SZ"
    )
    if args.preflight_only:
        runtime = RuntimeConfigStore().resolve()
        print(
            json.dumps(
                _preflight(
                    output_root,
                    suite_id=suite_id,
                    runtime=runtime,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    suite_root = run_suite(output_root, suite_id)
    print(suite_root / "suite_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
