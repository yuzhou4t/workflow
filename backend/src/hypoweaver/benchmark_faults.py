from __future__ import annotations

from typing import Any

from .benchmark_evaluator import (
    protected_numeric_consistency,
    seal_benchmark_packet,
    verify_benchmark_packet,
)
from .benchmark_models import (
    ABLATION_IDS,
    ABLATION_NATIVE_ARTIFACTS,
    FAULT_IDS,
    AblationId,
    AblationReplayResult,
    BenchmarkPacket,
    FaultId,
    FaultOutcome,
    FaultReplayReport,
    NormalizedReproduction,
)
from .claim_gate import causal_wording_violations
from .models import (
    AnalysisPlan,
    ExecutionProvenance,
    ExecutionRecord,
    ModelSpec,
    PlannedStep,
    ResearchRun,
)
from .reproducer import compare_panel_reproduction
from .seal import canonical_sha256
from .test_dag import finalize_test_dag_executions


_FAULT_COMPONENT: dict[FaultId, tuple[str, str]] = {
    "duplicate_merge_inflation": ("probe", "block"),
    "time_leakage_or_lead_misuse": ("h2", "downgrade"),
    "unit_amplification": ("independent_replication", "block"),
    "variable_timing_shift": ("h2", "block"),
    "wrong_clustering": ("independent_replication", "block"),
    "significant_subgroup_cherry_pick": ("reviewer", "disclose"),
    "table_text_mismatch": ("manuscript_ir", "block"),
    "association_to_causation": ("claim_gate", "block"),
    "deleted_null_or_failure_branch": ("h2", "block"),
}

_ABLATION_TARGETS: dict[AblationId, tuple[str, list[FaultId]]] = {
    "without_reviewer": ("reviewer", ["significant_subgroup_cherry_pick"]),
    "without_probe": ("probe", ["duplicate_merge_inflation"]),
    "without_h2": (
        "h2",
        [
            "time_leakage_or_lead_misuse",
            "variable_timing_shift",
            "deleted_null_or_failure_branch",
        ],
    ),
    "without_independent_replication": (
        "independent_replication",
        ["unit_amplification", "wrong_clustering"],
    ),
    "without_claim_gate": ("claim_gate", ["association_to_causation"]),
    "without_manuscript_ir": ("manuscript_ir", ["table_text_mismatch"]),
}


