from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

import hypoweaver.api as api_module
from hypoweaver.definition import build_app_a_definition
from hypoweaver.engine import PRESET_CASES, WorkflowEngine
from hypoweaver.models import (
    ClaimLedger,
    ClaimRecord,
    ExecutionRecord,
    ResearchRun,
    RunState,
)
from hypoweaver.plot_agent.renderer import resolve_artifact_uri
from hypoweaver.repository import RunRepository
from hypoweaver.visualization import (
    FigureRequest,
    FigureSource,
    LocalFigureRenderer,
    build_figure_requests,
)


def _research_run() -> ResearchRun:
    return ResearchRun(
        research_run_id="research-001",
        case_id="case-001",
        contract_hash="contract-sha256",
        plan_version=1,
        execution_status="succeeded",
        scientific_status="valid",
        fixture_only=False,
        executions=[
            ExecutionRecord(
                execution_id="execution-baseline",
                run_type="baseline",
                plan_step_id="baseline-model",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "green_finance",
                        "coefficient": 0.25,
                        "p_value": 0.04,
                        "confidence_interval_95": [0.02, 0.48],
                        "nobs": 120,
                    }
                ],
                diagnostic_results={
                    "rows_input": 150,
                    "rows_used": 120,
                    "rows_dropped": 30,
                },
            ),
            ExecutionRecord(
                execution_id="execution-robustness",
                run_type="robustness",
                plan_step_id="robustness-model",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "green_finance",
                        "coefficient": 0.2,
                        "confidence_interval_95": [-0.01, 0.41],
                        "nobs": 118,
                    }
                ],
                diagnostic_results={
                    "rows_input": 150,
                    "rows_used": 118,
                    "rows_dropped": 32,
                },
            ),
        ],
    )


def _approved_ledger() -> ClaimLedger:
    return ClaimLedger(
        ledger_id="ledger-001",
        case_id="case-001",
        research_run_id="research-001",
        claims=[
            ClaimRecord(
                claim_id="claim-H1",
                hypothesis_id="H1",
                claim_text="绿色金融变量与结果变量存在条件关联。",
                final_text="绿色金融变量与结果变量存在条件关联。",
                evidence_status="supported",
                allowed_strength="associational",
                supporting_runs=["execution-baseline"],
                opposing_runs=[],
                scope="冻结样本",
                robustness_status="limited",
                unresolved_risks=[],
                approval_status="approved",
                claim_type="associational",
            ),
            ClaimRecord(
                claim_id="claim-H2",
                hypothesis_id="H2",
                claim_text="未获授权结论。",
                evidence_status="inconclusive",
                allowed_strength="prohibited",
                supporting_runs=["execution-robustness"],
                opposing_runs=[],
                scope="冻结样本",
                robustness_status="not_admitted",
                unresolved_risks=[],
                approval_status="rejected",
                claim_type="associational",
            ),
        ],
        excluded_findings=[],
        unresolved_issues=[],
    )


class FigureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = FigureSource(
            artifact_id="workflow-run:research_run",
            artifact_key="research_run",
            sha256="a" * 64,
        )

    def test_contract_rejects_unknown_fields(self) -> None:
        request, _ = build_figure_requests(
            _research_run(),
            self.source,
            "evidence",
        )
        payload = request[0].model_dump(mode="json")
        payload["unknown"] = True

        with self.assertRaises(ValidationError):
            FigureRequest.model_validate(payload)

    def test_definition_places_plot_agent_on_both_workflow_boundaries(self) -> None:
        definition = build_app_a_definition()
        edges = {
            (edge["source"], edge["target"])
            for edge in definition["edges"]
        }

        self.assertIn(
            ("reproduction_audit", "evidence_visualization"),
            edges,
        )
        self.assertIn(
            ("evidence_visualization", "evidence_assessment"),
            edges,
        )
        self.assertIn(
            ("h3_gate", "publication_visualization"),
            edges,
        )
        self.assertIn(
            ("publication_visualization", "scientific_writer"),
            edges,
        )

    def test_evidence_requests_use_only_real_execution_values(self) -> None:
        requests, warnings = build_figure_requests(
            _research_run(),
            self.source,
            "evidence",
        )

        self.assertEqual(
            [request.recipe_id for request in requests],
            ["coefficient_forest", "sample_flow"],
        )
        coefficient = requests[0]
        self.assertEqual(
            coefficient.execution_ids,
            ["execution-baseline", "execution-robustness"],
        )
        self.assertEqual(coefficient.claim_ids, [])
        self.assertEqual(
            coefficient.bindings.data[0]["coefficient"],  # type: ignore[index]
            0.25,
        )
        self.assertEqual(warnings, [])

    def test_publication_requests_exclude_rejected_claim_executions(self) -> None:
        run = _research_run()
        run.executions[0].estimates.append(
            {
                "term": "unapproved_control",
                "coefficient": 0.1,
                "confidence_interval_95": [0.01, 0.19],
                "nobs": 120,
            }
        )
        requests, warnings = build_figure_requests(
            run,
            self.source,
            "publication",
            approved_ledger=_approved_ledger(),
            allowed_estimate_terms={"green_finance"},
        )

        self.assertEqual(len(requests), 2)
        self.assertTrue(
            all(
                request.execution_ids == ["execution-baseline"]
                and request.claim_ids == ["claim-H1"]
                for request in requests
            )
        )
        self.assertNotIn("execution-robustness", str(requests))
        self.assertNotIn("unapproved_control", str(requests))
        self.assertEqual(
            warnings,
            ["论文系数图已排除未进入 Writer 授权范围的估计项。"],
        )

    def test_fixture_never_creates_figure_requests(self) -> None:
        fixture = ResearchRun(
            research_run_id="fixture-run",
            case_id="case-001",
            contract_hash="contract-sha256",
            plan_version=1,
            execution_status="fixture_only",
            scientific_status="not_evaluated",
            fixture_only=True,
        )

        requests, warnings = build_figure_requests(
            fixture,
            self.source,
            "evidence",
        )

        self.assertEqual(requests, [])
        self.assertIn("Fixture", warnings[0])


