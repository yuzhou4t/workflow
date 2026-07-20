from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from .benchmark_models import (
    ABLATION_NATIVE_ARTIFACTS,
    BenchmarkDeliveryManifest,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    FrozenBenchmarkProtocol,
    OfficialAttemptBinding,
    PairedEvaluationRequest,
    PairedReviewSummary,
)
from .benchmark_packets import (
    build_agent_laboratory_packet,
    build_hypoweaver_packet,
)
from .benchmark_protocol import (
    begin_official_attempt,
    bind_official_packet_receipts,
    create_official_call_receipt,
    fail_official_attempt,
    freeze_protocol,
    hash_protocol_artifacts,
    load_official_attempt_binding,
    run_benchmark_delivery,
    verify_protocol,
)
from .benchmark_runner import AgentLaboratoryRunner, BaselineRunRequest
from .case_import import DatasetRegistry
from .engine import (
    DESIGN_RETRY_MODEL,
    REVIEWER_MODEL,
    WRITER_ESCALATION_MODEL,
    WorkflowEngine,
)
from .models import (
    AnalysisPlan,
    ClaimDecisionInput,
    ClaimLedger,
    ClaimRecord,
    CreateRunRequest,
    FormalResearchContract,
    GateDecisionRequest,
    ManuscriptPackage,
    ReproductionAudit,
    ResearchRun,
    RunState,
    StrictModel,
)
from .paired_blind import PairedBlindEngine
from .paired_blind_repository import PairedBlindRepository
from .qwen_single_pass_runner import QwenSinglePassRunner
from .research_api import runtime_identity as research_runtime_identity
from .repository import RunRepository
from .runtime_config import FrozenRuntimeConfigStore, RuntimeConfigStore
from .seal import canonical_sha256


class OfficialBenchmarkConfiguration(StrictModel):
    artifact_root: str
    protocol_path: str
    visible_input_path: str
    reference_path: str
    reference_summary_path: str
    runtime_public_path: str
    source_artifact_paths: dict[str, list[str]]
    configuration_artifact_paths: list[str]
    output_dir: str
    working_dir: str
    official_state_root: str
    agent_laboratory_root: str
    agent_timeout_seconds: int = Field(default=1800, ge=1)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=30)
    enforce_executable_source_coverage: bool = False

    @model_validator(mode="after")
    def validate_relative_artifacts(self) -> "OfficialBenchmarkConfiguration":
        relative_values = [
            self.protocol_path,
            self.visible_input_path,
            self.reference_path,
            self.reference_summary_path,
            self.runtime_public_path,
            self.agent_laboratory_root,
            *self.configuration_artifact_paths,
            *[
                value
                for values in self.source_artifact_paths.values()
                for value in values
            ],
        ]
        for value in relative_values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    "repository artifacts must be normalized paths relative to artifact_root"
                )
        if set(self.source_artifact_paths) != {
            "hypoweaver",
            "agent_laboratory",
            "benchmark_harness",
        }:
            raise ValueError("source_artifact_paths must contain the three benchmark systems")
        required_configuration = {
            self.visible_input_path,
            self.reference_path,
            self.reference_summary_path,
            self.runtime_public_path,
        }
        if not required_configuration.issubset(
            set(self.configuration_artifact_paths)
        ):
            raise ValueError(
                "visible input, reference, summary, and public runtime config must be frozen"
            )
        return self

    def resolve_artifact(self, relative_path: str) -> Path:
        root = Path(self.artifact_root).resolve(strict=True)
        candidate = (root / relative_path).resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise ValueError("benchmark artifact escapes artifact_root")
        return candidate