def inject_fault(clean_packet: BenchmarkPacket, fault_id: FaultId) -> BenchmarkPacket:
    """Return one re-sealed mutation of the same frozen benchmark packet."""

    verify_benchmark_packet(clean_packet)
    payload = clean_packet.model_dump(mode="json")
    payload["packet_id"] = f"{clean_packet.packet_id}-fault-{fault_id}"
    payload["packet_sha256"] = None
    payload["sealed_at"] = clean_packet.sealed_at

    if fault_id == "duplicate_merge_inflation":
        execution = _first_execution(payload)
        diagnostics = execution.setdefault("diagnostics", {})
        diagnostics["duplicate_primary_key_count"] = 3
        diagnostics["rows_before_merge"] = 100
        diagnostics["rows_after_merge"] = 103
        diagnostics["n_obs"] = int(diagnostics.get("n_obs", 100) or 100) + 3
    elif fault_id == "time_leakage_or_lead_misuse":
        diagnostics = _first_execution(payload).setdefault("diagnostics", {})
        diagnostics["lead_offset"] = 1
        diagnostics["expected_lead_offset"] = -1
    elif fault_id == "unit_amplification":
        estimate = _first_estimate(payload)
        changed = False
        for key in ("coefficient", "standard_error", "ci_lower", "ci_upper"):
            if isinstance(estimate.get(key), (int, float)):
                estimate[key] = float(estimate[key]) * 1000
                changed = True
        if not changed:
            raise ValueError("unit amplification requires a numeric estimate")
    elif fault_id == "variable_timing_shift":
        diagnostics = _first_execution(payload).setdefault("diagnostics", {})
        diagnostics["outcome_time_shift"] = 1
        diagnostics["contract_outcome_time_shift"] = 0
    elif fault_id == "wrong_clustering":
        payload["design"]["standard_error_strategy"] = "unclustered"
        diagnostics = _first_execution(payload).setdefault("diagnostics", {})
        diagnostics["cluster_strategy"] = "none"
    elif fault_id == "significant_subgroup_cherry_pick":
        execution = _first_execution(payload)
        diagnostics = execution.setdefault("diagnostics", {})
        diagnostics["subgroup_selection"] = "post_hoc_significance"
        diagnostics["subgroup_preregistered"] = False
        payload["claims"].append(
            {
                "claim_id": "fault-post-hoc-subgroup",
                "text": "仅显著子组呈现更强的条件关联。",
                "strength": "associational",
                "admission_status": "admitted",
                "check_ids": [execution["check_id"]],
                "execution_ids": [execution["execution_id"]],
                "gate_reasons": [],
            }
        )
    elif fault_id == "table_text_mismatch":
        protected = _first_protected_value(payload)
        rendered = str(protected.get("rendered_value") or "")
        if not rendered or rendered not in str(payload.get("manuscript_text") or ""):
            raise ValueError("table-text mismatch requires rendered manuscript text")
        payload["manuscript_text"] = str(payload["manuscript_text"]).replace(
            rendered,
            "999.0000",
            1,
        )
    elif fault_id == "association_to_causation":
        if not payload["claims"]:
            raise ValueError("association-to-causation requires a claim")
        payload["claims"][0]["text"] = "暴露变量导致结果变量发生改变。"
        payload["claims"][0]["strength"] = "associational"
    elif fault_id == "deleted_null_or_failure_branch":
        required = set(payload["design"].get("required_check_ids") or [])
        if not required:
            raise ValueError("branch deletion requires a frozen required check")
        removed = next(
            (
                item
                for item in payload["executions"]
                if item.get("check_id") in required
            ),
            None,
        )
        if removed is None:
            raise ValueError("branch deletion requires a matching execution")
        payload["executions"].remove(removed)
    else:  # pragma: no cover - Literal validation protects public callers
        raise ValueError(f"unknown fault: {fault_id}")

    return seal_benchmark_packet(BenchmarkPacket.model_validate(payload))


def replay_faults(
    clean_packet: BenchmarkPacket,
) -> list[FaultOutcome]:
    return [
        detect_injected_fault(
            clean_packet,
            inject_fault(clean_packet, fault_id),
            fault_id,
        )
        for fault_id in FAULT_IDS
    ]


def detect_injected_fault(
    clean_packet: BenchmarkPacket,
    injected_packet: BenchmarkPacket,
    fault_id: FaultId,
) -> FaultOutcome:
    verify_benchmark_packet(clean_packet)
    verify_benchmark_packet(injected_packet)
    if (
        clean_packet.visible_input_sha256 != injected_packet.visible_input_sha256
        or clean_packet.data_sha256 != injected_packet.data_sha256
    ):
        raise ValueError("fault replay changed the frozen visible input")

    present, evidence = _fault_invariant(clean_packet, injected_packet, fault_id)
    _component, action = _FAULT_COMPONENT[fault_id]
    detected = present
    return FaultOutcome(
        fault_id=fault_id,
        detected=detected,
        action=action if detected else "missed",
        evidence=evidence if detected else [],
    )


