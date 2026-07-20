from __future__ import annotations

from typing import Any

from .benchmark_evaluator import seal_benchmark_packet
from .benchmark_models import (
    BenchmarkPacket,
    BenchmarkResourceUsage,
    NormalizedClaim,
    NormalizedDesign,
    NormalizedExecution,
    NormalizedReproduction,
    NormalizedStatement,
)
from .models import (
    AnalysisPlan,
    ClaimLedger,
    FormalResearchContract,
    ManuscriptPackage,
    ResearchRun,
    ReproductionAudit,
)
from .manuscript_ir import render_statement
from .seal import canonical_sha256


def _all_plan_steps(plan: AnalysisPlan) -> list[Any]:
    return [
        *plan.baseline_models,
        *plan.diagnostics,
        *plan.robustness_tests,
        *plan.falsification_tests,
        *plan.mechanism_tests,
        *plan.heterogeneity_tests,
    ]


def _canonical_standard_error_strategy(
    value: Any,
    fixed_effects: list[str],
) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    if not rendered:
        return None
    normalized = rendered.casefold().replace(" ", "")
    clustered = any(marker in normalized for marker in ("cluster", "聚类"))
    entity_markers = {
        "entity",
        "firm",
        "company",
        "企业",
        "公司",
        "个体",
        "实体",
    }
    if fixed_effects:
        entity_markers.add(fixed_effects[0].casefold())
    if clustered and any(marker in normalized for marker in entity_markers):
        return "clustered_by_entity"
    return rendered


