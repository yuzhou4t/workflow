from __future__ import annotations

import asyncio
import math
import re
from numbers import Real
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .figure_data import derive_dataset_figure_inputs
from .models import (
    ClaimLedger,
    FormalResearchContract,
    ResearchRun,
    StrictModel,
)
from .plot_agent.recipe_contracts import (
    RECIPE_IDS,
    RecipeId,
    recipe_data_snapshot,
    validate_recipe_data,
)
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
    recipe_id: RecipeId
    recipe_version: Literal["1.0"] = "1.0"
    source: FigureSource
    data_sources: list[FigureSource] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
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
        if self.stage == "publication" and not self.execution_ids:
            raise ValueError("publication figures require execution_ids")
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("formats must be unique")
        source_keys = [
            (item.artifact_id, item.artifact_key, item.sha256)
            for item in self.data_sources
        ]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("data_sources must be unique")
        self.bindings.data = validate_recipe_data(
            self.recipe_id,
            self.bindings.data,
        )
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
    recipe_id: RecipeId
    recipe_version: Literal["1.0"]
    title: str = Field(min_length=1)
    caption: str = Field(min_length=1)
    alt_text: str = Field(min_length=1)
    execution_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    sources: list[FigureSource] = Field(default_factory=list)
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
        if self.stage == "publication" and any(
            not figure.execution_ids for figure in self.figures
        ):
            raise ValueError("publication figures require execution_ids")
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
    expected_sources = [request.source, *request.data_sources]
    if figure.sources != expected_sources:
        raise RuntimeError("Figure sources do not match request")
    returned_formats = {item.format for item in figure.files}
    if not set(request.formats).issubset(returned_formats):
        raise RuntimeError("Figure response is missing requested formats")
    expected_snapshot = recipe_data_snapshot(
        request.recipe_id,
        request.bindings.data,
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
    contract: FormalResearchContract | None = None,
    dataset_path: Path | None = None,
    dataset_source: FigureSource | None = None,
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

    requests: list[FigureRequest] = []
    coefficient_rows: list[dict[str, Any]] = []
    coefficient_execution_ids: set[str] = set()
    regular_ids = {
        execution_id
        for execution_id in allowed_execution_ids
        if succeeded[execution_id].run_type
        in {"baseline", "robustness", "replication"}
    }
    excluded_publication_estimate = False
    for execution_id in sorted(regular_ids):
        execution = succeeded[execution_id]
        for estimate in execution.estimates:
            parsed = _estimate_point(estimate)
            if parsed is None:
                warnings.append(
                    f"Execution {execution_id} 的一条估计缺少 term/coefficient/有效 95% CI。"
                )
                continue
            term, coefficient, ci_lower, ci_upper = parsed
            if stage == "publication" and term not in (
                allowed_estimate_terms or set()
            ):
                excluded_publication_estimate = True
                continue
            display_term = term
            if any(
                row["execution_id"] == execution_id
                and row["term"] == display_term
                for row in coefficient_rows
            ):
                qualifier = str(
                    estimate.get("effect_type")
                    or estimate.get("estimate_type")
                    or "alternate"
                ).strip()
                display_term = f"{term} · {qualifier}"
                suffix = 2
                while any(
                    row["execution_id"] == execution_id
                    and row["term"] == display_term
                    for row in coefficient_rows
                ):
                    display_term = f"{term} · {qualifier}-{suffix}"
                    suffix += 1
            row: dict[str, Any] = {
                "term": display_term,
                "coefficient": coefficient,
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                "execution_id": execution_id,
            }
            p_value = _finite_float(estimate.get("p_value"))
            sample_size = _finite_int(estimate.get("nobs"))
            if p_value is not None and 0 <= p_value <= 1:
                row["p_value"] = p_value
            if sample_size is not None and sample_size > 0:
                row["sample_size"] = sample_size
            coefficient_rows.append(row)
            coefficient_execution_ids.add(execution_id)
    if excluded_publication_estimate:
        warnings.append("论文图已排除未进入 Writer 授权范围的估计项。")

    if coefficient_rows:
        coefficient_rows.sort(key=lambda item: (item["execution_id"], item["term"]))
        execution_ids = sorted(coefficient_execution_ids)
        request_claim_ids = _claim_ids_for_executions(
            execution_ids,
            claims_by_execution,
            claim_ids,
        )
        try:
            request = _figure_request(
                run,
                source,
                stage,
                recipe_id="coefficient_forest",
                execution_ids=execution_ids,
                claim_ids=request_claim_ids,
                data=coefficient_rows,
            )
        except (TypeError, ValueError) as error:
            warnings.append(f"系数森林图输入被拒绝：{error}")
        else:
            requests.append(request)
    else:
        warnings.append("没有具备 95% 置信区间的成功估计，未生成系数图。")

    sample_candidate = _sample_flow_candidate(succeeded, allowed_execution_ids, warnings)
    if sample_candidate is not None:
        execution_id, data = sample_candidate
        try:
            request = _figure_request(
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
        except (TypeError, ValueError) as error:
            warnings.append(f"样本流程图输入被拒绝：{error}")
        else:
            requests.append(request)
    else:
        warnings.append("没有闭合的 rows_input/rows_used/rows_dropped，未生成样本流程图。")

    event_requests, event_warnings = _event_study_requests(
        run,
        source,
        stage,
        succeeded,
        allowed_execution_ids,
        claims_by_execution,
        claim_ids,
        allowed_estimate_terms or set(),
    )
    requests.extend(event_requests)
    warnings.extend(event_warnings)

    heterogeneity_requests, heterogeneity_warnings = _heterogeneity_requests(
        run,
        source,
        stage,
        succeeded,
        allowed_execution_ids,
        claims_by_execution,
        claim_ids,
        allowed_estimate_terms or set(),
    )
    requests.extend(heterogeneity_requests)
    warnings.extend(heterogeneity_warnings)

    specification_requests, specification_warnings = _specification_requests(
        run,
        source,
        stage,
        succeeded,
        allowed_execution_ids,
        claims_by_execution,
        claim_ids,
        allowed_estimate_terms or set(),
        contract,
    )
    requests.extend(specification_requests)
    warnings.extend(specification_warnings)

    if (
        stage == "evidence"
        and contract is not None
        and dataset_path is not None
        and dataset_source is not None
    ):
        try:
            derived_inputs, derived_warnings = derive_dataset_figure_inputs(
                contract,
                dataset_path,
            )
        except (OSError, ValueError) as error:
            warnings.append(f"描述类科研图派生失败：{error}")
        else:
            for item in derived_inputs:
                try:
                    request = _figure_request(
                        run,
                        source,
                        stage,
                        recipe_id=item.recipe_id,
                        execution_ids=[],
                        claim_ids=[],
                        data=item.data,
                        data_sources=[dataset_source],
                    )
                except (TypeError, ValueError) as error:
                    warnings.append(
                        f"{item.recipe_id} 的确定性聚合输入被拒绝：{error}"
                    )
                    continue
                requests.append(request)
            warnings.extend(derived_warnings)

    explicit_requests, explicit_warnings = _explicit_figure_input_requests(
        run,
        source,
        stage,
        succeeded,
        allowed_execution_ids,
        contract,
    )
    requests.extend(explicit_requests)
    warnings.extend(explicit_warnings)

    return requests, list(dict.fromkeys(warnings))


def _sample_flow_candidate(
    succeeded: dict[str, Any],
    allowed_execution_ids: set[str],
    warnings: list[str],
) -> tuple[str, dict[str, int]] | None:
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
    return sample_candidate


def _event_study_requests(
    run: ResearchRun,
    source: FigureSource,
    stage: FigureStage,
    succeeded: dict[str, Any],
    allowed_execution_ids: set[str],
    claims_by_execution: dict[str, set[str]],
    fallback_claim_ids: list[str],
    allowed_terms: set[str],
) -> tuple[list[FigureRequest], list[str]]:
    requests: list[FigureRequest] = []
    warnings: list[str] = []
    for execution_id in sorted(allowed_execution_ids):
        execution = succeeded[execution_id]
        if execution.run_type != "falsification":
            continue
        points: list[dict[str, Any]] = []
        policy_start_candidates: set[float] = set()
        seen_periods: set[float] = set()
        event_like = False
        for estimate in execution.estimates:
            if estimate.get("event_bin") == "remote_pre":
                event_like = True
                warnings.append(
                    f"Execution {execution_id} 的 remote-pre 聚合区间保留在 CSV 来源中，"
                    "但未伪装成单一事件期坐标。"
                )
                continue
            relative_time = _finite_float(estimate.get("relative_year"))
            event_year = _finite_int(estimate.get("event_year"))
            parsed = _estimate_point(estimate)
            if relative_time is None and event_year is None:
                continue
            event_like = True
            if parsed is None or relative_time is None:
                warnings.append(
                    f"Execution {execution_id} 的一个事件研究点缺少有效相对期、系数或 95% CI。"
                )
                continue
            term, coefficient, ci_lower, ci_upper = parsed
            if stage == "publication" and term not in allowed_terms:
                continue
            if relative_time in seen_periods:
                warnings.append(
                    f"Execution {execution_id} 的事件期 {relative_time:g} 重复，未生成事件研究图。"
                )
                points = []
                break
            seen_periods.add(relative_time)
            point: dict[str, Any] = {
                "relative_time": relative_time,
                "coefficient": coefficient,
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                "execution_id": execution_id,
            }
            if event_year is not None:
                point["event_year"] = event_year
                policy_start_candidates.add(event_year - relative_time)
            points.append(point)
        if not event_like or not points:
            continue
        points.sort(key=lambda item: item["relative_time"])
        data: dict[str, Any] = {"points": points}
        reference_year = _finite_int(
            execution.diagnostic_results.get("reference_year")
        )
        if reference_year is not None and len(policy_start_candidates) == 1:
            policy_start = next(iter(policy_start_candidates))
            data["reference_period"] = reference_year - policy_start
        joint_p = _finite_float(
            execution.diagnostic_results.get("joint_pretrend_p_value")
        )
        if joint_p is not None and 0 <= joint_p <= 1:
            data["joint_pretrend_p_value"] = joint_p
        try:
            request = _figure_request(
                run,
                source,
                stage,
                recipe_id="event_study",
                execution_ids=[execution_id],
                claim_ids=_claim_ids_for_executions(
                    [execution_id],
                    claims_by_execution,
                    fallback_claim_ids,
                ),
                data=data,
            )
        except (TypeError, ValueError) as error:
            warnings.append(
                f"Execution {execution_id} 的事件研究图输入被拒绝：{error}"
            )
            continue
        requests.append(request)
    return requests, warnings


def _heterogeneity_requests(
    run: ResearchRun,
    source: FigureSource,
    stage: FigureStage,
    succeeded: dict[str, Any],
    allowed_execution_ids: set[str],
    claims_by_execution: dict[str, set[str]],
    fallback_claim_ids: list[str],
    allowed_terms: set[str],
) -> tuple[list[FigureRequest], list[str]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for execution_id in sorted(allowed_execution_ids):
        execution = succeeded[execution_id]
        if execution.run_type != "heterogeneity":
            continue
        subgroup_variable = str(
            execution.diagnostic_results.get("subgroup_variable") or ""
        ).strip()
        subgroup_value = execution.diagnostic_results.get("subgroup_value")
        if not subgroup_variable or subgroup_value is None:
            warnings.append(
                f"Execution {execution_id} 缺少冻结 subgroup_variable/subgroup_value。"
            )
            continue
        for estimate in execution.estimates:
            parsed = _estimate_point(estimate)
            if parsed is None:
                continue
            term, coefficient, ci_lower, ci_upper = parsed
            if stage == "publication" and term not in allowed_terms:
                continue
            row: dict[str, Any] = {
                "subgroup": f"{subgroup_variable}={subgroup_value}",
                "subgroup_variable": subgroup_variable,
                "term": term,
                "coefficient": coefficient,
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                "execution_id": execution_id,
            }
            sample_size = _finite_int(estimate.get("nobs"))
            if sample_size is not None and sample_size > 0:
                row["sample_size"] = sample_size
            grouped.setdefault((subgroup_variable, term), []).append(row)

    requests: list[FigureRequest] = []
    for (_, _), rows in sorted(grouped.items()):
        labels = {row["subgroup"] for row in rows}
        if len(rows) < 2 or len(labels) != len(rows):
            warnings.append(
                "异质性森林图至少需要同一变量、同一估计项的两个不同冻结子组。"
            )
            continue
        rows.sort(key=lambda item: (item["subgroup"], item["execution_id"]))
        execution_ids = [row["execution_id"] for row in rows]
        try:
            request = _figure_request(
                run,
                source,
                stage,
                recipe_id="heterogeneity_forest",
                execution_ids=execution_ids,
                claim_ids=_claim_ids_for_executions(
                    execution_ids,
                    claims_by_execution,
                    fallback_claim_ids,
                ),
                data=rows,
            )
        except (TypeError, ValueError) as error:
            warnings.append(f"异质性森林图输入被拒绝：{error}")
            continue
        requests.append(request)
    return requests, warnings


def _specification_requests(
    run: ResearchRun,
    source: FigureSource,
    stage: FigureStage,
    succeeded: dict[str, Any],
    allowed_execution_ids: set[str],
    claims_by_execution: dict[str, set[str]],
    fallback_claim_ids: list[str],
    allowed_terms: set[str],
    contract: FormalResearchContract | None,
) -> tuple[list[FigureRequest], list[str]]:
    labels: dict[str, str] = {}
    excluded_steps: set[str] = set()
    if contract is not None:
        plan = contract.approved_plan
        baseline_outcome = (
            str(plan.baseline_models[0].outcome or "")
            if plan.baseline_models
            else ""
        )
        for model in plan.baseline_models:
            labels[model.step_id] = model.name or model.step_id
        for step in plan.robustness_tests:
            labels[step.step_id] = step.name or step.step_id
            alternative_outcome = str(
                step.parameters.get("alternative_outcome") or ""
            )
            if alternative_outcome and alternative_outcome != baseline_outcome:
                excluded_steps.add(step.step_id)

    grouped: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for execution_id in sorted(allowed_execution_ids):
        execution = succeeded[execution_id]
        if execution.run_type not in {"baseline", "robustness"}:
            continue
        if execution.plan_step_id in excluded_steps:
            warnings.append(
                f"规格 {execution.plan_step_id} 更换了结果变量量纲，未进入同尺度规格曲线。"
            )
            continue
        for estimate in execution.estimates:
            parsed = _estimate_point(estimate)
            if parsed is None:
                continue
            term, coefficient, ci_lower, ci_upper = parsed
            if stage == "publication" and term not in allowed_terms:
                continue
            effect_type = str(estimate.get("effect_type") or "").strip()
            curve_term = f"{term} · {effect_type}" if effect_type else term
            grouped.setdefault(curve_term, []).append(
                {
                    "specification": labels.get(
                        execution.plan_step_id,
                        execution.plan_step_id,
                    ),
                    "run_type": execution.run_type,
                    "coefficient": coefficient,
                    "ci_lower": ci_lower,
                    "ci_upper": ci_upper,
                    "execution_id": execution_id,
                }
            )

    requests: list[FigureRequest] = []
    for term, points in sorted(grouped.items()):
        run_types = {point["run_type"] for point in points}
        if not {"baseline", "robustness"}.issubset(run_types):
            continue
        points.sort(
            key=lambda item: (
                item["run_type"] != "baseline",
                item["specification"],
                item["execution_id"],
            )
        )
        duplicate_labels = {
            point["specification"]
            for point in points
            if sum(
                other["specification"] == point["specification"]
                for other in points
            )
            > 1
        }
        for point in points:
            if point["specification"] in duplicate_labels:
                point["specification"] += f" · {point['execution_id']}"
        execution_ids = [point["execution_id"] for point in points]
        try:
            request = _figure_request(
                run,
                source,
                stage,
                recipe_id="specification_curve",
                execution_ids=execution_ids,
                claim_ids=_claim_ids_for_executions(
                    execution_ids,
                    claims_by_execution,
                    fallback_claim_ids,
                ),
                data={"term": term, "points": points},
            )
        except (TypeError, ValueError) as error:
            warnings.append(f"规格曲线 {term} 的输入被拒绝：{error}")
            continue
        requests.append(request)
    return requests, warnings


def _explicit_figure_input_requests(
    run: ResearchRun,
    source: FigureSource,
    stage: FigureStage,
    succeeded: dict[str, Any],
    allowed_execution_ids: set[str],
    contract: FormalResearchContract | None,
) -> tuple[list[FigureRequest], list[str]]:
    explicit_recipes = {
        "spatial_choropleth",
        "mechanism_evidence_graph",
    }
    requests: list[FigureRequest] = []
    warnings: list[str] = []
    for execution_id in sorted(allowed_execution_ids):
        payloads = succeeded[execution_id].diagnostic_results.get(
            "figure_inputs"
        )
        if payloads is None:
            continue
        if not isinstance(payloads, list):
            warnings.append(
                f"Execution {execution_id} 的 figure_inputs 不是列表，已拒绝。"
            )
            continue
        if stage != "evidence":
            warnings.append(
                "Execution 自带的条件图形输入仅进入 H3 前证据图；"
                "论文图需要单独的 H3 字段授权。"
            )
            continue
        for index, payload in enumerate(payloads):
            if not isinstance(payload, dict):
                warnings.append(
                    f"Execution {execution_id} 的 figure_inputs[{index}] 不是对象。"
                )
                continue
            recipe_id = payload.get("recipe_id")
            if not isinstance(recipe_id, str) or recipe_id not in explicit_recipes:
                warnings.append(
                    f"Execution {execution_id} 的条件 Recipe {recipe_id!r} 不受支持。"
                )
                continue
            try:
                data = validate_recipe_data(recipe_id, payload.get("data"))
                request_execution_ids = [execution_id]
                request_data_sources: list[FigureSource] = []
                if recipe_id == "mechanism_evidence_graph":
                    assert isinstance(data, dict)
                    mechanism_step = _frozen_plan_step(
                        contract,
                        succeeded[execution_id].plan_step_id,
                    )
                    if (
                        succeeded[execution_id].run_type != "mechanism"
                        or mechanism_step is None
                    ):
                        raise ValueError(
                            "mechanism figure_inputs require a frozen mechanism step"
                        )
                    expected_graph = mechanism_step.parameters.get(
                        "mechanism_graph"
                    )
                    if expected_graph is None:
                        raise ValueError(
                            "frozen mechanism step has no mechanism_graph"
                        )
                    normalized_expected_graph = validate_recipe_data(
                        "mechanism_evidence_graph",
                        expected_graph,
                    )
                    if canonical_sha256(normalized_expected_graph) != canonical_sha256(
                        data
                    ):
                        raise ValueError(
                            "mechanism figure_input differs from the frozen "
                            "mechanism_graph"
                        )
                elif recipe_id == "spatial_choropleth":
                    assert isinstance(data, dict)
                    if (
                        contract is None
                        or contract.approved_plan.method_family != "spatial"
                    ):
                        raise ValueError(
                            "spatial choropleth requires a frozen spatial contract"
                        )
                    spatial_step = _frozen_plan_step(
                        contract,
                        succeeded[execution_id].plan_step_id,
                    )
                    expected_map = (
                        spatial_step.parameters.get("spatial_choropleth")
                        if spatial_step is not None
                        else None
                    )
                    if expected_map is None:
                        raise ValueError(
                            "frozen spatial step has no spatial_choropleth"
                        )
                    normalized_expected_map = validate_recipe_data(
                        "spatial_choropleth",
                        expected_map,
                    )
                    if canonical_sha256(normalized_expected_map) != canonical_sha256(
                        data
                    ):
                        raise ValueError(
                            "spatial figure_input differs from the frozen "
                            "spatial_choropleth"
                        )
                    refs_by_sha = {
                        item.sha256: item for item in contract.dataset_refs
                    }
                    source_hashes = {
                        data["geometry_source_sha256"],
                        data["value_source_sha256"],
                    }
                    missing_hashes = source_hashes - set(refs_by_sha)
                    if missing_hashes:
                        raise ValueError(
                            "spatial sources are not registered in the frozen contract: "
                            + ", ".join(sorted(missing_hashes))
                        )
                    request_data_sources = [
                        FigureSource(
                            artifact_id=f"dataset:{refs_by_sha[sha256].dataset_id}",
                            artifact_key=refs_by_sha[sha256].filename,
                            sha256=sha256,
                        )
                        for sha256 in sorted(source_hashes)
                    ]
                request = _figure_request(
                    run,
                    source,
                    stage,
                    recipe_id=recipe_id,
                    execution_ids=request_execution_ids,
                    claim_ids=[],
                    data=data,
                    data_sources=request_data_sources,
                )
            except (TypeError, ValueError) as error:
                warnings.append(
                    f"Execution {execution_id} 的 {recipe_id} 输入被拒绝：{error}"
                )
                continue
            requests.append(request)
    return requests, warnings


def _frozen_plan_step(
    contract: FormalResearchContract | None,
    step_id: str,
) -> Any | None:
    if contract is None:
        return None
    plan = contract.approved_plan
    collections = (
        plan.estimands,
        plan.sample_rules,
        plan.variable_construction,
        plan.baseline_models,
        plan.diagnostics,
        plan.robustness_tests,
        plan.falsification_tests,
        plan.mechanism_tests,
        plan.heterogeneity_tests,
    )
    return next(
        (
            step
            for collection in collections
            for step in collection
            if step.step_id == step_id
        ),
        None,
    )


def _estimate_point(
    estimate: dict[str, Any],
) -> tuple[str, float, float, float] | None:
    term = estimate.get("term")
    coefficient = _finite_float(estimate.get("coefficient"))
    interval = estimate.get("confidence_interval_95")
    if (
        not isinstance(term, str)
        or not term.strip()
        or coefficient is None
        or not isinstance(interval, list)
        or len(interval) != 2
    ):
        return None
    ci_lower = _finite_float(interval[0])
    ci_upper = _finite_float(interval[1])
    if (
        ci_lower is None
        or ci_upper is None
        or ci_lower > coefficient
        or coefficient > ci_upper
    ):
        return None
    return term.strip(), coefficient, ci_lower, ci_upper


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
        renderer={"name": "hypoweaver-plot-orchestrator", "version": "1.2"},
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
        renderer={"name": "hypoweaver-plot-orchestrator", "version": "1.2"},
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
    recipe_id: RecipeId,
    execution_ids: list[str],
    claim_ids: list[str],
    data: list[dict[str, Any]] | dict[str, Any],
    data_sources: list[FigureSource] | None = None,
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
        "data_sources": [
            item.model_dump(mode="json") for item in (data_sources or [])
        ],
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
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)