def derive_ablation_packet(
    source_packet: BenchmarkPacket,
    ablation_id: AblationId,
) -> BenchmarkPacket:
    """Remove one real safeguard product from the same frozen packet."""

    verify_benchmark_packet(source_packet)
    payload = source_packet.model_dump(mode="json")
    native_artifacts = dict(payload.get("native_artifact_sha256") or {})
    artifact_key = ABLATION_NATIVE_ARTIFACTS[ablation_id]
    if artifact_key not in native_artifacts:
        raise ValueError(
            f"ablation source is missing native artifact: {artifact_key}"
        )
    native_artifacts.pop(artifact_key)

    payload.update(
        {
            "packet_id": f"{source_packet.packet_id}-{ablation_id}",
            "system_id": "hypoweaver_ablation",
            "ablation_id": ablation_id,
            "resource_usage": {
                "llm_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "wall_time_seconds": 0,
                "technical_failures": [],
            },
            "official_receipts": [],
            "native_artifact_sha256": native_artifacts,
            "packet_sha256": None,
            "sealed_at": source_packet.sealed_at,
        }
    )

    if ablation_id == "without_reviewer":
        _strip_execution_diagnostics(
            payload,
            {"subgroup_selection", "subgroup_preregistered"},
        )
    elif ablation_id == "without_probe":
        _strip_execution_diagnostics(
            payload,
            {
                "duplicate_primary_key_count",
                "rows_before_merge",
                "rows_after_merge",
            },
        )
    elif ablation_id == "without_h2":
        design = payload["design"]
        design["required_check_ids"] = []
        design["check_threat_ids"] = {}
        design["frozen_before_execution"] = False
        design["contract_sha256"] = None
        for execution in payload.get("executions") or []:
            execution["contract_sha256"] = None
        _strip_execution_diagnostics(
            payload,
            {"expected_lead_offset", "contract_outcome_time_shift"},
        )
    elif ablation_id == "without_independent_replication":
        payload["reproduction"] = NormalizedReproduction().model_dump(mode="json")
    elif ablation_id == "without_claim_gate":
        for claim in payload.get("claims") or []:
            claim["admission_status"] = "unassessed"
            claim["gate_reasons"] = []
    elif ablation_id == "without_manuscript_ir":
        payload["statements"] = []
        payload["manuscript_sha256"] = None

    return seal_benchmark_packet(BenchmarkPacket.model_validate(payload))


def replay_ablations(clean_packet: BenchmarkPacket) -> FaultReplayReport:
    verify_benchmark_packet(clean_packet)
    missing_artifacts = sorted(
        set(ABLATION_NATIVE_ARTIFACTS.values())
        - set(clean_packet.native_artifact_sha256)
    )
    if missing_artifacts:
        raise ValueError(
            "full ablation source is missing native artifacts: "
            + ", ".join(missing_artifacts)
        )
    full_outcomes = replay_faults(clean_packet)
    full_by_id = {item.fault_id: item for item in full_outcomes}
    ablations: list[AblationReplayResult] = []
    for ablation_id in ABLATION_IDS:
        disabled_component, targets = _ABLATION_TARGETS[ablation_id]
        ablation_packet = derive_ablation_packet(clean_packet, ablation_id)
        outcomes = [
            detect_injected_fault(
                ablation_packet,
                derive_ablation_packet(
                    inject_fault(clean_packet, fault_id),
                    ablation_id,
                ),
                fault_id,
            )
            for fault_id in FAULT_IDS
        ]
        outcome_by_id = {item.fault_id: item for item in outcomes}
        target_degraded = all(
            full_by_id[fault_id].detected and not outcome_by_id[fault_id].detected
            for fault_id in targets
        )
        ablations.append(
            AblationReplayResult(
                ablation_id=ablation_id,
                disabled_component=disabled_component,
                packet_sha256=ablation_packet.packet_sha256 or "",
                target_fault_ids=targets,
                fault_outcomes=outcomes,
                detected_fault_count=sum(item.detected for item in outcomes),
                target_fault_degraded=target_degraded,
            )
        )

    return FaultReplayReport(
        case_id=clean_packet.case_id,
        clean_packet_sha256=clean_packet.packet_sha256 or "",
        full_system_outcomes=full_outcomes,
        clean_false_block_count=_clean_false_block_count(clean_packet),
        ablations=ablations,
    )


def _strip_execution_diagnostics(
    payload: dict[str, Any],
    keys: set[str],
) -> None:
    for execution in payload.get("executions") or []:
        diagnostics = execution.get("diagnostics") or {}
        for key in keys:
            diagnostics.pop(key, None)