@unittest.skipUnless(
    importlib.util.find_spec("matplotlib") is not None,
    "matplotlib is installed by backend/requirements.txt",
)
class LocalPlotAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_renderer_writes_all_formats_with_stable_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = FigureSource(
                artifact_id="workflow-run:research_run",
                artifact_key="research_run",
                sha256="a" * 64,
            )
            requests, _ = build_figure_requests(
                _research_run(),
                source,
                "evidence",
            )
            renderer = LocalFigureRenderer(root)

            first = await renderer.render(requests[0])
            second = await renderer.render(requests[0])

            first_files = first.figures[0].files
            second_files = second.figures[0].files
            self.assertEqual(
                [item.sha256 for item in first_files],
                [item.sha256 for item in second_files],
            )
            self.assertEqual(
                {item.format for item in first_files},
                {"svg", "png", "pdf", "csv"},
            )
            for file in first_files:
                path = resolve_artifact_uri(
                    file.artifact_uri,
                    artifact_root=root,
                    expected_sha256=file.sha256,
                )
                self.assertTrue(path.is_file())
            self.assertEqual(
                first.figures[0].data_snapshot,
                {"records": requests[0].bindings.data},
            )
            self.assertNotIn("显著", first.figures[0].caption)

            png = next(file for file in first_files if file.format == "png")
            png_path = resolve_artifact_uri(
                png.artifact_uri,
                artifact_root=root,
            )
            png_path.write_bytes(png_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ValueError, "sha256"):
                resolve_artifact_uri(
                    png.artifact_uri,
                    artifact_root=root,
                    expected_sha256=png.sha256,
                )

    async def test_main_api_streams_hash_verified_figure_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine = WorkflowEngine(RunRepository(root / "runs.db"))
            state = RunState(
                case_id="case-001",
                case_name="Figure API Test",
                mode="research",
                case_submission=PRESET_CASES["green-finance-did"],
            )
            run = _research_run()
            envelope = engine._put_artifact(state, "research_run", run)
            requests, _ = build_figure_requests(
                run,
                FigureSource(
                    artifact_id=envelope["artifact_id"],
                    artifact_key="research_run",
                    sha256=envelope["sha256"],
                ),
                "evidence",
            )
            bundle = await LocalFigureRenderer(root / "figures").render(
                requests[0]
            )
            engine._put_artifact(state, "evidence_figure_bundle", bundle)
            engine.repository.create(state)
            figure = bundle.figures[0]
            png = next(file for file in figure.files if file.format == "png")

            def resolve_for_test(
                artifact_uri: str,
                *,
                expected_sha256: str | None = None,
            ) -> Path:
                return resolve_artifact_uri(
                    artifact_uri,
                    artifact_root=root / "figures",
                    expected_sha256=expected_sha256,
                )

            with (
                patch.object(api_module, "engine", engine),
                patch.object(
                    api_module,
                    "resolve_artifact_uri",
                    side_effect=resolve_for_test,
                ),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api_module.app),
                    base_url="http://127.0.0.1",
                ) as client:
                    response = await client.get(
                        f"/api/v1/runs/{state.id}/figures/{figure.figure_id}/png"
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(
                hashlib.sha256(response.content).hexdigest(),
                png.sha256,
            )


if __name__ == "__main__":
    unittest.main()
