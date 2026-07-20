from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import ConfigDict, BaseModel, Field, model_validator

from .benchmark_evaluator import evaluate_hard_metrics, verify_benchmark_packet
from .benchmark_faults import replay_ablations
from .benchmark_models import BenchmarkPacket, FrozenBenchmarkProtocol
from .benchmark_protocol import verify_protocol
from .case_import import DatasetRegistry
from .local_recovery_runner import (
    LocalRecoveryRoundContext,
    LocalRecoveryRoundResult,
)
from .models import CreateRunRequest
from .production_recovery_backend import (
    ProductionRecoveryBackend,
    assert_recovery_paths_separate,
    load_recovery_source_configuration,
)
from .recovery_campaign import (
    _validate_round_usage_evidence,
    build_recovery_freeze,
    verify_recovery_campaign,
    verify_recovery_environment,
)
from .recovery_models import (
    RecoveryCallReceipt,
    RecoveryCampaign,
    RecoveryFreeze,
    RecoveryUsage,
)
from .seal import canonical_sha256


PROTOCOL_FILE = "engineering-replay-protocol.json"
FREEZE_FILE = "recovery-freeze.json"
STATE_FILE = "engineering-replay-state.json"
LOCK_FILE = ".engineering-replay.lock"
FULL_PROVIDER_ATTEMPT_BUDGET = 20
TERMINAL_STATUSES = {
    "completed_hard_gate_passed",
    "completed_hard_gate_failed",
    "technical_failed",
    "invalidated_unknown_usage",
    "invalidated_source_drift",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EngineeringReplayBudgetBinding(_FrozenModel):
    cumulative_call_ceiling: int = Field(ge=1)
    cumulative_calls_before: int = Field(ge=0)
    cumulative_calls_remaining: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_budget(self) -> "EngineeringReplayBudgetBinding":
        if (
            self.cumulative_calls_before + self.cumulative_calls_remaining
            != self.cumulative_call_ceiling
        ):
            raise ValueError("engineering replay cumulative budget must be additive")
        return self


PredecessorReplayTerminalStatus = Literal[
    "completed_hard_gate_passed",
    "completed_hard_gate_failed",
    "technical_failed",
]


class EngineeringReplayPredecessorBinding(_FrozenModel):
    binding_version: Literal[1] = 1
    replay_id: str = Field(min_length=1)
    terminal_status: PredecessorReplayTerminalStatus
    delivery_root: str
    working_root: str
    state_root: str
    protocol_file_sha256: str
    protocol_sha256: str
    state_file_sha256: str
    state_sha256: str
    manifest_file_sha256: str
    resource_usage_file_sha256: str
    model_call_receipts_file_sha256: str
    exact_usage: RecoveryUsage
    binding_sha256: str | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "EngineeringReplayPredecessorBinding":
        hashes = (
            self.protocol_file_sha256,
            self.protocol_sha256,
            self.state_file_sha256,
            self.state_sha256,
            self.manifest_file_sha256,
            self.resource_usage_file_sha256,
            self.model_call_receipts_file_sha256,
        )
        if self.binding_sha256 is not None:
            hashes = (*hashes, self.binding_sha256)
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise ValueError("predecessor replay hashes must be lowercase SHA256")
        payload = self.model_dump(mode="json", exclude={"binding_sha256"})
        expected = canonical_sha256(payload)
        if self.binding_sha256 is not None and self.binding_sha256 != expected:
            raise ValueError("predecessor replay binding sha256 mismatch")
        return self


def _seal_predecessor_replay_binding(
    binding: EngineeringReplayPredecessorBinding,
) -> EngineeringReplayPredecessorBinding:
    payload = binding.model_dump(mode="json", exclude={"binding_sha256"})
    expected = canonical_sha256(payload)
    if binding.binding_sha256 is not None and binding.binding_sha256 != expected:
        raise ValueError("predecessor replay binding sha256 mismatch")
    return binding.model_copy(update={"binding_sha256": expected})


class EngineeringReplayProtocol(_FrozenModel):
    protocol_version: Literal[1] = 1
    replay_id: str = Field(min_length=1)
    provenance_scope: Literal["seen_case_engineering_replay_non_benchmark"] = (
        "seen_case_engineering_replay_non_benchmark"
    )
    official: Literal[False] = False
    benchmark_eligible: Literal[False] = False
    seen_case: Literal[True] = True
    comparison_allowed: Literal[False] = False
    max_runs: Literal[1] = 1
    provider_attempt_ceiling: Literal[20] = 20
    predecessor_usage_inherited: Literal[False] = False
    predecessor_budget_carried_over: Literal[False] = False
    source_config_path: str
    source_config_sha256: str
    source_official_protocol_path: str
    source_official_protocol_sha256: str
    predecessor_campaign_path: str
    predecessor_campaign_id: str
    predecessor_campaign_file_sha256: str
    predecessor_campaign_sha256: str
    delivery_root: str
    working_root: str
    state_root: str
    freeze: RecoveryFreeze
    prepared_at: str
    protocol_sha256: str | None = None

    @model_validator(mode="after")
    def validate_hashes(self) -> "EngineeringReplayProtocol":
        hashes = (
            self.source_config_sha256,
            self.source_official_protocol_sha256,
            self.predecessor_campaign_file_sha256,
            self.predecessor_campaign_sha256,
        )
        if self.protocol_sha256 is not None:
            hashes = (*hashes, self.protocol_sha256)
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise ValueError("engineering replay hashes must be lowercase SHA256")
        if self.freeze.predecessor_binding is not None:
            raise ValueError("engineering replay must not inherit predecessor usage")
        return self


class EngineeringReplayProtocolV2(EngineeringReplayProtocol):
    protocol_version: Literal[2] = 2
    provider_attempt_ceiling: int = Field(ge=1, le=20)
    budget_binding: EngineeringReplayBudgetBinding
    predecessor_replay_binding: EngineeringReplayPredecessorBinding | None = None

    @model_validator(mode="after")
    def validate_v2_budget(self) -> "EngineeringReplayProtocolV2":
        if (
            self.provider_attempt_ceiling
            > self.budget_binding.cumulative_calls_remaining
        ):
            raise ValueError(
                "provider attempt ceiling exceeds cumulative calls remaining"
            )
        if (
            self.predecessor_replay_binding is not None
            and self.predecessor_replay_binding.binding_sha256 is None
        ):
            raise ValueError("predecessor replay binding must be sealed")
        return self


AnyEngineeringReplayProtocol = (
    EngineeringReplayProtocol | EngineeringReplayProtocolV2
)


EngineeringReplayStatus = Literal[
    "prepared",
    "running",
    "completed_hard_gate_passed",
    "completed_hard_gate_failed",
    "technical_failed",
    "invalidated_unknown_usage",
    "invalidated_source_drift",
]


class EngineeringReplayState(_FrozenModel):
    state_version: Literal[1] = 1
    replay_id: str
    protocol_sha256: str
    status: EngineeringReplayStatus
    run_count: int = Field(default=0, ge=0, le=1)
    provider_call_started: bool = False
    provider_attempt_ceiling: int = Field(default=20, ge=1, le=20)
    run_owner_id: str | None = None
    usage_evidence_status: Literal["not_started", "exact", "unknown"] = (
        "not_started"
    )
    usage: RecoveryUsage | None = None
    reason_code: str | None = None
    artifact_sha256: dict[str, str] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    state_sha256: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "EngineeringReplayState":
        if self.status == "prepared":
            if self.run_count or self.provider_call_started:
                raise ValueError("prepared replay cannot have started its provider run")
        elif self.status == "invalidated_source_drift":
            if self.run_count or self.provider_call_started:
                raise ValueError("source drift must be detected before provider entry")
        elif self.run_count != 1 or not self.provider_call_started:
            raise ValueError("provider-facing replay states require exactly one run")
        if self.status == "running":
            if self.usage is not None or self.reason_code is not None:
                raise ValueError("running replay cannot contain terminal evidence")
        elif self.status in TERMINAL_STATUSES and not self.reason_code:
            if not self.status.startswith("completed_hard_gate_"):
                raise ValueError("failed terminal replay requires a reason code")
        if self.usage_evidence_status == "exact":
            if self.usage is None:
                raise ValueError("exact usage evidence requires usage")
        elif self.usage is not None:
            raise ValueError("non-exact usage evidence cannot contain usage")
        if (
            self.usage is not None
            and self.usage.llm_calls > self.provider_attempt_ceiling
        ):
            raise ValueError("engineering replay exceeded its provider attempt ceiling")
        hashes = (self.protocol_sha256, *self.artifact_sha256.values())
        if self.state_sha256 is not None:
            hashes = (*hashes, self.state_sha256)
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise ValueError("engineering replay state hashes must be lowercase SHA256")
        return self


class EngineeringReplayBackend(Protocol):
    async def run_hypoweaver_round(
        self,
        context: LocalRecoveryRoundContext,
    ) -> LocalRecoveryRoundResult: ...


def _seal_protocol(
    protocol: AnyEngineeringReplayProtocol,
) -> AnyEngineeringReplayProtocol:
    payload = protocol.model_dump(mode="json", exclude={"protocol_sha256"})
    expected = canonical_sha256(payload)
    if protocol.protocol_sha256 is not None and protocol.protocol_sha256 != expected:
        raise ValueError("engineering replay protocol sha256 mismatch")
    return protocol.model_copy(update={"protocol_sha256": expected})


def _verify_protocol(protocol: AnyEngineeringReplayProtocol) -> None:
    if protocol.protocol_sha256 is None:
        raise ValueError("engineering replay protocol is not sealed")
    _seal_protocol(protocol)


def _parse_protocol(text: str) -> AnyEngineeringReplayProtocol:
    payload = json.loads(text)
    version = payload.get("protocol_version", 1)
    if version == 1:
        protocol: AnyEngineeringReplayProtocol = (
            EngineeringReplayProtocol.model_validate(payload)
        )
    elif version == 2:
        protocol = EngineeringReplayProtocolV2.model_validate(payload)
    else:
        raise ValueError("unsupported engineering replay protocol version")
    _verify_protocol(protocol)
    return protocol


def _seal_state(state: EngineeringReplayState) -> EngineeringReplayState:
    payload = state.model_dump(mode="json", exclude={"state_sha256"})
    expected = canonical_sha256(payload)
    if state.state_sha256 is not None and state.state_sha256 != expected:
        raise ValueError("engineering replay state sha256 mismatch")
    return state.model_copy(update={"state_sha256": expected})


def _verify_state(state: EngineeringReplayState) -> None:
    if state.state_sha256 is None:
        raise ValueError("engineering replay state is not sealed")
    _seal_state(state)


class EngineeringReplayController:
    """One provider-facing run on a seen case; never a formal benchmark."""

    def __init__(
        self,
        *,
        source_config_path: Path,
        delivery_root: Path,
        predecessor_campaign_path: Path,
        provider_attempt_ceiling: int = 20,
        predecessor_replay_delivery_root: Path | None = None,
        cumulative_call_ceiling: int | None = None,
        cumulative_calls_before: int | None = None,
        cumulative_calls_remaining: int | None = None,
        backend: EngineeringReplayBackend | None = None,
    ) -> None:
        self.source_config_path = source_config_path.resolve(strict=False)
        self.delivery_root = delivery_root.resolve(strict=False)
        self.working_root = self.delivery_root.with_name(
            f"{self.delivery_root.name}-work"
        )
        self.state_root = self.delivery_root.with_name(
            f"{self.delivery_root.name}-state"
        )
        self.predecessor_campaign_path = predecessor_campaign_path.resolve(
            strict=False
        )
        if not 1 <= provider_attempt_ceiling <= 20:
            raise ValueError("provider attempt ceiling must be between one and twenty")
        self.provider_attempt_ceiling = provider_attempt_ceiling
        self.predecessor_replay_delivery_root = (
            predecessor_replay_delivery_root.resolve(strict=False)
            if predecessor_replay_delivery_root is not None
            else None
        )
        budget_values = (
            cumulative_call_ceiling,
            cumulative_calls_before,
            cumulative_calls_remaining,
        )
        if any(value is not None for value in budget_values) and not all(
            value is not None for value in budget_values
        ):
            raise ValueError("engineering replay cumulative budget is incomplete")
        self.budget_binding = (
            EngineeringReplayBudgetBinding(
                cumulative_call_ceiling=cumulative_call_ceiling,
                cumulative_calls_before=cumulative_calls_before,
                cumulative_calls_remaining=cumulative_calls_remaining,
            )
            if all(value is not None for value in budget_values)
            else None
        )
        self.protocol_version = (
            2
            if self.provider_attempt_ceiling != 20
            or self.predecessor_replay_delivery_root is not None
            or self.budget_binding is not None
            else 1
        )
        if self.protocol_version == 2 and self.budget_binding is None:
            raise ValueError("engineering replay v2 requires cumulative budget binding")
        if (
            self.budget_binding is not None
            and self.provider_attempt_ceiling
            > self.budget_binding.cumulative_calls_remaining
        ):
            raise ValueError(
                "provider attempt ceiling exceeds cumulative calls remaining"
            )
        self.backend = backend
        self.protocol_path = self.delivery_root / PROTOCOL_FILE
        self.freeze_path = self.delivery_root / FREEZE_FILE
        self.state_path = self.state_root / STATE_FILE
        self.lock_path = self.state_root / LOCK_FILE

    def prepare(self) -> AnyEngineeringReplayProtocol:
        existing = self._load_existing_preparation()
        if existing is not None:
            return existing

        source = load_recovery_source_configuration(self.source_config_path)
        self._verify_root_isolation(source)
        predecessor = self._load_predecessor()
        predecessor_replay_binding = self._load_predecessor_replay_binding()
        official_protocol_path = source.resolve_source(source.protocol_path)
        official_protocol = FrozenBenchmarkProtocol.model_validate_json(
            official_protocol_path.read_text(encoding="utf-8")
        )
        verify_protocol(official_protocol)
        visible_input_path = source.resolve_source(source.visible_input_path)
        reference_path = source.resolve_source(source.reference_path)
        reference_summary_path = source.resolve_source(
            source.reference_summary_path
        )
        data_paths = self._data_paths(visible_input_path)
        freeze = build_recovery_freeze(
            official_protocol,
            artifact_root=Path(source.artifact_root),
            visible_input_path=visible_input_path,
            data_paths=data_paths,
            reference_path=reference_path,
            reference_summary_path=reference_summary_path,
            predecessor_campaign=None,
        )
        replay_identity = canonical_sha256(
            {
                "scope": "seen_case_engineering_replay_non_benchmark",
                "predecessor_campaign_id": predecessor.campaign_id,
                "predecessor_campaign_file_sha256": _file_sha256(
                    self.predecessor_campaign_path
                ),
                "freeze_sha256": freeze.freeze_sha256,
                "delivery_root": str(self.delivery_root),
                "provider_attempt_ceiling": self.provider_attempt_ceiling,
                "budget_binding": (
                    self.budget_binding.model_dump(mode="json")
                    if self.budget_binding is not None
                    else None
                ),
                "predecessor_replay_binding_sha256": (
                    predecessor_replay_binding.binding_sha256
                    if predecessor_replay_binding is not None
                    else None
                ),
            }
        )
        protocol_values = {
            "replay_id": f"engineering-replay-{replay_identity[:24]}",
            "source_config_path": str(self.source_config_path),
            "source_config_sha256": _file_sha256(self.source_config_path),
            "source_official_protocol_path": str(official_protocol_path),
            "source_official_protocol_sha256": str(
                official_protocol.protocol_sha256
            ),
            "predecessor_campaign_path": str(self.predecessor_campaign_path),
            "predecessor_campaign_id": predecessor.campaign_id,
            "predecessor_campaign_file_sha256": _file_sha256(
                self.predecessor_campaign_path
            ),
            "predecessor_campaign_sha256": predecessor.campaign_sha256,
            "delivery_root": str(self.delivery_root),
            "working_root": str(self.working_root),
            "state_root": str(self.state_root),
            "freeze": freeze,
            "prepared_at": _utc_now(),
        }
        protocol = _seal_protocol(
            EngineeringReplayProtocolV2(
                **protocol_values,
                provider_attempt_ceiling=self.provider_attempt_ceiling,
                budget_binding=self.budget_binding,
                predecessor_replay_binding=predecessor_replay_binding,
            )
            if self.protocol_version == 2
            else EngineeringReplayProtocol(**protocol_values)
        )
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        self.working_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        _write_once(self.freeze_path, freeze.model_dump(mode="json"))
        _write_once(self.protocol_path, protocol.model_dump(mode="json"))
        timestamp = _utc_now()
        state = _seal_state(
            EngineeringReplayState(
                replay_id=protocol.replay_id,
                protocol_sha256=str(protocol.protocol_sha256),
                status="prepared",
                provider_attempt_ceiling=protocol.provider_attempt_ceiling,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        _replace_json(self.state_path, state.model_dump(mode="json"))
        return protocol

    async def run(self) -> EngineeringReplayState:
        protocol = self.prepare()
        with self._locked():
            state = self._load_state()
            if state.status in TERMINAL_STATUSES:
                return state
            if state.status == "running":
                return self._finalize_without_evidence(
                    state,
                    status="invalidated_unknown_usage",
                    reason_code="interrupted_provider_run_has_unknown_usage",
                )
            if not isinstance(protocol, EngineeringReplayProtocolV2):
                raise ValueError(
                    "new provider-facing engineering replay requires a v2 "
                    "cumulative budget binding"
                )
            remaining = protocol.budget_binding.cumulative_calls_remaining
            if (
                protocol.provider_attempt_ceiling < FULL_PROVIDER_ATTEMPT_BUDGET
                or remaining < FULL_PROVIDER_ATTEMPT_BUDGET
            ):
                raise ValueError(
                    "engineering replay requires a full 20-attempt provider "
                    "budget before starting "
                    f"(ceiling={protocol.provider_attempt_ceiling}, "
                    f"remaining={remaining})"
                )
            try:
                official_protocol, reference = self._verify_frozen_sources(protocol)
            except Exception as error:
                return self._finalize_without_evidence(
                    state,
                    status="invalidated_source_drift",
                    reason_code=f"source_drift_{type(error).__name__}",
                )
            owner_id = f"engineering-replay-owner-{uuid4()}"
            running = _seal_state(
                state.model_copy(
                    update={
                        "status": "running",
                        "run_count": 1,
                        "provider_call_started": True,
                        "run_owner_id": owner_id,
                        "updated_at": _utc_now(),
                        "state_sha256": None,
                    }
                )
            )
            _replace_json(self.state_path, running.model_dump(mode="json"))

        backend = self.backend or ProductionRecoveryBackend(
            source_config_path=self.source_config_path,
            protocol=official_protocol,
            working_root=self.working_root,
            delivery_root=self.delivery_root,
            state_root=self.state_root,
            predecessor_campaign_path=None,
        )
        context = LocalRecoveryRoundContext(
            campaign_id=protocol.replay_id,
            reservation_id="engineering-replay-single-run",
            round_id="engineering-run-01",
            call_limit=protocol.provider_attempt_ceiling,
            freeze=protocol.freeze,
        )
        try:
            raw_result = await backend.run_hypoweaver_round(context)
            result = LocalRecoveryRoundResult.model_validate(raw_result)
        except Exception as error:
            with self._locked():
                current = self._load_state()
                if current.status != "running" or current.run_owner_id != owner_id:
                    return current
                return self._finalize_without_evidence(
                    current,
                    status="invalidated_unknown_usage",
                    reason_code=f"provider_exception_{type(error).__name__}",
                )

        with self._locked():
            current = self._load_state()
            if current.status != "running" or current.run_owner_id != owner_id:
                return current
            return self._finalize_result(current, protocol, reference, result)

    def _finalize_result(
        self,
        state: EngineeringReplayState,
        protocol: AnyEngineeringReplayProtocol,
        reference,
        result: LocalRecoveryRoundResult,
    ) -> EngineeringReplayState:
        if result.usage.llm_calls > protocol.provider_attempt_ceiling:
            return self._finalize_exact_failure(
                state,
                result,
                "provider_attempt_ceiling_exceeded",
            )
        if result.status != "completed" or result.packet is None:
            return self._finalize_exact_failure(
                state,
                result,
                result.reason_code or "engineering_replay_incomplete",
            )
        try:
            packet = result.packet
            verify_benchmark_packet(packet)
            self._verify_packet_binding(packet, protocol.freeze, result.usage)
            fault_report = replay_ablations(packet)
            hard_report = evaluate_hard_metrics(
                packet,
                reference,
                fault_outcomes=list(fault_report.full_system_outcomes),
                clean_false_block_count=fault_report.clean_false_block_count,
            )
        except Exception as error:
            return self._finalize_exact_failure(
                state,
                result,
                f"post_run_evaluation_{type(error).__name__}",
            )

        files = self._write_exact_evidence(
            result,
            protocol.provider_attempt_ceiling,
        )
        files.update(
            {
                "benchmark-packet.json": packet.model_dump(mode="json"),
                "fault-ablation-report.json": fault_report.model_dump(mode="json"),
                "hard-metrics.json": hard_report.model_dump(mode="json"),
            }
        )
        status: EngineeringReplayStatus = (
            "completed_hard_gate_passed"
            if hard_report.all_hard_gates_passed
            else "completed_hard_gate_failed"
        )
        report = self._report_text(
            protocol,
            status=status,
            usage=result.usage,
            reason_code=None,
            hard_gates_passed=hard_report.all_hard_gates_passed,
        )
        artifacts = self._write_delivery(files, report)
        terminal = _seal_state(
            state.model_copy(
                update={
                    "status": status,
                    "usage_evidence_status": "exact",
                    "usage": result.usage,
                    "reason_code": None,
                    "artifact_sha256": artifacts,
                    "updated_at": _utc_now(),
                    "state_sha256": None,
                }
            )
        )
        _replace_json(self.state_path, terminal.model_dump(mode="json"))
        return terminal

    def _finalize_exact_failure(
        self,
        state: EngineeringReplayState,
        result: LocalRecoveryRoundResult,
        reason_code: str,
    ) -> EngineeringReplayState:
        protocol = self._load_protocol()
        files = self._write_exact_evidence(
            result,
            protocol.provider_attempt_ceiling,
        )
        report = self._report_text(
            protocol,
            status="technical_failed",
            usage=result.usage,
            reason_code=reason_code,
            hard_gates_passed=None,
        )
        artifacts = self._write_delivery(files, report)
        terminal = _seal_state(
            state.model_copy(
                update={
                    "status": "technical_failed",
                    "usage_evidence_status": "exact",
                    "usage": result.usage,
                    "reason_code": reason_code,
                    "artifact_sha256": artifacts,
                    "updated_at": _utc_now(),
                    "state_sha256": None,
                }
            )
        )
        _replace_json(self.state_path, terminal.model_dump(mode="json"))
        return terminal

    def _finalize_without_evidence(
        self,
        state: EngineeringReplayState,
        *,
        status: Literal[
            "invalidated_unknown_usage",
            "invalidated_source_drift",
        ],
        reason_code: str,
    ) -> EngineeringReplayState:
        evidence_status = "unknown" if status == "invalidated_unknown_usage" else "not_started"
        protocol = self._load_protocol()
        files = {
            "model-call-receipts.json": {
                "evidence_status": evidence_status,
                "receipts": [],
            },
            "resource-usage.json": {
                "evidence_status": evidence_status,
                "provider_attempt_ceiling": protocol.provider_attempt_ceiling,
            },
        }
        report = self._report_text(
            protocol,
            status=status,
            usage=None,
            reason_code=reason_code,
            hard_gates_passed=None,
        )
        artifacts = self._write_delivery(files, report, prefix="invalidation-")
        terminal = _seal_state(
            state.model_copy(
                update={
                    "status": status,
                    "usage_evidence_status": evidence_status,
                    "usage": None,
                    "reason_code": reason_code,
                    "artifact_sha256": artifacts,
                    "updated_at": _utc_now(),
                    "state_sha256": None,
                }
            )
        )
        _replace_json(self.state_path, terminal.model_dump(mode="json"))
        return terminal

    def _write_exact_evidence(
        self,
        result: LocalRecoveryRoundResult,
        provider_attempt_ceiling: int,
    ) -> dict[str, object]:
        return {
            "round-result.json": result.model_dump(mode="json"),
            "model-call-receipts.json": {
                "evidence_status": "exact",
                "receipts": [
                    receipt.model_dump(mode="json") for receipt in result.receipts
                ],
            },
            "resource-usage.json": {
                "evidence_status": "exact",
                "provider_attempt_ceiling": provider_attempt_ceiling,
                "usage": result.usage.model_dump(mode="json"),
            },
        }

    def _write_delivery(
        self,
        files: dict[str, object],
        report: str,
        *,
        prefix: str = "",
    ) -> dict[str, str]:
        named_files = {f"{prefix}{name}": payload for name, payload in files.items()}
        for name, payload in named_files.items():
            _write_once(self.delivery_root / name, payload)
        report_path = self.delivery_root / f"{prefix}中文工程回放报告.md"
        _write_text_once(report_path, report)
        file_sha256 = {
            FREEZE_FILE: _file_sha256(self.freeze_path),
            PROTOCOL_FILE: _file_sha256(self.protocol_path),
            **{
                name: _file_sha256(self.delivery_root / name)
                for name in named_files
            },
            report_path.name: _file_sha256(report_path),
        }
        manifest = {
            "manifest_version": 1,
            "official": False,
            "benchmark_eligible": False,
            "seen_case": True,
            "comparison_allowed": False,
            "file_sha256": file_sha256,
            "sealed_at": _utc_now(),
        }
        manifest_path = self.delivery_root / f"{prefix}hash-manifest.json"
        _write_once(manifest_path, manifest)
        return {
            **file_sha256,
            manifest_path.name: _file_sha256(manifest_path),
        }

    def _report_text(
        self,
        protocol: AnyEngineeringReplayProtocol,
        *,
        status: EngineeringReplayStatus,
        usage: RecoveryUsage | None,
        reason_code: str | None,
        hard_gates_passed: bool | None,
    ) -> str:
        usage_text = "未知（不完整调用证据）" if usage is None else str(usage.llm_calls)
        gate_text = (
            "全部通过"
            if hard_gates_passed is True
            else "未全部通过"
            if hard_gates_passed is False
            else "未评估"
        )
        return (
            "# Task3 同案例工程回放报告\n\n"
            "本运行是已见案例上的一次性工程验证，不是正式 benchmark，"
            "不具备无偏性，不允许比较性结论。\n\n"
            f"- 回放 ID：`{protocol.replay_id}`\n"
            f"- 终态：`{status}`\n"
            f"- 模型 provider attempt：{usage_text} / "
            f"{protocol.provider_attempt_ceiling}\n"
            f"- 绝对硬指标：{gate_text}\n"
            f"- 原因代码：`{reason_code or 'none'}`\n"
            f"- 只读前置 campaign：`{protocol.predecessor_campaign_id}`\n"
            "- 前置 120 次账本：未继承、未重画、未修改\n"
            "- 比较流程：未运行\n"
        )

    def _verify_packet_binding(
        self,
        packet: BenchmarkPacket,
        freeze: RecoveryFreeze,
        usage: RecoveryUsage,
    ) -> None:
        if (
            packet.system_id != "hypoweaver"
            or packet.case_id != freeze.case_id
            or packet.visible_input_sha256 != freeze.visible_input_sha256
            or tuple(packet.data_sha256) != freeze.data_sha256
            or packet.official_receipts
            or packet.resource_usage.llm_calls != usage.llm_calls
        ):
            raise ValueError("engineering replay packet binding mismatch")

    def _verify_frozen_sources(self, protocol: AnyEngineeringReplayProtocol):
        _verify_protocol(protocol)
        if _file_sha256(self.source_config_path) != protocol.source_config_sha256:
            raise ValueError("source configuration drift")
        predecessor = self._load_predecessor()
        if (
            _file_sha256(self.predecessor_campaign_path)
            != protocol.predecessor_campaign_file_sha256
            or predecessor.campaign_id != protocol.predecessor_campaign_id
            or predecessor.campaign_sha256 != protocol.predecessor_campaign_sha256
        ):
            raise ValueError("predecessor campaign drift")
        if isinstance(protocol, EngineeringReplayProtocolV2):
            current_binding = self._load_predecessor_replay_binding()
            if current_binding != protocol.predecessor_replay_binding:
                raise ValueError("predecessor replay drift")
        source = load_recovery_source_configuration(self.source_config_path)
        self._verify_root_isolation(source)
        official_protocol_path = source.resolve_source(source.protocol_path)
        official_protocol = FrozenBenchmarkProtocol.model_validate_json(
            official_protocol_path.read_text(encoding="utf-8")
        )
        verify_protocol(official_protocol)
        if (
            str(official_protocol.protocol_sha256)
            != protocol.source_official_protocol_sha256
            or str(official_protocol_path)
            != protocol.source_official_protocol_path
        ):
            raise ValueError("official protocol drift")
        visible_input_path = source.resolve_source(source.visible_input_path)
        reference_path = source.resolve_source(source.reference_path)
        reference_summary_path = source.resolve_source(
            source.reference_summary_path
        )
        reference = verify_recovery_environment(
            protocol.freeze,
            official_protocol,
            artifact_root=Path(source.artifact_root),
            visible_input_path=visible_input_path,
            data_paths=self._data_paths(visible_input_path),
            reference_path=reference_path,
            reference_summary_path=reference_summary_path,
            predecessor_campaign_path=None,
        )
        return official_protocol, reference

    def _data_paths(self, visible_input_path: Path) -> tuple[Path, ...]:
        request = CreateRunRequest.model_validate_json(
            visible_input_path.read_text(encoding="utf-8")
        )
        if request.case is None:
            raise ValueError("engineering replay requires an explicit visible case")
        paths = tuple(
            DatasetRegistry().resolve(dataset_ref)
            for dataset_ref in request.case.dataset_refs
        )
        if not paths:
            raise ValueError("engineering replay requires frozen analysis data")
        return paths

    def _load_predecessor(self) -> RecoveryCampaign:
        predecessor = RecoveryCampaign.model_validate_json(
            self.predecessor_campaign_path.read_text(encoding="utf-8")
        )
        verify_recovery_campaign(predecessor)
        if predecessor.status != "invalidated":
            raise ValueError("engineering replay predecessor must be invalidated")
        return predecessor

    def _predecessor_replay_roots(self) -> tuple[Path, Path, Path] | None:
        if self.predecessor_replay_delivery_root is None:
            return None
        delivery_root = self.predecessor_replay_delivery_root
        return (
            delivery_root,
            delivery_root.with_name(f"{delivery_root.name}-work"),
            delivery_root.with_name(f"{delivery_root.name}-state"),
        )

    def _load_predecessor_replay_binding(
        self,
    ) -> EngineeringReplayPredecessorBinding | None:
        roots = self._predecessor_replay_roots()
        if roots is None:
            return None
        delivery_root, working_root, state_root = roots
        protocol_path = delivery_root / PROTOCOL_FILE
        state_path = state_root / STATE_FILE
        manifest_path = delivery_root / "hash-manifest.json"
        resource_usage_path = delivery_root / "resource-usage.json"
        model_call_receipts_path = delivery_root / "model-call-receipts.json"
        predecessor_protocol = _parse_protocol(
            protocol_path.read_text(encoding="utf-8")
        )
        predecessor_state = EngineeringReplayState.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
        _verify_state(predecessor_state)
        if (
            predecessor_protocol.delivery_root != str(delivery_root)
            or predecessor_protocol.working_root != str(working_root)
            or predecessor_protocol.state_root != str(state_root)
        ):
            raise ValueError("predecessor replay roots do not match its protocol")
        if (
            predecessor_state.replay_id != predecessor_protocol.replay_id
            or predecessor_state.protocol_sha256
            != predecessor_protocol.protocol_sha256
            or predecessor_state.provider_attempt_ceiling
            != predecessor_protocol.provider_attempt_ceiling
        ):
            raise ValueError("predecessor replay state does not match its protocol")
        if (
            predecessor_state.status
            not in {
                "completed_hard_gate_passed",
                "completed_hard_gate_failed",
                "technical_failed",
            }
            or predecessor_state.usage_evidence_status != "exact"
            or predecessor_state.usage is None
        ):
            raise ValueError("predecessor replay requires terminal exact usage")
        if isinstance(predecessor_protocol, EngineeringReplayProtocolV2):
            if self.budget_binding is None:
                raise ValueError(
                    "successor of replay v2 requires cumulative budget binding"
                )
            expected_calls_before = (
                predecessor_protocol.budget_binding.cumulative_calls_before
                + predecessor_state.usage.llm_calls
            )
            if self.budget_binding.cumulative_calls_before != expected_calls_before:
                raise ValueError(
                    "successor cumulative calls before does not continue predecessor usage"
                )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        resource_usage = json.loads(
            resource_usage_path.read_text(encoding="utf-8")
        )
        model_call_receipts = json.loads(
            model_call_receipts_path.read_text(encoding="utf-8")
        )
        manifest_hashes = manifest.get("file_sha256")
        if not isinstance(manifest_hashes, dict):
            raise ValueError("predecessor replay manifest has no file hashes")
        protocol_file_sha256 = _file_sha256(protocol_path)
        state_file_sha256 = _file_sha256(state_path)
        manifest_file_sha256 = _file_sha256(manifest_path)
        resource_usage_file_sha256 = _file_sha256(resource_usage_path)
        model_call_receipts_file_sha256 = _file_sha256(
            model_call_receipts_path
        )
        delivery_root_resolved = delivery_root.resolve(strict=True)
        for name, expected_sha256 in manifest_hashes.items():
            if (
                not isinstance(name, str)
                or not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_sha256
                )
            ):
                raise ValueError("predecessor replay manifest hash is invalid")
            artifact_path = (delivery_root / name).resolve(strict=True)
            if (
                not artifact_path.is_relative_to(delivery_root_resolved)
                or not artifact_path.is_file()
                or _file_sha256(artifact_path) != expected_sha256
            ):
                raise ValueError("predecessor replay manifest hash mismatch")
        expected_state_artifacts = {
            **manifest_hashes,
            "hash-manifest.json": manifest_file_sha256,
        }
        if (
            manifest.get("official") is not False
            or manifest.get("benchmark_eligible") is not False
            or manifest.get("seen_case") is not True
            or manifest.get("comparison_allowed") is not False
            or manifest_hashes.get(PROTOCOL_FILE) != protocol_file_sha256
            or manifest_hashes.get("resource-usage.json")
            != resource_usage_file_sha256
            or manifest_hashes.get("model-call-receipts.json")
            != model_call_receipts_file_sha256
            or predecessor_state.artifact_sha256 != expected_state_artifacts
        ):
            raise ValueError("predecessor replay manifest hash mismatch")
        if (
            model_call_receipts.get("evidence_status") != "exact"
            or not isinstance(model_call_receipts.get("receipts"), list)
        ):
            raise ValueError("predecessor replay receipt evidence is not exact")
        receipts = tuple(
            RecoveryCallReceipt.model_validate(item)
            for item in model_call_receipts["receipts"]
        )
        _validate_round_usage_evidence(
            receipts,
            predecessor_state.usage,
            require_complete=False,
        )
        if (
            resource_usage.get("evidence_status") != "exact"
            or resource_usage.get("provider_attempt_ceiling")
            != predecessor_protocol.provider_attempt_ceiling
            or RecoveryUsage.model_validate(resource_usage.get("usage"))
            != predecessor_state.usage
        ):
            raise ValueError("predecessor replay resource usage mismatch")
        return _seal_predecessor_replay_binding(
            EngineeringReplayPredecessorBinding(
                replay_id=predecessor_protocol.replay_id,
                terminal_status=predecessor_state.status,
                delivery_root=str(delivery_root),
                working_root=str(working_root),
                state_root=str(state_root),
                protocol_file_sha256=protocol_file_sha256,
                protocol_sha256=str(predecessor_protocol.protocol_sha256),
                state_file_sha256=state_file_sha256,
                state_sha256=str(predecessor_state.state_sha256),
                manifest_file_sha256=manifest_file_sha256,
                resource_usage_file_sha256=resource_usage_file_sha256,
                model_call_receipts_file_sha256=(
                    model_call_receipts_file_sha256
                ),
                exact_usage=predecessor_state.usage,
            )
        )

    def _verify_root_isolation(self, source) -> None:
        assert_recovery_paths_separate(
            source,
            working_root=self.working_root,
            delivery_root=self.delivery_root,
            state_root=self.state_root,
        )
        predecessor_state_root = self.predecessor_campaign_path.parent
        predecessor_roots = [
            predecessor_state_root,
            self.predecessor_campaign_path,
        ]
        if predecessor_state_root.name.endswith("-state"):
            base = predecessor_state_root.name.removesuffix("-state")
            predecessor_roots.extend(
                (
                    predecessor_state_root.with_name(base),
                    predecessor_state_root.with_name(f"{base}-work"),
                )
            )
        writable_roots = (self.delivery_root, self.working_root, self.state_root)
        replay_roots = self._predecessor_replay_roots()
        if replay_roots is not None:
            predecessor_roots.extend(replay_roots)
        if any(
            _paths_overlap(writable, protected)
            for writable in writable_roots
            for protected in predecessor_roots
        ):
            raise ValueError("engineering replay roots overlap predecessor roots")

    def _load_existing_preparation(self) -> AnyEngineeringReplayProtocol | None:
        present = (
            self.protocol_path.is_file(),
            self.freeze_path.is_file(),
            self.state_path.is_file(),
        )
        if not any(present):
            return None
        if not all(present):
            raise ValueError("engineering replay preparation is incomplete")
        protocol = self._load_protocol()
        state = self._load_state()
        freeze = RecoveryFreeze.model_validate_json(
            self.freeze_path.read_text(encoding="utf-8")
        )
        expected_paths = (
            protocol.source_config_path == str(self.source_config_path),
            protocol.predecessor_campaign_path
            == str(self.predecessor_campaign_path),
            protocol.delivery_root == str(self.delivery_root),
            protocol.working_root == str(self.working_root),
            protocol.state_root == str(self.state_root),
            protocol.provider_attempt_ceiling == self.provider_attempt_ceiling,
            freeze == protocol.freeze,
            state.replay_id == protocol.replay_id,
            state.protocol_sha256 == protocol.protocol_sha256,
            state.provider_attempt_ceiling == protocol.provider_attempt_ceiling,
        )
        if isinstance(protocol, EngineeringReplayProtocolV2):
            current_binding = self._load_predecessor_replay_binding()
            expected_paths = (
                *expected_paths,
                protocol.budget_binding == self.budget_binding,
                protocol.predecessor_replay_binding == current_binding,
                (
                    protocol.predecessor_replay_binding.delivery_root
                    if protocol.predecessor_replay_binding is not None
                    else None
                )
                == (
                    str(self.predecessor_replay_delivery_root)
                    if self.predecessor_replay_delivery_root is not None
                    else None
                ),
            )
        else:
            expected_paths = (
                *expected_paths,
                self.protocol_version == 1,
                self.predecessor_replay_delivery_root is None,
                self.budget_binding is None,
            )
        if not all(expected_paths):
            raise ValueError("existing engineering replay preparation mismatch")
        return protocol

    def _load_protocol(self) -> AnyEngineeringReplayProtocol:
        return _parse_protocol(self.protocol_path.read_text(encoding="utf-8"))

    def _load_state(self) -> EngineeringReplayState:
        state = EngineeringReplayState.model_validate_json(
            self.state_path.read_text(encoding="utf-8")
        )
        _verify_state(state)
        return state

    @contextmanager
    def _locked(self):
        self.state_root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _write_once(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if canonical_sha256(existing) != canonical_sha256(payload):
            raise ValueError("engineering replay artifact is append-only")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(rendered)


def _write_text_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("engineering replay report is append-only")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _replace_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or run one non-benchmark seen-case engineering replay."
    )
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--delivery-root", type=Path, required=True)
    parser.add_argument("--predecessor-campaign", type=Path, required=True)
    parser.add_argument("--provider-attempt-ceiling", type=int, default=20)
    parser.add_argument("--predecessor-replay-root", type=Path)
    parser.add_argument("--cumulative-call-ceiling", type=int)
    parser.add_argument("--cumulative-calls-before", type=int)
    parser.add_argument("--cumulative-calls-remaining", type=int)
    args = parser.parse_args()
    controller = EngineeringReplayController(
        source_config_path=args.source_config,
        delivery_root=args.delivery_root,
        predecessor_campaign_path=args.predecessor_campaign,
        provider_attempt_ceiling=args.provider_attempt_ceiling,
        predecessor_replay_delivery_root=args.predecessor_replay_root,
        cumulative_call_ceiling=args.cumulative_call_ceiling,
        cumulative_calls_before=args.cumulative_calls_before,
        cumulative_calls_remaining=args.cumulative_calls_remaining,
    )
    result = (
        controller.prepare().model_dump(mode="json")
        if args.action == "prepare"
        else asyncio.run(controller.run()).model_dump(mode="json")
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
