from __future__ import annotations

import hashlib
import json
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
        self.assertEqual(json.loads(config)["workflow"]["max_code_repairs"], 2)
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
        self.assertEqual(response.json()["system_id"], "agent_laboratory_social_science_adapted")
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
