from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from .benchmark_evaluator import seal_benchmark_packet, verify_benchmark_packet
from .benchmark_models import (
    ABLATION_NATIVE_ARTIFACTS,
    BenchmarkPacket,
    BenchmarkResourceUsage,
    FrozenBenchmarkProtocol,
    PairedBlindCallReceipt,
    PairedEvaluationRequest,
    PairedEvaluationView,
)
from .benchmark_packets import (
    build_agent_laboratory_packet,
    build_hypoweaver_packet,
)
from .benchmark_runner import AgentLaboratoryRunner, BaselineRunRequest
from .case_import import DatasetRegistry
from .claim_gate import (
    ClaimGateError,
    permitted_h3_decisions,
    validate_h3_claim_decision,
)
from .engine import (
    DESIGN_RETRY_MODEL,
    REVIEWER_MODEL,
    WRITER_ESCALATION_MODEL,
    WorkflowEngine,
)
from .local_recovery_runner import (
    LocalRecoveryComparisonContext,
    LocalRecoveryComparisonResult,
    LocalRecoveryRoundContext,
    LocalRecoveryRoundResult,
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
    ModelCallReceipt,
    ReproductionAudit,
    ResearchRun,
    RunState,
)
from .paired_blind import PairedBlindEngine
from .paired_blind_repository import PairedBlindRepository
from .qwen_single_pass_runner import (
    QwenSinglePassCallMetadata,
    QwenSinglePassRunner,
)
from .recovery_campaign import map_model_call_receipts, verify_recovery_environment
from .recovery_identity import research_runtime_identity_sha256
from .recovery_models import RecoveryCallReceipt, RecoveryFreeze, RecoveryUsage
from .repository import RunRepository
from .research_api import runtime_identity
from .runtime_config import FrozenRuntimeConfigStore, RuntimeConfigStore
from .seal import canonical_sha256


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoverySourceConfiguration(BaseModel):
    """Read-only source configuration; no recovery path is inherited from it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    agent_timeout_seconds: int = 1800
    poll_interval_seconds: float = 1.0
    enforce_executable_source_coverage: bool = True

    def resolve_source(self, relative_path: str) -> Path:
        root = Path(self.artifact_root).resolve(strict=True)
        candidate = (root / relative_path).resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise ValueError("recovery source artifact escapes artifact_root")
        return candidate


def load_recovery_source_configuration(path: Path) -> RecoverySourceConfiguration:
    return RecoverySourceConfiguration.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def assert_recovery_paths_separate(
    configuration: RecoverySourceConfiguration,
    *,
    working_root: Path,
    delivery_root: Path,
    state_root: Path,
) -> None:
    recovery_roots = tuple(path.resolve(strict=False) for path in (
        working_root,
        delivery_root,
        state_root,
    ))
    if len(set(recovery_roots)) != 3:
        raise ValueError("recovery working, delivery, and state roots must be distinct")
    for index, left in enumerate(recovery_roots):
        for right in recovery_roots[index + 1 :]:
            if _paths_overlap(left, right):
                raise ValueError("recovery roots cannot contain one another")
    protected_roots = (
        Path(configuration.output_dir).resolve(strict=False),
        Path(configuration.working_dir).resolve(strict=False),
        Path(configuration.official_state_root).resolve(strict=False),
        Path(configuration.artifact_root).resolve(strict=True),
    )
    for recovery_root in recovery_roots:
        for protected_root in protected_roots:
            if _paths_overlap(recovery_root, protected_root):
                raise ValueError("recovery paths overlap a protected source or formal path")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


RoundDriver = Callable[
    [LocalRecoveryRoundContext],
    Awaitable[tuple[BenchmarkPacket | None, list[ModelCallReceipt], str | None]],
]
ComparisonDriver = Callable[
    [LocalRecoveryComparisonContext, BenchmarkPacket, str],
    Awaitable[LocalRecoveryComparisonResult],
]


class ProductionRecoveryBackend:
    """Real non-formal recovery adapters with durable, receipt-backed outcomes."""

    def __init__(
        self,
        *,
        source_config_path: Path,
        protocol: FrozenBenchmarkProtocol,
        working_root: Path,
        delivery_root: Path,
        state_root: Path,
        predecessor_campaign_path: Path | None = None,
        runtime_config_store: RuntimeConfigStore | None = None,
        health_transport: httpx.AsyncBaseTransport | None = None,
        test_mode: bool = False,
        round_driver: RoundDriver | None = None,
        comparison_driver: ComparisonDriver | None = None,
    ) -> None:
        if (round_driver is not None or comparison_driver is not None) and not test_mode:
            raise ValueError("injected recovery drivers are test-only")
        self.source_config_path = source_config_path
        self.configuration = load_recovery_source_configuration(source_config_path)
        self.protocol = protocol
        self.working_root = working_root.resolve(strict=False)
        self.delivery_root = delivery_root.resolve(strict=False)
        self.state_root = state_root.resolve(strict=False)
        self.predecessor_campaign_path = (
            predecessor_campaign_path.resolve(strict=True)
            if predecessor_campaign_path is not None
            else None
        )
        if self.predecessor_campaign_path is not None:
            predecessor_state_root = self.predecessor_campaign_path.parent
            predecessor_roots = [
                self.predecessor_campaign_path,
                predecessor_state_root,
            ]
            if predecessor_state_root.name.endswith("-state"):
                predecessor_delivery_name = predecessor_state_root.name.removesuffix(
                    "-state"
                )
                predecessor_roots.extend(
                    (
                        predecessor_state_root.with_name(
                            predecessor_delivery_name
                        ),
                        predecessor_state_root.with_name(
                            f"{predecessor_delivery_name}-work"
                        ),
                    )
                )
            if any(
                _paths_overlap(predecessor_root, recovery_root)
                for predecessor_root in predecessor_roots
                for recovery_root in (
                    self.working_root,
                    self.delivery_root,
                    self.state_root,
                )
            ):
                raise ValueError(
                    "predecessor roots overlap a recovery writable root"
                )
        assert_recovery_paths_separate(
            self.configuration,
            working_root=self.working_root,
            delivery_root=self.delivery_root,
            state_root=self.state_root,
        )
        self.runtime_config_store = runtime_config_store or RuntimeConfigStore()
        self.health_transport = health_transport
        self.test_mode = test_mode
        self.round_driver = round_driver
        self.comparison_driver = comparison_driver
        self.source_root = Path(self.configuration.artifact_root).resolve(strict=True)
        self.visible_input_path = self.configuration.resolve_source(
            self.configuration.visible_input_path
        )
        self.reference_path = self.configuration.resolve_source(
            self.configuration.reference_path
        )
        self.reference_summary_path = self.configuration.resolve_source(
            self.configuration.reference_summary_path
        )
        self.runtime_public_path = self.configuration.resolve_source(
            self.configuration.runtime_public_path
        )
        self.registry = DatasetRegistry()

    async def preflight(self, freeze: RecoveryFreeze) -> FrozenRuntimeConfigStore:
        case_request = self._case_request()
        if case_request.case is None:
            raise ValueError("recovery requires an explicit visible case")
        data_paths = tuple(
            self.registry.resolve(dataset_ref)
            for dataset_ref in case_request.case.dataset_refs
        )
        verify_recovery_environment(
            freeze,
            self.protocol,
            artifact_root=self.source_root,
            visible_input_path=self.visible_input_path,
            data_paths=data_paths,
            reference_path=self.reference_path,
            reference_summary_path=self.reference_summary_path,
            predecessor_campaign_path=self.predecessor_campaign_path,
        )
        if research_runtime_identity_sha256() != freeze.research_runtime_identity_sha256:
            raise RuntimeError("local research runtime identity differs from the freeze")
        config = self.runtime_config_store.resolve()
        if not config.qwen_api_key or not config.research_engine_url:
            raise RuntimeError("recovery runtime credentials or Research Engine URL missing")
        expected_public = json.loads(self.runtime_public_path.read_text(encoding="utf-8"))
        local_identity = runtime_identity()
        actual_public = {
            "qwen_model": config.qwen_model,
            "qwen_base_url": config.qwen_base_url,
            "qwen_review_model": os.getenv("QWEN_REVIEW_MODEL") or config.qwen_model,
            "hypoweaver_reviewer_model": REVIEWER_MODEL,
            "hypoweaver_design_retry_model": DESIGN_RETRY_MODEL,
            "hypoweaver_writer_escalation_model": WRITER_ESCALATION_MODEL,
            "research_engine_url": config.research_engine_url,
            "python_environment_sha256": local_identity["environment_sha256"],
            "agent_laboratory_upstream_commit": (
                "d9017d90e329112d2a80b7712f37ee9094d2cd27"
            ),
            "agent_laboratory_max_calls": 20,
            "agent_laboratory_external_collection": "prohibited",
            "generated_code_isolation": "macos-sandbox-exec-deny-network",
        }
        if expected_public != actual_public:
            raise RuntimeError("actual runtime differs from frozen public configuration")
        trust_env = urlsplit(config.research_engine_url).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        async with httpx.AsyncClient(
            timeout=10,
            trust_env=trust_env,
            transport=self.health_transport,
        ) as client:
            response = await client.get(
                f"{config.research_engine_url.rstrip('/')}/v1/health"
            )
            response.raise_for_status()
            health = response.json()
        if health != {"status": "ok", **local_identity}:
            raise RuntimeError("Research Engine health identity differs from the freeze")
        for dataset_ref, data_path in zip(case_request.case.dataset_refs, data_paths):
            if _file_sha256(data_path) != dataset_ref.sha256:
                raise ValueError("registered recovery dataset sha256 mismatch")
        return FrozenRuntimeConfigStore(config)

    async def run_hypoweaver_round(
        self,
        context: LocalRecoveryRoundContext,
    ) -> LocalRecoveryRoundResult:
        result_path = self._round_root(context) / "round-result.json"
        cached = _load_result(result_path, LocalRecoveryRoundResult)
        if cached is not None:
            return cached
        started_at = _utc_now()
        packet: BenchmarkPacket | None = None
        source_receipts: list[ModelCallReceipt] = []
        reason_code: str | None = None
        round_started_path = self._round_root(context) / "round-started.json"
        orphaned_started_round = (
            self.round_driver is None and round_started_path.is_file()
        )
        try:
            if orphaned_started_round:
                raise RuntimeError("unfinished recovery round has unknown call evidence")
            if self.round_driver is not None:
                packet, source_receipts, reason_code = await self.round_driver(context)
            else:
                frozen_runtime = await self.preflight(context.freeze)
                _mark_round_started(
                    round_started_path,
                    context,
                )
                packet, source_receipts = await self._execute_round(
                    context,
                    frozen_runtime,
                )
            if any(receipt.provider != "qwen" for receipt in source_receipts):
                raise ValueError("production recovery rejects non-Qwen model receipts")
            mapped = map_model_call_receipts(
                source_receipts,
                campaign_id=context.campaign_id,
                round_id=context.round_id,
                require_complete=packet is not None and reason_code is None,
            )
            usage = _usage_from_model_receipts(source_receipts)
            if packet is not None and reason_code is None:
                if usage.llm_calls > context.call_limit:
                    raise ValueError("recovery round exceeded its reserved call limit")
                packet = _packet_with_usage(packet, usage)
                result = LocalRecoveryRoundResult(
                    status="completed",
                    started_at=_started_before_receipts(started_at, mapped),
                    completed_at=_completion_after_receipts(mapped),
                    usage=usage,
                    receipts=mapped,
                    packet=packet,
                )
            else:
                result = LocalRecoveryRoundResult(
                    status="technical_failed",
                    started_at=_started_before_receipts(started_at, mapped),
                    completed_at=_completion_after_receipts(mapped),
                    usage=usage,
                    receipts=mapped,
                    reason_code=reason_code or "hypoweaver_round_incomplete",
                )
        except Exception as error:
            if orphaned_started_round:
                raise
            if not source_receipts:
                recovered = self._recover_round_model_receipts(context)
                if recovered is not None:
                    source_receipts = recovered
            if (
                self.round_driver is None
                and (self._round_root(context) / "round-started.json").is_file()
                and not source_receipts
            ):
                # The model-facing round was entered, but the durable model-usage
                # artifact cannot prove whether a provider call escaped.  The
                # controller must invalidate and charge the entire reservation.
                raise
            mapped = map_model_call_receipts(
                source_receipts,
                campaign_id=context.campaign_id,
                round_id=context.round_id,
                require_complete=False,
            )
            result = LocalRecoveryRoundResult(
                status="technical_failed",
                started_at=_started_before_receipts(started_at, mapped),
                completed_at=_completion_after_receipts(mapped),
                usage=_usage_from_model_receipts(source_receipts),
                receipts=mapped,
                reason_code=f"round_{type(error).__name__}",
            )
        _write_once(result_path, result.model_dump(mode="json"))
        return result

    async def run_comparison(
        self,
        context: LocalRecoveryComparisonContext,
        qualified_packet: BenchmarkPacket,
        reference_summary: str,
    ) -> LocalRecoveryComparisonResult:
        result_path = self._comparison_root(context) / "comparison-result.json"
        cached = _load_result(result_path, LocalRecoveryComparisonResult)
        if cached is not None:
            return cached
        if self.comparison_driver is not None:
            result = await self.comparison_driver(
                context,
                qualified_packet,
                reference_summary,
            )
            _write_once(result_path, result.model_dump(mode="json"))
            return result
        started_at = _utc_now()
        qwen_usage = RecoveryUsage()
        agent_usage = RecoveryUsage()
        blind_usage = RecoveryUsage()
        receipts: list[RecoveryCallReceipt] = []
        qwen_packet: BenchmarkPacket | None = None
        agent_packet: BenchmarkPacket | None = None
        blind_view: PairedEvaluationView | None = None
        frozen_runtime: FrozenRuntimeConfigStore | None = None
        try:
            frozen_runtime = await self.preflight(context.freeze)
            qwen_packet, qwen_usage, qwen_receipt = await self._run_qwen_baseline(
                context,
                frozen_runtime,
            )
            receipts.append(qwen_receipt)
            if qwen_packet is None:
                raise RuntimeError("qwen_single_pass_failed")
            agent_packet, agent_usage, agent_receipts = await self._run_agent_baseline(
                context,
                frozen_runtime,
            )
            receipts.extend(agent_receipts)
            blind_view, blind_usage, blind_receipts = await self._run_blind_reviews(
                context,
                qualified_packet,
                agent_packet,
                reference_summary,
                frozen_runtime,
            )
            receipts.extend(blind_receipts)
            if blind_view.status != "completed" or blind_view.result is None:
                raise RuntimeError("paired_blind_failed")
            result = LocalRecoveryComparisonResult(
                status="completed",
                qwen_single_pass=qwen_usage,
                agent_laboratory=agent_usage,
                blind_reviews=blind_usage,
                receipts=tuple(receipts),
                started_at=_started_before_receipts(started_at, tuple(receipts)),
                completed_at=_completion_after_receipts(tuple(receipts)),
                qwen_packet=qwen_packet,
                agent_laboratory_packet=agent_packet,
                blind_summary=blind_view.result,
            )
        except Exception as error:
            self._write_comparison_assembly_error(context, error)
            completed = self._recover_completed_comparison(
                context,
                qualified_packet,
                started_at=started_at,
            )
            if completed is not None:
                result = completed
                _write_once(result_path, result.model_dump(mode="json"))
                return result
            recovered = self._recover_comparison_evidence(
                context,
                frozen_runtime=frozen_runtime,
            )
            if recovered is None:
                # A provider-facing phase was started but its durable source cannot
                # prove the exact call count.  Let LocalRecoveryRunner invalidate
                # the active reservation and conservatively charge all 26 calls.
                raise
            qwen_usage, agent_usage, blind_usage, recovered_receipts = recovered
            result = LocalRecoveryComparisonResult(
                status="technical_failed",
                qwen_single_pass=qwen_usage,
                agent_laboratory=agent_usage,
                blind_reviews=blind_usage,
                receipts=recovered_receipts,
                started_at=_started_before_receipts(started_at, recovered_receipts),
                completed_at=_completion_after_receipts(recovered_receipts),
                reason_code=f"comparison_{type(error).__name__}",
            )
        _write_once(result_path, result.model_dump(mode="json"))
        return result

    def _recover_completed_comparison(
        self,
        context: LocalRecoveryComparisonContext,
        qualified_packet: BenchmarkPacket,
        *,
        started_at: str,
    ) -> LocalRecoveryComparisonResult | None:
        """Reassemble only a fully sealed comparison after a late controller error."""

        root = self._comparison_root(context)
        qwen_path = root / "qwen-stage.json"
        agent_path = root / "agent-stage.json"
        blind_path = root / "blind-stage.json"
        if not all(path.is_file() for path in (qwen_path, agent_path, blind_path)):
            return None
        try:
            qwen_payload = json.loads(qwen_path.read_text(encoding="utf-8"))
            agent_payload = json.loads(agent_path.read_text(encoding="utf-8"))
            blind_payload = json.loads(blind_path.read_text(encoding="utf-8"))

            qwen_packet = BenchmarkPacket.model_validate(qwen_payload["packet"])
            qwen_usage = RecoveryUsage.model_validate(qwen_payload["usage"])
            qwen_receipts = (
                RecoveryCallReceipt.model_validate(qwen_payload["receipt"]),
            )
            agent_packet = BenchmarkPacket.model_validate(agent_payload["packet"])
            agent_usage = RecoveryUsage.model_validate(agent_payload["usage"])
            agent_receipts = tuple(
                RecoveryCallReceipt.model_validate(item)
                for item in agent_payload["receipts"]
            )
            blind_view = PairedEvaluationView.model_validate(blind_payload["view"])
            blind_usage = RecoveryUsage.model_validate(blind_payload["usage"])
            blind_receipts = tuple(
                RecoveryCallReceipt.model_validate(item)
                for item in blind_payload["receipts"]
            )

            for phase, usage, phase_receipts in (
                ("qwen_single_pass", qwen_usage, qwen_receipts),
                ("agent_laboratory", agent_usage, agent_receipts),
                ("blind_review", blind_usage, blind_receipts),
            ):
                _require_exact_phase_evidence(phase, usage, phase_receipts)
                _require_recovery_receipt_binding(context, phase_receipts)
                receipt_failures = sorted(
                    item.error_type
                    for item in phase_receipts
                    if item.error_type is not None
                )
                if receipt_failures != sorted(usage.technical_failures):
                    raise ValueError(f"{phase} technical-failure accounting mismatch")

            for expected_system, packet, usage in (
                ("qwen_single_pass", qwen_packet, qwen_usage),
                ("agent_laboratory", agent_packet, agent_usage),
            ):
                verify_benchmark_packet(packet)
                if packet.system_id != expected_system or packet.official_receipts:
                    raise ValueError("comparison packet provenance mismatch")
                if (
                    packet.case_id != context.freeze.case_id
                    or packet.visible_input_sha256
                    != context.freeze.visible_input_sha256
                    or tuple(packet.data_sha256) != context.freeze.data_sha256
                    or packet.resource_usage != _benchmark_usage(usage)
                ):
                    raise ValueError("comparison packet identity or usage mismatch")
            verify_benchmark_packet(qualified_packet)
            if (
                qualified_packet.system_id != "hypoweaver"
                or qualified_packet.official_receipts
                or qualified_packet.case_id != context.freeze.case_id
                or qualified_packet.visible_input_sha256
                != context.freeze.visible_input_sha256
                or tuple(qualified_packet.data_sha256) != context.freeze.data_sha256
            ):
                raise ValueError("qualified packet provenance mismatch")

            qwen_receipt = qwen_receipts[0]
            qwen_native = qwen_packet.native_artifact_sha256
            required_qwen_native = {
                "visible_input",
                "single_pass_prompt",
                "single_pass_config",
                "single_pass_raw_response",
            }
            if (
                len(qwen_receipts) != 1
                or qwen_receipt.provider != "qwen"
                or qwen_receipt.model != qwen_packet.model_id
                or qwen_receipt.outcome != "succeeded"
                or not required_qwen_native.issubset(qwen_native)
                or any(
                    not _is_nonzero_sha256(qwen_native[key])
                    for key in required_qwen_native
                )
                or qwen_native["visible_input"]
                != context.freeze.visible_input_sha256
                or qwen_receipt.input_sha256
                != context.freeze.visible_input_sha256
                or qwen_receipt.response_sha256
                != qwen_native["single_pass_raw_response"]
            ):
                raise ValueError("Qwen stage receipt binding mismatch")
            agent_native = agent_packet.native_artifact_sha256
            if (
                "benchmark_output" not in agent_native
                or any(not _is_nonzero_sha256(value) for value in agent_native.values())
            ):
                raise ValueError("Agent Laboratory stage artifact binding mismatch")

            if blind_view.status != "completed" or blind_view.result is None:
                raise ValueError("blind stage is not complete")
            runtime_payload = json.loads(
                self.runtime_public_path.read_text(encoding="utf-8")
            )
            expected_model = runtime_payload.get("qwen_review_model")
            blind_view.verify_runtime_receipts(
                expect_real_qwen=True,
                expected_model=(
                    str(expected_model) if expected_model is not None else None
                ),
            )
            summary = blind_view.result
            if (
                blind_view.case_id != context.freeze.case_id
                or blind_view.packet_a_id != qualified_packet.packet_id
                or blind_view.packet_b_id != agent_packet.packet_id
                or summary.case_id != context.freeze.case_id
                or summary.packet_a_id != qualified_packet.packet_id
                or summary.packet_b_id != agent_packet.packet_id
                or blind_view.sealed_label_orders
                != list(context.freeze.sealed_label_orders)
                or blind_view.sealed_system_assignments
                != list(context.freeze.sealed_system_assignments)
            ):
                raise ValueError("blind stage packet or schedule binding mismatch")
            reviews_by_sample = {item.sample_index: item for item in summary.reviews}
            sources_by_sample = {
                item.sample_index: item for item in blind_view.review_call_receipts
            }
            mapped_by_call = {item.call_id: item for item in blind_receipts}
            if (
                len(summary.reviews) != 5
                or set(reviews_by_sample) != {1, 2, 3, 4, 5}
                or set(sources_by_sample) != {1, 2, 3, 4, 5}
                or len(mapped_by_call) != 5
            ):
                raise ValueError("blind stage receipt cardinality mismatch")
            for sample_index in range(1, 6):
                review = reviews_by_sample[sample_index]
                source = sources_by_sample[sample_index]
                mapped = mapped_by_call.get(source.call_id)
                if (
                    review.label_order
                    != context.freeze.sealed_label_orders[sample_index - 1]
                    or review.system_assignment
                    != context.freeze.sealed_system_assignments[sample_index - 1]
                    or review.official_receipt is not None
                    or review.call_receipt != source
                    or source.outcome != "succeeded"
                    or source.provider != "qwen"
                    or mapped is None
                    or mapped.provider != source.provider
                    or mapped.model != source.model
                    or mapped.outcome != "succeeded"
                    or mapped.response_sha256 != source.response_sha256
                    or mapped.input_tokens != source.input_tokens
                    or mapped.output_tokens != source.output_tokens
                    or mapped.call_started_at != source.call_started_at
                    or mapped.call_completed_at != source.call_completed_at
                    or mapped.source_receipt_sha256
                    != canonical_sha256(source.model_dump(mode="json"))
                ):
                    raise ValueError("blind stage source receipt binding mismatch")
            if _blind_usage(blind_view) != blind_usage:
                raise ValueError("blind stage usage mismatch")

            receipts = qwen_receipts + agent_receipts + blind_receipts
            if (
                len({item.call_id for item in receipts}) != len(receipts)
                or any(
                    item.source_receipt_sha256 in (None, "0" * 64)
                    for item in receipts
                )
                or len({item.source_receipt_sha256 for item in receipts})
                != len(receipts)
            ):
                raise ValueError("comparison receipt identity is not unique")
            return LocalRecoveryComparisonResult(
                status="completed",
                qwen_single_pass=qwen_usage,
                agent_laboratory=agent_usage,
                blind_reviews=blind_usage,
                receipts=receipts,
                started_at=_started_before_receipts(started_at, receipts),
                completed_at=_completion_after_receipts(receipts),
                qwen_packet=qwen_packet,
                agent_laboratory_packet=agent_packet,
                blind_summary=summary,
            )
        except Exception:
            return None

    def _write_comparison_assembly_error(
        self,
        context: LocalRecoveryComparisonContext,
        error: Exception,
    ) -> None:
        path = self._comparison_root(context) / "comparison-assembly-error.json"
        if path.exists():
            return
        validation_errors: list[dict[str, object]] = []
        if isinstance(error, ValidationError):
            validation_errors = [
                {
                    "loc": list(item.get("loc", ())),
                    "type": str(item.get("type", "unknown")),
                }
                for item in error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            ]
        _write_once(
            path,
            {
                "event": "comparison_pipeline_exception",
                "error_type": type(error).__name__,
                "validation_errors": validation_errors,
            },
        )

    async def _execute_round(
        self,
        context: LocalRecoveryRoundContext,
        frozen_runtime: FrozenRuntimeConfigStore,
    ) -> tuple[BenchmarkPacket, list[ModelCallReceipt]]:
        round_root = self._round_root(context)
        round_root.mkdir(parents=True, exist_ok=True)
        repository = RunRepository(round_root / "hypoweaver.db")
        engine = WorkflowEngine(
            repository,
            dataset_registry=self.registry,
            runtime_config_store=frozen_runtime,
            model_call_limit=context.call_limit,
        )
        states = repository.list()
        if len(states) > 1:
            raise ValueError("recovery round database contains multiple runs")
        state = states[0] if states else await engine.create_run(self._case_request())
        state = await _drive_recovery_workflow(engine, state, context)
        packet = _build_round_packet(
            state,
            context,
            model=frozen_runtime.resolve().qwen_model,
        )
        receipts = _model_receipts_from_state(state)
        return packet, receipts

    def _recover_round_model_receipts(
        self,
        context: LocalRecoveryRoundContext,
    ) -> list[ModelCallReceipt] | None:
        database = self._round_root(context) / "hypoweaver.db"
        if not database.is_file():
            return None
        try:
            states = RunRepository(database).list()
            if len(states) != 1:
                return None
            return _model_receipts_from_state(states[0])
        except Exception:
            return None

    async def _run_qwen_baseline(
        self,
        context: LocalRecoveryComparisonContext,
        frozen_runtime: FrozenRuntimeConfigStore,
    ) -> tuple[BenchmarkPacket | None, RecoveryUsage, RecoveryCallReceipt]:
        stage_path = self._comparison_root(context) / "qwen-stage.json"
        if stage_path.is_file():
            payload = json.loads(stage_path.read_text(encoding="utf-8"))
            packet_payload = payload.get("packet")
            return (
                BenchmarkPacket.model_validate(packet_payload)
                if packet_payload is not None
                else None,
                RecoveryUsage.model_validate(payload["usage"]),
                RecoveryCallReceipt.model_validate(payload["receipt"]),
            )
        started_path = self._comparison_root(context) / "qwen-started.json"
        if started_path.is_file():
            raise RuntimeError("unfinished Qwen baseline has unknown call evidence")
        runner = QwenSinglePassRunner(config_store=frozen_runtime)
        _mark_phase_started(
            started_path,
            phase="qwen_single_pass",
            campaign_id=context.campaign_id,
            comparison_id=context.comparison_id,
        )
        result = await runner.run(
            packet_id=f"recovery-qwen-{context.campaign_id[-12:]}",
            case_id=context.freeze.case_id,
            data_sha256=list(context.freeze.data_sha256),
            visible_input_path=self.visible_input_path,
        )
        usage = _benchmark_usage(result.metadata.resource_usage)
        receipt = recovery_receipt_from_qwen_metadata(
            result.metadata,
            campaign_id=context.campaign_id,
            round_id=context.comparison_id,
            succeeded=result.status == "completed",
            error_type=result.error,
        )
        _write_once(
            stage_path,
            {
                "packet": (
                    result.packet.model_dump(mode="json")
                    if result.packet is not None
                    else None
                ),
                "usage": usage.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
            },
        )
        return result.packet, usage, receipt

    async def _run_agent_baseline(
        self,
        context: LocalRecoveryComparisonContext,
        frozen_runtime: FrozenRuntimeConfigStore,
    ) -> tuple[BenchmarkPacket, RecoveryUsage, tuple[RecoveryCallReceipt, ...]]:
        stage_path = self._comparison_root(context) / "agent-stage.json"
        if stage_path.is_file():
            payload = json.loads(stage_path.read_text(encoding="utf-8"))
            return (
                BenchmarkPacket.model_validate(payload["packet"]),
                RecoveryUsage.model_validate(payload["usage"]),
                tuple(
                    RecoveryCallReceipt.model_validate(item)
                    for item in payload["receipts"]
                ),
            )
        started_path = self._comparison_root(context) / "agent-started.json"
        case = self._case_request().case
        assert case is not None
        runner = self._agent_runner(context, frozen_runtime)
        existing = runner.list(case_id=case.case_id)
        if len(existing) > 1:
            raise ValueError("recovery comparison has multiple Agent Laboratory runs")
        if started_path.is_file() and (
            not existing or existing[0].status not in {"completed", "failed"}
        ):
            raise RuntimeError(
                "unfinished Agent Laboratory baseline has unknown call evidence"
            )
        _mark_phase_started(
            started_path,
            phase="agent_laboratory",
            campaign_id=context.campaign_id,
            comparison_id=context.comparison_id,
        )
        state = (
            existing[0]
            if existing
            else runner.start(
                BaselineRunRequest(case=case, execute_generated_code=True)
            )
        )
        deadline = time.monotonic() + self.configuration.agent_timeout_seconds
        while state.status not in {"completed", "failed"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("Agent Laboratory recovery comparison timed out")
            await asyncio.sleep(self.configuration.poll_interval_seconds)
            state = runner.get(state.id)
        artifacts = (
            runner.load_completed_artifacts(state.id)
            if state.status == "completed"
            else runner.load_terminal_failure_artifacts(state.id)
        )
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
            raise ValueError("Agent Laboratory is not the frozen original workflow")
        packet = build_agent_laboratory_packet(
            packet_id=f"recovery-agent-{context.campaign_id[-12:]}",
            output=artifacts.output,
            visible_input_sha256=context.freeze.visible_input_sha256,
            data_sha256=list(context.freeze.data_sha256),
            report_text=artifacts.report_text,
        )
        packet = seal_benchmark_packet(
            packet.model_copy(
                update={
                    "native_artifact_sha256": {
                        **packet.native_artifact_sha256,
                        "benchmark_output": artifacts.output_sha256,
                        **(
                            {"report": artifacts.report_sha256}
                            if artifacts.report_sha256 is not None
                            else {}
                        ),
                    },
                    "packet_sha256": None,
                }
            )
        )
        usage_payload = artifacts.output.get("execution_cost") or {}
        usage = RecoveryUsage(
            llm_calls=int(usage_payload.get("llm_calls", 0) or 0),
            input_tokens=int(usage_payload.get("input_tokens", 0) or 0),
            output_tokens=int(usage_payload.get("output_tokens", 0) or 0),
            wall_time_seconds=float(usage_payload.get("wall_time_seconds", 0) or 0),
            technical_failures=tuple(
                str(item) for item in usage_payload.get("technical_failures", [])
            ),
        )
        receipts = recovery_receipts_from_agent_usage(
            usage_payload,
            campaign_id=context.campaign_id,
            round_id=context.comparison_id,
        )
        if len(receipts) != usage.llm_calls or usage.llm_calls > 20:
            raise ValueError("Agent Laboratory recovery receipts are incomplete")
        _require_exact_phase_evidence("agent_laboratory", usage, receipts)
        packet = _packet_with_usage(packet, usage)
        _write_once(
            stage_path,
            {
                "packet": packet.model_dump(mode="json"),
                "usage": usage.model_dump(mode="json"),
                "receipts": [item.model_dump(mode="json") for item in receipts],
            },
        )
        return packet, usage, receipts

    async def _run_blind_reviews(
        self,
        context: LocalRecoveryComparisonContext,
        qualified_packet: BenchmarkPacket,
        agent_packet: BenchmarkPacket,
        reference_summary: str,
        frozen_runtime: FrozenRuntimeConfigStore,
    ) -> tuple[
        PairedEvaluationView,
        RecoveryUsage,
        tuple[RecoveryCallReceipt, ...],
    ]:
        database = self._comparison_root(context) / "paired-blind.db"
        stage_path = self._comparison_root(context) / "blind-stage.json"
        if stage_path.is_file():
            payload = json.loads(stage_path.read_text(encoding="utf-8"))
            view = PairedEvaluationView.model_validate(payload["view"])
            usage = RecoveryUsage.model_validate(payload["usage"])
            receipts = tuple(
                RecoveryCallReceipt.model_validate(item)
                for item in payload["receipts"]
            )
            _require_exact_phase_evidence("blind_review", usage, receipts)
            return view, usage, receipts
        started_path = self._comparison_root(context) / "blind-started.json"
        repository = PairedBlindRepository(database)
        existing = repository.list()
        review_model = json.loads(
            self.runtime_public_path.read_text(encoding="utf-8")
        )["qwen_review_model"]
        if existing:
            if len(existing) != 1:
                raise ValueError("recovery comparison has multiple blind evaluations")
            view = existing[0]
            if started_path.is_file() and view.updated_at == view.created_at:
                raise RuntimeError(
                    "unfinished blind review has unknown call evidence"
                )
        else:
            if started_path.is_file():
                raise RuntimeError(
                    "unfinished blind review has unknown call evidence"
                )
            engine = PairedBlindEngine(
                repository,
                config_store=frozen_runtime,
                review_model_override=str(review_model),
            )
            _mark_phase_started(
                started_path,
                phase="blind_review",
                campaign_id=context.campaign_id,
                comparison_id=context.comparison_id,
            )
            try:
                view = await engine.evaluate(
                    PairedEvaluationRequest(
                        packet_a=qualified_packet,
                        packet_b=agent_packet,
                        reference_summary=reference_summary,
                        model_provider="qwen",
                        official_attempt=None,
                        sealed_label_orders=list(context.freeze.sealed_label_orders),
                        sealed_system_assignments=list(
                            context.freeze.sealed_system_assignments
                        ),
                    )
                )
            except Exception:
                stored = repository.list()
                if len(stored) != 1:
                    raise
                view = stored[0]
        usage = _blind_usage(view)
        receipts = recovery_receipts_from_blind_view(
            view,
            campaign_id=context.campaign_id,
            round_id=context.comparison_id,
        )
        if len(receipts) != usage.llm_calls:
            raise ValueError("blind recovery receipts are incomplete")
        try:
            view.verify_runtime_receipts(
                expect_real_qwen=True,
                expected_model=str(review_model),
            )
        except Exception:
            view = view.model_copy(
                update={
                    "status": "failed",
                    "result": None,
                    "error": "ReceiptValidationError",
                }
            )
        _write_once(
            stage_path,
            {
                "view": view.model_dump(mode="json"),
                "usage": usage.model_dump(mode="json"),
                "receipts": [item.model_dump(mode="json") for item in receipts],
            },
        )
        return view, usage, receipts

    def _agent_runner(
        self,
        context: LocalRecoveryComparisonContext,
        frozen_runtime: FrozenRuntimeConfigStore,
    ) -> AgentLaboratoryRunner:
        return AgentLaboratoryRunner(
            root=self._comparison_root(context) / "agent-laboratory",
            agent_lab_root=self.configuration.resolve_source(
                self.configuration.agent_laboratory_root
            ),
            registry=self.registry,
            config_store=frozen_runtime,
            forbidden_read_paths=(self.reference_path, self.reference_summary_path),
            process_timeout_seconds=max(
                1,
                self.configuration.agent_timeout_seconds - 30,
            ),
        )

    def _recover_comparison_evidence(
        self,
        context: LocalRecoveryComparisonContext,
        *,
        frozen_runtime: FrozenRuntimeConfigStore | None,
    ) -> tuple[
        RecoveryUsage,
        RecoveryUsage,
        RecoveryUsage,
        tuple[RecoveryCallReceipt, ...],
    ] | None:
        """Recover exact durable accounting, or signal that conservative charging is required."""

        root = self._comparison_root(context)
        qwen_usage = RecoveryUsage()
        agent_usage = RecoveryUsage()
        blind_usage = RecoveryUsage()
        qwen_receipts: tuple[RecoveryCallReceipt, ...] = ()
        agent_receipts: tuple[RecoveryCallReceipt, ...] = ()
        blind_receipts: tuple[RecoveryCallReceipt, ...] = ()

        qwen_stage = root / "qwen-stage.json"
        if qwen_stage.is_file():
            try:
                payload = json.loads(qwen_stage.read_text(encoding="utf-8"))
                qwen_usage = RecoveryUsage.model_validate(payload["usage"])
                qwen_receipts = (
                    RecoveryCallReceipt.model_validate(payload["receipt"]),
                )
                _require_exact_phase_evidence(
                    "qwen_single_pass",
                    qwen_usage,
                    qwen_receipts,
                )
            except Exception:
                return None
        elif (root / "qwen-started.json").is_file():
            return None

        agent_stage = root / "agent-stage.json"
        if agent_stage.is_file():
            try:
                payload = json.loads(agent_stage.read_text(encoding="utf-8"))
                agent_usage = RecoveryUsage.model_validate(payload["usage"])
                agent_receipts = tuple(
                    RecoveryCallReceipt.model_validate(item)
                    for item in payload["receipts"]
                )
                _require_exact_phase_evidence(
                    "agent_laboratory",
                    agent_usage,
                    agent_receipts,
                )
            except Exception:
                return None
        elif (root / "agent-started.json").is_file():
            if frozen_runtime is None:
                return None
            recovered_agent = self._recover_terminal_agent_evidence(
                context,
                frozen_runtime,
            )
            if recovered_agent is None:
                return None
            agent_usage, agent_receipts = recovered_agent

        blind_stage = root / "blind-stage.json"
        blind_database = root / "paired-blind.db"
        blind_started = (root / "blind-started.json").is_file()
        if blind_stage.is_file():
            try:
                payload = json.loads(blind_stage.read_text(encoding="utf-8"))
                PairedEvaluationView.model_validate(payload["view"])
                blind_usage = RecoveryUsage.model_validate(payload["usage"])
                blind_receipts = tuple(
                    RecoveryCallReceipt.model_validate(item)
                    for item in payload["receipts"]
                )
                _require_exact_phase_evidence(
                    "blind_review",
                    blind_usage,
                    blind_receipts,
                )
            except Exception:
                return None
        elif blind_started and blind_database.is_file():
            try:
                stored = PairedBlindRepository(blind_database).list()
                if len(stored) != 1:
                    return None
                view = stored[0]
                if view.updated_at == view.created_at:
                    return None
                blind_usage = _blind_usage(view)
                blind_receipts = recovery_receipts_from_blind_view(
                    view,
                    campaign_id=context.campaign_id,
                    round_id=context.comparison_id,
                )
                _require_exact_phase_evidence(
                    "blind_review",
                    blind_usage,
                    blind_receipts,
                )
            except Exception:
                return None
        elif blind_started:
            return None

        receipts = qwen_receipts + agent_receipts + blind_receipts
        if len({item.call_id for item in receipts}) != len(receipts):
            return None
        return qwen_usage, agent_usage, blind_usage, receipts

    def _recover_terminal_agent_evidence(
        self,
        context: LocalRecoveryComparisonContext,
        frozen_runtime: FrozenRuntimeConfigStore,
    ) -> tuple[RecoveryUsage, tuple[RecoveryCallReceipt, ...]] | None:
        try:
            runner = self._agent_runner(context, frozen_runtime)
            case = self._case_request().case
            assert case is not None
            existing = runner.list(case_id=case.case_id)
            if len(existing) != 1 or existing[0].status not in {"completed", "failed"}:
                return None
            artifacts = (
                runner.load_completed_artifacts(existing[0].id)
                if existing[0].status == "completed"
                else runner.load_terminal_failure_artifacts(existing[0].id)
            )
            usage_payload = artifacts.output.get("execution_cost") or {}
            usage = RecoveryUsage(
                llm_calls=int(usage_payload.get("llm_calls", 0) or 0),
                input_tokens=int(usage_payload.get("input_tokens", 0) or 0),
                output_tokens=int(usage_payload.get("output_tokens", 0) or 0),
                wall_time_seconds=float(
                    usage_payload.get("wall_time_seconds", 0) or 0
                ),
                technical_failures=tuple(
                    str(item)
                    for item in usage_payload.get("technical_failures", [])
                ),
            )
            receipts = recovery_receipts_from_agent_usage(
                usage_payload,
                campaign_id=context.campaign_id,
                round_id=context.comparison_id,
            )
            _require_exact_phase_evidence("agent_laboratory", usage, receipts)
            if usage.llm_calls > 20:
                return None
            return usage, receipts
        except Exception:
            return None

    def _case_request(self) -> CreateRunRequest:
        request = CreateRunRequest.model_validate_json(
            self.visible_input_path.read_text(encoding="utf-8")
        )
        if (
            request.mode != "research"
            or request.model_provider != "qwen"
            or request.execution_mode != "external"
        ):
            raise ValueError("recovery visible request must use the real research runtime")
        return request

    def _round_root(self, context: LocalRecoveryRoundContext) -> Path:
        return self.working_root / context.campaign_id / context.round_id

    def _comparison_root(self, context: LocalRecoveryComparisonContext) -> Path:
        return self.working_root / context.campaign_id / context.comparison_id


async def _drive_recovery_workflow(
    engine: WorkflowEngine,
    state: RunState,
    context: LocalRecoveryRoundContext,
) -> RunState:
    if state.status == "completed":
        return state
    if state.status != "waiting_human":
        raise RuntimeError("recovery workflow is not at a resumable gate")
    if state.current_gate == "H1":
        state = await engine.decide_gate(
            state.id,
            "H1",
            _gate_request(state, context, "H1", action="approve"),
        )
    if state.status == "waiting_human" and state.current_gate == "H2":
        for index in range(2):
            arena = _artifact_payload(state, "design_arena")
            recommended = [
                str(value) for value in arena.get("recommended_candidate_ids", [])
            ]
            provisional = str(arena.get("provisional_candidate_id") or "")
            selected = (
                provisional
                if provisional in recommended
                else (sorted(recommended)[0] if recommended else "")
            )
            if not selected:
                raise RuntimeError("recovery H2 has no recommended candidate")
            state = await engine.decide_gate(
                state.id,
                "H2",
                _gate_request(
                    state,
                    context,
                    "H2",
                    action="approve",
                    selected_candidate_id=selected,
                    suffix=str(index),
                ),
            )
            if not (state.status == "waiting_human" and state.current_gate == "H2"):
                break
    if state.status == "waiting_human" and state.current_gate == "H3":
        for index in range(2):
            ledger = ClaimLedger.model_validate(_artifact_payload(state, "claim_ledger"))
            decisions = [_recovery_claim_decision(claim) for claim in ledger.claims]
            h3_action = (
                "approve"
                if any(
                    item.decision in {"approve", "downgrade"}
                    for item in decisions
                )
                else "generate_identification_failure_report"
            )
            state = await engine.decide_gate(
                state.id,
                "H3",
                _gate_request(
                    state,
                    context,
                    "H3",
                    action=h3_action,
                    claims=decisions,
                    suffix=str(index),
                ),
            )
            if not (state.status == "waiting_human" and state.current_gate == "H3"):
                break
    if state.status == "waiting_human" and state.current_gate == "H4":
        manuscript = ManuscriptPackage.model_validate(
            _artifact_payload(state, "manuscript_package")
        )
        if manuscript.audit_result != "pass_with_no_critical_issues":
            raise RuntimeError("recovery manuscript did not pass deterministic audit")
        state = await engine.decide_gate(
            state.id,
            "H4",
            _gate_request(state, context, "H4", action="approve"),
        )
    if state.status != "completed":
        raise RuntimeError("recovery workflow did not reach completed state")
    return state


def _gate_request(
    state: RunState,
    context: LocalRecoveryRoundContext,
    gate: str,
    *,
    action: str,
    selected_candidate_id: str | None = None,
    claims: list[ClaimDecisionInput] | None = None,
    suffix: str = "0",
) -> GateDecisionRequest:
    return GateDecisionRequest(
        action=action,
        actor="recovery_campaign_runner",
        comment="Code-owned seen-case recovery gate policy.",
        expected_run_version=state.version,
        idempotency_key=(
            f"recovery-{context.campaign_id}-{context.reservation_id}-"
            f"{context.round_id}-{gate.lower()}-{suffix}"
        ),
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


def _recovery_claim_decision(claim: ClaimRecord) -> ClaimDecisionInput:
    permitted = permitted_h3_decisions(claim)
    if "approve" in permitted:
        decision = "approve"
    elif "downgrade" in permitted:
        decision = "downgrade"
    else:
        validate_h3_claim_decision(claim, "reject")
        return ClaimDecisionInput(
            claim_id=claim.claim_id,
            decision="reject",
            reason="The deterministic recovery Claim Gate prohibited admission.",
        )
    subject = (
        "该机制或交互仅呈现关联边界"
        if claim.claim_type == "mechanism"
        else "核心解释变量与结果变量存在条件关联"
    )
    candidate_texts = (
        f"在冻结样本和模型设定下，{subject}；该结果不支持因果解释。",
        f"证据混合且存在不一致结果，{subject}未稳健；该结果不支持因果解释。",
        f"现有证据有限，{subject}仅属初步证据；该结果不支持因果解释。",
    )
    text = None
    for candidate_text in candidate_texts:
        try:
            # Reuse the Claim Gate's tighter allowed/max ceiling instead of
            # maintaining a second strength ranking in the recovery runner.
            validate_h3_claim_decision(claim, decision, candidate_text)
        except ClaimGateError:
            continue
        text = candidate_text
        break
    if text is None:
        raise RuntimeError(
            f"recovery H3 could not calibrate an admitted Claim: {claim.claim_id}"
        )
    return ClaimDecisionInput(
        claim_id=claim.claim_id,
        decision=decision,
        final_text=text,
        reason="Recovery H3 policy follows the deterministic Claim Gate ceiling.",
    )


def _build_round_packet(
    state: RunState,
    context: LocalRecoveryRoundContext,
    *,
    model: str,
) -> BenchmarkPacket:
    receipts = _model_receipts_from_state(state)
    usage = _usage_from_model_receipts(receipts)
    component_artifact_sha256: dict[str, str] = {}
    for artifact_key in dict.fromkeys(ABLATION_NATIVE_ARTIFACTS.values()):
        _artifact_payload(state, artifact_key)
        component_artifact_sha256[artifact_key] = str(
            state.artifacts[artifact_key]["sha256"]
        )
    packet = build_hypoweaver_packet(
        packet_id=f"recovery-hypoweaver-{context.round_id}",
        case_id=context.freeze.case_id,
        visible_input_sha256=context.freeze.visible_input_sha256,
        data_sha256=list(context.freeze.data_sha256),
        model_id=model,
        plan=AnalysisPlan.model_validate(_artifact_payload(state, "analysis_plan")),
        research_run=ResearchRun.model_validate(
            _artifact_payload(state, "research_run")
        ),
        claim_ledger=ClaimLedger.model_validate(
            _artifact_payload(state, "approved_claim_ledger")
        ),
        manuscript=ManuscriptPackage.model_validate(
            _artifact_payload(state, "manuscript_package")
        ),
        reproduction_audit=ReproductionAudit.model_validate(
            _artifact_payload(state, "reproduction_audit")
        ),
        formal_contract=FormalResearchContract.model_validate(
            _artifact_payload(state, "formal_research_contract")
        ),
        resource_usage=_benchmark_usage(usage),
        component_artifact_sha256=component_artifact_sha256,
    )
    sealed_output = _artifact_payload(state, "sealed_output")
    return seal_benchmark_packet(
        packet.model_copy(
            update={
                "native_artifact_sha256": {
                    **packet.native_artifact_sha256,
                    "sealed_output": canonical_sha256(sealed_output),
                },
                "official_receipts": [],
                "packet_sha256": None,
            }
        )
    )


def _artifact_payload(state: RunState, key: str) -> dict[str, Any]:
    envelope = state.artifacts.get(key)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise RuntimeError(f"recovery artifact is missing: {key}")
    payload = envelope["payload"]
    if canonical_sha256(payload) != envelope.get("sha256"):
        raise RuntimeError(f"recovery artifact sha256 mismatch: {key}")
    return payload


def _model_receipts_from_state(state: RunState) -> list[ModelCallReceipt]:
    payload = _artifact_payload(state, "model_usage")
    raw_receipts = payload.get("call_receipts")
    if not isinstance(raw_receipts, list):
        raise ValueError("recovery model usage has no receipt list")
    receipts = [ModelCallReceipt.model_validate(item) for item in raw_receipts]
    if int(payload.get("llm_calls", 0) or 0) != len(receipts):
        raise ValueError("recovery model usage receipt count mismatch")
    if int(payload.get("input_tokens", 0) or 0) != sum(
        item.input_tokens for item in receipts
    ) or int(payload.get("output_tokens", 0) or 0) != sum(
        item.output_tokens for item in receipts
    ):
        raise ValueError("recovery model usage receipt token mismatch")
    return receipts


def _usage_from_model_receipts(
    receipts: list[ModelCallReceipt],
) -> RecoveryUsage:
    failures = [
        str(receipt.error_type)
        for receipt in receipts
        if receipt.error_type is not None
    ]
    wall_time = sum(
        max(
            0.0,
            (
                datetime.fromisoformat(receipt.completed_at)
                - datetime.fromisoformat(receipt.started_at)
            ).total_seconds(),
        )
        for receipt in receipts
    )
    return RecoveryUsage(
        llm_calls=len(receipts),
        input_tokens=sum(item.input_tokens for item in receipts),
        output_tokens=sum(item.output_tokens for item in receipts),
        wall_time_seconds=wall_time,
        technical_failures=tuple(failures),
    )


def _blind_usage(view: PairedEvaluationView) -> RecoveryUsage:
    return RecoveryUsage(
        llm_calls=sum(item.llm_calls for item in view.review_resource_usage),
        input_tokens=sum(item.input_tokens for item in view.review_resource_usage),
        output_tokens=sum(item.output_tokens for item in view.review_resource_usage),
        wall_time_seconds=sum(
            item.wall_time_seconds for item in view.review_resource_usage
        ),
        technical_failures=tuple(
            failure
            for item in view.review_resource_usage
            for failure in item.technical_failures
        ),
    )


def _require_exact_phase_evidence(
    phase: str,
    usage: RecoveryUsage,
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    if len(receipts) != usage.llm_calls:
        raise ValueError(f"{phase} recovery receipt count is incomplete")
    if any(item.phase != phase for item in receipts):
        raise ValueError(f"{phase} recovery receipt phase mismatch")
    if sum(item.input_tokens for item in receipts) != usage.input_tokens:
        raise ValueError(f"{phase} recovery input-token accounting is incomplete")
    if sum(item.output_tokens for item in receipts) != usage.output_tokens:
        raise ValueError(f"{phase} recovery output-token accounting is incomplete")


def _require_recovery_receipt_binding(
    context: LocalRecoveryComparisonContext,
    receipts: tuple[RecoveryCallReceipt, ...],
) -> None:
    if any(item.campaign_id != context.campaign_id for item in receipts):
        raise ValueError("comparison receipt campaign binding mismatch")
    if any(item.round_id != context.comparison_id for item in receipts):
        raise ValueError("comparison receipt round binding mismatch")


def _is_nonzero_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and value != "0" * 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mark_phase_started(
    path: Path,
    *,
    phase: str,
    campaign_id: str,
    comparison_id: str,
) -> None:
    if path.is_file():
        return
    _write_once(
        path,
        {
            "event": "provider_phase_started",
            "phase": phase,
            "campaign_id": campaign_id,
            "comparison_id": comparison_id,
            "started_at": _utc_now(),
        },
    )


def _mark_round_started(
    path: Path,
    context: LocalRecoveryRoundContext,
) -> None:
    if path.is_file():
        return
    _write_once(
        path,
        {
            "event": "model_facing_round_started",
            "campaign_id": context.campaign_id,
            "round_id": context.round_id,
            "reservation_id": context.reservation_id,
            "call_limit": context.call_limit,
            "started_at": _utc_now(),
        },
    )


def recovery_receipt_from_qwen_metadata(
    metadata: QwenSinglePassCallMetadata,
    *,
    campaign_id: str,
    round_id: str,
    succeeded: bool,
    error_type: str | None,
) -> RecoveryCallReceipt:
    source_hash = canonical_sha256(metadata.model_dump(mode="json"))
    response_hash = metadata.raw_response_sha256 or canonical_sha256(
        {"source_receipt_sha256": source_hash, "error_type": error_type}
    )
    return RecoveryCallReceipt(
        call_id=str(uuid4()),
        campaign_id=campaign_id,
        round_id=round_id,
        phase="qwen_single_pass",
        attempt_type="primary",
        attempt_index=1,
        max_attempts=1,
        outcome="succeeded" if succeeded else "provider_failure",
        provider=metadata.provider,
        model=metadata.model,
        response_sha256=response_hash,
        input_sha256=metadata.input_sha256,
        source_receipt_sha256=source_hash,
        input_tokens=metadata.resource_usage.input_tokens,
        output_tokens=metadata.resource_usage.output_tokens,
        error_type=None if succeeded else (error_type or "QwenSinglePassFailure"),
        call_started_at=metadata.call_started_at,
        call_completed_at=metadata.call_completed_at,
    )


def recovery_receipts_from_agent_usage(
    usage: dict[str, Any],
    *,
    campaign_id: str,
    round_id: str,
) -> tuple[RecoveryCallReceipt, ...]:
    raw_receipts = usage.get("call_receipts") or []
    if not isinstance(raw_receipts, list):
        raise ValueError("Agent Laboratory call_receipts must be a list")
    total_input = int(usage.get("input_tokens", 0) or 0)
    total_output = int(usage.get("output_tokens", 0) or 0)
    receipt_token_fields_present = any(
        isinstance(item, dict)
        and ("input_tokens" in item or "output_tokens" in item)
        for item in raw_receipts
    )
    converted: list[RecoveryCallReceipt] = []
    for index, raw in enumerate(raw_receipts, start=1):
        if not isinstance(raw, dict):
            raise ValueError("Agent Laboratory receipt is malformed")
        succeeded = str(raw.get("status") or "") == "completed"
        source_hash = canonical_sha256(raw)
        converted.append(
            RecoveryCallReceipt(
                call_id=str(uuid4()),
                campaign_id=campaign_id,
                round_id=round_id,
                phase="agent_laboratory",
                attempt_type="legacy",
                attempt_index=1,
                max_attempts=1,
                outcome="succeeded" if succeeded else "provider_failure",
                provider=str(raw.get("provider") or ""),
                model=str(raw.get("model") or ""),
                response_sha256=str(raw.get("response_sha256") or ""),
                source_receipt_sha256=source_hash,
                input_tokens=int(
                    raw.get(
                        "input_tokens",
                        total_input
                        if index == 1 and not receipt_token_fields_present
                        else 0,
                    )
                    or 0
                ),
                output_tokens=int(
                    raw.get(
                        "output_tokens",
                        total_output
                        if index == 1 and not receipt_token_fields_present
                        else 0,
                    )
                    or 0
                ),
                error_type=None if succeeded else "AgentLaboratoryProviderFailure",
                error_category=(
                    None if succeeded else raw.get("error_category")
                ),
                call_started_at=str(
                    raw.get("call_started_at") or raw.get("started_at") or ""
                ),
                call_completed_at=str(
                    raw.get("call_completed_at") or raw.get("completed_at") or ""
                ),
            )
        )
    return tuple(converted)


def recovery_receipts_from_blind_view(
    view: PairedEvaluationView,
    *,
    campaign_id: str,
    round_id: str,
) -> tuple[RecoveryCallReceipt, ...]:
    converted: list[RecoveryCallReceipt] = []
    for source in view.review_call_receipts:
        converted.append(
            _recovery_receipt_from_blind_source(
                source,
                campaign_id=campaign_id,
                round_id=round_id,
            )
        )
    return tuple(converted)


def _recovery_receipt_from_blind_source(
    source: PairedBlindCallReceipt,
    *,
    campaign_id: str,
    round_id: str,
) -> RecoveryCallReceipt:
    succeeded = source.outcome == "succeeded"
    return RecoveryCallReceipt(
        call_id=source.call_id,
        campaign_id=campaign_id,
        round_id=round_id,
        phase="blind_review",
        attempt_type="primary",
        attempt_index=1,
        max_attempts=1,
        outcome="succeeded" if succeeded else "provider_failure",
        provider=source.provider,
        model=source.model,
        response_sha256=(
            source.response_sha256
            if succeeded
            else str(source.failure_package_sha256)
        ),
        source_receipt_sha256=canonical_sha256(source.model_dump(mode="json")),
        input_tokens=source.input_tokens,
        output_tokens=source.output_tokens,
        error_type=None if succeeded else "PairedBlindProviderFailure",
        call_started_at=source.call_started_at,
        call_completed_at=source.call_completed_at,
    )


def _packet_with_usage(packet: BenchmarkPacket, usage: RecoveryUsage) -> BenchmarkPacket:
    verify_benchmark_packet(packet)
    return seal_benchmark_packet(
        packet.model_copy(
            update={
                "resource_usage": _benchmark_usage(usage),
                "official_receipts": [],
                "packet_sha256": None,
            }
        )
    )


def _benchmark_usage(value: RecoveryUsage | BenchmarkResourceUsage) -> BenchmarkResourceUsage:
    if isinstance(value, BenchmarkResourceUsage):
        return value
    return BenchmarkResourceUsage(
        llm_calls=value.llm_calls,
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        wall_time_seconds=value.wall_time_seconds,
        technical_failures=list(value.technical_failures),
    )


def _completion_after_receipts(receipts: tuple[RecoveryCallReceipt, ...]) -> str:
    now = _utc_now()
    if not receipts:
        return now
    latest = max(datetime.fromisoformat(item.call_completed_at) for item in receipts)
    return max(datetime.fromisoformat(now), latest).isoformat()


def _started_before_receipts(
    started_at: str,
    receipts: tuple[RecoveryCallReceipt, ...],
) -> str:
    if not receipts:
        return started_at
    earliest = min(datetime.fromisoformat(item.call_started_at) for item in receipts)
    return min(datetime.fromisoformat(started_at), earliest).isoformat()


def _load_result(path: Path, model: type[Any]) -> Any | None:
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _write_once(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if canonical_sha256(existing) != canonical_sha256(payload):
            raise ValueError("recovery backend terminal result is append-only")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(rendered)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