class OfficialSystemRunner(Protocol):
    async def preflight(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
    ) -> None: ...

    async def run_qwen_single_pass(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
        binding: OfficialAttemptBinding,
    ) -> BenchmarkPacket: ...

    async def run_agent_laboratory(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
        binding: OfficialAttemptBinding,
    ) -> BenchmarkPacket: ...

    async def run_hypoweaver(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
        binding: OfficialAttemptBinding,
    ) -> BenchmarkPacket: ...

    async def run_blind_reviews(
        self,
        hypoweaver_packet: BenchmarkPacket,
        agent_laboratory_packet: BenchmarkPacket,
        reference_summary: str,
        binding: OfficialAttemptBinding,
    ) -> PairedReviewSummary: ...


class LocalOfficialSystemRunner:
    """Production adapters. Hidden reference material enters only run_blind_reviews."""

    def __init__(self, configuration: OfficialBenchmarkConfiguration) -> None:
        self.configuration = configuration
        self.runtime_config = RuntimeConfigStore()
        self.frozen_runtime_config: FrozenRuntimeConfigStore | None = None
        self.visible_input_path = configuration.resolve_artifact(
            configuration.visible_input_path
        )
        self.reference_path = configuration.resolve_artifact(
            configuration.reference_path
        )
        self.reference_summary_path = configuration.resolve_artifact(
            configuration.reference_summary_path
        )
        self.runtime_public_path = configuration.resolve_artifact(
            configuration.runtime_public_path
        )
        self.working_dir = Path(configuration.working_dir)
        self.qwen_runner: QwenSinglePassRunner | None = None
        self.agent_runner: AgentLaboratoryRunner | None = None
        self.workflow_engine: WorkflowEngine | None = None
        self.paired_engine: PairedBlindEngine | None = None

    async def preflight(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
    ) -> None:
        config = self.runtime_config.resolve()
        if not config.qwen_api_key:
            raise RuntimeError("Qwen API Key is required for the official benchmark")
        if not config.research_engine_url:
            raise RuntimeError("Research Engine URL is required for the official benchmark")
        expected_runtime = json.loads(
            self.runtime_public_path.read_text(encoding="utf-8")
        )
        expected_research_identity = research_runtime_identity()
        actual_runtime = {
            "qwen_model": config.qwen_model,
            "qwen_base_url": config.qwen_base_url,
            "qwen_review_model": os.getenv("QWEN_REVIEW_MODEL")
            or config.qwen_model,
            "hypoweaver_reviewer_model": REVIEWER_MODEL,
            "hypoweaver_design_retry_model": DESIGN_RETRY_MODEL,
            "hypoweaver_writer_escalation_model": WRITER_ESCALATION_MODEL,
            "research_engine_url": config.research_engine_url,
            "python_environment_sha256": expected_research_identity[
                "environment_sha256"
            ],
            "agent_laboratory_upstream_commit": (
                "d9017d90e329112d2a80b7712f37ee9094d2cd27"
            ),
            "agent_laboratory_max_calls": 20,
            "agent_laboratory_external_collection": "prohibited",
            "generated_code_isolation": "macos-sandbox-exec-deny-network",
        }
        if expected_runtime != actual_runtime:
            raise RuntimeError("public runtime configuration differs from the frozen benchmark")
        self.frozen_runtime_config = FrozenRuntimeConfigStore(config)
        trust_env = urlsplit(config.research_engine_url).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        async with httpx.AsyncClient(timeout=10, trust_env=trust_env) as client:
            response = await client.get(
                f"{config.research_engine_url.rstrip('/')}/v1/health"
            )
            response.raise_for_status()
            health = response.json()
        expected_health = {
            "status": "ok",
            **expected_research_identity,
        }
        if health != expected_health:
            raise RuntimeError(
                "Research Engine identity differs from the frozen implementation"
            )
        if self.working_dir.exists() and any(self.working_dir.iterdir()):
            raise RuntimeError("official benchmark working directory must be empty")
        self.working_dir.mkdir(parents=True, exist_ok=True)
        if case_request.case is None:
            raise ValueError("official benchmark requires an explicit case")
        registry = DatasetRegistry()
        for dataset_ref in case_request.case.dataset_refs:
            source = registry.resolve(dataset_ref)
            if _file_sha256(source) != dataset_ref.sha256:
                raise ValueError("registered benchmark dataset sha256 mismatch")
        frozen_runtime = self.frozen_runtime_config
        self.qwen_runner = QwenSinglePassRunner(config_store=frozen_runtime)
        self.agent_runner = AgentLaboratoryRunner(
            root=self.working_dir / "agent-laboratory",
            agent_lab_root=self.configuration.resolve_artifact(
                self.configuration.agent_laboratory_root
            ),
            registry=registry,
            config_store=frozen_runtime,
            forbidden_read_paths=(
                self.reference_path,
                self.reference_summary_path,
            ),
            process_timeout_seconds=max(
                1,
                self.configuration.agent_timeout_seconds - 30,
            ),
            max_llm_calls=protocol.call_budget.agent_laboratory_max_calls,
            max_steps=3,
        )
        self.agent_runner.verify_preflight()
        self.workflow_engine = WorkflowEngine(
            RunRepository(self.working_dir / "hypoweaver.db"),
            dataset_registry=registry,
            runtime_config_store=frozen_runtime,
        )
        self.paired_engine = PairedBlindEngine(
            PairedBlindRepository(self.working_dir / "paired-blind.db"),
            config_store=frozen_runtime,
            review_model_override=str(expected_runtime["qwen_review_model"]),
        )

    async def run_qwen_single_pass(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
        binding: OfficialAttemptBinding,
    ) -> BenchmarkPacket:
        if self.qwen_runner is None:
            raise RuntimeError("official runner preflight was not completed")
        result = await self.qwen_runner.run(
            packet_id=f"qwen-single-{binding.attempt_id[:16]}",
            case_id=protocol.case_id,
            data_sha256=protocol.data_sha256,
            visible_input_path=self.visible_input_path,
        )
        if result.status != "completed" or result.packet is None:
            raise RuntimeError("QwenSinglePassFailed")
        metadata = result.metadata
        if metadata.raw_response_sha256 is None:
            raise RuntimeError("Qwen single-pass response receipt is missing")
        receipt = create_official_call_receipt(
            binding,
            provider=metadata.provider,
            model=metadata.model,
            raw_response_sha256=metadata.raw_response_sha256,
            call_started_at=metadata.call_started_at,
            call_completed_at=metadata.call_completed_at,
        )
        return bind_official_packet_receipts(result.packet, [receipt])

    async def run_agent_laboratory(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
        binding: OfficialAttemptBinding,
    ) -> BenchmarkPacket:
        if self.agent_runner is None or case_request.case is None:
            raise RuntimeError("official runner preflight was not completed")
        state = self.agent_runner.start(
            BaselineRunRequest(
                case=case_request.case,
                execute_generated_code=True,
            )
        )
        deadline = time.monotonic() + self.configuration.agent_timeout_seconds
        while state.status not in {"completed", "failed"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("Agent Laboratory official run timed out")
            await asyncio.sleep(self.configuration.poll_interval_seconds)
            state = self.agent_runner.get(state.id)
        if state.status == "completed":
            artifacts = self.agent_runner.load_completed_artifacts(state.id)
        elif state.status == "failed":
            artifacts = self.agent_runner.load_terminal_failure_artifacts(
                state.id
            )
        else:
            raise RuntimeError("AgentLaboratoryMissingTerminalState")
        provenance = artifacts.output.get("provenance") or {}
        if (
            artifacts.output.get("system_id")
            != "agent_laboratory_upstream_original"
            or not isinstance(provenance, dict)
            or provenance.get("workflow_variant")
            != "upstream_laboratory_workflow"
            or provenance.get("upstream_entrypoint")
            != "ai_lab_repo.LaboratoryWorkflow.perform_research"
        ):
            raise RuntimeError("Agent Laboratory is not the frozen upstream workflow")
        packet = build_agent_laboratory_packet(
            packet_id=f"agent-laboratory-{binding.attempt_id[:16]}",
            output=artifacts.output,
            visible_input_sha256=protocol.visible_input_sha256,
            data_sha256=protocol.data_sha256,
            report_text=artifacts.report_text,
        )
        native_artifact_sha256 = {
            **packet.native_artifact_sha256,
            "benchmark_output": artifacts.output_sha256,
        }
        if artifacts.report_sha256 is not None:
            native_artifact_sha256["report"] = artifacts.report_sha256
        packet = packet.model_copy(
            update={
                "native_artifact_sha256": native_artifact_sha256,
                "packet_sha256": None,
            }
        )
        receipts = _receipts_from_usage(
            binding,
            artifacts.output.get("execution_cost") or {},
        )
        if len(receipts) != packet.resource_usage.llm_calls:
            raise RuntimeError("Agent Laboratory call receipts are incomplete")
        return bind_official_packet_receipts(packet, receipts)

    async def run_hypoweaver(
        self,
        case_request: CreateRunRequest,
        protocol: FrozenBenchmarkProtocol,
        binding: OfficialAttemptBinding,
    ) -> BenchmarkPacket:
        if self.workflow_engine is None:
            raise RuntimeError("official runner preflight was not completed")
        state = await _drive_hypoweaver(self.workflow_engine, case_request)
        plan = _artifact_model(state, "analysis_plan", AnalysisPlan)
        contract = _artifact_model(
            state, "formal_research_contract", FormalResearchContract
        )
        research_run = _artifact_model(state, "research_run", ResearchRun)
        claim_ledger = _artifact_model(
            state, "approved_claim_ledger", ClaimLedger
        )
        manuscript = _artifact_model(
            state, "manuscript_package", ManuscriptPackage
        )
        reproduction = _artifact_model(
            state, "reproduction_audit", ReproductionAudit
        )
        usage = _artifact_payload(state, "model_usage")
        resource_usage = BenchmarkResourceUsage(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            wall_time_seconds=float(usage.get("wall_time_seconds", 0) or 0),
            technical_failures=[
                str(value) for value in usage.get("technical_failures", [])
            ],
        )
        if resource_usage.llm_calls > protocol.call_budget.hypoweaver_max_calls:
            raise RuntimeError("HypoWeaver exceeded its official call budget")
        if self.frozen_runtime_config is None:
            raise RuntimeError("official runtime snapshot is unavailable")
        runtime = self.frozen_runtime_config.resolve()
        component_artifact_sha256 = _native_component_artifact_sha256(state)
        packet = build_hypoweaver_packet(
            packet_id=f"hypoweaver-{binding.attempt_id[:16]}",
            case_id=protocol.case_id,
            visible_input_sha256=protocol.visible_input_sha256,
            data_sha256=protocol.data_sha256,
            model_id=runtime.qwen_model,
            plan=plan,
            research_run=research_run,
            claim_ledger=claim_ledger,
            manuscript=manuscript,
            reproduction_audit=reproduction,
            formal_contract=contract,
            resource_usage=resource_usage,
            component_artifact_sha256=component_artifact_sha256,
        )
        sealed_output = _artifact_payload(state, "sealed_output")
        packet = packet.model_copy(
            update={
                "native_artifact_sha256": {
                    **packet.native_artifact_sha256,
                    "sealed_output": canonical_sha256(sealed_output),
                },
                "packet_sha256": None,
            }
        )
        receipts = _receipts_from_usage(binding, usage)
        if len(receipts) != resource_usage.llm_calls:
            raise RuntimeError("HypoWeaver call receipts are incomplete")
        return bind_official_packet_receipts(packet, receipts)

    async def run_blind_reviews(
        self,
        hypoweaver_packet: BenchmarkPacket,
        agent_laboratory_packet: BenchmarkPacket,
        reference_summary: str,
        binding: OfficialAttemptBinding,
    ) -> PairedReviewSummary:
        if self.paired_engine is None:
            raise RuntimeError("official runner preflight was not completed")
        view = await self.paired_engine.evaluate(
            PairedEvaluationRequest(
                packet_a=hypoweaver_packet,
                packet_b=agent_laboratory_packet,
                reference_summary=reference_summary,
                model_provider="qwen",
                official_attempt=binding,
            )
        )
        if view.status != "completed" or view.result is None:
            raise RuntimeError("PairedBlindReviewFailed")
        return view.result


class OfficialBenchmarkOrchestrator:
    def __init__(
        self,
        configuration: OfficialBenchmarkConfiguration,
        *,
        system_runner: OfficialSystemRunner | None = None,
    ) -> None:
        self.configuration = configuration
        self.system_runner = system_runner or LocalOfficialSystemRunner(configuration)

    async def run(self) -> BenchmarkDeliveryManifest:
        protocol, reference, case_request, reference_summary = self._preflight_inputs()
        await self.system_runner.preflight(case_request, protocol)
        output_dir = Path(self.configuration.output_dir)
        state_root = Path(self.configuration.official_state_root)
        begin_official_attempt(
            protocol,
            output_dir,
            state_root=state_root,
            artifact_root=Path(self.configuration.artifact_root),
        )
        try:
            binding = load_official_attempt_binding(output_dir)
            qwen_packet = await self.system_runner.run_qwen_single_pass(
                case_request, protocol, binding
            )
            agent_packet = await self.system_runner.run_agent_laboratory(
                case_request, protocol, binding
            )
            hypoweaver_packet = await self.system_runner.run_hypoweaver(
                case_request, protocol, binding
            )
            blind_summary = await self.system_runner.run_blind_reviews(
                hypoweaver_packet,
                agent_packet,
                reference_summary,
                binding,
            )
            return run_benchmark_delivery(
                protocol=protocol,
                reference=reference,
                qwen_packet=qwen_packet,
                agent_laboratory_packet=agent_packet,
                hypoweaver_packet=hypoweaver_packet,
                blind_summary=blind_summary,
                output_dir=output_dir,
                official=True,
                official_state_root=state_root,
            )
        except BaseException as error:
            fail_official_attempt(
                protocol,
                output_dir,
                error,
                state_root=state_root,
            )
            raise

    def _preflight_inputs(
        self,
    ) -> tuple[
        FrozenBenchmarkProtocol,
        BenchmarkReference,
        CreateRunRequest,
        str,
    ]:
        _assert_executable_source_coverage(self.configuration)
        protocol = FrozenBenchmarkProtocol.model_validate_json(
            self.configuration.resolve_artifact(
                self.configuration.protocol_path
            ).read_text(encoding="utf-8")
        )
        verify_protocol(protocol)
        reference = BenchmarkReference.model_validate_json(
            self.configuration.resolve_artifact(
                self.configuration.reference_path
            ).read_text(encoding="utf-8")
        )
        visible_path = self.configuration.resolve_artifact(
            self.configuration.visible_input_path
        )
        if _file_sha256(visible_path) != protocol.visible_input_sha256:
            raise ValueError("visible input does not match the frozen protocol")
        if canonical_sha256(reference.model_dump(mode="json")) != protocol.reference_sha256:
            raise ValueError("reference does not match the frozen protocol")
        case_request = CreateRunRequest.model_validate_json(
            visible_path.read_text(encoding="utf-8")
        )
        if (
            case_request.mode != "research"
            or case_request.model_provider != "qwen"
            or case_request.execution_mode != "external"
            or case_request.case is None
        ):
            raise ValueError("official visible input must request real Qwen/external execution")
        if (
            case_request.case.case_id != protocol.case_id
            or [item.sha256 for item in case_request.case.dataset_refs]
            != protocol.data_sha256
            or reference.case_id != protocol.case_id
            or reference.visible_input_sha256 != protocol.visible_input_sha256
            or reference.data_sha256 != protocol.data_sha256
        ):
            raise ValueError("official input identities are inconsistent")
        reference_summary = self.configuration.resolve_artifact(
            self.configuration.reference_summary_path
        ).read_text(encoding="utf-8").strip()
        if not reference_summary:
            raise ValueError("official reference summary is empty")
        return protocol, reference, case_request, reference_summary


def prepare_official_protocol(
    configuration: OfficialBenchmarkConfiguration,
) -> FrozenBenchmarkProtocol:
    _assert_executable_source_coverage(configuration)
    root = Path(configuration.artifact_root)
    visible_path = configuration.resolve_artifact(configuration.visible_input_path)
    reference_path = configuration.resolve_artifact(configuration.reference_path)
    reference = BenchmarkReference.model_validate_json(
        reference_path.read_text(encoding="utf-8")
    )
    case_request = CreateRunRequest.model_validate_json(
        visible_path.read_text(encoding="utf-8")
    )
    if case_request.case is None:
        raise ValueError("official protocol requires an explicit case")
    visible_sha256 = _file_sha256(visible_path)
    data_sha256 = [item.sha256 for item in case_request.case.dataset_refs]
    if (
        reference.case_id != case_request.case.case_id
        or reference.visible_input_sha256 != visible_sha256
        or reference.data_sha256 != data_sha256
    ):
        raise ValueError("reference identity does not match the visible case")
    source_sha256, configuration_sha256 = hash_protocol_artifacts(
        artifact_root=root,
        source_artifact_paths=configuration.source_artifact_paths,
        configuration_artifact_paths=configuration.configuration_artifact_paths,
    )
    protocol = FrozenBenchmarkProtocol(
        case_id=case_request.case.case_id,
        visible_input_sha256=visible_sha256,
        data_sha256=data_sha256,
        reference_sha256=canonical_sha256(reference.model_dump(mode="json")),
        source_sha256=source_sha256,
        configuration_sha256=configuration_sha256,
        source_artifact_paths=configuration.source_artifact_paths,
        configuration_artifact_paths=configuration.configuration_artifact_paths,
    )
    return freeze_protocol(
        protocol,
        configuration.resolve_artifact(configuration.protocol_path),
    )


async def _drive_hypoweaver(
    engine: WorkflowEngine,
    request: CreateRunRequest,
) -> RunState:
    state = await engine.create_run(request)
    _require_gate(state, "H1")
    state = await engine.decide_gate(
        state.id,
        "H1",
        _gate_request(state, "H1", action="approve"),
    )
    _require_gate(state, "H2")

    for index in range(2):
        arena = _artifact_payload(state, "design_arena")
        recommended = [str(value) for value in arena.get("recommended_candidate_ids", [])]
        provisional = str(arena.get("provisional_candidate_id") or "")
        selected = provisional if provisional in recommended else (sorted(recommended)[0] if recommended else "")
        if not selected:
            raise RuntimeError("HypoWeaver H2 has no recommended candidate")
        state = await engine.decide_gate(
            state.id,
            "H2",
            _gate_request(
                state,
                "H2",
                action="approve",
                selected_candidate_id=selected,
                suffix=str(index),
            ),
        )
        if state.status == "waiting_human" and state.current_gate == "H2":
            continue
        break
    _require_gate(state, "H3")

    for index in range(2):
        ledger = _artifact_model(state, "claim_ledger", ClaimLedger)
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
                suffix=str(index),
            ),
        )
        if state.status == "waiting_human" and state.current_gate == "H3":
            continue
        break
    _require_gate(state, "H4")
    manuscript = _artifact_model(state, "manuscript_package", ManuscriptPackage)
    if manuscript.audit_result != "pass_with_no_critical_issues":
        raise RuntimeError("HypoWeaver manuscript failed its deterministic audit")
    state = await engine.decide_gate(
        state.id,
        "H4",
        _gate_request(state, "H4", action="approve"),
    )
    if state.status != "completed":
        raise RuntimeError("HypoWeaver did not reach a completed sealed state")
    return state