def build_hypoweaver_packet(
    *,
    packet_id: str,
    case_id: str,
    visible_input_sha256: str,
    data_sha256: list[str],
    model_id: str,
    plan: AnalysisPlan,
    research_run: ResearchRun,
    claim_ledger: ClaimLedger,
    manuscript: ManuscriptPackage,
    reproduction_audit: ReproductionAudit,
    formal_contract: FormalResearchContract | None = None,
    resource_usage: BenchmarkResourceUsage | None = None,
    component_artifact_sha256: dict[str, str] | None = None,
    ablation_id: str | None = None,
) -> BenchmarkPacket:
    baseline = plan.baseline_models[0] if plan.baseline_models else None
    plan_steps = _all_plan_steps(plan)
    execution_contract_hashes = {
        execution.provenance.contract_sha256
        for execution in research_run.executions
        if execution.provenance is not None
        and execution.provenance.contract_sha256
    }
    contract_sha256 = (
        canonical_sha256(formal_contract.model_dump(mode="json"))
        if formal_contract is not None
        else (
            next(iter(execution_contract_hashes))
            if len(execution_contract_hashes) == 1
            else None
        )
    )
    design = NormalizedDesign(
        method_family=plan.method_family,
        outcomes=[baseline.outcome] if baseline and baseline.outcome else [],
        treatments_or_exposures=(baseline.treatments_or_exposures if baseline else []),
        controls=baseline.controls if baseline else [],
        fixed_effects=baseline.fixed_effects if baseline else [],
        standard_error_strategy=(
            _canonical_standard_error_strategy(
                baseline.standard_error_strategy,
                baseline.fixed_effects,
            )
            if baseline
            else None
        ),
        planned_check_ids=[step.step_id for step in plan_steps],
        required_check_ids=[
            step.step_id for step in plan_steps if step.required_for_admission
        ],
        check_threat_ids={
            step.step_id: getattr(step, "threat_id", None)
            for step in plan_steps
            if getattr(step, "threat_id", None)
        },
        frozen_before_execution=True,
        source_artifact_sha256=canonical_sha256(plan.model_dump(mode="json")),
        contract_sha256=contract_sha256,
    )
    executions: list[NormalizedExecution] = []
    for execution in research_run.executions:
        diagnostics = execution.diagnostic_results
        executed_fixed_effects: list[str] = []
        if baseline and diagnostics.get("entity_fixed_effects") is True:
            executed_fixed_effects.extend(baseline.fixed_effects[:1])
        if baseline and diagnostics.get("time_fixed_effects") is True:
            for fixed_effect in baseline.fixed_effects[1:]:
                if fixed_effect not in executed_fixed_effects:
                    executed_fixed_effects.append(fixed_effect)
        provenance = execution.provenance
        executions.append(
            NormalizedExecution(
                execution_id=execution.execution_id,
                check_id=execution.check_id or execution.plan_step_id,
                execution_status=execution.execution_status,
                run_type=execution.run_type,
                estimates=execution.estimates,
                diagnostics=diagnostics,
                not_executed_reason_code=execution.not_executed_reason_code,
                implementation_id=(provenance.implementation_id if provenance else None),
                implementation_version=(
                    provenance.implementation_version if provenance else None
                ),
                code_sha256=(provenance.code_sha256 if provenance else None),
                environment_sha256=(
                    provenance.environment_sha256 if provenance else None
                ),
                fixed_effects=executed_fixed_effects,
                standard_error_strategy=(
                    _canonical_standard_error_strategy(
                        diagnostics["standard_errors"],
                        executed_fixed_effects or (baseline.fixed_effects if baseline else []),
                    )
                    if diagnostics.get("standard_errors") is not None
                    else None
                ),
                contract_sha256=(provenance.contract_sha256 if provenance else None),
                data_sha256=(list(provenance.data_sha256) if provenance else []),
                source_artifact_sha256=canonical_sha256(
                    execution.model_dump(mode="json")
                ),
            )
        )
    claims = [
        NormalizedClaim(
            claim_id=claim.claim_id,
            text=claim.final_text or claim.claim_text,
            # The adapter must preserve the actual tightened strength. The
            # deterministic maximum is a ceiling, never a value to promote to.
            strength=claim.allowed_strength,
            admission_status=claim.admission_status,
            check_ids=claim.required_check_ids,
            execution_ids=[
                run_id
                for run_id in [*claim.supporting_runs, *claim.opposing_runs]
                if any(item.execution_id == run_id for item in research_run.executions)
            ],
            gate_reasons=claim.gate_reasons,
        )
        for claim in claim_ledger.claims
    ]
    statements: list[NormalizedStatement] = []
    for section in manuscript.manuscript_sections:
        for statement in section.statements:
            statements.append(
                NormalizedStatement(
                    statement_id=statement.statement_id,
                    text=render_statement(statement),
                    statement_kind=statement.statement_kind,
                    section_id=section.section_id,
                    claim_ids=statement.claim_ids,
                    execution_ids=statement.execution_ids,
                    protected_values=[
                        {
                            **value.model_dump(mode="json"),
                            "source_path": (
                                _normalized_execution_pointer(
                                    research_run,
                                    value.source_id,
                                    value.source_path,
                                )
                                if value.source_kind == "execution"
                                else value.source_path
                            ),
                        }
                        for value in statement.protected_values
                    ],
                )
            )
    manuscript_text = "\n\n".join(
        section.content_markdown
        for section in manuscript.manuscript_sections
        if section.status == "generated"
    )
    packet = BenchmarkPacket(
        packet_id=packet_id,
        system_id="hypoweaver_ablation" if ablation_id else "hypoweaver",
        ablation_id=ablation_id,
        case_id=case_id,
        visible_input_sha256=visible_input_sha256,
        data_sha256=data_sha256,
        model_id=model_id,
        design=design,
        executions=executions,
        claims=claims,
        statements=statements,
        manuscript_text=manuscript_text,
        manuscript_section_texts={
            section.section_id: section.content_markdown
            for section in manuscript.manuscript_sections
            if section.status == "generated"
        },
        manuscript_sha256=canonical_sha256(manuscript.model_dump(mode="json")),
        reproduction=NormalizedReproduction(
            mode=reproduction_audit.mode,
            status=reproduction_audit.status,
            covered_check_ids=reproduction_audit.covered_plan_step_ids,
            primary_implementation_id=reproduction_audit.primary_implementation_id,
            replication_implementation_id=reproduction_audit.replication_implementation_id,
            independence_scope=getattr(
                reproduction_audit,
                "independence_scope",
                "unspecified",
            ),
            shared_components=list(
                getattr(reproduction_audit, "shared_components", [])
            ),
        ),
        resource_usage=resource_usage or BenchmarkResourceUsage(),
        native_artifact_sha256={
            "analysis_plan": canonical_sha256(plan.model_dump(mode="json")),
            "research_run": canonical_sha256(research_run.model_dump(mode="json")),
            "claim_ledger": canonical_sha256(claim_ledger.model_dump(mode="json")),
            "manuscript": canonical_sha256(manuscript.model_dump(mode="json")),
            **(
                {"formal_research_contract": contract_sha256}
                if formal_contract is not None and contract_sha256 is not None
                else {}
            ),
            **(component_artifact_sha256 or {}),
        },
    )
    return seal_benchmark_packet(packet)


