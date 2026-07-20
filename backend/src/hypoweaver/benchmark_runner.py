from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .case_import import CaseImportError, DatasetRegistry
from .models import CaseSubmission, StrictModel
from .runtime_config import RuntimeConfigStore


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AGENT_LAB_ROOT = PROJECT_ROOT.parent / "Agent Laboratory"
DEFAULT_BENCHMARK_ROOT = Path(tempfile.gettempdir()) / "hypoweaver-benchmarks"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
DEFAULT_AGENT_LAB_PROVIDER_ATTEMPTS = 40
UPSTREAM_AGENT_LAB_SOURCE_FILES = (
    "ai_lab_repo.py",
    "agents.py",
    "mlesolver.py",
    "papersolver.py",
)
UPSTREAM_AGENT_LAB_SOURCE_SHA256 = {
    "ai_lab_repo.py": "799a3c078d835cd632487f52ed5b8e9f155bb9514ca104a30442ecfbcb7765ff",
    "agents.py": "d8d1eda040eb1fa897d84596a1b36f277e83da75433c1a9ac2d9efe5be5c749a",
    "mlesolver.py": "cc110f2b07642532cca11bd8cbf74becfc41016c7f876c16f6d40379ec59882c",
    "papersolver.py": "8c16d68577570fbf6332d3f070e5aec975ed3fd3e1440918940f16eb8911366c",
}
PACKET_ELIGIBLE_FAILURE_REASONS = {
    "prohibited_external_data_collection",
    "model_call_budget_exhausted",
    "upstream_workflow_error",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaselineRunRequest(StrictModel):
    case: CaseSubmission
    execute_generated_code: bool = False


class BaselinePhase(StrictModel):
    id: str
    title: str
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"


class BaselineRun(StrictModel):
    id: str
    system_id: Literal[
        "agent_laboratory_social_science_adapted",
        "agent_laboratory_upstream_original",
    ] = "agent_laboratory_upstream_original"
    case_id: str
    case_name: str
    status: Literal["queued", "running", "completed", "failed"]
    phases: list[BaselinePhase]
    execution_status: str = "not_started"
    scientific_status: str = "not_assessed"
    method_family: str | None = None
    llm_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0, ge=0)
    error: str | None = None
    created_at: str
    updated_at: str


class CompletedBaselineArtifacts(StrictModel):
    run_id: str
    output_dir: str
    output: dict[str, object]
    report_text: str
    output_sha256: str
    report_sha256: str | None = None


PHASES = [
    ("plan", "研究计划", "analysis_plan.json"),
    ("data", "数据准备", "data_profile.json"),
    ("execute", "运行实验", "research_run.json"),
    ("interpret", "解释结果", "result_interpretation.json"),
    ("write", "生成报告", "benchmark_output.json"),
]


class BaselineRunNotFoundError(KeyError):
    pass


