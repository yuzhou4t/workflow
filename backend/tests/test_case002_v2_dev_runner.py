from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import httpx

from hypoweaver.case002_v2_dev_runner import (
    BENCHMARK_TASK_TRACK,
    CASE_DIRECTORIES,
    CELL_WALL_TIME_LIMIT_SECONDS,
    COMPARISON_ESTIMAND,
    FROZEN_DEFAULT_MODEL,
    FROZEN_ESCALATION_MODEL,
    HYPOWEAVER_V2_BUDGET_FLAG,
    INDEPENDENT_CASE_ID,
    MODEL_CONTROL,
    WORKSPACE_ROOT,
    Cell,
    _build_hypoweaver_command,
    _canonical_sha256,
    _is_research_engine_unavailable,
    _model_disclosure,
    _normalize_native_output,
    _preflight,
    _run_cell,
    _source_snapshot,
    _validate_agent_code_runner,
    _validate_research_engine_health,
    _validate_visible_manifest,
    run_suite,
)
from hypoweaver.runtime_config import EffectiveRuntimeConfig
from hypoweaver.research_api import registry_path_sha256, runtime_identity


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime() -> EffectiveRuntimeConfig:
    return EffectiveRuntimeConfig(
        qwen_api_key="secret-never-written",
        qwen_model="qwen3.7-plus",
        qwen_base_url="https://frozen.example/v1",
        research_engine_url="http://127.0.0.1:9000",
        research_engine_token="research-secret-never-written",
        sources={
            "qwen_api_key": "file",
            "qwen_model": "file",
            "qwen_base_url": "file",
            "research_engine_url": "file",
            "research_engine_token": "file",
        },
    )


def _native_output(
    *,
    system_id: str = "hypoweaver",
    run_status: str = "completed",
    logical_calls: int = 1,
) -> dict[str, object]:
    receipts = [
        {
            "logical_call_id": f"logical-{index}",
            "attempt_index": 1,
            "attempt_type": "primary",
            "outcome": "succeeded",
            "input_sha256": f"{index + 1:064x}",
        }
        for index in range(logical_calls)
    ]
    return {
        "run_status": run_status,
        "execution_status": "success" if run_status == "completed" else "failed",
        "scientific_status": "limited" if run_status == "completed" else "invalid",
        "execution_cost": {
            "provider_attempts": logical_calls,
            "call_receipts": receipts,
            "technical_failures": [],
        },
        "system_id": system_id,
    }


def _terminal_transport_output(
    *,
    reason_code: str = "model_technical_failure",
    error_type: str = "RemoteDisconnected",
) -> dict[str, object]:
    native = _native_output(run_status="failed")
    native["failure"] = {"reason_code": reason_code}
    native["execution_cost"] = {
        "provider_attempts": 3,
        "technical_failures": [error_type, error_type, error_type],
        "call_receipts": [
            {
                "provider": "qwen",
                "model": FROZEN_DEFAULT_MODEL,
                "logical_call_id": 17,
                "attempt_index": attempt_index,
                "attempt_type": (
                    "primary" if attempt_index == 1 else "transport_retry"
                ),
                "input_sha256": "1" * 64,
                "error_type": error_type,
                "status": "failed",
            }
            for attempt_index in (1, 2, 3)
        ],
    }
    return native


