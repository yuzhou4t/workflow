from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .case_import import (
    CaseUploadStore,
    CaseImportError,
    LocalCaseImporter,
    LocalCaseImportRequest,
    LocalCaseImportResponse,
)
from .definition import build_app_a_definition
from .engine import WorkflowEngine, WorkflowTransitionError
from .models import CreateRunRequest, GateDecisionRequest, RevisionRequest, RunState
from .repository import (
    RunNotFoundError,
    RunRepository,
    TransitionInProgressError,
    VersionConflictError,
)
from .runtime_config import (
    RuntimeConfigStatus,
    RuntimeConfigStore,
    RuntimeConfigUpdate,
    RuntimeConnectionTestRequest,
    RuntimeConnectionTestResult,
    test_runtime_connection,
)


repository = RunRepository()
engine = WorkflowEngine(repository)
runtime_config_store = RuntimeConfigStore()
case_importer = LocalCaseImporter()
case_upload_store = CaseUploadStore()
app = FastAPI(
    title="HypoWeaver-Qwen Workflow API",
    version="1.0.0",
    description="Code-native workflow runtime. Dify YAML is not loaded at runtime.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://127.0.0.1:5174", "http://127.0.0.1:5175", "http://localhost:5173", "http://localhost:5174", "http://localhost:5175"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def mutation_actor(
    request: Request,
    x_hypoweaver_token: str | None = Header(default=None),
) -> str:
    configured = os.getenv("HYPOWEAVER_API_TOKEN")
    if configured:
        if not x_hypoweaver_token or not hmac.compare_digest(
            configured, x_hypoweaver_token
        ):
            raise HTTPException(status_code=401, detail="invalid workflow API token")
    else:
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(
                status_code=403,
                detail="mutation endpoints are loopback-only unless HYPOWEAVER_API_TOKEN is configured",
            )
    return os.getenv("HYPOWEAVER_ACTOR", "local_researcher")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "runtime": "code-native", "definition": "app-a@1.0.0"}


@app.get("/api/v1/definitions/app-a")
def get_app_a_definition() -> dict:
    return build_app_a_definition()


@app.get("/api/v1/runtime-config", response_model=RuntimeConfigStatus)
def get_runtime_config() -> RuntimeConfigStatus:
    return runtime_config_store.status()


@app.put("/api/v1/runtime-config", response_model=RuntimeConfigStatus)
def update_runtime_config(
    request: RuntimeConfigUpdate,
    _actor: str = Depends(mutation_actor),
) -> RuntimeConfigStatus:
    try:
        return runtime_config_store.update(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post(
    "/api/v1/runtime-config/tests",
    response_model=RuntimeConnectionTestResult,
)
async def test_runtime_config_connection(
    request: RuntimeConnectionTestRequest,
    _actor: str = Depends(mutation_actor),
) -> RuntimeConnectionTestResult:
    return await test_runtime_connection(request, runtime_config_store)


@app.post(
    "/api/v1/case-imports/local",
    response_model=LocalCaseImportResponse,
)
def import_local_case(
    request: LocalCaseImportRequest,
    _actor: str = Depends(mutation_actor),
) -> LocalCaseImportResponse:
    try:
        return case_importer.import_folder(request.path)
    except CaseImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post(
    "/api/v1/case-imports/upload",
    response_model=LocalCaseImportResponse,
)
async def upload_case_file(
    request: Request,
    filename: str,
    _actor: str = Depends(mutation_actor),
) -> LocalCaseImportResponse:
    try:
        uploaded = await case_upload_store.save(filename, request.stream())
        return case_importer.import_folder(uploaded.parent)
    except CaseImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/runs", response_model=list[RunState])
def list_runs() -> list[RunState]:
    return engine.list_runs()


@app.post("/api/v1/runs", response_model=RunState, status_code=201)
async def create_run(
    request: CreateRunRequest,
    _actor: str = Depends(mutation_actor),
) -> RunState:
    try:
        return await engine.create_run(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/v1/runs/{run_id}", response_model=RunState)
def get_run(run_id: str) -> RunState:
    try:
        return engine.get_run(run_id)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


@app.post("/api/v1/runs/{run_id}/advance", response_model=RunState)
async def advance_run(
    run_id: str,
    _actor: str = Depends(mutation_actor),
) -> RunState:
    try:
        return await engine.advance(run_id)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    except WorkflowTransitionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/v1/runs/{run_id}/gates/{gate}", response_model=RunState)
async def decide_gate(
    run_id: str,
    gate: str,
    request: GateDecisionRequest,
    actor: str = Depends(mutation_actor),
) -> RunState:
    try:
        trusted_request = request.model_copy(update={"actor": actor})
        return await engine.decide_gate(run_id, gate, trusted_request)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    except VersionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TransitionInProgressError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except WorkflowTransitionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/v1/runs/{run_id}/revisions", response_model=RunState)
async def submit_revision(
    run_id: str,
    request: RevisionRequest,
    actor: str = Depends(mutation_actor),
) -> RunState:
    try:
        trusted_request = request.model_copy(update={"actor": actor})
        return await engine.submit_revision(run_id, trusted_request)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    except (VersionConflictError, TransitionInProgressError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except WorkflowTransitionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/runs/{run_id}/artifacts/{artifact_key}")
def get_artifact(run_id: str, artifact_key: str) -> dict:
    try:
        run = engine.get_run(run_id)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    try:
        return run.artifacts[artifact_key]
    except KeyError as error:
        raise HTTPException(status_code=404, detail="artifact not found") from error


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DIST_DIR = PROJECT_ROOT / "dist"
if DIST_DIR.exists():
    assets = DIST_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str) -> FileResponse:
        candidate = (DIST_DIR / path).resolve()
        if path and candidate.is_relative_to(DIST_DIR.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST_DIR / "index.html")