class AgentLaboratoryRunner:
    """Runs the existing Agent Laboratory adapter without importing or changing it."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        agent_lab_root: Path | None = None,
        registry: DatasetRegistry | None = None,
        config_store: RuntimeConfigStore | None = None,
        forbidden_read_paths: tuple[Path, ...] = (),
        process_timeout_seconds: int = 1795,
        max_llm_calls: int = DEFAULT_AGENT_LAB_PROVIDER_ATTEMPTS,
        max_steps: int = 5,
        mlesolver_max_steps: int = 1,
        papersolver_max_steps: int = 0,
    ) -> None:
        configured_root = os.getenv("HYPOWEAVER_BENCHMARK_ROOT")
        configured_agent_lab = os.getenv("AGENT_LAB_ROOT")
        self.root = Path(configured_root) if root is None and configured_root else (root or DEFAULT_BENCHMARK_ROOT)
        self.agent_lab_root = (
            Path(configured_agent_lab)
            if agent_lab_root is None and configured_agent_lab
            else (agent_lab_root or DEFAULT_AGENT_LAB_ROOT)
        )
        self.registry = registry or DatasetRegistry()
        self.config_store = config_store or RuntimeConfigStore()
        self.forbidden_read_paths = tuple(
            path.resolve() for path in forbidden_read_paths
        )
        if process_timeout_seconds < 1:
            raise ValueError("Agent Laboratory process timeout must be positive")
        self.process_timeout_seconds = process_timeout_seconds
        if max_llm_calls < 1:
            raise ValueError("Agent Laboratory model budget must be positive")
        if max_steps < 3:
            raise ValueError("Agent Laboratory max_steps must be at least 3")
        self.max_llm_calls = max_llm_calls
        self.max_steps = max_steps
        self.mlesolver_max_steps = mlesolver_max_steps
        self.papersolver_max_steps = papersolver_max_steps
        self._lock = threading.Lock()

    def start(self, request: BaselineRunRequest) -> BaselineRun:
        if not request.execute_generated_code:
            raise ValueError("启动 Agent Laboratory 前必须明确授权本次生成代码执行。")
        self.verify_preflight()
        config = self.config_store.resolve()
        if not config.qwen_api_key:
            raise ValueError("Qwen API Key is required for Agent Laboratory")

        run_id = f"baseline-{uuid4()}"
        workspace = self.root / run_id
        output_dir = workspace / "output" / request.case.case_id / run_id
        self._ensure_isolated_workspace(workspace)
        self._prepare_case(workspace, request.case, config.qwen_model, config.qwen_base_url)
        now = utc_now()
        state = BaselineRun(
            id=run_id,
            case_id=request.case.case_id,
            case_name=request.case.title,
            status="queued",
            phases=[BaselinePhase(id=phase_id, title=title) for phase_id, title, _ in PHASES],
            created_at=now,
            updated_at=now,
        )
        self._write_state(state)
        thread = threading.Thread(
            target=self._run,
            args=(state.id, workspace, output_dir, config.qwen_api_key, config.qwen_base_url),
            daemon=True,
            name=f"agent-lab-{state.id}",
        )
        thread.start()
        return state

    def verify_preflight(self) -> None:
        adapter_entrypoint = (
            self.agent_lab_root / "benchmark_adapter" / "__main__.py"
        )
        if adapter_entrypoint.is_symlink() or not adapter_entrypoint.is_file():
            raise ValueError("Agent Laboratory benchmark adapter is unavailable")
        if not SANDBOX_EXEC.is_file():
            raise ValueError(
                "macOS sandbox-exec is required for generated-code isolation"
            )
        for filename, expected_sha256 in UPSTREAM_AGENT_LAB_SOURCE_SHA256.items():
            path = self.agent_lab_root / filename
            if (
                path.is_symlink()
                or not path.is_file()
                or _sha256(path) != expected_sha256
            ):
                raise ValueError(
                    f"Agent Laboratory frozen upstream source mismatch: {filename}"
                )
        self._ensure_isolated_workspace(self.root / "preflight-probe")

    def get(self, run_id: str) -> BaselineRun:
        path = self._state_path(run_id)
        if not path.is_file():
            raise BaselineRunNotFoundError(run_id)
        state = BaselineRun.model_validate_json(path.read_text(encoding="utf-8"))
        return self._refresh_phases(state)

    def list(self, *, case_id: str | None = None) -> list[BaselineRun]:
        states: list[BaselineRun] = []
        for path in self.root.glob("*/state.json"):
            try:
                state = BaselineRun.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if case_id is None or state.case_id == case_id:
                states.append(self._refresh_phases(state))
        return sorted(states, key=lambda state: state.created_at, reverse=True)

    def load_completed_artifacts(self, run_id: str) -> CompletedBaselineArtifacts:
        """Load one completed baseline without trusting model-owned paths or hashes."""

        root = self.root.resolve()
        workspace = self.root / run_id
        if (
            not run_id
            or workspace.parent.resolve() != root
            or workspace.is_symlink()
        ):
            raise ValueError("invalid Agent Laboratory run id")
        state = self.get(run_id)
        if state.id != run_id or state.status != "completed":
            raise RuntimeError("Agent Laboratory run is not completed")
        output_dir = workspace / "output" / state.case_id / run_id
        expected_files = [
            output_dir / artifact
            for _, _, artifact in PHASES
        ]
        report_path = output_dir / "report.md"
        for path in [*expected_files, report_path]:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(
                    f"Agent Laboratory completed artifact is unavailable: {path.name}"
                )
        output_path = output_dir / "benchmark_output.json"
        try:
            output = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Agent Laboratory benchmark output is unreadable"
            ) from error
        if not isinstance(output, dict):
            raise RuntimeError("Agent Laboratory benchmark output must be an object")
        if (
            output.get("schema_version") != "1.0"
            or output.get("system_id")
            not in {
                "agent_laboratory_social_science_adapted",
                "agent_laboratory_upstream_original",
            }
            or output.get("case_id") != state.case_id
            or output.get("run_status", "completed") != "completed"
        ):
            raise RuntimeError("Agent Laboratory benchmark output identity mismatch")
        if output.get("system_id") == "agent_laboratory_upstream_original":
            self._validate_upstream_provenance(output)

        manuscript = output.get("manuscript")
        if not isinstance(manuscript, dict):
            raise RuntimeError("Agent Laboratory manuscript provenance is missing")
        declared_path = manuscript.get("path")
        declared_sha256 = manuscript.get("sha256")
        try:
            declared_resolved = Path(str(declared_path)).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(
                "Agent Laboratory manuscript path is invalid"
            ) from error
        if declared_resolved != report_path.resolve(strict=True):
            raise RuntimeError("Agent Laboratory manuscript path escapes its run")
        report_bytes = report_path.read_bytes()
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
        if not isinstance(declared_sha256, str) or not hmac.compare_digest(
            report_sha256, declared_sha256
        ):
            raise RuntimeError("Agent Laboratory manuscript sha256 mismatch")

        research_run = output.get("research_run") or {}
        method_route = output.get("method_route") or {}
        usage = output.get("execution_cost") or {}
        if not all(
            isinstance(value, dict)
            for value in (research_run, method_route, usage)
        ):
            raise RuntimeError("Agent Laboratory benchmark summaries are malformed")
        if (
            str(research_run.get("execution_status", "unknown"))
            != state.execution_status
            or str(research_run.get("scientific_status", "not_assessed"))
            != state.scientific_status
            or method_route.get("method_family") != state.method_family
            or int(usage.get("llm_calls", 0) or 0) != state.llm_calls
            or state.llm_calls > self.max_llm_calls
        ):
            raise RuntimeError("Agent Laboratory state/output summary mismatch")
        return CompletedBaselineArtifacts(
            run_id=run_id,
            output_dir=str(output_dir.resolve()),
            output=output,
            report_text=report_bytes.decode("utf-8"),
            output_sha256=_sha256(output_path),
            report_sha256=report_sha256,
        )

    def load_terminal_failure_artifacts(
        self,
        run_id: str,
    ) -> CompletedBaselineArtifacts:
        """Load an expected upstream capability failure as a neutral packet input."""

        root = self.root.resolve()
        workspace = self.root / run_id
        if (
            not run_id
            or workspace.parent.resolve() != root
            or workspace.is_symlink()
        ):
            raise ValueError("invalid Agent Laboratory run id")
        state = self.get(run_id)
        if state.id != run_id or state.status != "failed":
            raise RuntimeError("Agent Laboratory run is not failed")
        output_dir = workspace / "output" / state.case_id / run_id
        required_paths = {
            "output": output_dir / "benchmark_output.json",
            "failure": output_dir / "workflow_failure.json",
            "usage": output_dir / "model_usage.json",
        }
        if any(path.is_symlink() or not path.is_file() for path in required_paths.values()):
            raise RuntimeError("Agent Laboratory structured failure artifacts are unavailable")
        try:
            output = json.loads(required_paths["output"].read_text(encoding="utf-8"))
            failure_record = json.loads(
                required_paths["failure"].read_text(encoding="utf-8")
            )
            model_usage = json.loads(
                required_paths["usage"].read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Agent Laboratory structured failure artifacts are unreadable"
            ) from error
        if not all(
            isinstance(value, dict)
            for value in (output, failure_record, model_usage)
        ):
            raise RuntimeError("Agent Laboratory structured failure is malformed")
        failure = output.get("failure")
        research_run = output.get("research_run")
        usage = output.get("execution_cost")
        provenance = output.get("provenance")
        if not all(
            isinstance(value, dict)
            for value in (failure, research_run, usage, provenance)
        ):
            raise RuntimeError("Agent Laboratory failure summary is malformed")
        reason_code = str(failure.get("reason_code") or "")
        if (
            output.get("schema_version") != "1.0"
            or output.get("run_status") != "failed"
            or output.get("system_id") != "agent_laboratory_upstream_original"
            or output.get("case_id") != state.case_id
            or output.get("manuscript") is not None
            or research_run.get("execution_status") != "failed"
            or reason_code not in PACKET_ELIGIBLE_FAILURE_REASONS
            or provenance.get("workflow_variant")
            != "upstream_laboratory_workflow"
            or provenance.get("upstream_entrypoint")
            != "ai_lab_repo.LaboratoryWorkflow.perform_research"
            or provenance.get("hidden_reference_accessed") is not False
        ):
            raise RuntimeError(
                "Agent Laboratory failure is not eligible for neutral comparison"
            )
        self._validate_upstream_provenance(output)
        usage_fields = (
            "logical_calls",
            "llm_calls",
            "input_tokens",
            "output_tokens",
            "wall_time_seconds",
            "provider_wait_seconds",
            "technical_failures",
            "call_receipts",
        )
        if any(usage.get(key) != model_usage.get(key) for key in usage_fields):
            raise RuntimeError("Agent Laboratory failure usage artifacts disagree")
        if (
            failure_record.get("failure") != failure
            or failure_record.get("model_usage") != model_usage
            or int(usage.get("llm_calls", 0) or 0) != state.llm_calls
            or state.llm_calls > self.max_llm_calls
            or len(usage.get("call_receipts") or []) != state.llm_calls
        ):
            raise RuntimeError("Agent Laboratory failure state/output mismatch")
        return CompletedBaselineArtifacts(
            run_id=run_id,
            output_dir=str(output_dir.resolve()),
            output=output,
            report_text="",
            output_sha256=_sha256(required_paths["output"]),
        )

    def _validate_upstream_provenance(self, output: dict[str, object]) -> None:
        provenance = output.get("provenance")
        if not isinstance(provenance, dict):
            raise RuntimeError("Agent Laboratory upstream provenance is missing")
        expected_source_hashes = {
            filename: _sha256(self.agent_lab_root / filename)
            for filename in UPSTREAM_AGENT_LAB_SOURCE_FILES
        }
        if (
            provenance.get("upstream_repository")
            != "https://github.com/SamuelSchmidgall/AgentLaboratory"
            or provenance.get("upstream_commit")
            != "d9017d90e329112d2a80b7712f37ee9094d2cd27"
            or provenance.get("upstream_source_hashes")
            != expected_source_hashes
        ):
            raise RuntimeError("Agent Laboratory upstream provenance mismatch")

    def _prepare_case(
        self,
        workspace: Path,
        case: CaseSubmission,
        model: str,
        base_url: str,
    ) -> None:
        if not case.dataset_refs:
            raise ValueError("Agent Laboratory requires a registered CSV dataset")
        dataset_ref = next((item for item in case.dataset_refs if item.role == "main"), case.dataset_refs[0])
        source = self.registry.resolve(dataset_ref)
        visible = workspace / "case" / "01_model_input"
        visible.mkdir(parents=True, mode=0o700)
        target = visible / "main_data.csv"
        shutil.copyfile(source, target)
        os.chmod(target, 0o400)
        if _sha256(target) != dataset_ref.sha256:
            raise CaseImportError("baseline input hash does not match the registered dataset")

        supplementary_assets: list[dict[str, str]] = []
        for asset_ref in (item for item in case.dataset_refs if item.role == "supplementary"):
            source = self.registry.resolve(asset_ref)
            asset_target = visible / Path(asset_ref.filename).name
            shutil.copyfile(source, asset_target)
            os.chmod(asset_target, 0o400)
            if _sha256(asset_target) != asset_ref.sha256:
                raise CaseImportError(
                    f"baseline supplementary input hash does not match: {asset_ref.filename}"
                )
            supplementary_assets.append(
                {
                    "filename": asset_target.name,
                    "sha256": asset_ref.sha256,
                    "role": asset_ref.role,
                }
            )

        profile = [
            f"# {case.title}",
            "",
            f"研究问题：{case.research_question}",
            f"分析单位：{case.unit_of_analysis or '待确认'}",
            f"样本范围：{case.sample_period or '待确认'}",
            "",
            "## 待验证假设",
            *[f"- {item.hypothesis_id}: {item.statement}" for item in case.hypotheses],
            "",
            "## 客观事实与约束",
            *[f"- {item}" for item in [*case.known_policy_facts, *case.constraints]],
        ]
        if supplementary_assets:
            profile.extend(
                [
                    "",
                    "## 可见补充资产",
                    *[
                        f"- {item['filename']}（SHA256: {item['sha256']}）"
                        for item in supplementary_assets
                    ],
                ]
            )
        (visible / "case_profile.md").write_text("\n".join(profile) + "\n", encoding="utf-8")
        (visible / "data_description.md").write_text(
            "数据由同一 Benchmark 输入上传并按 SHA256 锁定。变量角色来自 H1 前的保守识别，正式解释需结合研究边界。\n",
            encoding="utf-8",
        )
        with (visible / "data_dictionary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name", "label", "role", "definition", "source"])
            for variable in case.variables:
                writer.writerow(
                    [variable.name, variable.label or "", variable.role, variable.definition or "", variable.source or ""]
                )
        runner_config = {
            "case": {
                "case_id": case.case_id,
                "model_input_dir": "case/01_model_input",
                "files": {
                    "case_profile": "case_profile.md",
                    "main_data": "main_data.csv",
                    "supplementary_assets": [
                        item["filename"] for item in supplementary_assets
                    ],
                    "data_dictionary": "data_dictionary.csv",
                    "data_description": "data_description.md",
                },
                "input_sha256": {
                    "main_data.csv": dataset_ref.sha256,
                    **{
                        item["filename"]: item["sha256"]
                        for item in supplementary_assets
                    },
                },
            },
            "model": {
                "name": model,
                "api_key_env": "DASHSCOPE_API_KEY",
                "base_url": base_url,
                "timeout_seconds": 360,
                "max_tokens": 12_288,
            },
            "workflow": {
                "output_dir": "output",
                "execution_timeout_seconds": 600,
                "max_code_repairs": 2,
                "max_llm_calls": self.max_llm_calls,
                "max_steps": self.max_steps,
                "num_papers_lit_review": 1,
                "mlesolver_max_steps": self.mlesolver_max_steps,
                "papersolver_max_steps": self.papersolver_max_steps,
            },
        }
        (workspace / "runner_config.json").write_text(
            json.dumps(runner_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        runtime_root = workspace / "runtime"
        shutil.copytree(
            self.agent_lab_root / "benchmark_adapter",
            runtime_root / "benchmark_adapter",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        for filename in UPSTREAM_AGENT_LAB_SOURCE_FILES:
            source_path = self.agent_lab_root / filename
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError(
                    f"Agent Laboratory upstream source is unavailable: {filename}"
                )
            shutil.copyfile(source_path, runtime_root / filename)
        forbidden_paths = [
            PROJECT_ROOT,
            self.agent_lab_root,
            *self.forbidden_read_paths,
        ]
        configured_forbidden = os.getenv(
            "HYPOWEAVER_BENCHMARK_FORBIDDEN_READ_PATHS", ""
        )
        forbidden_paths.extend(
            Path(item)
            for item in configured_forbidden.split(os.pathsep)
            if item.strip()
        )
        forbidden_read_rules = "".join(
            f"(deny file-read* (subpath {_sandbox_literal(path.resolve())}))\n"
            for path in dict.fromkeys(forbidden_paths)
        )
        sandbox_profile = (
            "(version 1)\n"
            "(allow default)\n"
            "(deny file-write*)\n"
            f"(allow file-write* (subpath {_sandbox_literal(workspace / 'output')}))\n"
            f"(allow file-write* (subpath {_sandbox_literal(workspace / 'state_saves')}))\n"
            + forbidden_read_rules
        )
        (workspace / "agent-lab.sb").write_text(sandbox_profile, encoding="utf-8")

    def _run(
        self,
        run_id: str,
        workspace: Path,
        output_dir: Path,
        api_key: str,
        base_url: str,
    ) -> None:
        state = self.get(run_id).model_copy(update={"status": "running", "updated_at": utc_now()})
        self._write_state(state)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(workspace),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(workspace / "runtime"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "DASHSCOPE_API_KEY": api_key,
            "QWEN_BASE_URL": base_url,
        }
        command = [
            str(SANDBOX_EXEC),
            "-f",
            str(workspace / "agent-lab.sb"),
            sys.executable,
            "-m",
            "benchmark_adapter",
            "--config",
            str(workspace / "runner_config.json"),
            "--execute-generated-code",
            "--run-id",
            run_id,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                text=True,
                capture_output=True,
                timeout=self.process_timeout_seconds,
                check=False,
            )
            runner_config = json.loads(
                (workspace / "runner_config.json").read_text(encoding="utf-8")
            )
            visible = workspace / "case" / "01_model_input"
            for filename, expected_sha256 in runner_config["case"][
                "input_sha256"
            ].items():
                if _sha256(visible / filename) != expected_sha256:
                    raise RuntimeError(
                        f"Agent Laboratory modified frozen input: {filename}"
                    )
            output = json.loads(
                (output_dir / "benchmark_output.json").read_text(encoding="utf-8")
            )
            if not isinstance(output, dict):
                raise RuntimeError("Agent Laboratory output is not an object")
            research_run = output.get("research_run", {})
            usage = output.get("execution_cost", {})
            method_route = output.get("method_route") or {}
            if not all(
                isinstance(value, dict)
                for value in (research_run, usage, method_route)
            ):
                raise RuntimeError("Agent Laboratory output summary is malformed")
            if int(usage.get("llm_calls", 0) or 0) > self.max_llm_calls:
                raise RuntimeError(
                    f"Agent Laboratory exceeded {self.max_llm_calls} provider attempts"
                )
            run_status = str(output.get("run_status") or "completed")
            if completed.returncode == 0 and run_status != "completed":
                raise RuntimeError("Agent Laboratory success exit has failed output")
            if completed.returncode == 2 and run_status != "failed":
                raise RuntimeError("Agent Laboratory failure exit has no failed output")
            if completed.returncode not in {0, 2}:
                raise RuntimeError("Agent Laboratory process failed without a terminal artifact")
            failure = output.get("failure") or {}
            if not isinstance(failure, dict):
                raise RuntimeError("Agent Laboratory failure summary is malformed")
            state = self.get(run_id).model_copy(
                update={
                    "status": "completed" if run_status == "completed" else "failed",
                    "execution_status": str(research_run.get("execution_status", "unknown")),
                    "scientific_status": str(research_run.get("scientific_status", "not_assessed")),
                    "method_family": method_route.get("method_family"),
                    "llm_calls": int(usage.get("llm_calls", 0) or 0),
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "wall_time_seconds": float(usage.get("wall_time_seconds", 0) or 0),
                    "error": (
                        str(failure.get("reason_code") or "upstream_workflow_error")
                        if run_status == "failed"
                        else None
                    ),
                    "updated_at": utc_now(),
                }
            )
        except Exception as error:
            state = self.get(run_id).model_copy(
                update={
                    "status": "failed",
                    "execution_status": "failed",
                    "scientific_status": "invalid",
                    "error": type(error).__name__,
                    "updated_at": utc_now(),
                }
            )
        self._write_state(self._refresh_phases(state))

    def _ensure_isolated_workspace(self, workspace: Path) -> None:
        resolved = workspace.resolve()
        for forbidden in (PROJECT_ROOT.resolve(), self.agent_lab_root.resolve()):
            try:
                resolved.relative_to(forbidden)
            except ValueError:
                continue
            raise ValueError(
                f"benchmark workspace must be outside source repositories: {resolved}"
            )

    def _refresh_phases(self, state: BaselineRun) -> BaselineRun:
        output_dir = self.root / state.id / "output" / state.case_id / state.id
        phases: list[BaselinePhase] = []
        first_pending_seen = False
        for phase_id, title, artifact in PHASES:
            if (output_dir / artifact).is_file():
                status = "succeeded"
            elif state.status == "failed" and not first_pending_seen:
                status = "failed"
                first_pending_seen = True
            elif state.status == "running" and not first_pending_seen:
                status = "running"
                first_pending_seen = True
            else:
                status = "pending"
            phases.append(BaselinePhase(id=phase_id, title=title, status=status))
        return state.model_copy(update={"phases": phases})

    def _state_path(self, run_id: str) -> Path:
        return self.root / run_id / "state.json"

    def _write_state(self, state: BaselineRun) -> None:
        with self._lock:
            path = self._state_path(state.id)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent, prefix=".state-", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(state.model_dump_json(indent=2))
                    handle.write("\n")
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sandbox_literal(path: Path) -> str:
    escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
