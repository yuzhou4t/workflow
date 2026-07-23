from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "case_operator.py"
SPEC = importlib.util.spec_from_file_location("case_operator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
case_operator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = case_operator
SPEC.loader.exec_module(case_operator)


class CaseOperatorTests(unittest.TestCase):
    def test_policy_contains_exactly_two_assignments(self) -> None:
        policies = case_operator.load_policies()
        self.assertEqual(tuple(sorted(policies)), ("005", "007"))
        self.assertTrue(policies["005"]["external_execution_allowed"])
        self.assertTrue(policies["007"]["external_execution_allowed"])

    def test_release_example_keeps_every_case_disabled(self) -> None:
        path = MODULE_PATH.parents[1] / "config" / "release-package.example.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "sixbench-student-release-v1")
        self.assertTrue(all(not row["execution_enabled"] for row in payload["cases"].values()))

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
