from __future__ import annotations

import csv
import hashlib
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
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT / "backend" / "var" / "benchmarks"


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
    system_id: Literal["agent_laboratory_social_science_adapted"] = (
        "agent_laboratory_social_science_adapted"
    )
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
        self._lock = threading.Lock()

    def start(self, request: BaselineRunRequest) -> BaselineRun:
        if not request.execute_generated_code:
            raise ValueError("启动 Agent Laboratory 前必须明确授权本次生成代码执行。")
        if not (self.agent_lab_root / "benchmark_adapter" / "__main__.py").is_file():
            raise ValueError("Agent Laboratory benchmark adapter is unavailable")
        config = self.config_store.resolve()
        if not config.qwen_api_key:
            raise ValueError("Qwen API Key is required for Agent Laboratory")

        run_id = f"baseline-{uuid4()}"
        workspace = self.root / run_id
        output_dir = workspace / "output" / request.case.case_id / run_id
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
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        if _sha256(target) != dataset_ref.sha256:
            raise CaseImportError("baseline input hash does not match the registered dataset")

        supplementary_assets: list[dict[str, str]] = []
        for asset_ref in (item for item in case.dataset_refs if item.role == "supplementary"):
            source = self.registry.resolve(asset_ref)
            asset_target = visible / Path(asset_ref.filename).name
            try:
                os.link(source, asset_target)
            except OSError:
                shutil.copyfile(source, asset_target)
            os.chmod(asset_target, 0o600)
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
            },
            "model": {
                "name": model,
                "api_key_env": "DASHSCOPE_API_KEY",
                "base_url": base_url,
                "timeout_seconds": 120,
            },
            "workflow": {
                "output_dir": "output",
                "execution_timeout_seconds": 180,
                "max_code_repairs": 2,
            },
        }
        (workspace / "runner_config.json").write_text(
            json.dumps(runner_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

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
            "PYTHONPATH": str(self.agent_lab_root),
            "DASHSCOPE_API_KEY": api_key,
            "QWEN_BASE_URL": base_url,
        }
        command = [
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
                cwd=self.agent_lab_root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=1800,
                check=False,
            )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "Agent Laboratory failed")[-4000:]
                raise RuntimeError(message)
            output = json.loads((output_dir / "benchmark_output.json").read_text(encoding="utf-8"))
            research_run = output.get("research_run", {})
            usage = output.get("execution_cost", {})
            method_route = output.get("method_route", {})
            state = self.get(run_id).model_copy(
                update={
                    "status": "completed",
                    "execution_status": str(research_run.get("execution_status", "unknown")),
                    "scientific_status": str(research_run.get("scientific_status", "not_assessed")),
                    "method_family": method_route.get("method_family"),
                    "llm_calls": int(usage.get("llm_calls", 0) or 0),
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "wall_time_seconds": float(usage.get("wall_time_seconds", 0) or 0),
                    "updated_at": utc_now(),
                }
            )
        except Exception as error:
            state = self.get(run_id).model_copy(
                update={
                    "status": "failed",
                    "execution_status": "failed",
                    "scientific_status": "invalid",
                    "error": str(error),
                    "updated_at": utc_now(),
                }
            )
        self._write_state(self._refresh_phases(state))

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
