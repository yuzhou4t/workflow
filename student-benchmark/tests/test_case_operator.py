from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "case_operator.py"
SPEC = importlib.util.spec_from_file_location("case_operator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
case_operator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = case_operator
SPEC.loader.exec_module(case_operator)


class CaseOperatorTests(unittest.TestCase):
    def test_policy_partitions_six_systems_between_two_people_per_case(self) -> None:
        policies = case_operator.load_policies()
        self.assertEqual(tuple(sorted(policies)), ("005", "007"))
        for policy in policies.values():
            self.assertTrue(policy["external_execution_allowed"])
            assignments = policy["assignments"]
            self.assertEqual(assignments["hypoweaver"]["systems"], ["hypoweaver"])
            self.assertEqual(
                assignments["baselines"]["systems"],
                [
                    "agent_laboratory",
                    "data_to_paper",
                    "direct_qwen",
                    "qwen_code_agent_writer",
                    "deep_scientist",
                ],
            )
            self.assertEqual(assignments["hypoweaver"]["expected_external_cells"], 4)
            self.assertEqual(assignments["baselines"]["expected_external_cells"], 20)
            assigned_systems = (
                assignments["hypoweaver"]["systems"]
                + assignments["baselines"]["systems"]
            )
            self.assertEqual(tuple(assigned_systems), case_operator.SYSTEMS)

    def test_status_requires_an_explicit_assignment(self) -> None:
        arguments = case_operator.parser().parse_args(
            ["status", "--case", "005", "--assignment", "hypoweaver"]
        )
        self.assertEqual(arguments.assignment, "hypoweaver")

    def test_release_example_keeps_every_case_disabled(self) -> None:
        path = MODULE_PATH.parents[1] / "config" / "release-package.example.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "sixbench-student-release-v1")
        self.assertTrue(all(not row["execution_enabled"] for row in payload["cases"].values()))
        self.assertTrue(
            all(not row["enabled_assignments"] for row in payload["cases"].values())
        )

    def test_preflight_rejects_an_assignment_not_enabled_by_release(self) -> None:
        context = case_operator.Context(
            case_number="005",
            policy={
                "case_id": "case_005_agri_green_finance",
                "execution_mode": "validation_matrix",
                "external_execution_allowed": True,
            },
            assignment_name="hypoweaver",
            assignment={
                "systems": ["hypoweaver"],
                "expected_external_cells": 4,
            },
            release={"release_id": "test-release"},
            case_release={
                "execution_enabled": True,
                "enabled_assignments": ["baselines"],
            },
            workspace=Path("."),
            harness=Path("."),
            python=Path(sys.executable),
            protocol=Path("protocol.json"),
            suite_path=None,
            suite=None,
            executor_contract=None,
            runtime_config=None,
            output_root=None,
        )
        with (
            mock.patch.object(case_operator, "run_local_tests"),
            mock.patch.object(
                case_operator,
                "enumerate_case_cells",
                return_value=[{"cell_id": str(index)} for index in range(4)],
            ),
        ):
            report = case_operator.preflight(context)
        self.assertTrue(report["preflight_passed"])
        self.assertFalse(report["external_execution_ready"])
        self.assertIn(
            "release package does not enable the hypoweaver assignment",
            report["blockers"],
        )

    def test_resolve_beneath_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(case_operator.OperatorError):
                case_operator.resolve_beneath(root, "../outside", "test")
            with self.assertRaises(case_operator.OperatorError):
                case_operator.resolve_beneath(root, "/tmp/outside", "test")

    def test_runtime_config_is_private_and_contains_no_fixed_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-config.json"
            case_operator.write_runtime_config(
                target,
                api_key="test-key",
                model="qwen3.7-plus",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                replace=False,
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["qwen_api_key"], "test-key")
            self.assertNotEqual(payload["research_engine_token"], "test-key")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaises(case_operator.OperatorError):
                case_operator.write_runtime_config(
                    target,
                    api_key="replacement",
                    model="qwen3.7-plus",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    replace=False,
                )


if __name__ == "__main__":
    unittest.main()