def build_agent_laboratory_packet(
    *,
    packet_id: str,
    output: dict[str, Any],
    visible_input_sha256: str,
    data_sha256: list[str],
    report_text: str,
) -> BenchmarkPacket:
    plan = output.get("analysis_plan") or {}
    variables = plan.get("variables") or {}
    native_run = output.get("research_run") or {}
    parsed = native_run.get("parsed_result") or {}
    models = parsed.get("models") or {}
    native_execution_status = _agent_lab_execution_status(native_run, parsed)
    model_items = list(models.items())
    if native_execution_status in {"failed", "not_executed"} and not model_items:
        model_items = [("workflow", {})]
    executions = [
        NormalizedExecution(
            execution_id=f"agent-lab-{name}",
            check_id=str(name),
            execution_status=native_execution_status,
            run_type=_agent_lab_run_type(str(name)),
            estimates=(
                [value]
                if native_execution_status == "succeeded" and isinstance(value, dict)
                else []
            ),
            diagnostics=(parsed.get("diagnostics") or {}) if name == "baseline_H1" else {},
            not_executed_reason_code=(
                "not_executable"
                if native_execution_status == "not_executed"
                else None
            ),
        )
        for name, value in model_items
    ]
    interpretation = output.get("result_interpretation") or {}
    findings = interpretation.get("main_findings") or []
    allowed_strength = str(
        interpretation.get("allowed_claim_strength") or "not_assessed"
    )
    claims = [
        NormalizedClaim(
            claim_id=f"agent-lab-finding-{index}",
            text=str(text),
            strength=allowed_strength,
            admission_status="unassessed",
        )
        for index, text in enumerate(findings, start=1)
    ]
    usage = output.get("execution_cost") or {}
    model = output.get("model") or {}
    if isinstance(model, str):
        model_id = model
    else:
        model_id = str(model.get("name") or model.get("model") or "unknown")
    packet = BenchmarkPacket(
        packet_id=packet_id,
        system_id="agent_laboratory",
        case_id=str(output.get("case_id") or "unknown"),
        visible_input_sha256=visible_input_sha256,
        data_sha256=data_sha256,
        model_id=model_id,
        design=NormalizedDesign(
            method_family=str(plan.get("method_family") or ""),
            outcomes=[str(item) for item in variables.get("outcome", [])],
            treatments_or_exposures=[
                str(item) for item in variables.get("treatment_or_exposure", [])
            ],
            controls=[str(item) for item in variables.get("controls", [])],
            fixed_effects=[str(item) for item in plan.get("fixed_effects", [])],
            standard_error_strategy=_canonical_standard_error_strategy(
                plan.get("standard_errors"),
                [str(item) for item in plan.get("fixed_effects", [])],
            ),
            planned_check_ids=[
                *[f"diagnostic-{index}" for index, _ in enumerate(plan.get("required_diagnostics", []), 1)],
                *[f"robustness-{index}" for index, _ in enumerate(plan.get("robustness_checks", []), 1)],
                *[f"falsification-{index}" for index, _ in enumerate(plan.get("falsification_tests", []), 1)],
            ],
            required_check_ids=[],
            frozen_before_execution=bool(plan),
            source_artifact_sha256=(canonical_sha256(plan) if plan else None),
        ),
        executions=executions,
        claims=claims,
        statements=[],
        manuscript_text=report_text,
        manuscript_section_texts={"report": report_text},
        manuscript_sha256=(
            str((output.get("manuscript") or {}).get("sha256"))
            if isinstance(output.get("manuscript"), dict)
            else canonical_sha256(report_text)
        ),
        reproduction=NormalizedReproduction(),
        resource_usage=BenchmarkResourceUsage(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            wall_time_seconds=float(usage.get("wall_time_seconds", 0) or 0),
            technical_failures=[
                str(value) for value in usage.get("technical_failures", [])
            ],
        ),
        native_artifact_sha256={
            key: canonical_sha256(value)
            for key, value in output.items()
            if key in {"analysis_plan", "research_run", "result_interpretation"}
        },
    )
    return seal_benchmark_packet(packet)


