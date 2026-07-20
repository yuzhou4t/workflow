from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import hypoweaver.api as api_module
from hypoweaver.benchmark_runner import (
    AgentLaboratoryRunner,
    BaselinePhase,
    BaselineRun,
    BaselineRunRequest,
)
from hypoweaver.case_import import DatasetRegistry
from hypoweaver.models import CaseSubmission, DatasetRef, Hypothesis, VariableSpec
from hypoweaver.runtime_config import RuntimeConfigStore, RuntimeConfigUpdate


class AgentLaboratoryRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.data_path = self.root / "source.csv"
        self.data_path.write_text("firm,year,y,x\nA,2020,1,2\nA,2021,2,3\n", encoding="utf-8")
        digest = hashlib.sha256(self.data_path.read_bytes()).hexdigest()
        self.dataset_ref = DatasetRef(
            dataset_id=f"ds_{digest[:16]}",
            filename="source.csv",
            sha256=digest,
            size_bytes=self.data_path.stat().st_size,
        )
        registry = DatasetRegistry(self.root / "datasets.json")
        registry.register(self.dataset_ref, self.data_path)
        self.weights_path = self.root / "spatial_weights.csv"
        self.weights_path.write_text(
            "spatial_id,A,B\nA,0,1\nB,1,0\n",
            encoding="utf-8",
        )
        weights_digest = hashlib.sha256(self.weights_path.read_bytes()).hexdigest()
        self.weights_ref = DatasetRef(
            dataset_id=f"ds_{weights_digest[:16]}",
            role="supplementary",
            filename="spatial_weights.csv",
            sha256=weights_digest,
            size_bytes=self.weights_path.stat().st_size,
        )
        registry.register(self.weights_ref, self.weights_path)
        config_store = RuntimeConfigStore(self.root / "runtime-config.json")
        config_store.update(
            RuntimeConfigUpdate(qwen_api_key="secret", qwen_model="qwen-test")
        )
        agent_lab_root = self.root / "Agent Laboratory"
        (agent_lab_root / "benchmark_adapter").mkdir(parents=True)
        (agent_lab_root / "benchmark_adapter" / "__main__.py").write_text("", encoding="utf-8")
        for filename in (
            "ai_lab_repo.py",
            "agents.py",
            "mlesolver.py",
            "papersolver.py",
        ):
            (agent_lab_root / filename).write_text(
                f"# frozen test source: {filename}\n",
                encoding="utf-8",
            )
        self.runner = AgentLaboratoryRunner(
            root=self.root / "benchmarks",
            agent_lab_root=agent_lab_root,
            registry=registry,
            config_store=config_store,
        )
        self.case = _case(self.dataset_ref, self.weights_ref)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_prepare_case_keeps_only_visible_input_and_same_dataset_hash(self) -> None:
        workspace = self.root / "prepared"
        self.runner._prepare_case(workspace, self.case, "qwen-test", "https://example.test/v1")

        visible = workspace / "case" / "01_model_input"
        self.assertEqual(
            hashlib.sha256((visible / "main_data.csv").read_bytes()).hexdigest(),
            self.dataset_ref.sha256,
        )
        self.assertNotEqual(
            self.data_path.stat().st_ino,
            (visible / "main_data.csv").stat().st_ino,
        )
        self.assertEqual((visible / "main_data.csv").stat().st_mode & 0o777, 0o400)
        self.assertTrue((visible / "case_profile.md").is_file())
        self.assertTrue((visible / "data_dictionary.csv").is_file())
        self.assertEqual(
            hashlib.sha256((visible / "spatial_weights.csv").read_bytes()).hexdigest(),
            self.weights_ref.sha256,
        )
        profile = (visible / "case_profile.md").read_text(encoding="utf-8")
        self.assertIn("spatial_weights.csv", profile)
        config = (workspace / "runner_config.json").read_text(encoding="utf-8")
        self.assertIn('"supplementary_assets"', config)
        parsed_config = json.loads(config)
        self.assertEqual(parsed_config["workflow"]["max_code_repairs"], 2)
        self.assertEqual(parsed_config["workflow"]["max_llm_calls"], 40)
        self.assertEqual(parsed_config["workflow"]["max_steps"], 5)
        self.assertEqual(parsed_config["workflow"]["mlesolver_max_steps"], 1)
        self.assertEqual(parsed_config["workflow"]["papersolver_max_steps"], 0)
        self.assertEqual(
            parsed_config["case"]["input_sha256"]["main_data.csv"],
            self.dataset_ref.sha256,
        )
        self.assertFalse(any("02_hidden_reference" in str(path) for path in workspace.rglob("*")))
        self.assertNotIn("secret", config)

    def test_start_requires_explicit_generated_code_authorization(self) -> None:
        with self.assertRaisesRegex(ValueError, "明确授权"):
            self.runner.start(BaselineRunRequest(case=self.case))

    def test_list_returns_latest_matching_case(self) -> None:
        older = BaselineRun(
            id="baseline-older",
            case_id="case-test",
            case_name="测试案例",
            status="completed",
            phases=[],
            created_at="2026-07-14T00:00:00Z",
            updated_at="2026-07-14T00:00:00Z",
        )
        newer = older.model_copy(
            update={
                "id": "baseline-newer",
                "created_at": "2026-07-15T00:00:00Z",
                "updated_at": "2026-07-15T00:00:00Z",
            }
        )
        other = older.model_copy(update={"id": "baseline-other", "case_id": "other"})
        for state in (older, newer, other):
            self.runner._write_state(state)

        states = self.runner.list(case_id="case-test")

        self.assertEqual([state.id for state in states], ["baseline-newer", "baseline-older"])

    def test_adapter_process_runs_from_isolated_workspace(self) -> None:
        run_id = "baseline-isolated"
        workspace = self.runner.root / run_id
        output_dir = workspace / "output" / self.case.case_id / run_id
        self.runner._prepare_case(
            workspace, self.case, "qwen-test", "https://example.test/v1"
        )
        self.runner._write_state(
            BaselineRun(
                id=run_id,
                case_id=self.case.case_id,
                case_name=self.case.title,
                status="queued",
                phases=[],
                created_at="2026-07-16T00:00:00Z",
                updated_at="2026-07-16T00:00:00Z",
            )
        )
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            output_dir.mkdir(parents=True)
            (output_dir / "benchmark_output.json").write_text(
                json.dumps(
                    {
                        "research_run": {
                            "execution_status": "success",
                            "scientific_status": "not_assessed",
                        },
                        "execution_cost": {"llm_calls": 8},
                        "method_route": {"method_family": "panel_association"},
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch("hypoweaver.benchmark_runner.subprocess.run", side_effect=fake_run):
            self.runner._run(
                run_id,
                workspace,
                output_dir,
                "secret",
                "https://example.test/v1",
            )

        self.assertEqual(captured["cwd"], workspace)
        environment = captured["env"]
        self.assertEqual(environment["HOME"], str(workspace))
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(environment["PYTHONPATH"], str(workspace / "runtime"))
        self.assertEqual(captured["command"][0], "/usr/bin/sandbox-exec")
        profile = (workspace / "agent-lab.sb").read_text(encoding="utf-8")
        self.assertIn("deny file-read", profile)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(
            f'(allow file-write* (subpath "{(workspace / "output").resolve()}"))',
            profile,
        )
        self.assertIn(str(self.runner.agent_lab_root), profile)
        self.assertTrue(
            (workspace / "runtime" / "benchmark_adapter" / "__main__.py").is_file()
        )
        self.assertNotEqual(captured["cwd"], self.runner.agent_lab_root)
        self.assertEqual(self.runner.get(run_id).status, "completed")

    def test_explicit_hidden_reference_path_is_denied_to_generated_code(self) -> None:
        hidden_reference = self.root / "sealed-reference.json"
        hidden_reference.write_text('{"answer":"hidden"}', encoding="utf-8")
        runner = AgentLaboratoryRunner(
            root=self.runner.root,
            agent_lab_root=self.runner.agent_lab_root,
            registry=self.runner.registry,
            config_store=self.runner.config_store,
            forbidden_read_paths=(hidden_reference,),
        )
        workspace = self.root / "hidden-denied"

        runner._prepare_case(
            workspace,
            self.case,
            "qwen-test",
            "https://example.test/v1",
        )

        profile = (workspace / "agent-lab.sb").read_text(encoding="utf-8")
        self.assertIn(
            f'(deny file-read* (subpath "{hidden_reference.resolve()}"))',
            profile,
        )

    def test_workspace_inside_either_source_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside source repositories"):
            self.runner._ensure_isolated_workspace(
                self.runner.agent_lab_root / "generated-output"
            )

    def test_completed_artifacts_are_rehashed_before_loading(self) -> None:
        run_id = "baseline-completed"
        output_dir = self._write_completed_artifacts(run_id)

        artifacts = self.runner.load_completed_artifacts(run_id)

        self.assertEqual(artifacts.run_id, run_id)
        self.assertEqual(artifacts.report_text, "verified report\n")
        self.assertEqual(
            artifacts.output_sha256,
            hashlib.sha256(
                (output_dir / "benchmark_output.json").read_bytes()
            ).hexdigest(),
        )

    def test_completed_artifacts_reject_report_tampering(self) -> None:
        run_id = "baseline-tampered"
        output_dir = self._write_completed_artifacts(run_id)
        (output_dir / "report.md").write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "manuscript sha256 mismatch"):
            self.runner.load_completed_artifacts(run_id)

    def test_expected_upstream_failure_is_loaded_without_inventing_a_report(self) -> None:
        run_id = "baseline-upstream-failure"
        self._write_failure_artifacts(
            run_id,
            reason_code="prohibited_external_data_collection",
        )

        artifacts = self.runner.load_terminal_failure_artifacts(run_id)

        self.assertEqual(artifacts.report_text, "")
        self.assertIsNone(artifacts.report_sha256)
        self.assertEqual(artifacts.output["run_status"], "failed")

    def test_technical_upstream_failure_cannot_become_comparison_packet(self) -> None:
        run_id = "baseline-technical-failure"
        self._write_failure_artifacts(
            run_id,
            reason_code="model_technical_failure",
        )

        with self.assertRaisesRegex(RuntimeError, "not eligible"):
            self.runner.load_terminal_failure_artifacts(run_id)

    def _write_completed_artifacts(self, run_id: str) -> Path:
        output_dir = (
            self.runner.root / run_id / "output" / self.case.case_id / run_id
        )
        output_dir.mkdir(parents=True)
        for filename in (
            "analysis_plan.json",
            "data_profile.json",
            "research_run.json",
            "result_interpretation.json",
        ):
            (output_dir / filename).write_text("{}\n", encoding="utf-8")
        report_path = output_dir / "report.md"
        report_path.write_text("verified report\n", encoding="utf-8")
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        output = {
            "schema_version": "1.0",
            "system_id": "agent_laboratory_social_science_adapted",
            "case_id": self.case.case_id,
            "method_route": {"method_family": "panel_association"},
            "research_run": {
                "execution_status": "success",
                "scientific_status": "not_assessed",
            },
            "execution_cost": {"llm_calls": 6},
            "manuscript": {
                "path": str(report_path.resolve()),
                "sha256": report_sha256,
            },
        }
        (output_dir / "benchmark_output.json").write_text(
            json.dumps(output, ensure_ascii=False), encoding="utf-8"
        )
        self.runner._write_state(
            BaselineRun(
                id=run_id,
                case_id=self.case.case_id,
                case_name=self.case.title,
                status="completed",
                phases=[],
                execution_status="success",
                scientific_status="not_assessed",
                method_family="panel_association",
                llm_calls=6,
                created_at="2026-07-16T00:00:00Z",
                updated_at="2026-07-16T00:00:00Z",
            )
        )
        return output_dir

    def _write_failure_artifacts(self, run_id: str, *, reason_code: str) -> Path:
        output_dir = (
            self.runner.root / run_id / "output" / self.case.case_id / run_id
        )
        output_dir.mkdir(parents=True)
        failure = {
            "reason_code": reason_code,
            "error_type": "ProhibitedDataCollectionError",
            "failed_phase": "literature review",
        }
        usage = {
            "max_calls": 20,
            "llm_calls": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "wall_time_seconds": 0.1,
            "technical_failures": [],
            "call_receipts": [
                {
                    "provider": "qwen",
                    "model": "qwen-test",
                    "started_at": "2026-07-16T00:00:00+00:00",
                    "completed_at": "2026-07-16T00:00:01+00:00",
                    "response_sha256": "a" * 64,
                }
            ],
        }
        output = {
            "schema_version": "1.0",
            "run_status": "failed",
            "system_id": "agent_laboratory_upstream_original",
            "case_id": self.case.case_id,
            "model": "qwen-test",
            "method_route": None,
            "analysis_plan": None,
            "research_run": {
                "execution_status": "failed",
                "scientific_status": "invalid",
                "failure_reason_code": reason_code,
            },
            "result_interpretation": None,
            "claim_ledger": None,
            "manuscript": None,
            "failure": failure,
            "execution_cost": {
                **usage,
                "estimated_cost_usd": None,
                "human_minutes": 0,
            },
            "phase_statistics": {},
            "provenance": {
                "upstream_repository": (
                    "https://github.com/SamuelSchmidgall/AgentLaboratory"
                ),
                "upstream_commit": "d9017d90e329112d2a80b7712f37ee9094d2cd27",
                "upstream_source_hashes": {
                    filename: hashlib.sha256(
                        (self.runner.agent_lab_root / filename).read_bytes()
                    ).hexdigest()
                    for filename in (
                        "ai_lab_repo.py",
                        "agents.py",
                        "mlesolver.py",
                        "papersolver.py",
                    )
                },
                "workflow_variant": "upstream_laboratory_workflow",
                "upstream_entrypoint": (
                    "ai_lab_repo.LaboratoryWorkflow.perform_research"
                ),
                "hidden_reference_accessed": False,
            },
        }
        (output_dir / "benchmark_output.json").write_text(
            json.dumps(output, ensure_ascii=False),
            encoding="utf-8",
        )
        (output_dir / "model_usage.json").write_text(
            json.dumps(usage, ensure_ascii=False),
            encoding="utf-8",
        )
        (output_dir / "workflow_failure.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_status": "failed",
                    "system_id": "agent_laboratory_upstream_original",
                    "case_id": self.case.case_id,
                    "failure": failure,
                    "model_usage": usage,
                    "hidden_reference_accessed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.runner._write_state(
            BaselineRun(
                id=run_id,
                case_id=self.case.case_id,
                case_name=self.case.title,
                status="failed",
                phases=[],
                execution_status="failed",
                scientific_status="invalid",
                llm_calls=1,
                input_tokens=10,
                output_tokens=5,
                wall_time_seconds=0.1,
                error=reason_code,
                created_at="2026-07-16T00:00:00Z",
                updated_at="2026-07-16T00:00:00Z",
            )
        )
        return output_dir


class BaselineApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url="http://127.0.0.1",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_start_endpoint_returns_independent_baseline_state(self) -> None:
        dataset_ref = DatasetRef(
            dataset_id="ds_test",
            filename="data.csv",
            sha256="0" * 64,
            size_bytes=1,
        )
        state = BaselineRun(
            id="baseline-test",
            case_id="case-test",
            case_name="测试案例",
            status="queued",
            phases=[BaselinePhase(id="plan", title="研究计划")],
            created_at="2026-07-14T00:00:00Z",
            updated_at="2026-07-14T00:00:00Z",
        )

        class FakeRunner:
            def start(self, request: BaselineRunRequest) -> BaselineRun:
                self.request = request
                return state

        fake = FakeRunner()
        with patch.object(api_module, "baseline_runner", fake):
            response = await self.client.post(
                "/api/v1/baselines/agent-laboratory/runs",
                json={
                    "case": _case(dataset_ref).model_dump(mode="json"),
                    "execute_generated_code": True,
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["system_id"],
            "agent_laboratory_upstream_original",
        )
        self.assertTrue(fake.request.execute_generated_code)

    async def test_list_endpoint_filters_by_case(self) -> None:
        state = BaselineRun(
            id="baseline-test",
            case_id="case-test",
            case_name="测试案例",
            status="completed",
            phases=[],
            created_at="2026-07-14T00:00:00Z",
            updated_at="2026-07-14T00:00:00Z",
        )

        class FakeRunner:
            def list(self, *, case_id: str | None = None) -> list[BaselineRun]:
                self.case_id = case_id
                return [state]

        fake = FakeRunner()
        with patch.object(api_module, "baseline_runner", fake):
            response = await self.client.get(
                "/api/v1/baselines/agent-laboratory/runs?case_id=case-test"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "baseline-test")
        self.assertEqual(fake.case_id, "case-test")


def _case(dataset_ref: DatasetRef, *supplementary_refs: DatasetRef) -> CaseSubmission:
    return CaseSubmission(
        case_id="case-test",
        title="测试案例",
        research_question="x 是否影响 y？",
        hypotheses=[Hypothesis(hypothesis_id="H1", statement="x 影响 y。")],
        unit_of_analysis="企业—年度",
        sample_period="2020—2021",
        data_structure_hint="panel",
        variables=[
            VariableSpec(name="firm", role="id"),
            VariableSpec(name="year", role="time"),
            VariableSpec(name="y", role="outcome"),
            VariableSpec(name="x", role="exposure"),
        ],
        dataset_refs=[dataset_ref, *supplementary_refs],
    )


if __name__ == "__main__":
    unittest.main()