def _claim_decision(claim: ClaimRecord) -> ClaimDecisionInput:
    if claim.admission_status == "admitted" and claim.allowed_strength not in {
        "insufficient",
        "prohibited",
    }:
        decision = "approve"
    elif claim.admission_status == "downgrade_required" and claim.allowed_strength not in {
        "insufficient",
        "prohibited",
    }:
        decision = "downgrade"
    else:
        effective_strength = claim.allowed_strength
        deterministic_ceiling = claim.max_allowed_strength or claim.allowed_strength
        return ClaimDecisionInput(
            claim_id=claim.claim_id,
            decision="reject",
            reason=(
                f"Effective allowed_strength={effective_strength} is not admissible; "
                f"the deterministic ceiling is max_allowed_strength={deterministic_ceiling}. "
                "H3 preserves the tighter candidate calibration instead of raising it."
            ),
        )
    # Claim Gate has already tightened candidate calibration against the
    # deterministic ceiling into allowed_strength. Never raise it back to the
    # looser max_allowed_strength when composing the H3 text.
    effective_strength = claim.allowed_strength
    if effective_strength == "mixed":
        text = (
            "证据混合，识别、证伪或稳健性检查之间存在不一致；"
            "冻结估计呈现的统计关联只能作为受限关联报告，不能支持因果解释。"
        )
    elif effective_strength in {"preliminary", "insufficient"}:
        subject = (
            "该机制或交互仅呈现关联边界"
            if claim.claim_type == "mechanism"
            else "核心解释变量与结果变量存在条件关联"
        )
        text = f"现有证据有限且检查未完成，{subject}仅属初步证据；该结果不支持因果解释。"
    else:
        subject = (
            "该机制或交互仅呈现关联边界"
            if claim.claim_type == "mechanism"
            else "核心解释变量与结果变量存在条件关联"
        )
        text = f"在冻结样本和模型设定下，{subject}；该结果不支持因果解释。"
    return ClaimDecisionInput(
        claim_id=claim.claim_id,
        decision=decision,
        final_text=text,
        reason="Code-owned H3 policy follows the deterministic Claim Gate ceiling.",
    )


