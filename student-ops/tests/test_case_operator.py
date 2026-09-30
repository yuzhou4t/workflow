from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "case_operator.py"
SPEC = importlib.util.spec_from_file_location("case_operator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
case_operator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = case_operator
SPEC.loader.exec_module(case_operator)


class CaseOperatorTests(unittest.TestCase):
    def context(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            case_number="005",
            policy={"execution_mode": "validation_matrix", "case_id": "case-005"},
            python=Path(sys.executable),
            protocol=root / "protocol.json",
            harness=root,
            suite_path=root / "suite.json",
            suite={"runtime": {}},
        )

    def enumerate_plan(self, context, mutate=lambda rows: None):
        def write_plan(command, **kwargs):
            board = command[command.index("--leaderboard") + 1]
            rows = [
                {
                    "case_id": "case-005",
                    "cell_id": f"{board}-{view}-{system}",
                    "leaderboard": board,
                    "input_view": view,
                    "system_id": system,
                }
                for view in reversed(case_operator.VIEWS)
                for system in reversed(case_operator.SYSTEMS)
            ]
            mutate(rows)
            Path(command[command.index("--output") + 1]).write_text(json.dumps(rows))
            return ""
        with patch.object(case_operator, "run_checked", side_effect=write_plan):
            return case_operator.enumerate_case_cells(context)

    def test_plan_requires_full_cartesian_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self.context(Path(directory))
            rows = self.enumerate_plan(context)
            self.assertEqual(len(rows), 24)
            self.assertEqual(
                [(row["leaderboard"], row["input_view"], row["system_id"]) for row in rows],
                [(board, view, system) for board in case_operator.BOARDS
                 for view in case_operator.VIEWS for system in case_operator.SYSTEMS],
            )
            for field in ("input_view", "system_id"):
                with self.subTest(field=field), self.assertRaises(case_operator.OperatorError):
                    self.enumerate_plan(context, lambda rows: rows[0].update({field: rows[-1][field]}))
            with self.assertRaises(case_operator.OperatorError):
                self.enumerate_plan(context, lambda rows: rows[0].update(input_view="unknown"))
            with self.assertRaises(case_operator.OperatorError):
                self.enumerate_plan(context, lambda rows: rows[0].pop("system_id"))
            with self.assertRaises(case_operator.OperatorError):
                self.enumerate_plan(context, lambda rows: rows[0].update(leaderboard="wrong-board"))
            with self.assertRaises(case_operator.OperatorError):
                self.enumerate_plan(context, lambda rows: rows[0].update(cell_id=rows[1]["cell_id"]))

    def test_partial_native_runs_are_preserved_without_running_or_adapting(self) -> None:
        for artifacts in (
            (), ("benchmark_output.json",), ("normalized_result.json",),
            ("evidence_manifest.json",),
            ("benchmark_output.json", "normalized_result.json"),
            ("benchmark_output.json", "evidence_manifest.json"),
        ):
            with self.subTest(artifacts=artifacts), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run_root = root / "runs"
                run_dir = run_root / "cell"
                run_dir.mkdir(parents=True)
                for name in artifacts:
                    (run_dir / name).write_text("{}")
                with patch.object(case_operator, "run_step") as run_step:
                    with self.assertRaisesRegex(case_operator.OperatorError, "partial native run"):
                        case_operator.run_native_cell(
                            self.context(root), {"cell_id": "cell", "system_id": "hypoweaver"},
                            root / "manifest.json", run_root, root / "logs",
                        )
                    run_step.assert_not_called()
                self.assertEqual(sorted(path.name for path in run_dir.iterdir()), sorted(artifacts))
                self.assertTrue(all(path.read_text() == "{}" for path in run_dir.iterdir()))

    def test_completed_native_run_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "cell"
            run_dir.mkdir(parents=True)
            for name in ("normalized_result.json", "evidence_manifest.json"):
                (run_dir / name).write_text("{}")
            with patch.object(case_operator, "run_step") as run_step:
                case_operator.run_native_cell(
                    self.context(root), {"cell_id": "cell", "system_id": "hypoweaver"},
                    root / "manifest.json", root / "runs", root / "logs",
                )
                run_step.assert_not_called()

    def test_new_native_run_can_run_and_adapt_in_one_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "cell"
            def complete_step(*args, step, **kwargs):
                run_dir.mkdir(parents=True, exist_ok=True)
                names = ("benchmark_output.json",) if step == "run" else (
                    "normalized_result.json", "evidence_manifest.json",
                )
                for name in names:
                    (run_dir / name).write_text('{"run_status": "completed"}')
                return 0
            with patch.object(case_operator, "run_step", side_effect=complete_step) as run_step:
                case_operator.run_native_cell(
                    self.context(root), {"cell_id": "cell", "system_id": "hypoweaver"},
                    root / "manifest.json", root / "runs", root / "logs",
                )
                self.assertEqual([call.kwargs["step"] for call in run_step.call_args_list], ["run", "adapt"])

    def test_policy_contains_exactly_three_case_assignments(self) -> None:
        policies = case_operator.load_policies()
        self.assertEqual(tuple(sorted(policies)), ("005", "007", "009"))
        self.assertTrue(all(row["external_execution_allowed"] for row in policies.values()))
        self.assertTrue(all(row["expected_external_cells"] == 24 for row in policies.values()))
        self.assertEqual(policies["009"]["assignment_state"], "waiting_for_post_repair_release")

    def test_release_example_keeps_every_case_disabled(self) -> None:
        path = MODULE_PATH.parents[1] / "config" / "release-package.example.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "sixbench-student-release-v1")
        self.assertEqual(tuple(sorted(payload["cases"])), ("005", "007", "009"))
        self.assertTrue(all(not row["execution_enabled"] for row in payload["cases"].values()))

    def test_superseded_release_is_rejected(self) -> None:
        policy = case_operator.load_policies()["009"]
        with self.assertRaisesRegex(case_operator.OperatorError, "superseded"):
            case_operator.validate_release_generation(policy, "benchmark-v3-pilot-rc10")
        case_operator.validate_release_generation(policy, "benchmark-v3-pilot-rc11")

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