def build_qwen_single_pass_packet(
    *,
    packet_id: str,
    case_id: str,
    output: dict[str, Any],
    visible_input_sha256: str,
    data_sha256: list[str],
    report_text: str,
) -> BenchmarkPacket:
    """Normalize only capabilities explicitly present in the one-call output."""

    usage = output.get("resource_usage") or output.get("execution_cost") or {}
    reported_calls = int(usage.get("llm_calls", 1) or 1)
    if reported_calls != 1:
        raise ValueError("qwen_single_pass packet must represent exactly one model call")
    raw_design = output.get("design") or output.get("analysis_plan") or {}
    raw_executions = output.get("executions") or []
    raw_claims = output.get("claims") or output.get("main_findings") or []
    raw_statements = output.get("statements") or []

    executions = [
        NormalizedExecution.model_validate(item)
        for item in raw_executions
        if isinstance(item, dict)
        and item.get("execution_id")
        and item.get("check_id")
        and item.get("execution_status")
        and item.get("run_type")
    ]
    claims: list[NormalizedClaim] = []
    for index, item in enumerate(raw_claims, start=1):
        if isinstance(item, str):
            claims.append(
                NormalizedClaim(
                    claim_id=f"qwen-finding-{index}",
                    text=item,
                    strength="not_assessed",
                    admission_status="unassessed",
                )
            )
        elif isinstance(item, dict) and item.get("text"):
            claims.append(
                NormalizedClaim(
                    claim_id=str(item.get("claim_id") or f"qwen-finding-{index}"),
                    text=str(item["text"]),
                    strength=str(item.get("strength") or "not_assessed"),
                    admission_status=str(
                        item.get("admission_status") or "unassessed"
                    ),
                    check_ids=[str(value) for value in item.get("check_ids", [])],
                    execution_ids=[
                        str(value) for value in item.get("execution_ids", [])
                    ],
                    gate_reasons=[
                        str(value) for value in item.get("gate_reasons", [])
                    ],
                )
            )
    statements = [
        NormalizedStatement.model_validate(item)
        for item in raw_statements
        if isinstance(item, dict)
        and item.get("statement_id")
        and item.get("text")
        and item.get("statement_kind")
    ]

    packet = BenchmarkPacket(
        packet_id=packet_id,
        system_id="qwen_single_pass",
        case_id=case_id,
        visible_input_sha256=visible_input_sha256,
        data_sha256=data_sha256,
        model_id=str(output.get("model_id") or output.get("model") or "unknown"),
        design=NormalizedDesign(
            method_family=_optional_string(raw_design.get("method_family")),
            outcomes=[str(value) for value in raw_design.get("outcomes", [])],
            treatments_or_exposures=[
                str(value)
                for value in raw_design.get("treatments_or_exposures", [])
            ],
            controls=[str(value) for value in raw_design.get("controls", [])],
            fixed_effects=[
                str(value) for value in raw_design.get("fixed_effects", [])
            ],
            standard_error_strategy=_canonical_standard_error_strategy(
                raw_design.get("standard_error_strategy"),
                [str(value) for value in raw_design.get("fixed_effects", [])],
            ),
            planned_check_ids=[
                str(value) for value in raw_design.get("planned_check_ids", [])
            ],
            required_check_ids=[
                str(value) for value in raw_design.get("required_check_ids", [])
            ],
            frozen_before_execution=bool(
                raw_design.get("frozen_before_execution", False)
            ),
            source_artifact_sha256=_optional_string(
                raw_design.get("source_artifact_sha256")
            ),
        ),
        executions=executions,
        claims=claims,
        statements=statements,
        manuscript_text=report_text,
        manuscript_section_texts={"report": report_text},
        manuscript_sha256=canonical_sha256(report_text),
        reproduction=NormalizedReproduction(),
        resource_usage=BenchmarkResourceUsage(
            llm_calls=1,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            wall_time_seconds=float(usage.get("wall_time_seconds", 0) or 0),
        ),
        native_artifact_sha256={
            "single_pass_output": canonical_sha256(output),
        },
    )
    return seal_benchmark_packet(packet)


def _agent_lab_run_type(name: str) -> str:
    lowered = name.lower()
    if "robust" in lowered:
        return "robustness"
    if "dynamic" in lowered or "lead" in lowered or "placebo" in lowered:
        return "falsification"
    if "mechanism" in lowered:
        return "mechanism"
    return "baseline"


def _agent_lab_execution_status(
    native_run: dict[str, Any],
    parsed: dict[str, Any],
) -> str:
    statuses = [
        str(value).strip().casefold()
        for value in (
            native_run.get("execution_status"),
            parsed.get("execution_status"),
        )
        if value is not None and str(value).strip()
    ]
    success = {"success", "succeeded"}
    not_executed = {"not_executed", "cancelled", "canceled"}
    if any(status not in success | not_executed for status in statuses):
        return "failed"
    if any(status in not_executed for status in statuses):
        return "not_executed"
    if statuses and all(status in success for status in statuses):
        return "succeeded"
    return "not_available"


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _normalized_execution_pointer(
    run: ResearchRun,
    execution_id: str,
    source_path: str,
) -> str:
    for index, execution in enumerate(run.executions):
        prefix = f"/executions/{index}"
        if execution.execution_id == execution_id and source_path.startswith(prefix + "/"):
            normalized = source_path.removeprefix(prefix)
            if normalized.startswith("/diagnostic_results/"):
                return normalized.replace(
                    "/diagnostic_results/",
                    "/diagnostics/",
                    1,
                )
            return normalized
    return source_path
