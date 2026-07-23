from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import (
    FixtureModelGateway,
    V2_LOGICAL_CALL_BUDGET,
    V2_PROVIDER_ATTEMPT_BUDGET,
    V3_LOGICAL_CALL_BUDGET,
    V3_PROVIDER_ATTEMPT_BUDGET,
)
from .case_import import DatasetRegistry
from .claim_gate import apply_claim_gate, code_owned_claims_for_registry
from .engine import WorkflowEngine
from .models import (
    AnalysisPlan,
    CaseSubmission,
    ClaimLedger,
    CreateRunRequest,
    FormalResearchContract,
    ManuscriptPackage,
    ReproductionAudit,
    ResearchPackage,
    ResearchRun,
    RunState,
    ScientificAudit,
)
from .official_benchmark_runner import (
    _claim_decision,
    _drive_hypoweaver,
    _gate_request,
    _require_gate,
)
from .repository import RunRepository
from .runtime_config import RuntimeConfigStore
from .seal import canonical_sha256
from .test_dag import build_evidence_registry


WRITER_RECOVERY_FROZEN_ARTIFACTS = (
    "research_package",
    "data_profile",
    "method_route",
    "analysis_plan",
    "critic_report",
    "formal_research_contract",
    "research_run",
    "replication_run",
    "reproduction_audit",
    "evidence_registry",
    "claim_gate_report",
    "approved_claim_ledger",
    "model_usage",
)


class _CodeOwnedWriterRecoveryGateway:
    """Generate only safe writer drafts without consuming another model call."""

    provider_name = "code_owned_writer_recovery"

    def __init__(self) -> None:
        self._fixture = FixtureModelGateway()

    async def generate(
        self,
        prompt_key: str,
        payload: dict[str, Any],
        output_model: type[Any],
        *,
        call_context: Any = None,
    ) -> Any:
        if prompt_key != "manuscript_section_draft_batch":
            raise RuntimeError(
                "writer recovery gateway only accepts manuscript section drafts"
            )
        return await self._fixture.generate(
            prompt_key,
            payload,
            output_model,
            call_context=call_context,
        )


class _WriterRecoveryWorkflowEngine(WorkflowEngine):
    """Reuse the production writer pipeline with a code-owned recovery draft."""

    def __init__(self, repository: RunRepository) -> None:
        super().__init__(repository, runtime_config_store=RuntimeConfigStore())
        self._writer_recovery_gateway = _CodeOwnedWriterRecoveryGateway()

    def _gateway(self, state: RunState) -> _CodeOwnedWriterRecoveryGateway:
        if state.status not in {"failed", "running"} or state.current_node_id != "scientific_writer":
            raise RuntimeError(
                "code-owned writer recovery cannot run outside scientific_writer"
            )
        return self._writer_recovery_gateway


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact_payload(state: RunState, key: str) -> Any:
    envelope = state.artifacts.get(key)
    if not isinstance(envelope, dict) or "payload" not in envelope:
        raise ValueError(f"artifact envelope is missing: {key}")
    payload = envelope["payload"]
    if canonical_sha256(payload) != envelope.get("sha256"):
        raise ValueError(f"artifact sha256 mismatch: {key}")
    return payload


