from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from .models import FormalResearchContract, ResearchRun
from .research_engine import PanelResearchEngine, SUPPORTED_METHODS


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: FormalResearchContract


app = FastAPI(title="HypoWeaver Python Research Engine", version="0.1.0")
engine = PanelResearchEngine()


def authorize(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("RESEARCH_ENGINE_TOKEN")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid research engine token")


@app.get("/v1/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "hypoweaver-research-engine",
        "supported_methods": sorted(SUPPORTED_METHODS),
    }


@app.post("/v1/runs", response_model=ResearchRun, dependencies=[Depends(authorize)])
def execute(request: ExecuteRequest) -> ResearchRun:
    return engine.execute(request.contract)
