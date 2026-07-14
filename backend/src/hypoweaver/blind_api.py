from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .blind_engine import BlindEngine, SealValidationError
from .blind_models import BlindEvaluationRequest, BlindEvaluationView
from .blind_prompts import build_app_b_definition
from .blind_repository import BlindEvaluationNotFoundError, BlindRepository


repository = BlindRepository()
engine = BlindEngine(repository)
app = FastAPI(
    title="HypoWeaver-Qwen Blind Evaluation API",
    version="1.0.0",
    description="Independent App B; it cannot mutate App A.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_blind_auth(
    request: Request,
    x_hypoweaver_token: str | None = Header(default=None),
) -> None:
    configured = os.getenv("HYPOWEAVER_BLIND_API_TOKEN")
    if configured:
        if not x_hypoweaver_token or not hmac.compare_digest(
            configured, x_hypoweaver_token
        ):
            raise HTTPException(status_code=401, detail="invalid blind API token")
    else:
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(
                status_code=403,
                detail="blind mutations are loopback-only unless HYPOWEAVER_BLIND_API_TOKEN is configured",
            )


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "runtime": "blind-isolated", "definition": "app-b@1.0.0"}


@app.get("/api/v1/definitions/app-b")
def definition() -> dict:
    return build_app_b_definition()


@app.get("/api/v1/evaluations", response_model=list[BlindEvaluationView])
def list_evaluations() -> list[BlindEvaluationView]:
    return engine.list()


@app.post("/api/v1/evaluations", response_model=BlindEvaluationView, status_code=201)
async def create_evaluation(
    request: BlindEvaluationRequest,
    _authorized: None = Depends(require_blind_auth),
) -> BlindEvaluationView:
    try:
        return await engine.evaluate(request)
    except SealValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/evaluations/{evaluation_id}", response_model=BlindEvaluationView)
def get_evaluation(evaluation_id: str) -> BlindEvaluationView:
    try:
        return engine.get(evaluation_id)
    except BlindEvaluationNotFoundError as error:
        raise HTTPException(status_code=404, detail="evaluation not found") from error
