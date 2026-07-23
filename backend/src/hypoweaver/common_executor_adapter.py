"""Native HypoWeaver stop/resume hook for the common-executor board.

Planning still runs through the real :class:`WorkflowEngine` and stops at H2.
Only an exact, hash-bound common-executor result may resume the run.  The
result is converted to native ``ResearchRun`` records, then the existing H3
evidence/audit/Claim Gate path continues unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .adapters import (
    MODEL_CALL_GROUP_LIMITS,
    V3_LOGICAL_CALL_BUDGET,
    V3_PROVIDER_ATTEMPT_BUDGET,
)
from .models import (
    AnalysisPlan,
    ClaimLedger,
    ExecutionProvenance,
    ExecutionRecord,
    FormalResearchContract,
    ReproductionAudit,
    ResearchRun,
    RunState,
)
from .seal import canonical_sha256
from .test_dag import (
    THREAT_FE_CLUSTER_FEASIBILITY,
    THREAT_KEY_SAMPLE_FLOW,
    THREAT_MISSINGNESS_WITHIN_VARIANCE,
    THREAT_POLICY_ENTITY_CLUSTER,
    THREAT_POLICY_EVENT_STUDY,
    THREAT_POLICY_GROUP_FIXED_PRE,
    THREAT_POLICY_PERMUTATION_PLACEBO,
    THREAT_POLICY_PLACEBO,
    THREAT_POLICY_SUPPORT,
    schedule_test_dag,
)

if TYPE_CHECKING:
    from .engine import WorkflowEngine


ANALYSIS_REQUEST_VERSION = "sixbench-analysis-request-v1"
EXECUTION_RESULT_VERSION = "sixbench-common-execution-result-v1"
CLAIM_DECISION_VERSION = "sixbench-common-claim-decision-v1"
COMMON_LEADERBOARD = "common_executor_reasoning_control"
COMMON_EXECUTION_MODE = "common_executor_reasoning_control"

_METHODS = {
    "panel_twfe",
    "spatial_sdm",
    "classic_did_city_month_v1",
    "firm_quarter_interaction_panel_v1",
}
_POST_H2_ARTIFACTS = {
    "formal_research_contract",
    "common_executor_request_binding",
    "common_executor_result_binding",
    "research_run",
    "replication_run",
    "reproduction_audit",
    "evidence_registry",
    "evidence_assessment",
    "candidate_claim_ledger",
    "scientific_audit",
    "claim_gate_report",
    "claim_ledger",
    "approved_claim_ledger",
    "manuscript_package",
}
_RESULT_KEYS = {
    "coefficient",
    "coefficients",
    "confidence_interval",
    "effect_size",
    "estimate",
    "estimates",
    "log_likelihood",
    "observed_sign",
    "p_value",
    "p_values",
    "pvalue",
    "r_squared",
    "research_run",
    "result",
    "results",
    "rho",
    "significance",
    "standard_error_value",
    "t_stat",
    "t_statistic",
    "t_value",
}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_DIRECTION_ZERO_TOLERANCE = 1e-12


class CommonExecutorAdapterError(ValueError):
    pass


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CommonExecutorAdapterError(f"{label} must be an object")
    return value


def _exact(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing or unknown:
        raise CommonExecutorAdapterError(
            f"{label} fields differ; missing={missing}, unknown={unknown}"
        )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommonExecutorAdapterError(f"{label} must be non-empty text")
    return value


def _strings(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise CommonExecutorAdapterError(f"{label} must be a string list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise CommonExecutorAdapterError(f"{label} contains an invalid item")
    if len(value) != len(set(value)):
        raise CommonExecutorAdapterError(f"{label} contains duplicates")
    return list(value)


def _sha(value: Any, label: str) -> str:
    text = _text(value, label)
    if not _SHA.fullmatch(text):
        raise CommonExecutorAdapterError(f"{label} must be a lowercase sha256")
    return text


def _artifact(state: RunState, key: str) -> Any:
    envelope = state.artifacts.get(key)
    if not isinstance(envelope, Mapping) or "payload" not in envelope:
        raise CommonExecutorAdapterError(f"required H2 artifact is missing: {key}")
    if envelope.get("sha256") != canonical_sha256(envelope["payload"]):
        raise CommonExecutorAdapterError(f"artifact sha256 mismatch: {key}")
    return envelope["payload"]


def _reject_results(value: Any, location: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _RESULT_KEYS:
                raise CommonExecutorAdapterError(
                    f"post-result field is forbidden before H2: {location}.{key}"
                )
            _reject_results(nested, f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_results(nested, f"{location}[{index}]")


def assert_h2_pre_result_boundary(state: RunState) -> None:
    """Require a genuine result-free H2 pause with auditable model receipts."""

    if (
        state.mode != "research"
        or state.model_provider != "qwen"
        or state.execution_mode != "external"
    ):
        raise CommonExecutorAdapterError(
            "common planning requires the native Qwen research-mode workflow"
        )
    if state.status != "waiting_human" or state.current_gate != "H2":
        raise CommonExecutorAdapterError("common planning must stop at waiting_human/H2")
    if state.execution_status != "not_started" or state.claims:
        raise CommonExecutorAdapterError("execution or claims exist before H2 export")
    leaked = sorted(set(state.artifacts) & _POST_H2_ARTIFACTS)
    if leaked:
        raise CommonExecutorAdapterError(
            "post-H2 artifacts exist before common execution: " + ", ".join(leaked)
        )
    state_payload = state.model_dump(mode="json")
    for section in ("steps", "events", "decisions"):
        _reject_results(state_payload[section], f"state.{section}")
    for key in state.artifacts:
        _reject_results(_artifact(state, key), f"state.artifacts.{key}")
    usage = _object(_artifact(state, "model_usage"), "model_usage")
    if (
        usage.get("budget_mode") != "v3"
        or usage.get("provider_attempt_ceiling") != V3_PROVIDER_ATTEMPT_BUDGET
        or usage.get("logical_call_ceiling") != V3_LOGICAL_CALL_BUDGET
        or usage.get("group_limits") != MODEL_CALL_GROUP_LIMITS
    ):
        raise CommonExecutorAdapterError("native H2 state is outside the frozen v3 budget")
    receipts = usage.get("call_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise CommonExecutorAdapterError("native H2 state has no provider receipts")
    logical_ids: set[str] = set()
    attempts_by_logical_id: dict[str, int] = {}
    prompts_by_logical_id: dict[str, str] = {}
    for receipt in receipts:
        row = _object(receipt, "model receipt")
        if row.get("call_group") != "h1_h2":
            raise CommonExecutorAdapterError("post-H2 receipt leaked into planning")
        logical_id = _text(row.get("logical_call_id"), "logical_call_id")
        logical_ids.add(logical_id)
        prompt_key = _text(row.get("prompt_key"), "receipt.prompt_key")
        if prompts_by_logical_id.setdefault(logical_id, prompt_key) != prompt_key:
            raise CommonExecutorAdapterError("logical call changed prompt identity")
        attempts_by_logical_id[logical_id] = (
            attempts_by_logical_id.get(logical_id, 0) + 1
        )
        for key in ("input_sha256", "response_sha256", "output_schema_sha256"):
            _sha(row.get(key), f"receipt.{key}")
    if (
        len(logical_ids) != 5
        or len(receipts) > 15
        or any(attempts > 3 for attempts in attempts_by_logical_id.values())
        or sorted(prompts_by_logical_id.values())
        != sorted(
            [
                "hypothesis_decomposition",
                "candidate_plan_batch",
                "candidate_plan_batch",
                "reviewer_report_batch",
                "reviewer_report_batch",
            ]
        )
    ):
        raise CommonExecutorAdapterError("H2 planning violated the frozen 5/15 stage budget")
    if (
        usage.get("provider_attempts") != len(receipts)
        or usage.get("llm_calls") != len(receipts)
        or usage.get("logical_calls") != len(logical_ids)
        or usage.get("group_usage")
        != {"h1_h2": len(logical_ids), "h3": 0, "h4": 0}
    ):
        raise CommonExecutorAdapterError("native H2 receipt counters are inconsistent")


def validate_analysis_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    _reject_results(payload, "analysis_request")
    request = dict(_object(payload, "AnalysisRequest"))
    root_keys = {
        "schema_version", "run_id", "case_id", "input_view", "system_id",
        "seed", "method_selection", "outcome", "treatments", "controls",
        "fixed_effects", "standard_error", "diagnostics", "claim_plan",
    }
    _exact(request, root_keys, "AnalysisRequest")
    if request["schema_version"] != ANALYSIS_REQUEST_VERSION:
        raise CommonExecutorAdapterError("unsupported AnalysisRequest schema")
    if request["system_id"] != "hypoweaver":
        raise CommonExecutorAdapterError("AnalysisRequest has wrong system_id")
    if request["input_view"] not in {"discovery_blind", "reproduction_aligned"}:
        raise CommonExecutorAdapterError("AnalysisRequest has wrong input_view")
    if (
        isinstance(request["seed"], bool)
        or not isinstance(request["seed"], int)
        or request["seed"] < 0
    ):
        raise CommonExecutorAdapterError("AnalysisRequest seed must be an integer >= 0")
    for key in ("run_id", "case_id", "outcome"):
        _text(request[key], f"AnalysisRequest.{key}")
    method = _object(request["method_selection"], "method_selection")
    _exact(method, {"method", "rationale"}, "method_selection")
    if method.get("method") not in _METHODS:
        raise CommonExecutorAdapterError("AnalysisRequest method is unsupported")
    _text(method.get("rationale"), "method_selection.rationale")
    treatments = _strings(request["treatments"], "treatments", allow_empty=False)
    controls = _strings(request["controls"], "controls")
    fixed_effects = _strings(request["fixed_effects"], "fixed_effects", allow_empty=False)
    if len(fixed_effects) != 2:
        raise CommonExecutorAdapterError("common executor requires two fixed effects")
    _strings(request["diagnostics"], "diagnostics")
    if request["outcome"] in {*treatments, *controls} or set(treatments) & set(controls):
        raise CommonExecutorAdapterError("AnalysisRequest variable roles overlap")
    standard_error = _object(request["standard_error"], "standard_error")
    if not {"strategy"}.issubset(standard_error) or set(standard_error) - {"strategy", "cluster"}:
        raise CommonExecutorAdapterError("standard_error fields are invalid")
    _text(standard_error.get("strategy"), "standard_error.strategy")
    if "cluster" in standard_error:
        _text(standard_error["cluster"], "standard_error.cluster")
    claim_plan = _object(request["claim_plan"], "claim_plan")
    if not {"target_terms", "maximum_strength"}.issubset(claim_plan) or set(claim_plan) - {"target_terms", "maximum_strength", "rationale"}:
        raise CommonExecutorAdapterError("claim_plan fields are invalid")
    targets = _strings(claim_plan["target_terms"], "claim_plan.target_terms", allow_empty=False)
    if not set(targets).issubset(treatments):
        raise CommonExecutorAdapterError("claim target terms must be treatments")
    maximum_strength = claim_plan["maximum_strength"]
    if maximum_strength not in {
        "descriptive",
        "associational",
        "causal_contingent",
    }:
        raise CommonExecutorAdapterError("claim strength exceeds the common board")
    if (
        maximum_strength == "causal_contingent"
        and method["method"] != "classic_did_city_month_v1"
    ):
        raise CommonExecutorAdapterError(
            "causal_contingent is only valid for the classic DID common method"
        )
    if "rationale" in claim_plan:
        _text(claim_plan["rationale"], "claim_plan.rationale")
    return request


def _validate_request_against_plan(request: Mapping[str, Any], plan: AnalysisPlan) -> None:
    if len(plan.baseline_models) != 1:
        raise CommonExecutorAdapterError("common executor needs one H2 baseline model")
    model = plan.baseline_models[0]
    expected = {
        "outcome": model.outcome,
        "treatments": model.treatments_or_exposures,
        "controls": model.controls,
        "fixed_effects": model.fixed_effects,
    }
    for key, value in expected.items():
        if request[key] != value:
            raise CommonExecutorAdapterError(f"AnalysisRequest {key} drifted from H2")
    compatible = {
        "panel_association": {"panel_twfe", "firm_quarter_interaction_panel_v1"},
        "mechanism_boundary": {"panel_twfe", "firm_quarter_interaction_panel_v1"},
        "policy_causal": {"classic_did_city_month_v1"},
        "spatial": {"spatial_sdm"},
    }
    method = request["method_selection"]["method"]
    if method not in compatible.get(plan.method_family, set()):
        raise CommonExecutorAdapterError(
            f"AnalysisRequest method {method} is incompatible with H2 {plan.method_family}"
        )


def build_pre_result_binding(
    state: RunState,
    analysis_request: Mapping[str, Any],
    *,
    selected_candidate_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assert_h2_pre_result_boundary(state)
    request = validate_analysis_request(analysis_request)
    arena = _object(_artifact(state, "design_arena"), "design_arena")
    selected = selected_candidate_id or arena.get("provisional_candidate_id")
    if selected not in arena.get("recommended_candidate_ids", []):
        raise CommonExecutorAdapterError("selected H2 candidate is not recommended")
    candidate = next(
        (
            item for item in arena.get("candidates", [])
            if isinstance(item, Mapping) and item.get("candidate_id") == selected
        ),
        None,
    )
    if candidate is None:
        raise CommonExecutorAdapterError("selected H2 candidate is missing")
    plan = AnalysisPlan.model_validate(candidate.get("plan"))
    _validate_request_against_plan(request, plan)
    if state.case_id not in {request["case_id"], f"{request['case_id']}_{request['input_view']}"}:
        raise CommonExecutorAdapterError("AnalysisRequest case identity does not match H2")
    package = _object(_artifact(state, "research_package"), "research_package")
    main_hashes = {
        item.get("sha256")
        for item in package.get("dataset_refs", [])
        if isinstance(item, Mapping) and item.get("role") == "main"
    }
    supplementary_hashes = {
        item.get("sha256")
        for item in package.get("dataset_refs", [])
        if isinstance(item, Mapping) and item.get("role") == "supplementary"
    }
    if len(main_hashes) != 1:
        raise CommonExecutorAdapterError("H2 must bind exactly one main dataset hash")
    if len(supplementary_hashes) > 1:
        raise CommonExecutorAdapterError("H2 has more than one supplementary asset")
    data_sha = _sha(next(iter(main_hashes)), "research_package.main_data_sha256")
    weights_sha = (
        _sha(
            next(iter(supplementary_hashes)),
            "research_package.supplementary_data_sha256",
        )
        if supplementary_hashes
        else None
    )
    binding = {
        "schema_version": "hypoweaver-common-pre-result-binding-v1",
        **{key: request[key] for key in ("run_id", "case_id", "input_view", "system_id", "seed")},
        "hypoweaver_run_id": state.id,
        "h2_state_sha256": canonical_sha256(state.model_dump(mode="json")),
        "selected_candidate_id": selected,
        "selected_plan_sha256": canonical_sha256(plan.model_dump(mode="json")),
        "analysis_request_sha256": canonical_sha256(request),
        "data_sha256": data_sha,
        "weights_sha256": weights_sha,
    }
    return request, binding


def _canonical_term(estimate: Mapping[str, Any]) -> str:
    term = _text(estimate.get("term"), "estimate.term")
    effect = estimate.get("effect_type")
    if effect is None:
        return term
    if effect not in {"direct", "indirect", "total"}:
        raise CommonExecutorAdapterError("unsupported spatial effect_type")
    return f"{term}:{effect}"


def validate_sealed_common_result(
    result_bytes: bytes,
    *,
    pre_result_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        result = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommonExecutorAdapterError(f"invalid common result JSON: {exc}") from exc
    result = dict(_object(result, "common execution result"))
    root_keys = {
        "schema_version", "run_id", "case_id", "input_view", "system_id",
        "seed", "execution_mode", "native_system_execution",
        "analysis_request", "executions", "provenance",
    }
    _exact(result, root_keys, "common execution result")
    if result["schema_version"] != EXECUTION_RESULT_VERSION:
        raise CommonExecutorAdapterError("unsupported common result schema")
    if result["execution_mode"] != COMMON_EXECUTION_MODE or result["native_system_execution"] is not False:
        raise CommonExecutorAdapterError("common result producer identity is invalid")
    request = validate_analysis_request(_object(result["analysis_request"], "analysis_request"))
    request_sha = canonical_sha256(request)
    if request_sha != pre_result_binding["analysis_request_sha256"]:
        raise CommonExecutorAdapterError("common result does not match the H2 request")
    for key in ("run_id", "case_id", "input_view", "system_id", "seed"):
        if result[key] != request[key] or result[key] != pre_result_binding[key]:
            raise CommonExecutorAdapterError(f"common result {key} binding mismatch")
    executions = result["executions"]
    if not isinstance(executions, list) or len(executions) != 1:
        raise CommonExecutorAdapterError("common result needs one primary execution")
    execution = _object(executions[0], "common execution")
    execution_keys = {
        "execution_id", "check_id", "outcome", "status", "method", "estimates",
        "diagnostics", "requested_diagnostics", "claim_plan",
        "implementation_id", "independence_scope", "shared_components",
    }
    _exact(execution, execution_keys, "common execution")
    for key in (
        "execution_id",
        "check_id",
        "outcome",
        "implementation_id",
        "independence_scope",
    ):
        _text(execution[key], f"common execution.{key}")
    if (
        execution["check_id"] != "baseline"
        or execution["outcome"] != request["outcome"]
    ):
        raise CommonExecutorAdapterError(
            "common execution check or outcome drifted from the H2 request"
        )
    shared_components = _strings(
        execution["shared_components"], "common execution.shared_components"
    )
    _object(execution["diagnostics"], "common execution.diagnostics")
    if (
        execution["status"] != "completed"
        or execution["method"] != request["method_selection"]["method"]
    ):
        raise CommonExecutorAdapterError("common execution did not complete the requested method")
    if (
        execution["claim_plan"] != request["claim_plan"]
        or execution["requested_diagnostics"] != request["diagnostics"]
    ):
        raise CommonExecutorAdapterError("common execution drifted from the request")
    estimates = execution["estimates"]
    if not isinstance(estimates, list) or not estimates:
        raise CommonExecutorAdapterError("common execution has no estimates")
    regressors = {*request["treatments"], *request["controls"]}
    allowed_terms = (
        {"rho", *regressors, *(f"W:{term}" for term in regressors)}
        if execution["method"] == "spatial_sdm" else regressors
    )
    canonical_terms: list[str] = []
    for raw in estimates:
        estimate = _object(raw, "estimate")
        term = _text(estimate.get("term"), "estimate.term")
        if term not in allowed_terms:
            raise CommonExecutorAdapterError(f"unregistered estimate term: {term}")
        coefficient = estimate.get("coefficient")
        if isinstance(coefficient, bool) or not isinstance(coefficient, (int, float)) or not math.isfinite(float(coefficient)):
            raise CommonExecutorAdapterError(f"estimate {term} is not finite")
        canonical_terms.append(_canonical_term(estimate))
    if len(canonical_terms) != len(set(canonical_terms)):
        raise CommonExecutorAdapterError("common estimate terms are not unique")
    if not set(request["claim_plan"]["target_terms"]).issubset(
        {str(item["term"]) for item in estimates}
    ):
        raise CommonExecutorAdapterError("common result omitted a target term")
    provenance = _object(result["provenance"], "common provenance")
    _exact(
        provenance,
        {
            "executor_kind",
            "execution_mode",
            "native_system_execution",
            "reasoning_source_system_id",
            "implementation_id",
            "reference_executor_functions_reused",
            "shared_components",
            "request_sha256",
            "execution_contract_sha256",
            "case_manifest_sha256",
            "data_sha256",
            "weights_sha256",
            "hidden_reference_accessed",
            "network_access",
            "selected_case_package_only",
            "contract_paths_used_for_execution",
        },
        "common provenance",
    )
    if (
        provenance.get("executor_kind") != "benchmark_owned_common_executor"
        or provenance.get("execution_mode") != COMMON_EXECUTION_MODE
        or provenance.get("native_system_execution") is not False
        or provenance.get("reasoning_source_system_id") != "hypoweaver"
        or provenance.get("reference_executor_functions_reused") is not False
        or provenance.get("shared_components") != shared_components
        or provenance.get("hidden_reference_accessed") is not False
        or provenance.get("network_access") != "denied"
        or provenance.get("selected_case_package_only") is not True
        or provenance.get("contract_paths_used_for_execution") is not False
        or provenance.get("request_sha256") != request_sha
        or provenance.get("implementation_id") != execution["implementation_id"]
        or provenance.get("data_sha256") != pre_result_binding["data_sha256"]
        or provenance.get("weights_sha256")
        != pre_result_binding["weights_sha256"]
    ):
        raise CommonExecutorAdapterError("common provenance is not admissible")
    for key in ("execution_contract_sha256", "case_manifest_sha256", "data_sha256"):
        _sha(provenance.get(key), f"provenance.{key}")
    if provenance["weights_sha256"] is not None:
        _sha(provenance["weights_sha256"], "provenance.weights_sha256")
    result_sha = hashlib.sha256(result_bytes).hexdigest()
    binding = {
        "schema_version": "hypoweaver-common-result-binding-v1",
        **{key: result[key] for key in ("run_id", "case_id", "input_view", "system_id", "seed")},
        "analysis_request_sha256": request_sha,
        "execution_result_sha256": result_sha,
        "executor_implementation_id": execution["implementation_id"],
        "native_system_execution": False,
        "hidden_reference_accessed": False,
    }
    return result, binding


def read_sealed_common_result(
    path: Path,
    *,
    pre_result_binding: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise CommonExecutorAdapterError("common result cannot be a symlink")
    try:
        resolved = expanded.resolve(strict=True)
        if not resolved.is_file():
            raise OSError("not a regular file")
        result_bytes = resolved.read_bytes()
    except OSError as exc:
        raise CommonExecutorAdapterError(f"cannot read common result: {exc}") from exc
    result, binding = validate_sealed_common_result(
        result_bytes, pre_result_binding=pre_result_binding
    )
    return result_bytes, result, binding


def _diagnostic_for_step(
    threat_id: str | None,
    requested: list[str],
    diagnostics: Mapping[str, Any],
) -> str | None:
    mapping = {
        THREAT_KEY_SAMPLE_FLOW: (("sample_attrition", "panel_key_uniqueness"), ("sample_attrition", "rows_used")),
        THREAT_MISSINGNESS_WITHIN_VARIANCE: (("sample_attrition",), ("sample_attrition", "rows_dropped_missing")),
        THREAT_FE_CLUSTER_FEASIBILITY: (("cluster_count", "wild_cluster_bootstrap"), ("wild_cluster_bootstrap", "entity_count", "industry_cluster_count")),
        THREAT_POLICY_SUPPORT: (("treatment_assignment", "sample_attrition"), ("treatment_assignment", "sample_attrition")),
        THREAT_POLICY_EVENT_STUDY: (("event_study",), ("event_study",)),
        THREAT_POLICY_GROUP_FIXED_PRE: (("event_study",), ("event_study",)),
        THREAT_POLICY_PLACEBO: (("placebo_timing",), ("placebo_timing",)),
        THREAT_POLICY_ENTITY_CLUSTER: (("wild_cluster_bootstrap", "small_treated_group"), ("wild_cluster_bootstrap", "small_treated_group")),
        THREAT_POLICY_PERMUTATION_PLACEBO: (("treatment_assignment_placebo", "small_treated_group"), ("small_treated_group",)),
    }
    requested_names, result_names = mapping.get(threat_id, ((), ()))
    if requested_names and not set(requested_names).intersection(requested):
        return None
    return next((name for name in result_names if name in diagnostics), None)


def build_bound_research_run(
    result: Mapping[str, Any],
    contract: FormalResearchContract,
) -> tuple[ResearchRun, ReproductionAudit]:
    """Bind validated common evidence to the contract frozen inside H2."""

    request = _object(result["analysis_request"], "analysis_request")
    plan = contract.approved_plan
    _validate_request_against_plan(request, plan)
    provenance_row = _object(result["provenance"], "provenance")
    if provenance_row.get("data_sha256") not in contract.data_hashes:
        raise CommonExecutorAdapterError("common result data hash is outside H2")
    execution = _object(result["executions"][0], "common execution")
    contract_sha = canonical_sha256(contract.model_dump(mode="json"))
    provenance = ExecutionProvenance(
        implementation_id=execution["implementation_id"],
        implementation_version=EXECUTION_RESULT_VERSION,
        code_sha256=hashlib.sha256(execution["implementation_id"].encode()).hexdigest(),
        environment_sha256=canonical_sha256(
            {
                "execution_contract_sha256": provenance_row["execution_contract_sha256"],
                "shared_components": execution["shared_components"],
            }
        ),
        contract_sha256=contract_sha,
        data_sha256=list(contract.data_hashes),
    )
    baseline = plan.baseline_models[0]
    records = [
        ExecutionRecord(
            execution_id=execution["execution_id"],
            run_type="baseline",
            plan_step_id=baseline.step_id,
            execution_status="succeeded",
            estimates=[dict(item) for item in execution["estimates"]],
            diagnostic_results=dict(execution["diagnostics"]),
            warnings=["Executed by the benchmark-owned common executor."],
            check_id=baseline.step_id,
            provenance=provenance,
        )
    ]
    completed = {baseline.step_id}
    for scheduled in schedule_test_dag(plan):
        step = scheduled.step
        if step.step_id in completed:
            continue
        diagnostic = _diagnostic_for_step(
            step.threat_id,
            execution["requested_diagnostics"],
            execution["diagnostics"],
        )
        records.append(
            ExecutionRecord(
                execution_id=f"common-{step.step_id}",
                run_type=scheduled.run_type,
                plan_step_id=step.step_id,
                execution_status="succeeded" if diagnostic else "not_executed",
                estimates=[],
                diagnostic_results=(
                    {diagnostic: execution["diagnostics"][diagnostic]}
                    if diagnostic else {}
                ),
                warnings=[
                    "Executed by the common executor."
                    if diagnostic else
                    "The common result contains no evidence for this frozen step."
                ],
                check_id=step.step_id,
                not_executed_reason_code=None if diagnostic else "not_executable",
                provenance=provenance if diagnostic else None,
            )
        )
    research_run_id = "common-" + canonical_sha256(
        {"result": result, "contract": contract.model_dump(mode="json")}
    )[:24]
    run = ResearchRun(
        research_run_id=research_run_id,
        case_id=contract.case_id,
        contract_hash=contract.approved_plan_hash,
        plan_version=plan.plan_version,
        execution_status="succeeded",
        scientific_status="limited",
        fixture_only=False,
        executions=records,
        warnings=[
            "HypoWeaver selected the design but did not execute the estimator.",
            "The common condition supplies no independent replication run.",
        ],
    )
    reproduction = ReproductionAudit(
        audit_id=f"common-reproduction-{research_run_id[7:]}",
        primary_run_id=research_run_id,
        status="not_applicable",
        differences=["One common execution is not an independent replication."],
        mode="same_implementation_rerun",
        shared_components=list(execution["shared_components"]),
        primary_implementation_id=execution["implementation_id"],
    )
    return run, reproduction


def claim_decision_from_h3(
    *,
    result: Mapping[str, Any],
    result_binding: Mapping[str, Any],
    claim_ledger: ClaimLedger,
) -> dict[str, Any]:
    """Export Claim Gate outcomes without copying numerical result fields."""

    execution = result["executions"][0]
    request = result["analysis_request"]
    preregistered_maximum = request["claim_plan"]["maximum_strength"]
    calibration = execution["diagnostics"].get("claim_calibration")
    causal_calibration_passed = (
        "claim_calibration" in request["diagnostics"]
        and isinstance(calibration, Mapping)
        and calibration.get("status") in {"passed", "admissible"}
        and calibration.get("causal_claim_admissible") is True
        and calibration.get("maximum_supported_claim_strength") == "causal"
        and calibration.get("blockers") == []
    )
    targets = set(result["analysis_request"]["claim_plan"]["target_terms"])
    estimates = [item for item in execution["estimates"] if item["term"] in targets]
    claims: list[dict[str, Any]] = []
    for index, estimate in enumerate(estimates):
        gated = claim_ledger.claims[min(index, len(claim_ledger.claims) - 1)] if claim_ledger.claims else None
        term = _canonical_term(estimate)
        if gated is None:
            claim_id, text, admission, strength = (
                f"claim-common-{index + 1}",
                "The frozen evidence did not produce an admissible claim.",
                "rejected",
                "descriptive",
            )
        else:
            admission = {"admitted": "admitted", "downgrade_required": "downgraded"}.get(gated.admission_status, "rejected")
            native_supports_association = (
                admission != "rejected"
                and gated.allowed_strength
                in {"associational", "causal_cautious", "causal_strong"}
            )
            if (
                preregistered_maximum == "causal_contingent"
                and admission == "admitted"
                and gated.allowed_strength in {"causal_cautious", "causal_strong"}
                and causal_calibration_passed
            ):
                strength = "causal"
            elif (
                preregistered_maximum
                in {"associational", "causal_contingent"}
                and native_supports_association
            ):
                strength = "associational"
            else:
                strength = "descriptive"
            claim_id = gated.claim_id if len(estimates) == 1 else f"{gated.claim_id}:{term}"
            text = (gated.final_text or gated.claim_text).strip()
        coefficient = float(estimate["coefficient"])
        direction = (
            "uncertain" if admission == "rejected" else
            "increase" if coefficient > _DIRECTION_ZERO_TOLERANCE else
            "decrease" if coefficient < -_DIRECTION_ZERO_TOLERANCE else "zero"
        )
        claims.append(
            {
                "claim_id": claim_id,
                "text": text,
                "strength": strength,
                "admission_status": admission,
                "direction": direction,
                "variable_id": result["analysis_request"]["outcome"],
                "coefficient_term": term,
                "unit": estimate.get("unit") or f"source_scale:{result['analysis_request']['outcome']}",
                "evidence_references": [
                    {"execution_id": execution["execution_id"], "coefficient_term": term}
                ],
            }
        )
    decision = {
        "schema_version": CLAIM_DECISION_VERSION,
        "leaderboard_id": COMMON_LEADERBOARD,
        "execution_result_sha256": result_binding["execution_result_sha256"],
        **{key: result[key] for key in ("run_id", "case_id", "input_view", "system_id", "seed")},
        "claims": claims,
    }
    return decision


async def run_to_h2_stop(engine: "WorkflowEngine", create_request: Any) -> RunState:
    """Run the real workflow through H1 and stop before H2 approval."""

    from .models import GateDecisionRequest

    if engine.model_call_budget_mode != "v3":
        raise CommonExecutorAdapterError("common planning requires the frozen v3 budget")
    state = await engine.create_run(create_request)
    if state.status != "waiting_human" or state.current_gate != "H1":
        raise CommonExecutorAdapterError("native workflow did not reach H1")
    state = await engine.decide_gate(
        state.id,
        "H1",
        GateDecisionRequest(
            action="approve",
            actor="benchmark_common_executor",
            expected_run_version=state.version,
            reviewed_artifact_hashes={
                "research_package": state.artifacts["research_package"]["sha256"]
            },
        ),
    )
    if state.status != "waiting_human" or state.current_gate != "H2":
        raise CommonExecutorAdapterError("native workflow did not reach H2")
    arena = _object(_artifact(state, "design_arena"), "design_arena")
    selected = _text(
        arena.get("provisional_candidate_id"),
        "design_arena.provisional_candidate_id",
    )
    if engine._refresh_h2_test_dag_if_needed(state, selected):
        state = engine.repository.save(state, expected_version=state.version)
    assert_h2_pre_result_boundary(state)
    return state


async def resume_with_sealed_common_result(
    engine: "WorkflowEngine",
    *,
    hypoweaver_run_id: str,
    analysis_request: Mapping[str, Any],
    execution_result_path: Path,
    selected_candidate_id: str | None = None,
) -> tuple[RunState, dict[str, Any], dict[str, Any]]:
    """Freeze H2, ingest exact bytes, resume H3, and export ClaimDecision."""

    state = engine.get_run(hypoweaver_run_id)
    request, pre_binding = build_pre_result_binding(
        state, analysis_request, selected_candidate_id=selected_candidate_id
    )
    result_bytes, result, result_binding = read_sealed_common_result(
        execution_result_path, pre_result_binding=pre_binding
    )
    state = await engine.ingest_external_research_run(
        hypoweaver_run_id,
        selected_candidate_id=pre_binding["selected_candidate_id"],
        analysis_request=request,
        execution_result_bytes=result_bytes,
        execution_result_sha256=result_binding["execution_result_sha256"],
        expected_run_version=state.version,
    )
    ledger = ClaimLedger.model_validate(_artifact(state, "claim_ledger"))
    decision = claim_decision_from_h3(
        result=result, result_binding=result_binding, claim_ledger=ledger
    )
    audit = {
        "schema_version": "hypoweaver-common-stage-audit-v1",
        "stage": "post_result",
        "hypoweaver_run_id": hypoweaver_run_id,
        **{
            key: pre_binding[key]
            for key in (
                "h2_state_sha256",
                "selected_plan_sha256",
                "analysis_request_sha256",
            )
        },
        "execution_result_sha256": result_binding["execution_result_sha256"],
        "claim_decision_sha256": canonical_sha256(decision),
        "native_executor_skipped": True,
        "hidden_reference_accessed": False,
    }
    return state, decision, audit


__all__ = [
    "CommonExecutorAdapterError",
    "build_pre_result_binding",
    "resume_with_sealed_common_result",
    "run_to_h2_stop",
]