class Case002V2DevRunnerTests(unittest.TestCase):
    def _health_transport(
        self,
        registry_path: Path,
        *,
        identity_override: dict[str, object] | None = None,
    ) -> httpx.MockTransport:
        identity = identity_override or runtime_identity()

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.headers.get("authorization"),
                "Bearer research-secret-never-written",
            )
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    **identity,
                    "dataset_registry_path_sha256": registry_path_sha256(
                        registry_path
                    ),
                },
            )

        return httpx.MockTransport(handler)

    def test_research_health_accepts_frozen_identity_and_registry(self) -> None:
        runtime = _runtime()
        registry_path = Path("/tmp/suite/hypoweaver-datasets.json")
        result = _validate_research_engine_health(
            runtime,
            expected_registry_path=registry_path,
            transport=self._health_transport(registry_path),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["dataset_registry_path_sha256"],
            registry_path_sha256(registry_path),
        )
        self.assertNotIn(runtime.research_engine_token, json.dumps(result))

    def test_research_health_rejects_wrong_registry(self) -> None:
        with self.assertRaisesRegex(ValueError, "registry identity mismatch"):
            _validate_research_engine_health(
                _runtime(),
                expected_registry_path=Path("/tmp/expected/datasets.json"),
                transport=self._health_transport(Path("/tmp/wrong/datasets.json")),
            )

    def test_research_health_rejects_wrong_runtime_identity(self) -> None:
        identity = runtime_identity()
        identity["source_sha256"] = "0" * 64
        registry_path = Path("/tmp/suite/datasets.json")
        with self.assertRaisesRegex(ValueError, "runtime identity mismatch"):
            _validate_research_engine_health(
                _runtime(),
                expected_registry_path=registry_path,
                transport=self._health_transport(
                    registry_path,
                    identity_override=identity,
                ),
            )

    def test_research_health_rejects_connection_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        with self.assertRaisesRegex(ValueError, "unavailable"):
            _validate_research_engine_health(
                _runtime(),
                expected_registry_path=Path("/tmp/suite/datasets.json"),
                transport=httpx.MockTransport(handler),
            )

    def test_hypoweaver_cell_uses_frozen_v2_budget_and_common_wall_limit(self) -> None:
        command = _build_hypoweaver_command(
            case_root=Path("/case"),
            output_root=Path("/output"),
            registry_path=Path("/registry.json"),
            run_id="run-v2",
        )
        self.assertIn(HYPOWEAVER_V2_BUDGET_FLAG, command)
        self.assertEqual(CELL_WALL_TIME_LIMIT_SECONDS, 2_700)

    def test_model_disclosure_freezes_native_unequal_role_routing(self) -> None:
        disclosure = _model_disclosure(FROZEN_DEFAULT_MODEL)

        self.assertEqual(disclosure["model"], FROZEN_DEFAULT_MODEL)
        self.assertEqual(disclosure["default_model"], FROZEN_DEFAULT_MODEL)
        self.assertEqual(
            disclosure["comparison_estimand"], COMPARISON_ESTIMAND
        )
        self.assertEqual(disclosure["model_control"], MODEL_CONTROL)
        routing = disclosure["system_model_routing"]
        self.assertEqual(
            routing["agent_laboratory"],
            {
                "default_model": FROZEN_DEFAULT_MODEL,
                "all_roles_model": FROZEN_DEFAULT_MODEL,
                "role_overrides": {},
            },
        )
        self.assertEqual(
            routing["hypoweaver"]["default_model"], FROZEN_DEFAULT_MODEL
        )
        self.assertEqual(
            routing["hypoweaver"]["role_overrides"],
            {
                "reviewer": FROZEN_ESCALATION_MODEL,
                "scientific_audit": FROZEN_ESCALATION_MODEL,
                "design_retry": FROZEN_ESCALATION_MODEL,
                "writer_escalation": FROZEN_ESCALATION_MODEL,
            },
        )

    def test_preflight_hashes_public_model_disclosure(self) -> None:
        runtime = _runtime().model_copy(
            update={
                "qwen_base_url": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                )
            }
        )
        source_snapshot = {
            "source_sha256": {
                "hypoweaver": "1" * 64,
                "agent_laboratory": "2" * 64,
                "benchmark_harness": "3" * 64,
            },
            "source_component_sha256": {},
            "source_files": {},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            with (
                patch(
                    "hypoweaver.case002_v2_dev_runner._validate_agent_code_runner",
                    return_value={"status": "valid"},
                ),
                patch(
                    "hypoweaver.case002_v2_dev_runner._source_snapshot",
                    return_value=source_snapshot,
                ),
            ):
                preflight = _preflight(
                    output_root,
                    suite_id="model-disclosure",
                    runtime=runtime,
                    research_transport=self._health_transport(
                        output_root
                        / "suites"
                        / "model-disclosure"
                        / "hypoweaver-datasets.json"
                    ),
                )

        envelope = preflight["public_runtime_envelope"]
        for key in (
            "model",
            "default_model",
            "comparison_estimand",
            "model_control",
            "system_model_routing",
        ):
            self.assertEqual(preflight[key], envelope[key])
        expected_configuration_sha256 = _canonical_sha256(
            {
                "independent_case_id": INDEPENDENT_CASE_ID,
                "independent_case_count": 1,
                "suite_id": "model-disclosure",
                "cases": preflight["configuration_files"],
                "runtime_envelope": envelope,
            }
        )
        self.assertEqual(
            preflight["configuration_sha256"], expected_configuration_sha256
        )
        model_disclosure = {
            key: preflight[key]
            for key in (
                "model",
                "default_model",
                "comparison_estimand",
                "model_control",
                "system_model_routing",
            )
        }
        self.assertEqual(
            preflight["protocol_sha256"],
            _canonical_sha256(
                {
                    "schema_version": "case002-v2-dev-protocol-v1",
                    "classification": "development_calibration_only",
                    "split": "dev",
                    "independent_case_id": INDEPENDENT_CASE_ID,
                    "source_sha256": source_snapshot["source_sha256"],
                    "configuration_sha256": expected_configuration_sha256,
                    "model_disclosure": model_disclosure,
                }
            ),
        )
    def test_case002_given_input_track_has_no_literature_completion_gate(self) -> None:
        expected_budget = {
            "provider_attempt_cap": 40,
            "max_steps": 5,
            "num_papers_lit_review": 0,
            "mlesolver_max_steps": 1,
            "papersolver_max_steps": 0,
        }
        for view, directory_name in CASE_DIRECTORIES.items():
            case_root = WORKSPACE_ROOT / "benchmark-cases" / directory_name
            config = json.loads(
                (case_root / "agent_laboratory_config_v2.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads(
                (case_root / "case_manifest.json").read_text(encoding="utf-8")
            )

            workflow = config["workflow"]
            self.assertEqual(
                workflow["benchmark_task_track"], BENCHMARK_TASK_TRACK
            )
            self.assertEqual(workflow["max_steps"], 5)
            self.assertEqual(workflow["num_papers_lit_review"], 0)
            self.assertEqual(workflow["mlesolver_max_steps"], 1)
            self.assertEqual(workflow["papersolver_max_steps"], 0)
            self.assertEqual(
                manifest["benchmark_task_track"], BENCHMARK_TASK_TRACK
            )
            self.assertEqual(manifest["agent_laboratory_budget"], expected_budget)
            policy = json.loads(
                (case_root / "01_model_input" / "case_profile.json").read_text(
                    encoding="utf-8"
                )
            )["policy_design"]
            policy_freeze = manifest["policy_design_freeze"]
            if view == "reproduction_aligned":
                self.assertEqual(
                    policy["event_remote_pre_years"],
                    [1998, 1999, 2000, 2001],
                )
                self.assertEqual(
                    policy["event_term_scaling"],
                    "binary_group_year_contrast",
                )
                self.assertEqual(
                    policy_freeze["event_remote_pre_years"],
                    [1998, 1999, 2000, 2001],
                )
                self.assertEqual(
                    policy_freeze["event_term_scaling"],
                    "binary_group_year_contrast",
                )
            else:
                self.assertNotIn("event_remote_pre_years", policy)
                self.assertNotIn("event_term_scaling", policy)
                self.assertNotIn("event_remote_pre_years", policy_freeze)
                self.assertNotIn("event_term_scaling", policy_freeze)

    def test_agent_code_runner_smoke_requires_the_terminal_marker(self) -> None:
        completed = CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "valid",
                    "code_runner_smoke": "AGENT_LAB_CODE_RUNNER_SMOKE_OK",
                    "network_called": False,
                }
            ),
            stderr="",
        )
        with patch(
            "hypoweaver.case002_v2_dev_runner.subprocess.run",
            return_value=completed,
        ):
            result = _validate_agent_code_runner(Path("config.json"))
        self.assertEqual(result["status"], "valid")

    def test_agent_code_runner_smoke_rejects_a_generic_success(self) -> None:
        completed = CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "valid",
                    "code_runner_smoke": "wrong-marker",
                    "network_called": False,
                }
            ),
            stderr="",
        )
        with patch(
            "hypoweaver.case002_v2_dev_runner.subprocess.run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(ValueError, "did not seal success"):
                _validate_agent_code_runner(Path("config.json"))

    def test_visible_manifest_rejects_a_stale_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            case_root = Path(temporary_directory)
            input_root = case_root / "01_model_input"
            input_root.mkdir()
            visible_path = input_root / "case_profile.json"
            visible_path.write_text("{}\n", encoding="utf-8")
            manifest_path = case_root / "case_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "hidden_reference_access": "denied",
                        "visible_input": {
                            "files": [
                                {
                                    "path": "01_model_input/case_profile.json",
                                    "sha256": _sha256(visible_path),
                                    "size_bytes": visible_path.stat().st_size,
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_validate_visible_manifest(case_root), _sha256(manifest_path))

            visible_path.write_text('{"changed": true}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                _validate_visible_manifest(case_root)

    def test_timeout_overrides_a_native_completed_status(self) -> None:
        normalized = _normalize_native_output(
            Cell("hypoweaver", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=_native_output(),
            native_output_sha256="b" * 64,
            return_code=0,
            timed_out=True,
        )

        self.assertEqual(normalized.failure_class, "system_capability")
        self.assertEqual(
            normalized.failure_reason_code,
            "cell_wall_time_budget_exhausted",
        )
        self.assertEqual(normalized.score_eligibility, "development_only")
        self.assertFalse(normalized.cell_within_wall_time_budget)
        self.assertFalse(normalized.budget_compliant)

    def test_return_code_native_status_contradiction_is_infrastructure(self) -> None:
        normalized = _normalize_native_output(
            Cell("hypoweaver", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=_native_output(),
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "benchmark_infrastructure")
        self.assertEqual(
            normalized.failure_reason_code,
            "return_code_native_status_mismatch",
        )

    def test_budget_noncompliance_cannot_be_normalized_as_success(self) -> None:
        normalized = _normalize_native_output(
            Cell("hypoweaver", "reproduction_aligned"),
            protocol_sha256="a" * 64,
            native_output=_native_output(logical_calls=21),
            native_output_sha256="b" * 64,
            return_code=0,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "system_capability")
        self.assertEqual(normalized.failure_reason_code, "budget_noncompliant")
        self.assertFalse(normalized.budget_compliant)
        self.assertEqual(normalized.score_eligibility, "development_only")

    def test_usage_parse_failure_precedes_provider_transport_terminal(self) -> None:
        native = _native_output(run_status="failed")
        native["failure"] = {"reason_code": "model_technical_failure"}
        native["execution_cost"] = "malformed"
        normalized = _normalize_native_output(
            Cell("agent_laboratory", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "benchmark_infrastructure")
        self.assertEqual(normalized.failure_reason_code, "usage_parse_failed")
        self.assertIsNone(normalized.usage)

    def test_r6_legacy_remote_disconnect_receipts_remain_transport(self) -> None:
        native = _terminal_transport_output()
        normalized = _normalize_native_output(
            Cell("agent_laboratory", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
            suite_id="suite-r4",
            run_id="suite-r4-discovery_blind-agent_laboratory",
            cell_elapsed_seconds=14.5,
        )

        self.assertEqual(normalized.failure_class, "provider_transport")
        self.assertEqual(normalized.scientific_status, "not_evaluated")
        self.assertEqual(
            normalized.score_eligibility,
            "excluded_infrastructure_failure",
        )
        self.assertEqual(normalized.suite_id, "suite-r4")
        self.assertEqual(
            normalized.run_id,
            "suite-r4-discovery_blind-agent_laboratory",
        )
        self.assertEqual(normalized.input_view, "discovery_blind")
        self.assertEqual(normalized.independent_case_id, INDEPENDENT_CASE_ID)
        self.assertEqual(normalized.hidden_reference_access, "denied")
        self.assertEqual(normalized.cell_elapsed_seconds, 14.5)
        self.assertFalse(normalized.timed_out)
        self.assertTrue(normalized.cell_within_wall_time_budget)

    def test_explicit_model_transport_exhausted_is_provider_transport(self) -> None:
        native = _native_output(run_status="failed")
        native["failure"] = {"reason_code": "model_transport_exhausted"}
        normalized = _normalize_native_output(
            Cell("hypoweaver", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "provider_transport")
        self.assertEqual(
            normalized.failure_reason_code, "provider_transport_terminal"
        )

    def test_http_400_receipts_are_system_capability_not_transport(self) -> None:
        native = _terminal_transport_output(error_type="HTTPError")
        normalized = _normalize_native_output(
            Cell("agent_laboratory", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "system_capability")
        self.assertEqual(normalized.failure_reason_code, "native_system_failure")
        self.assertEqual(normalized.score_eligibility, "development_only")

    def test_response_contract_failure_is_system_capability_not_transport(self) -> None:
        native = _native_output(run_status="failed")
        native["failure"] = {"reason_code": "response_contract"}
        normalized = _normalize_native_output(
            Cell("agent_laboratory", "reproduction_aligned"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "system_capability")
        self.assertEqual(normalized.failure_reason_code, "native_system_failure")
        self.assertEqual(normalized.score_eligibility, "development_only")

    def test_r1_research_connection_failure_is_infrastructure(self) -> None:
        native = _native_output(run_status="failed")
        native["execution_status"] = "not_started"
        native["scientific_status"] = "not_evaluated"
        native["failure"] = (
            "RuntimeError: HypoWeaver did not reach H3: status=failed, "
            "gate=None, error=All connection attempts failed"
        )
        self.assertTrue(
            _is_research_engine_unavailable(
                Cell("hypoweaver", "discovery_blind"), native
            )
        )

        normalized = _normalize_native_output(
            Cell("hypoweaver", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=2,
            timed_out=False,
        )
        self.assertEqual(normalized.failure_class, "benchmark_infrastructure")
        self.assertEqual(
            normalized.failure_reason_code,
            "research_engine_unavailable",
        )

    def test_receipt_identity_violation_is_budget_noncompliant(self) -> None:
        native = _native_output()
        native["execution_cost"] = {
            "provider_attempts": 2,
            "technical_failures": ["TimeoutError"],
            "call_receipts": [
                {
                    "logical_call_id": "logical-1",
                    "attempt_index": 1,
                    "attempt_type": "primary",
                    "outcome": "transport_failure",
                    "input_sha256": "1" * 64,
                },
                {
                    "logical_call_id": "logical-1",
                    "attempt_index": 2,
                    "attempt_type": "transport_retry",
                    "outcome": "succeeded",
                    "input_sha256": "2" * 64,
                },
            ],
        }
        normalized = _normalize_native_output(
            Cell("hypoweaver", "discovery_blind"),
            protocol_sha256="a" * 64,
            native_output=native,
            native_output_sha256="b" * 64,
            return_code=0,
            timed_out=False,
        )

        self.assertEqual(normalized.failure_class, "system_capability")
        self.assertEqual(normalized.failure_reason_code, "budget_noncompliant")
        self.assertFalse(normalized.usage.retry_request_identity_verified)

    def test_source_snapshot_is_deterministic_and_separates_agent_components(self) -> None:
        first = _source_snapshot()
        second = _source_snapshot()

        self.assertEqual(first, second)
        self.assertEqual(
            set(first["source_sha256"]),
            {"hypoweaver", "agent_laboratory", "benchmark_harness"},
        )
        self.assertIn(
            "agent_laboratory_adapter",
            first["source_component_sha256"],
        )
        self.assertIn(
            "agent_laboratory_frozen_upstream",
            first["source_component_sha256"],
        )

    def test_suite_resolves_runtime_once_and_counts_two_views_as_one_case(self) -> None:
        runtime = _runtime()
        observed_runtimes: list[EffectiveRuntimeConfig] = []

        def fake_preflight(
            _output_root: Path,
            *,
            suite_id: str,
            runtime: EffectiveRuntimeConfig,
        ) -> dict[str, object]:
            self.assertEqual(suite_id, "single-runtime")
            observed_runtimes.append(runtime)
            disclosure = _model_disclosure(runtime.qwen_model)
            return {
                "protocol_sha256": "c" * 64,
                **disclosure,
                "public_runtime_envelope": {
                    "benchmark_task_track": BENCHMARK_TASK_TRACK,
                    **disclosure,
                },
            }

        def fake_run_cell(
            cell: Cell,
            *,
            runtime: EffectiveRuntimeConfig,
            **_kwargs: object,
        ) -> dict[str, object]:
            observed_runtimes.append(runtime)
            failure_class = (
                "provider_transport"
                if cell == Cell("agent_laboratory", "discovery_blind")
                else (
                    "none"
                    if cell == Cell("hypoweaver", "reproduction_aligned")
                    else "system_capability"
                )
            )
            return {
                "system_id": cell.system_id,
                "input_view": cell.input_view,
                "independent_case_id": INDEPENDENT_CASE_ID,
                "failure_class": failure_class,
                "run_status": "completed" if failure_class == "none" else "failed",
                "execution_status": (
                    "succeeded" if failure_class == "none" else "failed"
                ),
                "scientific_status": (
                    "limited" if failure_class == "none" else "not_evaluated"
                ),
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            with (
                patch(
                    "hypoweaver.case002_v2_dev_runner.RuntimeConfigStore.resolve",
                    return_value=runtime,
                ) as resolve,
                patch(
                    "hypoweaver.case002_v2_dev_runner._preflight",
                    side_effect=fake_preflight,
                ),
                patch(
                    "hypoweaver.case002_v2_dev_runner._run_cell",
                    side_effect=fake_run_cell,
                ),
            ):
                suite_root = run_suite(output_root, "single-runtime")

            self.assertEqual(resolve.call_count, 1)
            self.assertTrue(all(value is runtime for value in observed_runtimes))
            manifest = json.loads(
                (suite_root / "suite_manifest.json").read_text(encoding="utf-8")
            )
            summary = json.loads(
                (suite_root / "suite_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["independent_case_count"], 1)
            self.assertEqual(manifest["model"], FROZEN_DEFAULT_MODEL)
            self.assertEqual(manifest["default_model"], FROZEN_DEFAULT_MODEL)
            self.assertEqual(
                manifest["comparison_estimand"], COMPARISON_ESTIMAND
            )
            self.assertEqual(manifest["model_control"], MODEL_CONTROL)
            self.assertEqual(
                manifest["system_model_routing"],
                manifest["preflight"]["system_model_routing"],
            )
            self.assertEqual(
                manifest["public_runtime_envelope"]["system_model_routing"],
                manifest["system_model_routing"],
            )
            self.assertEqual(summary["independent_case_count"], 1)
            self.assertEqual(
                {result["input_view"] for result in summary["results"]},
                {"discovery_blind", "reproduction_aligned"},
            )
            self.assertEqual(summary["non_infrastructure_terminal_cells"], 3)
            self.assertEqual(summary["scientifically_comparable_terminal_cells"], 3)
            self.assertEqual(
                summary["legacy_field_aliases"],
                {
                    "scientifically_comparable_terminal_cells": (
                        "non_infrastructure_terminal_cells"
                    )
                },
            )
            self.assertEqual(summary["paired_completion_eligible_views"], 1)
            self.assertEqual(summary["paired_artifact_quality_eligible_views"], 0)
            by_view = {
                item["input_view"]: item for item in summary["pair_eligibility"]
            }
            self.assertFalse(by_view["discovery_blind"]["completion"]["eligible"])
            self.assertEqual(
                by_view["discovery_blind"]["completion"]["reason_code"],
                "infrastructure_failure_in_pair",
            )
            self.assertTrue(
                by_view["reproduction_aligned"]["completion"]["eligible"]
            )
            self.assertFalse(
                by_view["reproduction_aligned"]["artifact_quality"]["eligible"]
            )
            self.assertEqual(
                by_view["reproduction_aligned"]["artifact_quality"][
                    "reason_code"
                ],
                "one_or_more_system_capability_failures",
            )

    def test_cell_uses_frozen_runtime_and_missing_native_is_explicit(self) -> None:
        runtime = _runtime()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspace"
            project_root = workspace_root / "hypoweaver-workflow"
            case_root = (
                workspace_root
                / "benchmark-cases"
                / "case_002_green_credit_high_pollution_discovery_blind_v2_dev"
            )
            (case_root / "01_model_input").mkdir(parents=True)
            (case_root / "01_model_input" / "case_profile.json").write_text(
                json.dumps({"case_id": "native-case-id"}),
                encoding="utf-8",
            )
            (case_root / "agent_laboratory_config_v2.json").write_text(
                json.dumps({"workflow": {"output_dir": "runs"}}),
                encoding="utf-8",
            )
            (workspace_root / "Agent Laboratory").mkdir()
            project_root.mkdir()
            suite_root = root / "suite"
            completed = CompletedProcess(args=[], returncode=1, stdout="", stderr="")
            with (
                patch(
                    "hypoweaver.case002_v2_dev_runner.WORKSPACE_ROOT",
                    workspace_root,
                ),
                patch(
                    "hypoweaver.case002_v2_dev_runner.PROJECT_ROOT",
                    project_root,
                ),
                patch(
                    "hypoweaver.case002_v2_dev_runner.RuntimeConfigStore.resolve",
                    side_effect=AssertionError("cell must not resolve runtime"),
                ),
                patch(
                    "hypoweaver.case002_v2_dev_runner.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                result = _run_cell(
                    Cell("agent_laboratory", "discovery_blind"),
                    suite_id="suite",
                    suite_root=suite_root,
                    output_root=root / "outputs",
                    runtime=runtime,
                    protocol_sha256="d" * 64,
                )

            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["DASHSCOPE_API_KEY"], runtime.qwen_api_key)
            self.assertEqual(environment["QWEN_BASE_URL"], runtime.qwen_base_url)
            self.assertEqual(environment["QWEN_MODEL"], runtime.qwen_model)
            self.assertIsNone(result["normalized_output"])
            self.assertIsNone(result["native_output_sha256"])
            self.assertEqual(result["failure_class"], "benchmark_infrastructure")
            self.assertEqual(
                result["normalization_reason_code"],
                "native_output_missing",
            )
            self.assertNotIn(
                str(runtime.qwen_api_key),
                (suite_root / "cells" / "discovery_blind-agent_laboratory.json")
                .read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
