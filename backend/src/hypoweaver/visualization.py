from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .models import ClaimLedger, ResearchRun, StrictModel
from .seal import canonical_sha256


FigureStage = Literal["evidence", "publication"]
FigureStatus = Literal["succeeded", "not_generated", "failed"]

_FORMATS = ["svg", "png", "pdf", "csv"]
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNAUTHORIZED_METADATA = re.compile(
    r"显著(?:正向|负向|促进|抑制)|假设(?:得到支持|成立)|证明(?:了)?|导致"
)


class FigureSource(StrictModel):
    artifact_id: str = Field(min_length=1)
    artifact_key: str = Field(min_length=1)
    sha256: str = Field(min_length=1)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _HEX_SHA256.fullmatch(value):
            raise ValueError("source sha256 must be a lowercase hex digest")
        return value


class FigureBindings(StrictModel):
    data: list[dict[str, Any]] | dict[str, Any]


class FigureRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1)
    stage: FigureStage
    case_id: str = Field(min_length=1)
    research_run_id: str = Field(min_length=1)
    contract_hash: str = Field(min_length=1)
    recipe_id: Literal["coefficient_forest", "sample_flow"]
    recipe_version: Literal["1.0"] = "1.0"
    source: FigureSource
    execution_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    bindings: FigureBindings
    style_profile: Literal["journal_bw_v1"] = "journal_bw_v1"
    locale: Literal["zh-CN"] = "zh-CN"
    formats: list[Literal["svg", "png", "pdf", "csv"]] = Field(
        default_factory=lambda: list(_FORMATS),
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_references(self) -> "FigureRequest":
        if len(self.execution_ids) != len(set(self.execution_ids)):
            raise ValueError("execution_ids must be unique")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("claim_ids must be unique")
        if self.stage == "evidence" and self.claim_ids:
            raise ValueError("evidence figures cannot claim H3 authorization")
        if self.stage == "publication" and not self.claim_ids:
            raise ValueError("publication figures require H3-authorized claim_ids")
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("formats must be unique")
        return self


class FigureFile(StrictModel):
    format: Literal["svg", "png", "pdf", "csv"]
    mime_type: str = Field(min_length=1)
    artifact_uri: str = Field(min_length=1)
    sha256: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_file_identity(self) -> "FigureFile":
        if not self.artifact_uri.startswith("artifact://"):
            raise ValueError("figure files must use artifact:// URIs")
        if not _HEX_SHA256.fullmatch(self.sha256):
            raise ValueError("figure file sha256 must be a lowercase hex digest")
        return self


class FigureArtifact(StrictModel):
    figure_id: str = Field(min_length=1)
    recipe_id: Literal["coefficient_forest", "sample_flow"]
    recipe_version: Literal["1.0"]
    title: str = Field(min_length=1)
    caption: str = Field(min_length=1)
    alt_text: str = Field(min_length=1)
    execution_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    files: list[FigureFile] = Field(min_length=1)
    data_snapshot: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_metadata_and_references(self) -> "FigureArtifact":
        if len(self.execution_ids) != len(set(self.execution_ids)):
            raise ValueError("figure execution_ids must be unique")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("figure claim_ids must be unique")
        formats = [file.format for file in self.files]
        if len(formats) != len(set(formats)):
            raise ValueError("figure file formats must be unique")
        if _UNAUTHORIZED_METADATA.search(
            " ".join((self.title, self.caption, self.alt_text))
        ):
            raise ValueError("figure metadata contains unauthorized conclusion language")
        return self


class FigureBundle(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(min_length=1)
    stage: FigureStage
    status: FigureStatus
    figures: list[FigureArtifact] = Field(default_factory=list)
    renderer: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "FigureBundle":
        figure_ids = [figure.figure_id for figure in self.figures]
        if len(figure_ids) != len(set(figure_ids)):
            raise ValueError("figure_ids must be unique")
        if self.status == "succeeded" and not self.figures:
            raise ValueError("succeeded FigureBundle requires at least one figure")
        if self.status != "succeeded" and self.figures:
            raise ValueError("non-succeeded FigureBundle cannot contain figures")
        return self


class FigureRenderer(Protocol):
    async def render(self, request: FigureRequest) -> FigureBundle: ...


class LocalFigureRenderer:
    def __init__(
        self,
        artifact_root: Path | None = None,
    ) -> None:
        self.artifact_root = artifact_root

    async def render(self, request: FigureRequest) -> FigureBundle:
        from .plot_agent.renderer import render_request

        payload = await asyncio.to_thread(
            render_request,
            request.model_dump(mode="json"),
            artifact_root=self.artifact_root,
        )
        bundle = FigureBundle.model_validate(payload)
        _validate_renderer_response(request, bundle)
        return bundle


def _validate_renderer_response(
    request: FigureRequest,
    bundle: FigureBundle,
) -> None:
    if bundle.stage != request.stage:
        raise RuntimeError("FigureBundle stage does not match FigureRequest")
    if len(bundle.figures) != 1:
        raise RuntimeError("one FigureRequest must return exactly one figure")
    figure = bundle.figures[0]
    if (
        figure.recipe_id != request.recipe_id
        or figure.recipe_version != request.recipe_version
    ):
        raise RuntimeError("Figure recipe identity does not match request")
    if figure.execution_ids != request.execution_ids:
        raise RuntimeError("Figure execution_ids do not match request")
    if figure.claim_ids != request.claim_ids:
        raise RuntimeError("Figure claim_ids do not match request")
    returned_formats = {item.format for item in figure.files}
    if not set(request.formats).issubset(returned_formats):
        raise RuntimeError("Figure response is missing requested formats")
    data = request.bindings.data
    expected_snapshot = (
        {"records": data}
        if request.recipe_id == "coefficient_forest"
        else data
    )
    if canonical_sha256(figure.data_snapshot) != canonical_sha256(
        expected_snapshot
    ):
        raise RuntimeError(
            "Figure data_snapshot differs from requested data; "
            "test-data fallback is forbidden"
        )


def build_figure_requests(
    run: ResearchRun,
    source: FigureSource,
    stage: FigureStage,
    *,
    approved_ledger: ClaimLedger | None = None,
    allowed_estimate_terms: set[str] | None = None,
) -> tuple[list[FigureRequest], list[str]]:
    warnings: list[str] = []
    if run.fixture_only or run.execution_status in {"not_executed", "fixture_only"}:
        return [], ["Fixture 或未执行 ResearchRun 禁止生成实证图。"]

    succeeded = {
        execution.execution_id: execution
        for execution in run.executions
        if execution.execution_status == "succeeded"
    }
    claim_ids: list[str] = []
    allowed_execution_ids = set(succeeded)
    claims_by_execution: dict[str, set[str]] = {}

    if stage == "publication":
        if approved_ledger is None:
            return [], ["Publication 绘图缺少 approved_claim_ledger。"]
        if (
            approved_ledger.case_id != run.case_id
            or approved_ledger.research_run_id != run.research_run_id
        ):
            raise ValueError(
                "approved_claim_ledger does not match the ResearchRun"
            )
        approved_claims = [
            claim
            for claim in approved_ledger.claims
            if claim.approval_status in {"approved", "downgraded"}
        ]
        if not approved_claims:
            return [], ["H3 没有批准或降级的 Claim，不生成论文图。"]
        allowed_execution_ids = set()
        for claim in approved_claims:
            referenced = set(claim.supporting_runs) | set(claim.opposing_runs)
            matched = referenced & set(succeeded)
            if not matched:
                warnings.append(
                    f"Claim {claim.claim_id} 没有引用成功的 Execution，未进入论文图。"
                )
                continue
            claim_ids.append(claim.claim_id)
            allowed_execution_ids.update(matched)
            for execution_id in matched:
                claims_by_execution.setdefault(execution_id, set()).add(
                    claim.claim_id
                )
        if not allowed_execution_ids:
            return [], list(dict.fromkeys(warnings))

    coefficient_rows: list[dict[str, Any]] = []
    coefficient_execution_ids: set[str] = set()
    excluded_publication_estimate = False
    for execution_id in sorted(allowed_execution_ids):
        execution = succeeded[execution_id]
        for estimate in execution.estimates:
            term = estimate.get("term")
            if (
                stage == "publication"
                and (
                    not isinstance(term, str)
                    or term not in (allowed_estimate_terms or set())
                )
            ):
                excluded_publication_estimate = True
                continue
            coefficient = _finite_float(estimate.get("coefficient"))
            interval = estimate.get("confidence_interval_95")
            if (
                not isinstance(term, str)
                or not term.strip()
                or not isinstance(interval, list)
                or len(interval) != 2
                or coefficient is None
            ):
                warnings.append(
                    f"Execution {execution_id} 的一条估计缺少 term/coefficient/95% CI。"
                )
                continue
            ci_lower = _finite_float(interval[0])
            ci_upper = _finite_float(interval[1])
            if ci_lower is None or ci_upper is None or ci_lower > ci_upper:
                warnings.append(
                    f"Execution {execution_id} 的一条估计包含无效 95% CI。"
                )
                continue
            row: dict[str, Any] = {
                "term": term.strip(),
                "coefficient": coefficient,
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                "execution_id": execution_id,
            }
            p_value = _finite_float(estimate.get("p_value"))
            sample_size = _finite_int(estimate.get("nobs"))
            if p_value is not None:
                row["p_value"] = p_value
            if sample_size is not None:
                row["sample_size"] = sample_size
            coefficient_rows.append(row)
            coefficient_execution_ids.add(execution_id)

    if excluded_publication_estimate:
        warnings.append(
            "论文系数图已排除未进入 Writer 授权范围的估计项。"
        )

    requests: list[FigureRequest] = []
    if coefficient_rows:
        coefficient_rows.sort(key=lambda item: (item["execution_id"], item["term"]))
        execution_ids = sorted(coefficient_execution_ids)
        request_claim_ids = _claim_ids_for_executions(
            execution_ids,
            claims_by_execution,
            claim_ids,
        )
        requests.append(
            _figure_request(
                run,
                source,
                stage,
                recipe_id="coefficient_forest",
                execution_ids=execution_ids,
                claim_ids=request_claim_ids,
                data=coefficient_rows,
            )
        )
    else:
        warnings.append("没有具备 95% 置信区间的成功估计，未生成系数图。")

    sample_candidate = None
    for execution_id in sorted(
        allowed_execution_ids,
        key=lambda item: (succeeded[item].run_type != "baseline", item),
    ):
        diagnostics = succeeded[execution_id].diagnostic_results
        rows_input = _finite_int(diagnostics.get("rows_input"))
        rows_used = _finite_int(diagnostics.get("rows_used"))
        rows_dropped = _finite_int(diagnostics.get("rows_dropped"))
        if None in {rows_input, rows_used, rows_dropped}:
            continue
        assert rows_input is not None and rows_used is not None and rows_dropped is not None
        if min(rows_input, rows_used, rows_dropped) < 0:
            continue
        if rows_input != rows_used + rows_dropped:
            warnings.append(
                f"Execution {execution_id} 的样本数不闭合，未生成样本流程图。"
            )
            continue
        sample_candidate = (
            execution_id,
            {
                "rows_input": rows_input,
                "rows_used": rows_used,
                "rows_dropped": rows_dropped,
            },
        )
        break
    if sample_candidate is not None:
        execution_id, data = sample_candidate
        requests.append(
            _figure_request(
                run,
                source,
                stage,
                recipe_id="sample_flow",
                execution_ids=[execution_id],
                claim_ids=_claim_ids_for_executions(
                    [execution_id],
                    claims_by_execution,
                    claim_ids,
                ),
                data=data,
            )
        )
    else:
        warnings.append("没有闭合的 rows_input/rows_used/rows_dropped，未生成样本流程图。")

    return requests, list(dict.fromkeys(warnings))


async def render_figure_requests(
    renderer: FigureRenderer,
    requests: list[FigureRequest],
    stage: FigureStage,
    *,
    initial_warnings: list[str] | None = None,
) -> FigureBundle:
    figures: list[FigureArtifact] = []
    warnings = list(initial_warnings or [])
    for request in requests:
        try:
            bundle = await renderer.render(request)
        except Exception as error:
            warnings.append(
                f"{request.recipe_id}@{request.recipe_version} 渲染失败：{error}"
            )
            continue
        figures.extend(bundle.figures)
        warnings.extend(bundle.warnings)
    status: FigureStatus = "succeeded" if figures else "failed"
    identity = {
        "stage": stage,
        "request_ids": [request.request_id for request in requests],
        "figure_files": [
            file.sha256 for figure in figures for file in figure.files
        ],
    }
    return FigureBundle(
        bundle_id=f"figure-bundle-{canonical_sha256(identity)[:24]}",
        stage=stage,
        status=status,
        figures=figures,
        renderer={"name": "hypoweaver-task4-adapter", "version": "1.0"},
        warnings=list(dict.fromkeys(warnings)),
    )


def empty_figure_bundle(
    stage: FigureStage,
    reason: str,
    *,
    status: Literal["not_generated", "failed"] = "not_generated",
) -> FigureBundle:
    identity = {"stage": stage, "status": status, "reason": reason}
    return FigureBundle(
        bundle_id=f"figure-bundle-{canonical_sha256(identity)[:24]}",
        stage=stage,
        status=status,
        renderer={"name": "hypoweaver-task4-adapter", "version": "1.0"},
        warnings=[reason],
    )


def publication_figure_problems(
    bundle: FigureBundle,
    *,
    approved_claim_ids: set[str],
    allowed_execution_ids: set[str],
) -> list[str]:
    if bundle.status != "succeeded":
        return []
    problems: list[str] = []
    for figure in bundle.figures:
        if not set(figure.claim_ids).issubset(approved_claim_ids):
            problems.append(
                f"Publication Figure {figure.figure_id} 引用了未经 H3 授权的 Claim"
            )
        if not set(figure.execution_ids).issubset(allowed_execution_ids):
            problems.append(
                f"Publication Figure {figure.figure_id} 引用了不存在的 Execution"
            )
    return problems


def _figure_request(
    run: ResearchRun,
    source: FigureSource,
    stage: FigureStage,
    *,
    recipe_id: Literal["coefficient_forest", "sample_flow"],
    execution_ids: list[str],
    claim_ids: list[str],
    data: list[dict[str, Any]] | dict[str, Any],
) -> FigureRequest:
    payload = {
        "schema_version": "1.0",
        "stage": stage,
        "case_id": run.case_id,
        "research_run_id": run.research_run_id,
        "contract_hash": run.contract_hash,
        "recipe_id": recipe_id,
        "recipe_version": "1.0",
        "source": source.model_dump(mode="json"),
        "execution_ids": execution_ids,
        "claim_ids": claim_ids,
        "bindings": {"data": data},
        "style_profile": "journal_bw_v1",
        "locale": "zh-CN",
        "formats": _FORMATS,
    }
    return FigureRequest(
        request_id=f"figure-request-{canonical_sha256(payload)[:24]}",
        **payload,
    )


def _claim_ids_for_executions(
    execution_ids: list[str],
    claims_by_execution: dict[str, set[str]],
    fallback: list[str],
) -> list[str]:
    if not claims_by_execution:
        return sorted(set(fallback))
    return sorted(
        {
            claim_id
            for execution_id in execution_ids
            for claim_id in claims_by_execution.get(execution_id, set())
        }
    )


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)