def _fault_invariant(
    clean: BenchmarkPacket,
    injected: BenchmarkPacket,
    fault_id: FaultId,
) -> tuple[bool, list[str]]:
    injected_payload = injected.model_dump(mode="json")
    if fault_id == "duplicate_merge_inflation":
        return _probe_merge_cardinality(injected_payload)
    if fault_id == "time_leakage_or_lead_misuse":
        return _h2_temporal_direction(injected_payload)
    if fault_id == "unit_amplification":
        return _independent_replication_check(clean, injected)
    if fault_id == "variable_timing_shift":
        return _h2_variable_timing(injected_payload)
    if fault_id == "wrong_clustering":
        return _independent_replication_check(clean, injected)
    if fault_id == "significant_subgroup_cherry_pick":
        return _reviewer_subgroup_registration(injected_payload)
    if fault_id == "table_text_mismatch":
        valid, total, failures = protected_numeric_consistency(injected)
        return total > 0 and valid != total, failures
    if fault_id == "association_to_causation":
        return _claim_gate_causal_wording(injected)
    if fault_id == "deleted_null_or_failure_branch":
        return _dag_terminal_closure(injected)
    raise ValueError(f"unknown fault: {fault_id}")


def _clean_false_block_count(clean_packet: BenchmarkPacket) -> int:
    count = 0
    for fault_id in FAULT_IDS:
        present, _ = _fault_invariant(clean_packet, clean_packet, fault_id)
        count += int(present)
    return count


