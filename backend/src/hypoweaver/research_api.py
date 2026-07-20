from __future__ import annotations

import hashlib
import json
import os
import platform
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from .case_import import DatasetRegistry
from .models import FormalResearchContract, ResearchRun
from .policy_causal import (
    POLICY_PRIMARY_IMPLEMENTATION_ID,
    POLICY_REPRODUCTION_IMPLEMENTATION_ID,
)
from .research_engine import (
    PANEL_IMPLEMENTATION_ID,
    SPATIAL_IMPLEMENTATION_ID,
    PanelResearchEngine,
    SUPPORTED_METHODS,
)
from .reproducer import (
    IMPLEMENTATION_ID as REPRODUCTION_IMPLEMENTATION_ID,
    PANEL_METHODS,
    ResearchReproducer,
)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: FormalResearchContract


app = FastAPI(title="HypoWeaver Python Research Engine", version="0.1.0")
dataset_registry = DatasetRegistry()
engine = PanelResearchEngine(dataset_registry)
reproducer = ResearchReproducer(dataset_registry)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version(name: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "missing"


def registry_path_sha256(path: Path) -> str:
    """Identify a registry location without disclosing its local path."""

    resolved = path.expanduser().resolve()
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


def dataset_registry_path_sha256() -> str:
    """Return the active shared primary/reproducer registry identity."""

    if engine.registry is not reproducer.registry:
        raise RuntimeError(
            "primary and reproduction engines do not share one dataset registry"
        )
    return registry_path_sha256(engine.registry.path)


def _runtime_identity() -> dict[str, object]:
    package_root = Path(__file__).resolve().parent
    source_files = {
        path.relative_to(package_root).as_posix(): path
        for path in sorted(package_root.rglob("*.py"))
        if path.is_file() and not path.is_symlink()
    }
    source_hashes = {
        name: _sha256_file(path) for name, path in source_files.items()
    }
    environment = {
        "python": platform.python_version(),
        "numpy": _package_version("numpy"),
        "pandas": _package_version("pandas"),
        "scipy": _package_version("scipy"),
        "linearmodels": _package_version("linearmodels"),
        "pyyaml": _package_version("PyYAML"),
    }
    return {
        "service": "hypoweaver-research-engine",
        "api_version": app.version,
        "source_sha256": hashlib.sha256(
            json.dumps(
                source_hashes,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "environment_sha256": hashlib.sha256(
            json.dumps(
                environment,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "primary_implementation_ids": sorted(
            [
                PANEL_IMPLEMENTATION_ID,
                POLICY_PRIMARY_IMPLEMENTATION_ID,
                SPATIAL_IMPLEMENTATION_ID,
            ]
        ),
        "reproduction_implementation_id": REPRODUCTION_IMPLEMENTATION_ID,
        "reproduction_implementation_ids": sorted(
            [
                REPRODUCTION_IMPLEMENTATION_ID,
                POLICY_REPRODUCTION_IMPLEMENTATION_ID,
            ]
        ),
        "supported_methods": sorted(SUPPORTED_METHODS),
        "independent_reproduction_methods": sorted(PANEL_METHODS),
        "reproduction_scope_by_method": {
            "mechanism_boundary": "data_preparation_and_estimator",
            "panel_association": "data_preparation_and_estimator",
            "policy_causal": "estimator_only",
        },
    }


RUNTIME_IDENTITY = _runtime_identity()


def runtime_identity() -> dict[str, object]:
    return dict(RUNTIME_IDENTITY)


def authorize(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("RESEARCH_ENGINE_TOKEN")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid research engine token")


@app.get("/v1/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        **runtime_identity(),
        "dataset_registry_path_sha256": dataset_registry_path_sha256(),
    }


@app.post("/v1/runs", response_model=ResearchRun, dependencies=[Depends(authorize)])
def execute(request: ExecuteRequest) -> ResearchRun:
    return engine.execute(request.contract)


@app.post(
    "/v1/reproductions",
    response_model=ResearchRun,
    dependencies=[Depends(authorize)],
)
def reproduce(request: ExecuteRequest) -> ResearchRun:
    return reproducer.execute(request.contract)