def _gate_request(
    state: RunState,
    gate: str,
    *,
    action: str,
    selected_candidate_id: str | None = None,
    claims: list[ClaimDecisionInput] | None = None,
    suffix: str = "0",
) -> GateDecisionRequest:
    return GateDecisionRequest(
        action=action,
        actor="official_benchmark_orchestrator",
        comment="Code-owned formal benchmark gate policy.",
        expected_run_version=state.version,
        idempotency_key=f"official-{state.id}-{gate.lower()}-{suffix}",
        reviewed_artifact_hashes=_gate_hashes(state, gate),
        selected_candidate_id=selected_candidate_id,
        claims=claims or [],
    )


def _gate_hashes(state: RunState, gate: str) -> dict[str, str]:
    keys = {
        "H1": ("research_package",),
        "H2": ("design_arena", "analysis_plan", "critic_report"),
        "H3": ("claim_ledger", "research_run"),
        "H4": ("manuscript_package",),
    }[gate]
    return {
        key: str(state.artifacts[key]["sha256"])
        for key in keys
        if key in state.artifacts
    }


def _require_gate(state: RunState, gate: str) -> None:
    if state.status != "waiting_human" or state.current_gate != gate:
        raise RuntimeError(
            f"HypoWeaver did not reach {gate}: status={state.status}, "
            f"gate={state.current_gate}, error={state.last_error or 'none'}"
        )