def prepare_case(
    case_root: Path,
    *,
    registry_path: Path,
) -> tuple[CaseSubmission, DatasetRegistry, dict[str, Any]]:
    case_root = case_root.resolve(strict=True)
    input_root = (case_root / "01_model_input").resolve(strict=True)
    if input_root.parent != case_root or input_root.name != "01_model_input":
        raise ValueError("model input must be the case-local 01_model_input directory")
    profile_path = input_root / "case_profile.json"
    case = CaseSubmission.model_validate_json(profile_path.read_text(encoding="utf-8"))
    if not case.dataset_refs:
        raise ValueError("case has no registered dataset reference")
    main_refs = [item for item in case.dataset_refs if item.role == "main"]
    if len(main_refs) != 1:
        raise ValueError("case must register exactly one main dataset reference")

    registry = DatasetRegistry(registry_path)
    datasets: list[dict[str, Any]] = []
    for dataset_ref in case.dataset_refs:
        source_path = (input_root / dataset_ref.filename).resolve(strict=True)
        if source_path.parent != input_root:
            raise ValueError("dataset reference escapes 01_model_input")
        actual_size = source_path.stat().st_size
        actual_sha256 = _sha256(source_path)
        if actual_size != dataset_ref.size_bytes or actual_sha256 != dataset_ref.sha256:
            raise ValueError(
                f"dataset identity mismatch for {dataset_ref.dataset_id}"
            )
        registry.register(dataset_ref, source_path)
        datasets.append(
            {
                "dataset_id": dataset_ref.dataset_id,
                "filename": dataset_ref.filename,
                "role": dataset_ref.role,
                "sha256": actual_sha256,
                "size_bytes": actual_size,
            }
        )

    manifest = {
        "case_id": case.case_id,
        "benchmark_track": (
            case.design_envelope.benchmark_track
            if case.design_envelope is not None
            else None
        ),
        "case_profile_sha256": _sha256(profile_path),
        "model_input_root": str(input_root),
        "hidden_reference_access": "denied_by_runner",
        "datasets": datasets,
    }
    return case, registry, manifest


def _manuscript_markdown(state: RunState) -> str | None:
    if "manuscript_package" not in state.artifacts:
        return None
    package = _artifact_payload(state, "manuscript_package")
    if not isinstance(package, dict):
        return None
    sections = package.get("manuscript_sections") or []
    rendered = []
    for section in sections:
        if not isinstance(section, dict) or section.get("status") != "generated":
            continue
        rendered.append(
            f"# {section.get('title') or section.get('section_id')}\n\n"
            f"{str(section.get('content_markdown') or '').strip()}"
        )
    return "\n\n".join(rendered).strip() or None


def _summary_output(
    state: RunState | None,
    *,
    case: CaseSubmission,
    run_id: str,
    manifest: dict[str, Any],
    elapsed_seconds: float,
    error: str | None,
) -> dict[str, Any]:
    artifacts = {}
    usage: dict[str, Any] = {}
    method_family = None
    if state is not None:
        artifacts = {
            key: str(value.get("sha256"))
            for key, value in state.artifacts.items()
            if isinstance(value, dict) and value.get("sha256")
        }
        if "model_usage" in state.artifacts:
            payload = _artifact_payload(state, "model_usage")
            if isinstance(payload, dict):
                usage = payload
        if "analysis_plan" in state.artifacts:
            payload = _artifact_payload(state, "analysis_plan")
            if isinstance(payload, dict):
                method_family = payload.get("method_family")
    expected_budget_mode = str(manifest.get("model_budget_mode", "legacy"))
    expected_provider_ceiling = {
        "legacy": 20,
        "v2": V2_PROVIDER_ATTEMPT_BUDGET,
        "v3": V3_PROVIDER_ATTEMPT_BUDGET,
    }.get(expected_budget_mode, 20)
    expected_logical_ceiling = {
        "legacy": None,
        "v2": V2_LOGICAL_CALL_BUDGET,
        "v3": V3_LOGICAL_CALL_BUDGET,
    }.get(expected_budget_mode)
    return {
        "schema_version": "case-benchmark-output-v1",
        "system_id": "hypoweaver_code_first",
        "case_id": case.case_id,
        "benchmark_track": manifest.get("benchmark_track"),
        "requested_run_id": run_id,
        "workflow_run_id": state.id if state is not None else None,
        "run_status": (
            "completed" if state is not None and state.status == "completed" else "failed"
        ),
        "workflow_status": state.status if state is not None else "not_created",
        "execution_status": state.execution_status if state is not None else "not_started",
        "scientific_status": state.scientific_status if state is not None else "not_evaluated",
        "method_family": method_family,
        "failure": error,
        "execution_cost": {
            "budget_mode": str(
                usage.get("budget_mode", expected_budget_mode)
            ),
            "provider_attempt_ceiling": int(
                usage.get(
                    "provider_attempt_ceiling",
                    usage.get("max_calls", expected_provider_ceiling),
                )
                or 0
            ),
            "logical_call_ceiling": (
                int(usage["logical_call_ceiling"])
                if usage.get("logical_call_ceiling") is not None
                else expected_logical_ceiling
            ),
            "provider_attempt_counting_unit": str(
                usage.get("provider_attempt_counting_unit", "provider_request")
            ),
            "logical_call_counting_unit": str(
                usage.get(
                    "logical_call_counting_unit",
                    "distinct_logical_call_id",
                )
            ),
            "logical_calls": int(usage.get("logical_calls", 0) or 0),
            "provider_attempts": int(
                usage.get("provider_attempts", usage.get("llm_calls", 0)) or 0
            ),
            "llm_calls": int(usage.get("llm_calls", 0) or 0),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "model_wall_time_seconds": float(
                usage.get("wall_time_seconds", 0) or 0
            ),
            "total_wall_time_seconds": elapsed_seconds,
            "technical_failures": list(usage.get("technical_failures", [])),
            "call_receipts": list(usage.get("call_receipts", [])),
            "group_limits": dict(usage.get("group_limits", {})),
            "group_usage": dict(usage.get("group_usage", {})),
            "group_counting_unit": str(
                usage.get(
                    "group_counting_unit",
                    (
                        "logical_call"
                        if expected_budget_mode in {"v2", "v3"}
                        else "provider_attempt"
                    ),
                )
            ),
            "max_attempts_per_logical_call": int(
                (usage.get("shared_retry_policy") or {}).get(
                    "max_attempts_per_logical_call", 3
                )
                or 3
            ),
        },
        "artifact_sha256": artifacts,
        "input_manifest": manifest,
    }


