from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "package-template"
    / "tools"
    / "student_handoff.py"
)
SPEC = importlib.util.spec_from_file_location("student_handoff", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
student_handoff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = student_handoff
SPEC.loader.exec_module(student_handoff)

BOOTSTRAP_PATH = MODULE_PATH.with_name("bootstrap.py")
BOOTSTRAP_SPEC = importlib.util.spec_from_file_location("student_bootstrap", BOOTSTRAP_PATH)
assert BOOTSTRAP_SPEC is not None and BOOTSTRAP_SPEC.loader is not None
student_bootstrap = importlib.util.module_from_spec(BOOTSTRAP_SPEC)
sys.modules[BOOTSTRAP_SPEC.name] = student_bootstrap
BOOTSTRAP_SPEC.loader.exec_module(student_bootstrap)


class StudentHandoffTests(unittest.TestCase):
    def make_package(self, root: Path) -> dict[str, object]:
        cells = [
            {
                "cell_id": "cell-a",
                "system_id": "hypoweaver",
                "input_view": "discovery_blind",
                "leaderboard_id": "native_system_package",
                "seed": 20260720,
            },
            {
                "cell_id": "cell-b",
                "system_id": "hypoweaver",
                "input_view": "reproduction_aligned",
                "leaderboard_id": "common_executor_reasoning_control",
                "seed": 20260720,
            },
        ]
        assignment = {
            "schema_version": "sixbench-student-package-v1",
            "package_id": "test-case005-hypoweaver",
            "execution_state": "formal",
            "release_id": "test-release",
            "case_number": "005",
            "case_id": "case_005_agri_green_finance",
            "case_title_zh": "农业绿色金融",
            "assignment": "hypoweaver",
            "assignment_title_zh": "HypoWeaver 执行",
            "systems": ["hypoweaver"],
            "expected_cell_count": len(cells),
            "paths": {
                "workspace": ".",
                "release_package": "release-package.json",
                "operator": "workflow/student-benchmark/scripts/case_operator.py",
                "output_root": "results",
                "runs": "results/suite/runs",
                "contracts": "results/contracts",
                "orchestration": "results/suite/student-orchestration/hypoweaver",
            },
            "expected_cells": cells,
        }
        (root / "ASSIGNMENT.json").write_text(
            json.dumps(assignment),
            encoding="utf-8",
        )
        return assignment

    def seal_cell(
        self,
        root: Path,
        assignment: dict[str, object],
        cell: dict[str, object],
        run_status: str,
    ) -> None:
        contracts = root / "results" / "contracts" / str(cell["cell_id"])
        runs = root / "results" / "suite" / "runs" / str(cell["cell_id"])
        contracts.mkdir(parents=True)
        runs.mkdir(parents=True)
        identity = {
            **cell,
            "case_id": assignment["case_id"],
        }
        (contracts / "cell_manifest.json").write_text(
            json.dumps(identity),
            encoding="utf-8",
        )
        (runs / "normalized_result.json").write_text(
            json.dumps(
                {
                    **identity,
                    "run_status": run_status,
                    "failure_class": "workflow" if run_status == "failed" else None,
                    "score_eligibility": "validation_only",
                    "claims": [{"claim_id": "claim-1"}],
                    "executions": [{"execution_id": "execution-1"}],
                    "reported_completed_check_ids": ["check-1", "check-2"],
                    "false_evidence_flags": [],
                }
            ),
            encoding="utf-8",
        )
        (runs / "evidence_manifest.json").write_text(
            json.dumps({"run_id": cell["cell_id"], "artifacts": [], "facts": []}),
            encoding="utf-8",
        )

    def test_report_keeps_delivery_completeness_separate_from_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = self.make_package(root)
            cells = assignment["expected_cells"]
            assert isinstance(cells, list)
            self.seal_cell(root, assignment, cells[0], "completed")
            self.seal_cell(root, assignment, cells[1], "failed")

            report = student_handoff.build_report(root)

            self.assertEqual(report["collection_status"], "complete")
            self.assertEqual(report["scientific_outcome"], "mixed")
            self.assertEqual(report["counts"]["sealed"], 2)
            self.assertEqual(report["counts"]["successful"], 1)
            self.assertEqual(report["counts"]["failed"], 1)
            self.assertEqual(report["cells"][0]["claim_count"], 1)
            self.assertEqual(report["cells"][0]["execution_count"], 1)
            self.assertEqual(report["cells"][0]["completed_check_count"], 2)
            markdown = (root / "RETURN" / "RESULT_SUMMARY.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("| cell-a |", markdown)
            self.assertIn("| cell-b |", markdown)

    def test_missing_artifact_marks_collection_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = self.make_package(root)
            cells = assignment["expected_cells"]
            assert isinstance(cells, list)
            self.seal_cell(root, assignment, cells[0], "completed")

            report = student_handoff.build_report(root)

            self.assertEqual(report["collection_status"], "incomplete")
            self.assertEqual(report["scientific_outcome"], "not_evaluated")
            self.assertEqual(report["missing_cells"], ["cell-b"])

    def test_unexpected_cell_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = self.make_package(root)
            cells = assignment["expected_cells"]
            assert isinstance(cells, list)
            for cell in cells:
                self.seal_cell(root, assignment, cell, "completed")
            rogue = root / "results" / "suite" / "runs" / "rogue-cell"
            rogue.mkdir(parents=True)
            (rogue / "normalized_result.json").write_text("{}", encoding="utf-8")

            report = student_handoff.build_report(root)

            self.assertEqual(report["collection_status"], "needs_review")
            self.assertEqual(report["unexpected_cells"], ["rogue-cell"])

    def test_invalid_json_is_partial_and_not_silently_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = self.make_package(root)
            cells = assignment["expected_cells"]
            assert isinstance(cells, list)
            for cell in cells:
                self.seal_cell(root, assignment, cell, "completed")
            broken = (
                root
                / "results"
                / "suite"
                / "runs"
                / "cell-a"
                / "evidence_manifest.json"
            )
            broken.write_text("{not-json", encoding="utf-8")

            report = student_handoff.build_report(root)

            self.assertEqual(report["collection_status"], "needs_review")
            self.assertEqual(report["cells"][0]["delivery_status"], "partial")
            self.assertFalse(
                report["cells"][0]["artifact_valid_json"][
                    "evidence_manifest.json"
                ]
            )

    def test_bundle_rejects_a_symlinked_result_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = self.make_package(root)
            cells = assignment["expected_cells"]
            assert isinstance(cells, list)
            for cell in cells:
                self.seal_cell(root, assignment, cell, "completed")
            evidence = (
                root
                / "results"
                / "suite"
                / "runs"
                / "cell-a"
                / "evidence_manifest.json"
            )
            outside = root.parent / f"{root.name}-outside.json"
            outside.write_text('{"run_id":"cell-a"}', encoding="utf-8")
            evidence.unlink()
            evidence.symlink_to(outside)
            try:
                report = student_handoff.build_report(root)
                self.assertEqual(report["collection_status"], "needs_review")
                self.assertNotIn(
                    "evidence_manifest.json",
                    report["cells"][0]["artifact_sha256"],
                )
                with self.assertRaises(student_handoff.PackageError):
                    student_handoff.build_return_bundle(root)
            finally:
                outside.unlink(missing_ok=True)

    def test_bundle_uses_allowlist_and_writes_structured_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = self.make_package(root)
            cells = assignment["expected_cells"]
            assert isinstance(cells, list)
            for cell in cells:
                self.seal_cell(root, assignment, cell, "completed")
            secret = root / "hypoweaver-workflow" / "backend" / "var"
            secret.mkdir(parents=True)
            (secret / "runtime-config.json").write_text(
                '{"credential_marker":"must-not-return"}',
                encoding="utf-8",
            )

            pointer, archive = student_handoff.build_return_bundle(root)

            self.assertEqual(
                pointer["schema_version"],
                "sixbench-student-return-pointer-v1",
            )
            self.assertEqual(pointer["collection_status"], "complete")
            self.assertTrue(archive.is_file())
            self.assertEqual(pointer["return_archive_sha256"], student_handoff.sha256_file(archive))
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                self.assertTrue(
                    any(name.endswith("summary/RESULT_SUMMARY.json") for name in names)
                )
                self.assertTrue(
                    any(name.endswith("artifacts/cell-a/normalized_result.json") for name in names)
                )
                self.assertFalse(any("runtime-config.json" in name for name in names))
            pointer_path = root / "RETURN" / "RETURN_POINTER.json"
            self.assertTrue(pointer_path.is_file())

    def test_explain_reports_the_real_absolute_package_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_package(root)

            payload = student_handoff.explain_payload(root)

            self.assertEqual(payload["package_root"], str(root.resolve()))
            self.assertEqual(payload["case_number"], "005")
            self.assertEqual(payload["expected_cell_count"], 2)

    def test_bootstrap_resolves_only_package_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_package(root)
            harness = root / "harness"
            configs = harness / "configs"
            configs.mkdir(parents=True)
            (harness / "environment.txt").write_text("pytest==7.4.4\n", encoding="utf-8")
            (configs / "inventory.json").write_text(
                json.dumps(
                    {
                        "frozen_files": {
                            "harness_environment_freeze": "environment.txt"
                        }
                    }
                ),
                encoding="utf-8",
            )
            (configs / "protocol.json").write_text(
                json.dumps(
                    {
                        "release_requirements": {
                            "inventory": {"path": "configs/inventory.json"}
                        }
                    }
                ),
                encoding="utf-8",
            )
            (configs / "suite.json").write_text(
                json.dumps(
                    {
                        "paths": {
                            "data_to_paper_repo": "../../data-to-paper",
                            "deep_scientist_repo": "../../DeepScientist"
                        },
                        "provider": {
                            "model": "qwen3.7-plus",
                            "default_base_url": "https://example.invalid/v1"
                        },
                        "outbound_authorization": {
                            "provider_id": "test-provider",
                            "release_lock_path": "../../results/release-lock.json"
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "release-package.json").write_text(
                json.dumps(
                    {
                        "release_id": "test-release",
                        "harness_repo": "harness",
                        "python": "harness/.venv/bin/python",
                        "protocol": "harness/configs/protocol.json",
                        "cases": {"005": {"suite": "harness/configs/suite.json"}}
                    }
                ),
                encoding="utf-8",
            )

            context = student_bootstrap.load_setup_context(root)
            setup_status = student_bootstrap.status(root, context)

            self.assertEqual(context["harness"], harness.resolve())
            self.assertEqual(
                context["release_lock"],
                (root / "results" / "release-lock.json").resolve(),
            )
            self.assertFalse(setup_status["ready"])
            self.assertEqual(
                set(setup_status["checks"]),
                {"harness_python", "release_lock"},
            )


if __name__ == "__main__":
    unittest.main()