def _artifact_payload(state: RunState, key: str) -> dict[str, Any]:
    envelope = state.artifacts.get(key)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise RuntimeError(f"HypoWeaver artifact is missing: {key}")
    payload = envelope["payload"]
    if canonical_sha256(payload) != envelope.get("sha256"):
        raise RuntimeError(f"HypoWeaver artifact sha256 mismatch: {key}")
    return payload


def _artifact_model(state: RunState, key: str, model: type[Any]) -> Any:
    return model.model_validate(_artifact_payload(state, key))


def _native_component_artifact_sha256(state: RunState) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for artifact_key in dict.fromkeys(ABLATION_NATIVE_ARTIFACTS.values()):
        _artifact_payload(state, artifact_key)
        hashes[artifact_key] = str(state.artifacts[artifact_key]["sha256"])
    return hashes


def _receipts_from_usage(
    binding: OfficialAttemptBinding,
    usage: dict[str, Any],
) -> list[Any]:
    values = usage.get("call_receipts", [])
    if not isinstance(values, list):
        raise RuntimeError("model call receipts must be a list")
    receipts = []
    for value in values:
        if not isinstance(value, dict):
            raise RuntimeError("model call receipt is malformed")
        receipts.append(
            create_official_call_receipt(
                binding,
                provider=str(value.get("provider") or ""),
                model=str(value.get("model") or ""),
                raw_response_sha256=str(value.get("response_sha256") or ""),
                call_started_at=str(
                    value.get("call_started_at") or value.get("started_at") or ""
                ),
                call_completed_at=str(
                    value.get("call_completed_at")
                    or value.get("completed_at")
                    or ""
                ),
            )
        )
    return receipts


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_executable_source_coverage(
    configuration: OfficialBenchmarkConfiguration,
) -> None:
    if not configuration.enforce_executable_source_coverage:
        return
    artifact_root = Path(configuration.artifact_root).resolve(strict=True)
    package_root = Path(__file__).resolve().parent
    agent_root = configuration.resolve_artifact(
        configuration.agent_laboratory_root
    )
    required_paths = {
        "hypoweaver": [
            package_root / filename
            for filename in (
                "adapters.py",
                "claim_gate.py",
                "engine.py",
                "manuscript_ir.py",
                "models.py",
                "reproducer.py",
                "research_api.py",
                "research_engine.py",
                "runtime_config.py",
                "test_dag.py",
            )
        ],
        "agent_laboratory": [
            agent_root / "benchmark_adapter" / "__main__.py",
            *[
                agent_root / filename
                for filename in (
                    "ai_lab_repo.py",
                    "agents.py",
                    "mlesolver.py",
                    "papersolver.py",
                )
            ],
        ],
        "benchmark_harness": [
            package_root / filename
            for filename in (
                "benchmark_evaluator.py",
                "benchmark_faults.py",
                "benchmark_models.py",
                "benchmark_packets.py",
                "benchmark_protocol.py",
                "benchmark_runner.py",
                "official_benchmark_runner.py",
                "paired_blind.py",
                "qwen_single_pass_runner.py",
            )
        ],
    }
    for group, paths in required_paths.items():
        declared = [
            Path(value)
            for value in configuration.source_artifact_paths[group]
        ]
        for path in paths:
            try:
                relative = path.resolve(strict=True).relative_to(artifact_root)
            except (FileNotFoundError, ValueError) as error:
                raise ValueError(
                    f"official executable source is outside artifact_root: {path.name}"
                ) from error
            if not any(
                relative == candidate or relative.is_relative_to(candidate)
                for candidate in declared
            ):
                raise ValueError(
                    f"official source group {group} does not cover {relative.as_posix()}"
                )


def _load_configuration(path: Path) -> OfficialBenchmarkConfiguration:
    return OfficialBenchmarkConfiguration.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze or run the one-shot Task3 enterprise-panel benchmark."
    )
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configuration = _load_configuration(args.config)
    if args.command == "prepare":
        frozen = prepare_official_protocol(configuration)
        print(frozen.model_dump_json(indent=2))
        return 0
    manifest = asyncio.run(OfficialBenchmarkOrchestrator(configuration).run())
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
