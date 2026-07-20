from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from hypoweaver.case_benchmark_cli import (
    _summary_output,
    build_parser,
    main,
    prepare_case,
    resume_writer_run,
)
from hypoweaver.engine import WorkflowEngine
from hypoweaver.models import (
    CaseSubmission,
    ClaimLedger,
    CreateRunRequest,
    GateDecisionRequest,
    ResearchRun,
)
from hypoweaver.repository import RunRepository


class CaseBenchmarkCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.case_root = self.root / "case"
        self.input_root = self.case_root / "01_model_input"
        self.input_root.mkdir(parents=True)
        self.csv_path = self.input_root / "main_data.csv"
        self.csv_path.write_text("firm,year,y\nF1,2020,1\n", encoding="utf-8")
        digest = hashlib.sha256(self.csv_path.read_bytes()).hexdigest()
        profile = {
            "case_id": "case-test",
            "title": "test",
            "research_question": "question",
            "hypotheses": [
                {
                    "hypothesis_id": "H1",
                    "statement": "statement",
                    "expected_direction": "unspecified",
                    "mechanism": "mechanism",
                }
            ],
            "variables": [
                {
                    "name": "y",
                    "label": "y",
                    "role": "outcome",
                    "definition": "y",
                }
            ],
            "dataset_refs": [
                {
                    "dataset_id": "ds-test",
                    "filename": "main_data.csv",
                    "role": "main",
                    "sha256": digest,
                    "size_bytes": self.csv_path.stat().st_size,
                }
            ],
        }
        (self.input_root / "case_profile.json").write_text(
            json.dumps(profile), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_prepares_only_visible_input_and_registers_exact_dataset(self) -> None:
        hidden = self.case_root / "02_hidden_reference"
        hidden.mkdir()
        (hidden / "reference.json").write_text("not json", encoding="utf-8")

        case, registry, manifest = prepare_case(
            self.case_root,
            registry_path=self.root / "registry.json",
        )

        self.assertEqual(case.case_id, "case-test")
        self.assertEqual(manifest["hidden_reference_access"], "denied_by_runner")
        self.assertEqual(
            registry.resolve(case.dataset_refs[0]),
            self.csv_path.resolve(),
        )

    def test_rejects_dataset_identity_change(self) -> None:
        self.csv_path.write_text("firm,year,y\nF1,2020,2\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "dataset identity mismatch"):
            prepare_case(
                self.case_root,
                registry_path=self.root / "registry.json",
            )

    def test_registers_supplementary_dataset_with_the_main_dataset(self) -> None:
        weights_path = self.input_root / "spatial_weights.csv"
        weights_path.write_text(",A\nA,0\n", encoding="utf-8")
        profile_path = self.input_root / "case_profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["dataset_refs"].append(
            {
                "dataset_id": "ds-spatial-weights",
                "filename": weights_path.name,
                "role": "supplementary",
                "sha256": hashlib.sha256(weights_path.read_bytes()).hexdigest(),
                "size_bytes": weights_path.stat().st_size,
            }
        )
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

        case, registry, manifest = prepare_case(
            self.case_root,
            registry_path=self.root / "registry.json",
        )

        self.assertEqual(len(case.dataset_refs), 2)
        self.assertEqual(
            registry.resolve(case.dataset_refs[1]),
            weights_path.resolve(),
        )
        self.assertEqual(
            [item["role"] for item in manifest["datasets"]],
            ["main", "supplementary"],
        )

    def test_rejects_case_without_exactly_one_main_dataset(self) -> None:
        profile_path = self.input_root / "case_profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["dataset_refs"][0]["role"] = "supplementary"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "exactly one main"):
            prepare_case(
                self.case_root,
                registry_path=self.root / "registry.json",
            )

    def test_validate_only_does_not_require_an_output_root(self) -> None:
        with redirect_stdout(StringIO()):
            status = main(
                [
                    "--case-root",
                    str(self.case_root),
                    "--registry-path",
                    str(self.root / "registry.json"),
                    "--validate-only",
                ]
            )

        self.assertEqual(status, 0)

    def test_v2_budget_flag_is_explicit_and_audited_in_output(self) -> None:
        args = build_parser().parse_args(["--v2-model-budget"])
        self.assertTrue(args.v2_model_budget)
        case = CaseSubmission.model_validate_json(
            (self.input_root / "case_profile.json").read_text(encoding="utf-8")
        )
        output = _summary_output(
            None,
            case=case,
            run_id="v2-test",
            manifest={"model_budget_mode": "v2"},
            elapsed_seconds=0.0,
            error="pre-model failure",
        )
        cost = output["execution_cost"]
        self.assertEqual(cost["budget_mode"], "v2")
        self.assertEqual(cost["provider_attempt_ceiling"], 40)
        self.assertEqual(cost["logical_call_ceiling"], 20)
        self.assertEqual(cost["group_counting_unit"], "logical_call")


class CaseBenchmarkWriterRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.run_dir = self.root / "case-test" / "writer-recovery"
        self.run_dir.mkdir(parents=True)
        self.repository = RunRepository(self.run_dir / "hypoweaver.db")
        engine = WorkflowEngine(self.repository)
        state = await engine.create_run(
            CreateRunRequest(preset_case_id="green-finance-did")
        )
        state = await engine.decide_gate(
            state.id,
            "H1",
            GateDecisionRequest(action="approve", idempotency_key="writer-test-h1"),
        )
        state = await engine.decide_gate(
            state.id,
            "H2",
            GateDecisionRequest(action="approve", idempotency_key="writer-test-h2"),
        )

        research_run = ResearchRun.model_validate(
            state.artifacts["research_run"]["payload"]
        )
        research_run.fixture_only = False
        research_run.execution_status = "succeeded"
        research_run.scientific_status = "limited"
        research_run.not_executed_reason = None
        research_run.executions[0].execution_status = "succeeded"
        execution_id = research_run.executions[0].execution_id
        engine._put_artifact(state, "research_run", research_run)

        ledger = ClaimLedger.model_validate(
            state.artifacts["claim_ledger"]["payload"]
        )
        ledger.research_run_id = research_run.research_run_id
        for claim in ledger.claims:
            claim.evidence_status = "supported"
            claim.allowed_strength = "associational"
            claim.supporting_runs = [execution_id]
            claim.approval_status = "downgraded"
            claim.final_text = "基准模型提供初步关联证据，不支持因果解释。"
        state.claims = ledger.claims
        engine._put_artifact(state, "approved_claim_ledger", ledger)
        engine._put_artifact(
            state,
            "model_usage",
            {
                "max_calls": 20,
                "llm_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "wall_time_seconds": 0,
                "technical_failures": [],
                "call_receipts": [],
            },
        )
        state.mode = "research"
        state.model_provider = "qwen"
        state.execution_mode = "external"
        state.execution_status = "succeeded"
        state.scientific_status = "limited"
        state.plan_only = False
        state.status = "failed"
        state.current_gate = None
        state.current_node_id = "scientific_writer"
        state.last_error = "writer attempt budget exhausted"
        self.state = self.repository.save(state, expected_version=state.version)
        self.frozen_hashes = {
            key: value["sha256"]
            for key, value in self.state.artifacts.items()
            if key
            in {
                "analysis_plan",
                "formal_research_contract",
                "research_run",
                "reproduction_audit",
                "approved_claim_ledger",
                "model_usage",
            }
        }
        self.steps_before = len(self.state.steps)
        (self.run_dir / "input_manifest.json").write_text(
            json.dumps(
                {
                    "case_id": self.state.case_id,
                    "benchmark_track": "strict_blind",
                    "hidden_reference_access": "denied_by_runner",
                }
            ),
            encoding="utf-8",
        )
        self.previous_output = {
            "schema_version": "case-benchmark-output-v1",
            "system_id": "hypoweaver_code_first",
            "case_id": self.state.case_id,
            "requested_run_id": "writer-recovery",
            "run_status": "failed",
            "workflow_status": "failed",
            "failure": "RuntimeError: writer attempt budget exhausted",
            "execution_cost": {"total_wall_time_seconds": 12.5},
            "technical_repair": {"resumed_from_gate": "H3"},
        }
        (self.run_dir / "benchmark_output.json").write_text(
            json.dumps(self.previous_output),
            encoding="utf-8",
        )
        (self.run_dir / "run_state.json").write_text(
            self.state.model_dump_json(),
            encoding="utf-8",
        )
        (self.run_dir / "workflow_failure.json").write_text(
            json.dumps({"error": self.state.last_error}),
            encoding="utf-8",
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_resumes_only_writer_and_preserves_frozen_evidence(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HYPOWEAVER_SEAL_SECRET": (
                    "writer-recovery-test-secret-32-bytes"
                )
            },
        ):
            run_dir, output = await resume_writer_run(run_dir=self.run_dir)

        self.assertEqual(run_dir, self.run_dir.resolve())
        self.assertEqual(output["run_status"], "completed")
        self.assertEqual(output["workflow_status"], "completed")
        self.assertFalse(
            output["technical_repair"]["qwen_writer_calls_repeated"]
        )
        self.assertEqual(len(output["technical_repairs"]), 2)
        completed = self.repository.get(self.state.id)
        for key, expected_hash in self.frozen_hashes.items():
            self.assertEqual(completed.artifacts[key]["sha256"], expected_hash)
        new_step_ids = [
            step.node_id for step in completed.steps[self.steps_before :]
        ]
        self.assertIn("writer_technical_repair", new_step_ids)
        self.assertIn("scientific_writer", new_step_ids)
        self.assertIn("h4_gate", new_step_ids)
        self.assertIn("complete", new_step_ids)
        self.assertNotIn("external_executor", new_step_ids)
        self.assertNotIn("replication_executor", new_step_ids)
        self.assertFalse(any(step.startswith("design_") for step in new_step_ids))
        self.assertTrue(
            (self.run_dir / "pre_writer_repair_benchmark_output.json").is_file()
        )
        self.assertTrue(
            (self.run_dir / "pre_writer_repair_run_state.json").is_file()
        )
        self.assertTrue(
            (self.run_dir / "pre_writer_repair_workflow_failure.json").is_file()
        )
        repair = json.loads(
            (self.run_dir / "writer_technical_repair.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(repair["status"], "completed")
        self.assertFalse(repair["estimation_repeated"])
        self.assertFalse(repair["reproduction_repeated"])
        self.assertFalse(repair["qwen_writer_calls_repeated"])

    async def test_rejects_non_writer_failure_before_mutation(self) -> None:
        state = self.repository.get(self.state.id)
        state.current_node_id = "external_executor"
        self.repository.save(state, expected_version=state.version)

        with self.assertRaisesRegex(ValueError, "failed/scientific_writer"):
            await resume_writer_run(run_dir=self.run_dir)

        self.assertFalse(
            (self.run_dir / "pre_writer_repair_benchmark_output.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