def _probe_merge_cardinality(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    diagnostics = _first_execution(payload).get("diagnostics", {})
    duplicates = int(diagnostics.get("duplicate_primary_key_count", 0) or 0)
    before = int(diagnostics.get("rows_before_merge", 0) or 0)
    after = int(diagnostics.get("rows_after_merge", 0) or 0)
    invalid = duplicates > 0 and after > before
    return invalid, ["duplicate primary keys inflated the post-merge sample"] if invalid else []


def _h2_temporal_direction(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    diagnostics = _first_execution(payload).get("diagnostics", {})
    observed = diagnostics.get("lead_offset")
    frozen = diagnostics.get("expected_lead_offset")
    invalid = (
        observed is not None
        and frozen is not None
        and (observed != frozen or observed >= 0)
    )
    return invalid, ["lead offset violates the frozen temporal direction"] if invalid else []


def _h2_variable_timing(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    diagnostics = _first_execution(payload).get("diagnostics", {})
    observed = diagnostics.get("outcome_time_shift")
    frozen = diagnostics.get("contract_outcome_time_shift")
    invalid = observed is not None and frozen is not None and observed != frozen
    return invalid, ["outcome timing no longer matches the frozen contract"] if invalid else []


def _reviewer_subgroup_registration(
    payload: dict[str, Any],
) -> tuple[bool, list[str]]:
    diagnostics = _first_execution(payload).get("diagnostics", {})
    selected_post_hoc = diagnostics.get("subgroup_selection") == "post_hoc_significance"
    preregistered = diagnostics.get("subgroup_preregistered")
    invalid = selected_post_hoc and preregistered is not True
    return invalid, ["reported subgroup was selected post hoc for significance"] if invalid else []


def _claim_gate_causal_wording(packet: BenchmarkPacket) -> tuple[bool, list[str]]:
    evidence: list[str] = []
    known_strengths = {
        "causal_strong",
        "causal_cautious",
        "associational",
        "preliminary",
        "mixed",
        "insufficient",
        "prohibited",
    }
    for claim in packet.claims:
        if claim.admission_status not in {"admitted", "downgrade_required"}:
            continue
        strength = claim.strength if claim.strength in known_strengths else "prohibited"
        evidence.extend(
            f"{claim.claim_id}:{predicate}"
            for predicate in causal_wording_violations(claim.text, strength)
        )
    return bool(evidence), evidence


def _dag_terminal_closure(packet: BenchmarkPacket) -> tuple[bool, list[str]]:
    plan = _normalized_plan(packet)
    executions = [
        ExecutionRecord(
            execution_id=item.execution_id,
            run_type=item.run_type,
            plan_step_id=item.check_id,
            check_id=item.check_id,
            execution_status=item.execution_status,
            estimates=item.estimates,
            diagnostic_results=item.diagnostics,
            not_executed_reason_code=item.not_executed_reason_code,
        )
        for item in packet.executions
    ]
    original_check_ids = {item.check_id for item in packet.executions}
    # Keep the production finalizer in this check, but distinguish records it
    # synthesizes from explicit terminal records already present in the packet.
    finalize_test_dag_executions(plan, executions)
    required = set(packet.design.required_check_ids)
    missing = sorted(required - original_check_ids)
    return bool(missing), missing


def _normalized_plan(packet: BenchmarkPacket) -> AnalysisPlan:
    planned = list(dict.fromkeys(packet.design.planned_check_ids))
    run_types = {item.check_id: item.run_type for item in packet.executions}
    baseline_ids = [
        check_id for check_id in planned if run_types.get(check_id) == "baseline"
    ]
    if not baseline_ids and planned:
        baseline_ids = [planned[0]]
    baseline_set = set(baseline_ids)
    required = set(packet.design.required_check_ids)

    def planned_step(check_id: str, run_type: str) -> PlannedStep:
        roles = {
            "diagnostic": "diagnostic",
            "robustness": "robustness",
            "falsification": "falsification",
            "mechanism": "robustness",
            "heterogeneity": "robustness",
            "replication": "replication",
        }
        return PlannedStep(
            step_id=check_id,
            name=check_id,
            rationale="normalized benchmark check",
            priority="required" if check_id in required else "optional",
            test_role=roles.get(run_type),
            required_for_admission=check_id in required,
        )

    sections: dict[str, list[PlannedStep]] = {
        "diagnostics": [],
        "robustness_tests": [],
        "falsification_tests": [],
        "mechanism_tests": [],
        "heterogeneity_tests": [],
    }
    destination = {
        "diagnostic": "diagnostics",
        "robustness": "robustness_tests",
        "falsification": "falsification_tests",
        "mechanism": "mechanism_tests",
        "heterogeneity": "heterogeneity_tests",
        "replication": "robustness_tests",
    }
    for check_id in planned:
        if check_id in baseline_set:
            continue
        run_type = run_types.get(check_id, "diagnostic")
        sections[destination.get(run_type, "diagnostics")].append(
            planned_step(check_id, run_type)
        )
    baselines = [
        ModelSpec(
            **planned_step(check_id, "baseline").model_dump(mode="python"),
            estimator="panel_ols",
            outcome=(packet.design.outcomes[0] if packet.design.outcomes else None),
            treatments_or_exposures=packet.design.treatments_or_exposures,
            controls=packet.design.controls,
            fixed_effects=packet.design.fixed_effects,
            standard_error_strategy=packet.design.standard_error_strategy,
        )
        for check_id in baseline_ids
    ]
    return AnalysisPlan(
        plan_id=f"benchmark-{packet.packet_id}",
        plan_version=1,
        method_family=packet.design.method_family or "panel_association",
        design_only=False,
        estimands=[],
        sample_rules=[],
        variable_construction=[],
        baseline_models=baselines,
        diagnostics=sections["diagnostics"],
        robustness_tests=sections["robustness_tests"],
        falsification_tests=sections["falsification_tests"],
        mechanism_tests=sections["mechanism_tests"],
        heterogeneity_tests=sections["heterogeneity_tests"],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=[],
        unsupported_requested_analyses=[],
        check_registry_version="enterprise-panel-v1",
    )


def _independent_replication_check(
    clean: BenchmarkPacket,
    injected: BenchmarkPacket,
) -> tuple[bool, list[str]]:
    if not _has_independent_reproduction(clean) or not _has_independent_reproduction(
        injected
    ):
        return False, []
    contract_hash = canonical_sha256(
        {
            "design": clean.design.model_dump(mode="json"),
            "visible_input_sha256": clean.visible_input_sha256,
            "data_sha256": clean.data_sha256,
        }
    )
    primary_id = (
        clean.reproduction.primary_implementation_id
        or next(
            (item.implementation_id for item in clean.executions if item.implementation_id),
            "linearmodels-panelols-v1",
        )
    )
    replication_id = (
        clean.reproduction.replication_implementation_id
        or "numpy-two-way-within-v1"
    )
    primary = _normalized_research_run(
        injected,
        run_id="benchmark-primary",
        implementation_id=primary_id,
        contract_hash=contract_hash,
    )
    replication = _normalized_research_run(
        clean,
        run_id="benchmark-independent-replication",
        implementation_id=replication_id,
        contract_hash=contract_hash,
    )
    audit = compare_panel_reproduction(primary, replication)
    invalid = audit.status != "matched"
    return invalid, audit.differences


def _has_independent_reproduction(packet: BenchmarkPacket) -> bool:
    reproduction = packet.reproduction
    estimate_check_ids = {
        execution.check_id
        for execution in packet.executions
        if execution.estimates
    }
    return bool(
        reproduction.mode == "independent_implementation"
        and reproduction.status == "matched"
        and reproduction.primary_implementation_id
        and reproduction.replication_implementation_id
        and reproduction.primary_implementation_id
        != reproduction.replication_implementation_id
        and estimate_check_ids
        and estimate_check_ids.issubset(set(reproduction.covered_check_ids))
    )


def _normalized_research_run(
    packet: BenchmarkPacket,
    *,
    run_id: str,
    implementation_id: str,
    contract_hash: str,
) -> ResearchRun:
    executions: list[ExecutionRecord] = []
    for item in packet.executions:
        diagnostics = dict(item.diagnostics)
        diagnostics.update(
            {
                "entity_fixed_effects": bool(packet.design.fixed_effects),
                "time_fixed_effects": len(packet.design.fixed_effects) > 1,
                "standard_errors": packet.design.standard_error_strategy,
                "cluster_correction": diagnostics.get(
                    "cluster_correction", "finite_sample_debiased"
                ),
            }
        )
        executions.append(
            ExecutionRecord(
                execution_id=f"{run_id}-{item.execution_id}",
                run_type=item.run_type,
                plan_step_id=item.check_id,
                check_id=item.check_id,
                execution_status=item.execution_status,
                estimates=item.estimates,
                diagnostic_results=diagnostics,
                not_executed_reason_code=item.not_executed_reason_code,
                provenance=ExecutionProvenance(
                    implementation_id=implementation_id,
                    implementation_version="benchmark-normalized-v1",
                    code_sha256=canonical_sha256(implementation_id),
                    environment_sha256=canonical_sha256("benchmark-environment-v1"),
                    contract_sha256=contract_hash,
                    data_sha256=packet.data_sha256,
                ),
            )
        )
    estimative = [item for item in executions if item.estimates]
    succeeded = bool(estimative) and all(
        item.execution_status == "succeeded" for item in estimative
    )
    return ResearchRun(
        research_run_id=run_id,
        case_id=packet.case_id,
        contract_hash=contract_hash,
        plan_version=1,
        execution_status="succeeded" if succeeded else "failed",
        scientific_status="pending_review" if succeeded else "invalid",
        fixture_only=False,
        executions=executions,
    )


def _first_execution(payload: dict[str, Any]) -> dict[str, Any]:
    executions = payload.get("executions") or []
    if not executions:
        raise ValueError("fault replay requires at least one execution")
    return executions[0]


def _first_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    for execution in payload.get("executions") or []:
        estimates = execution.get("estimates") or []
        if estimates:
            return estimates[0]
    raise ValueError("fault replay requires at least one estimate")


def _first_protected_value(payload: dict[str, Any]) -> dict[str, Any]:
    numeric_kinds = {
        "count",
        "coefficient",
        "standard_error",
        "interval_bound",
        "p_value",
        "fit_statistic",
        "year",
    }
    for statement in payload.get("statements") or []:
        for value in statement.get("protected_values") or []:
            if (
                value.get("source_kind", "execution") == "execution"
                and value.get("value_kind") in numeric_kinds
            ):
                return value
    raise ValueError("fault replay requires at least one protected numeric value")