def export_run(
    run_dir: Path,
    *,
    state: RunState | None,
    output: dict[str, Any],
) -> None:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        _write_json(run_dir / "run_state.json", state.model_dump(mode="json"))
        for key in sorted(state.artifacts):
            payload = _artifact_payload(state, key)
            _write_json(artifacts_dir / f"{key}.json", payload)
        manuscript = _manuscript_markdown(state)
        if manuscript is not None:
            (run_dir / "manuscript.md").write_text(
                manuscript + "\n", encoding="utf-8"
            )
    _write_json(run_dir / "benchmark_output.json", output)


async def run_case(
    *,
    case_root: Path,
    output_root: Path,
    registry_path: Path,
    run_id: str,
    v2_model_budget: bool = False,
    v3_model_budget: bool = False,
) -> tuple[Path, dict[str, Any]]:
    case, registry, manifest = prepare_case(
        case_root,
        registry_path=registry_path,
    )
    if v2_model_budget and v3_model_budget:
        raise ValueError("v2 and v3 model budget modes are mutually exclusive")
    budget_mode = (
        "v3" if v3_model_budget else ("v2" if v2_model_budget else "legacy")
    )
    manifest["model_budget_mode"] = budget_mode
    runtime_config_store = RuntimeConfigStore()
    forced_model = (
        runtime_config_store.resolve().qwen_model
        if v3_model_budget
        else None
    )
    manifest["forced_model"] = forced_model
    run_dir = (output_root / case.case_id / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(run_dir / "input_manifest.json", manifest)

    repository = RunRepository(run_dir / "hypoweaver.db")
    engine = WorkflowEngine(
        repository,
        dataset_registry=registry,
        runtime_config_store=runtime_config_store,
        model_call_budget_mode=budget_mode,
        forced_model=forced_model,
    )
    request = CreateRunRequest(
        mode="research",
        model_provider="qwen",
        execution_mode="external",
        case=case,
    )
    started = time.monotonic()
    state: RunState | None = None
    failure: str | None = None
    try:
        state = await _drive_hypoweaver(engine, request)
    except Exception as exc:  # terminal failure must still produce auditable output
        failure = f"{type(exc).__name__}: {exc}"
        states = repository.list()
        if len(states) > 1:
            failure = (
                f"{failure}; isolated run repository contains multiple RunState records"
            )
            state = None
        elif states:
            state = engine.get_run(states[0].id)
        else:
            state = None
    elapsed = time.monotonic() - started
    output = _summary_output(
        state,
        case=case,
        run_id=run_id,
        manifest=manifest,
        elapsed_seconds=elapsed,
        error=failure or (state.last_error if state is not None else None),
    )
    export_run(run_dir, state=state, output=output)
    if failure is not None:
        _write_json(
            run_dir / "workflow_failure.json",
            {
                "case_id": case.case_id,
                "requested_run_id": run_id,
                "workflow_run_id": state.id if state is not None else None,
                "error": failure,
            },
        )
    return run_dir, output


def _refresh_h3_claim_gate(
    engine: WorkflowEngine,
    state: RunState,
) -> RunState:
    _require_gate(state, "H3")
    plan = engine._artifact(state, "analysis_plan", AnalysisPlan)
    research_run = engine._artifact(state, "research_run", ResearchRun)
    candidate = engine._artifact(
        state,
        "candidate_claim_ledger",
        ClaimLedger,
    )
    contract = engine._artifact(
        state,
        "formal_research_contract",
        FormalResearchContract,
    )
    package = engine._artifact(state, "research_package", ResearchPackage)
    reproduction = engine._artifact(
        state,
        "reproduction_audit",
        ReproductionAudit,
    )
    scientific_audit = engine._artifact(
        state,
        "scientific_audit",
        ScientificAudit,
    )
    registry_claims = code_owned_claims_for_registry(
        candidate,
        plan,
        package.hypotheses,
    )
    evidence_registry = build_evidence_registry(
        plan,
        research_run,
        registry_claims,
        reproduction_audit=reproduction,
        scientific_audit=scientific_audit,
    )
    ledger, gate_report = apply_claim_gate(
        candidate,
        plan,
        research_run,
        evidence_registry,
        package.hypotheses,
        contract=contract,
        reproduction_audit=reproduction,
        scientific_audit=scientific_audit,
        research_package=package,
    )
    engine._put_artifact(state, "evidence_registry", evidence_registry)
    engine._put_artifact(state, "claim_gate_report", gate_report)
    engine._put_artifact(state, "claim_ledger", ledger)
    state.claims = ledger.claims
    state.last_error = None
    engine._record_step(
        state,
        "claim_gate_technical_repair",
        "succeeded",
        input_value={
            "source_candidate_ledger": candidate.ledger_id,
            "repair_scope": [
                "robustness_target_term_binding",
                "negated_causal_disclaimer",
            ],
        },
        output_value=gate_report,
        logs=[
            "重新运行纯确定性 Claim Gate；未重新估计、未修改冻结计划、未调用模型。"
        ],
    )
    return engine.repository.save(state, expected_version=state.version)


async def resume_h3_run(
    *,
    run_dir: Path,
    registry_path: Path,
) -> tuple[Path, dict[str, Any]]:
    run_dir = run_dir.resolve(strict=True)
    previous_output_path = run_dir / "benchmark_output.json"
    previous_output = json.loads(previous_output_path.read_text(encoding="utf-8"))
    pre_repair_path = run_dir / "pre_repair_benchmark_output.json"
    if not pre_repair_path.exists():
        shutil.copyfile(previous_output_path, pre_repair_path)

    repository = RunRepository(run_dir / "hypoweaver.db")
    states = repository.list()
    if len(states) != 1:
        raise ValueError("resume requires exactly one isolated RunState")
    registry = DatasetRegistry(registry_path)
    engine = WorkflowEngine(
        repository,
        dataset_registry=registry,
        runtime_config_store=RuntimeConfigStore(),
    )
    state = engine.get_run(states[0].id)
    started = time.monotonic()
    failure: str | None = None
    try:
        state = _refresh_h3_claim_gate(engine, state)
        ledger = engine._artifact(state, "claim_ledger", ClaimLedger)
        decisions = [_claim_decision(claim) for claim in ledger.claims]
        h3_action = (
            "approve"
            if any(item.decision in {"approve", "downgrade"} for item in decisions)
            else "generate_identification_failure_report"
        )
        state = await engine.decide_gate(
            state.id,
            "H3",
            _gate_request(
                state,
                "H3",
                action=h3_action,
                claims=decisions,
                suffix="technical-repair",
            ),
        )
        if state.status == "failed" and state.current_node_id == "scientific_writer":
            state = await engine.retry_writing(state.id)
        _require_gate(state, "H4")
        manuscript = engine._artifact(
            state,
            "manuscript_package",
            ManuscriptPackage,
        )
        if manuscript.audit_result != "pass_with_no_critical_issues":
            raise RuntimeError("repaired manuscript failed deterministic audit")
        state = await engine.decide_gate(
            state.id,
            "H4",
            _gate_request(
                state,
                "H4",
                action="approve",
                suffix="technical-repair",
            ),
        )
        if state.status != "completed":
            raise RuntimeError("repaired run did not reach completed state")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        state = engine.get_run(state.id)

    elapsed = time.monotonic() - started
    manifest = json.loads((run_dir / "input_manifest.json").read_text(encoding="utf-8"))
    requested_run_id = str(previous_output.get("requested_run_id") or run_dir.name)
    output = _summary_output(
        state,
        case=state.case_submission,
        run_id=requested_run_id,
        manifest=manifest,
        elapsed_seconds=(
            float(
                previous_output.get("execution_cost", {}).get(
                    "total_wall_time_seconds",
                    0,
                )
                or 0
            )
            + elapsed
        ),
        error=failure or (state.last_error if state.status != "completed" else None),
    )
    output["technical_repair"] = {
        "resumed_from_gate": "H3",
        "reused_frozen_estimates": True,
        "reused_independent_reproduction": True,
        "model_design_repeated": False,
        "pre_repair_output": pre_repair_path.name,
    }
    export_run(run_dir, state=state, output=output)
    _write_json(
        run_dir / "technical_repair.json",
        {
            "status": "completed" if state.status == "completed" else "failed",
            "original_failure": previous_output.get("failure"),
            "repair_scope": [
                "robustness_target_term_binding",
                "negated_causal_disclaimer",
            ],
            "reestimated": False,
            "design_model_calls_repeated": False,
            "resume_wall_time_seconds": elapsed,
            "failure": failure,
        },
    )
    return run_dir, output


def _copy_once(source: Path, destination: Path) -> None:
    if source.is_file() and not destination.exists():
        shutil.copyfile(source, destination)


def _writer_recovery_frozen_hashes(state: RunState) -> dict[str, str]:
    return {
        key: str(state.artifacts[key]["sha256"])
        for key in WRITER_RECOVERY_FROZEN_ARTIFACTS
        if key in state.artifacts
    }


def _require_writer_recovery_state(state: RunState) -> None:
    if (
        state.status != "failed"
        or state.current_node_id != "scientific_writer"
        or state.current_gate is not None
    ):
        raise ValueError(
            "writer resume requires failed/scientific_writer with no open gate"
        )
    if state.mode != "research" or state.plan_only:
        raise ValueError("writer resume requires a real research Run")
    required = {
        "research_package",
        "analysis_plan",
        "formal_research_contract",
        "research_run",
        "reproduction_audit",
        "approved_claim_ledger",
    }
    missing = sorted(required - set(state.artifacts))
    if missing:
        raise ValueError(
            "writer resume is missing frozen artifacts: " + ", ".join(missing)
        )
    if "model_usage" in state.artifacts:
        usage = _artifact_payload(state, "model_usage")
        if not isinstance(usage, dict):
            raise ValueError("writer resume model_usage is malformed")
        receipts = usage.get("call_receipts") or []
        if int(usage.get("llm_calls", 0) or 0) != len(receipts):
            raise ValueError(
                "writer resume requires complete model call receipts"
            )


async def resume_writer_run(
    *,
    run_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Recover H4 with code-owned prose while preserving all frozen evidence."""

    run_dir = run_dir.resolve(strict=True)
    previous_output_path = run_dir / "benchmark_output.json"
    previous_output = json.loads(previous_output_path.read_text(encoding="utf-8"))
    database_path = run_dir / "hypoweaver.db"
    if not database_path.is_file():
        raise ValueError("writer resume requires the existing hypoweaver.db")
    repository = RunRepository(database_path)
    states = repository.list()
    if len(states) != 1:
        raise ValueError("writer resume requires exactly one isolated RunState")
    engine = _WriterRecoveryWorkflowEngine(repository)
    state = engine.get_run(states[0].id)
    _require_writer_recovery_state(state)

    pre_repair_output_path = run_dir / "pre_writer_repair_benchmark_output.json"
    pre_repair_state_path = run_dir / "pre_writer_repair_run_state.json"
    pre_repair_failure_path = run_dir / "pre_writer_repair_workflow_failure.json"
    _copy_once(previous_output_path, pre_repair_output_path)
    if not pre_repair_state_path.exists():
        _write_json(pre_repair_state_path, state.model_dump(mode="json"))
    _copy_once(run_dir / "workflow_failure.json", pre_repair_failure_path)

    original_failure = state.last_error or previous_output.get("failure")
    frozen_before = _writer_recovery_frozen_hashes(state)
    steps_before = len(state.steps)
    state.last_error = original_failure
    engine._record_step(
        state,
        "writer_technical_repair",
        "succeeded",
        input_value={
            "failed_node": "scientific_writer",
            "original_failure": original_failure,
            "frozen_artifact_sha256": frozen_before,
        },
        output_value={
            "repair_mode": "code_owned_writer_recovery",
            "qwen_calls_repeated": False,
            "estimation_repeated": False,
            "reproduction_repeated": False,
        },
        logs=[
            "H4 技术恢复只重建论文章节；H1/H2、估计、独立复算与 H3 授权保持封存。"
        ],
    )
    state.status = "failed"
    state.current_node_id = "scientific_writer"
    state.current_gate = None
    state.last_error = original_failure
    state = repository.save(state, expected_version=state.version)

    started = time.monotonic()
    failure: str | None = None
    try:
        state = await engine.retry_writing(state.id)
        _require_gate(state, "H4")
        if _writer_recovery_frozen_hashes(state) != frozen_before:
            raise RuntimeError(
                "writer recovery changed a frozen pre-H4 artifact"
            )
        manuscript = engine._artifact(
            state,
            "manuscript_package",
            ManuscriptPackage,
        )
        if manuscript.audit_result != "pass_with_no_critical_issues":
            raise RuntimeError(
                "writer recovery manuscript failed deterministic audit"
            )
        state = await engine.decide_gate(
            state.id,
            "H4",
            _gate_request(
                state,
                "H4",
                action="approve",
                suffix="writer-technical-repair",
            ),
        )
        if state.status != "completed":
            raise RuntimeError("writer recovery did not reach completed state")
        if _writer_recovery_frozen_hashes(state) != frozen_before:
            raise RuntimeError(
                "writer recovery changed a frozen pre-H4 artifact"
            )
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        state = engine.get_run(state.id)

    elapsed = time.monotonic() - started
    new_steps = state.steps[steps_before:]
    new_step_ids = [step.node_id for step in new_steps]
    fallback_section_ids = sorted(
        {
            str(step.input["section_id"])
            for step in new_steps
            if isinstance(step.input, dict)
            and step.input.get("fallback_type")
            == "deterministic_safe_fallback"
            and step.input.get("section_id")
        }
    )
    allowed_step_ids = {
        "writer_technical_repair",
        "scientific_writer",
        "manuscript_ir_compile",
        "consistency_audit",
        "h4_gate",
        "complete",
    }
    unexpected_steps = sorted(set(new_step_ids) - allowed_step_ids)
    if unexpected_steps and failure is None:
        failure = (
            "RuntimeError: writer recovery executed prohibited stages: "
            + ", ".join(unexpected_steps)
        )

    manifest = json.loads(
        (run_dir / "input_manifest.json").read_text(encoding="utf-8")
    )
    requested_run_id = str(previous_output.get("requested_run_id") or run_dir.name)
    output = _summary_output(
        state,
        case=state.case_submission,
        run_id=requested_run_id,
        manifest=manifest,
        elapsed_seconds=(
            float(
                previous_output.get("execution_cost", {}).get(
                    "total_wall_time_seconds",
                    0,
                )
                or 0
            )
            + elapsed
        ),
        error=failure or (state.last_error if state.status != "completed" else None),
    )
    if failure is not None:
        output["run_status"] = "failed"
    repair_summary = {
        "resumed_from_node": "scientific_writer",
        "repair_mode": "code_owned_writer_recovery",
        "reused_frozen_estimates": True,
        "reused_independent_reproduction": True,
        "reused_approved_claim_ledger": True,
        "model_design_repeated": False,
        "qwen_writer_calls_repeated": False,
        "deterministic_fallback_section_ids": fallback_section_ids,
        "pre_repair_output": pre_repair_output_path.name,
        "pre_repair_state": pre_repair_state_path.name,
    }
    previous_repairs = previous_output.get("technical_repairs")
    if not isinstance(previous_repairs, list):
        previous_repair = previous_output.get("technical_repair")
        previous_repairs = [previous_repair] if isinstance(previous_repair, dict) else []
    output["technical_repair"] = repair_summary
    output["technical_repairs"] = [*previous_repairs, repair_summary]
    export_run(run_dir, state=state, output=output)

    repair_record = {
        "status": "completed" if state.status == "completed" else "failed",
        "original_failure": original_failure,
        "repair_scope": ["scientific_writer", "manuscript_ir", "H4"],
        "repair_mode": "code_owned_writer_recovery",
        "frozen_artifact_sha256_before": frozen_before,
        "frozen_artifact_sha256_after": _writer_recovery_frozen_hashes(state),
        "new_step_ids": new_step_ids,
        "deterministic_fallback_section_ids": fallback_section_ids,
        "estimation_repeated": False,
        "reproduction_repeated": False,
        "design_model_calls_repeated": False,
        "qwen_writer_calls_repeated": False,
        "resume_wall_time_seconds": elapsed,
        "failure": failure,
    }
    _write_json(run_dir / "writer_technical_repair.json", repair_record)
    if failure is not None:
        _write_json(
            run_dir / "writer_workflow_failure.json",
            {
                "case_id": state.case_id,
                "requested_run_id": requested_run_id,
                "workflow_run_id": state.id,
                "error": failure,
            },
        )
    return run_dir, output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one frozen benchmark input view through HypoWeaver."
    )
    parser.add_argument("--case-root", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--registry-path", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--v2-model-budget",
        action="store_true",
        help=(
            "use the frozen v2 budget: 40 provider attempts, 20 logical calls, "
            "and logical-call stage caps"
        ),
    )
    parser.add_argument(
        "--v3-model-budget",
        action="store_true",
        help=(
            "use the frozen benchmark-v3 envelope: 80 provider attempts, "
            "20 logical calls, and at most three attempts per logical call"
        ),
    )
    parser.add_argument("--resume-h3-run-dir", type=Path, default=None)
    parser.add_argument("--resume-writer-run-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.resume_writer_run_dir is not None:
        run_dir, output = asyncio.run(
            resume_writer_run(run_dir=args.resume_writer_run_dir)
        )
        print(run_dir / "benchmark_output.json")
        return 0 if output["run_status"] == "completed" else 2
    if args.resume_h3_run_dir is not None:
        if args.registry_path is None:
            raise SystemExit("--registry-path is required for H3 recovery")
        run_dir, output = asyncio.run(
            resume_h3_run(
                run_dir=args.resume_h3_run_dir,
                registry_path=args.registry_path,
            )
        )
        print(run_dir / "benchmark_output.json")
        return 0 if output["run_status"] == "completed" else 2
    if args.case_root is None:
        raise SystemExit("--case-root is required for validation or a new run")
    if args.registry_path is None:
        raise SystemExit("--registry-path is required for validation or a new run")
    if args.validate_only:
        case, _, manifest = prepare_case(
            args.case_root,
            registry_path=args.registry_path,
        )
        print(
            json.dumps(
                {"status": "valid", "case_id": case.case_id, "manifest": manifest},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.output_root is None:
        raise SystemExit("--output-root is required for a new run")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir, output = asyncio.run(
        run_case(
            case_root=args.case_root,
            output_root=args.output_root,
            registry_path=args.registry_path,
            run_id=run_id,
            v2_model_budget=args.v2_model_budget,
            v3_model_budget=args.v3_model_budget,
        )
    )
    print(run_dir / "benchmark_output.json")
    return 0 if output["run_status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
