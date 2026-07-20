from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .benchmark_models import (
    BenchmarkDeliveryManifest,
    BenchmarkPacket,
    BenchmarkReference,
    FaultReplayReport,
    FrozenBenchmarkProtocol,
    HardMetricReport,
    OfficialAttemptBinding,
    PairedReviewSummary,
)
from .benchmark_protocol import verify_protocol
from .case_import import DatasetRegistry
from .local_recovery_runner import LocalRecoveryRoundResult, LocalRecoveryRunner
from .models import CreateRunRequest, ModelCallReceipt
from .production_recovery_backend import (
    ProductionRecoveryBackend,
    assert_recovery_paths_separate,
    load_recovery_source_configuration,
)
from .recovery_campaign import (
    RecoveryCampaignStore,
    build_recovery_freeze,
    canonical_recovery_campaign_path,
    create_recovery_campaign,
    cumulative_llm_calls,
    import_prior_usage_from_ledger,
    map_model_call_receipts,
    verify_recovery_campaign,
    verify_recovery_environment,
)
from .recovery_models import RecoveryCampaign, RecoveryUsage
from .repository import RunRepository
from .seal import canonical_sha256


DEFAULT_SOURCE_CONFIG = Path(
    "backend/var/benchmarks/task3-official-v1/official-config.json"
)
DEFAULT_DELIVERY_ROOT = Path("/private/tmp/hypoweaver-task3-recovery-20260716")
RECOVERY_DELIVERY_MANIFEST = "recovery-delivery-manifest.json"


class PreparedRecoveryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration_version: int = 1
    source_config_path: str
    source_artifact_root: str
    protocol_path: str
    visible_input_path: str
    reference_path: str
    reference_summary_path: str
    runtime_public_path: str
    delivery_root: str
    working_root: str
    state_root: str
    campaign_path: str
    campaign_id: str
    freeze_sha256: str
    predecessor_campaign_path: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_delivery_root: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_working_root: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_state_root: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


def prepare_recovery_campaign(
    *,
    source_config_path: Path,
    delivery_root: Path,
    predecessor_campaign_path: Path | None = None,
) -> RecoveryCampaign:
    source_config_path = source_config_path.resolve(strict=True)
    source = load_recovery_source_configuration(source_config_path)
    delivery_root = delivery_root.resolve(strict=False)
    working_root = delivery_root.with_name(f"{delivery_root.name}-work")
    state_root = delivery_root.with_name(f"{delivery_root.name}-state")
    predecessor_campaign = None
    predecessor_roots: tuple[Path, Path, Path] | None = None
    if predecessor_campaign_path is not None:
        predecessor_campaign_path = predecessor_campaign_path.resolve(strict=True)
        predecessor_campaign = RecoveryCampaignStore(
            predecessor_campaign_path
        ).load()
        predecessor_roots = _predecessor_roots(
            predecessor_campaign_path,
            predecessor_campaign,
        )
        for writable_root in (delivery_root, working_root, state_root):
            if any(
                writable_root == predecessor_root
                or writable_root.is_relative_to(predecessor_root)
                or predecessor_root.is_relative_to(writable_root)
                for predecessor_root in predecessor_roots
            ):
                raise ValueError("replacement roots overlap predecessor roots")
    assert_recovery_paths_separate(
        source,
        working_root=working_root,
        delivery_root=delivery_root,
        state_root=state_root,
    )
    prepared_path = delivery_root / "recovery-run-config.json"
    protocol_path = source.resolve_source(source.protocol_path)
    visible_input_path = source.resolve_source(source.visible_input_path)
    reference_path = source.resolve_source(source.reference_path)
    reference_summary_path = source.resolve_source(source.reference_summary_path)
    runtime_public_path = source.resolve_source(source.runtime_public_path)
    protocol = FrozenBenchmarkProtocol.model_validate_json(
        protocol_path.read_text(encoding="utf-8")
    )
    verify_protocol(protocol)
    request = CreateRunRequest.model_validate_json(
        visible_input_path.read_text(encoding="utf-8")
    )
    if request.case is None:
        raise ValueError("recovery preparation requires an explicit case")
    registry = DatasetRegistry()
    data_paths = tuple(
        registry.resolve(dataset_ref) for dataset_ref in request.case.dataset_refs
    )
    prior = _import_failed_source_usage(source)
    state_root.mkdir(parents=True, exist_ok=True)

    if prepared_path.is_file():
        existing_prepared = PreparedRecoveryConfiguration.model_validate_json(
            prepared_path.read_text(encoding="utf-8")
        )
        expected_paths = {
            "source_config_path": str(source_config_path),
            "source_artifact_root": str(
                Path(source.artifact_root).resolve(strict=True)
            ),
            "protocol_path": str(protocol_path),
            "visible_input_path": str(visible_input_path),
            "reference_path": str(reference_path),
            "reference_summary_path": str(reference_summary_path),
            "runtime_public_path": str(runtime_public_path),
            "delivery_root": str(delivery_root),
            "working_root": str(working_root),
            "state_root": str(state_root),
            "predecessor_campaign_path": (
                str(predecessor_campaign_path)
                if predecessor_campaign_path is not None
                else None
            ),
            "predecessor_delivery_root": (
                str(predecessor_roots[0]) if predecessor_roots is not None else None
            ),
            "predecessor_working_root": (
                str(predecessor_roots[1]) if predecessor_roots is not None else None
            ),
            "predecessor_state_root": (
                str(predecessor_roots[2]) if predecessor_roots is not None else None
            ),
        }
        if any(
            getattr(existing_prepared, key) != value
            for key, value in expected_paths.items()
        ):
            raise ValueError("existing recovery preparation is bound to other paths")
        campaign_path = Path(existing_prepared.campaign_path).resolve(strict=True)
        if campaign_path != canonical_recovery_campaign_path(
            state_root,
            RecoveryCampaignStore(campaign_path).load().freeze,
        ).resolve(strict=False):
            raise ValueError("existing recovery campaign path is not canonical")
        campaign = RecoveryCampaignStore(campaign_path).load()
    else:
        existing_campaigns = sorted(state_root.glob("*.json"))
        if len(existing_campaigns) > 1:
            raise ValueError("recovery state root contains multiple campaigns")
        if existing_campaigns:
            campaign_path = existing_campaigns[0].resolve(strict=True)
            campaign = RecoveryCampaignStore(campaign_path).load()
            if campaign_path != canonical_recovery_campaign_path(
                state_root,
                campaign.freeze,
            ).resolve(strict=False):
                raise ValueError("existing recovery campaign path is not canonical")
        else:
            freeze = build_recovery_freeze(
                protocol,
                artifact_root=Path(source.artifact_root),
                visible_input_path=visible_input_path,
                data_paths=data_paths,
                reference_path=reference_path,
                reference_summary_path=reference_summary_path,
                predecessor_campaign=predecessor_campaign,
            )
            campaign_path = canonical_recovery_campaign_path(state_root, freeze)
            campaign = create_recovery_campaign(freeze, prior)
            RecoveryCampaignStore(campaign_path).create(campaign)

    verify_recovery_environment(
        campaign.freeze,
        protocol,
        artifact_root=Path(source.artifact_root),
        visible_input_path=visible_input_path,
        data_paths=data_paths,
        reference_path=reference_path,
        reference_summary_path=reference_summary_path,
        predecessor_campaign_path=predecessor_campaign_path,
    )
    if not _same_prior_usage_binding(campaign.prior_usage, prior):
        raise ValueError("existing recovery campaign prior usage binding drifted")

    if prepared_path.is_file():
        if (
            existing_prepared.campaign_id != campaign.campaign_id
            or existing_prepared.freeze_sha256 != campaign.freeze.freeze_sha256
        ):
            raise ValueError("existing recovery preparation binding mismatch")

    prepared = PreparedRecoveryConfiguration(
        source_config_path=str(source_config_path),
        source_artifact_root=str(Path(source.artifact_root).resolve(strict=True)),
        protocol_path=str(protocol_path),
        visible_input_path=str(visible_input_path),
        reference_path=str(reference_path),
        reference_summary_path=str(reference_summary_path),
        runtime_public_path=str(runtime_public_path),
        delivery_root=str(delivery_root),
        working_root=str(working_root),
        state_root=str(state_root),
        campaign_path=str(campaign_path),
        campaign_id=campaign.campaign_id,
        freeze_sha256=str(campaign.freeze.freeze_sha256),
        predecessor_campaign_path=(
            str(predecessor_campaign_path)
            if predecessor_campaign_path is not None
            else None
        ),
        predecessor_delivery_root=(
            str(predecessor_roots[0]) if predecessor_roots is not None else None
        ),
        predecessor_working_root=(
            str(predecessor_roots[1]) if predecessor_roots is not None else None
        ),
        predecessor_state_root=(
            str(predecessor_roots[2]) if predecessor_roots is not None else None
        ),
    )
    if prepared_path.is_file() and existing_prepared != prepared:
        raise ValueError("existing recovery preparation binding mismatch")
    delivery_root.mkdir(parents=True, exist_ok=True)
    _write_once(prepared_path, prepared.model_dump(mode="json"))
    _write_once(
        delivery_root / "campaign-protocol.json",
        campaign.freeze.model_dump(mode="json"),
    )
    _write_once(
        delivery_root / "prior-official-usage-import.json",
        campaign.prior_usage.model_dump(mode="json"),
    )
    return campaign


async def run_prepared_recovery(*, delivery_root: Path) -> RecoveryCampaign:
    delivery_root = delivery_root.resolve(strict=True)
    prepared = PreparedRecoveryConfiguration.model_validate_json(
        (delivery_root / "recovery-run-config.json").read_text(encoding="utf-8")
    )
    if Path(prepared.delivery_root).resolve(strict=True) != delivery_root:
        raise ValueError("prepared recovery delivery_root mismatch")
    expected_working_root = delivery_root.with_name(f"{delivery_root.name}-work")
    expected_state_root = delivery_root.with_name(f"{delivery_root.name}-state")
    if (
        Path(prepared.working_root).resolve(strict=False) != expected_working_root
        or Path(prepared.state_root).resolve(strict=True) != expected_state_root
    ):
        raise ValueError("prepared recovery roots are not canonical")
    source_config_path = Path(prepared.source_config_path).resolve(strict=True)
    source = load_recovery_source_configuration(source_config_path)
    expected_source_paths = {
        "source_artifact_root": str(Path(source.artifact_root).resolve(strict=True)),
        "protocol_path": str(source.resolve_source(source.protocol_path)),
        "visible_input_path": str(source.resolve_source(source.visible_input_path)),
        "reference_path": str(source.resolve_source(source.reference_path)),
        "reference_summary_path": str(
            source.resolve_source(source.reference_summary_path)
        ),
        "runtime_public_path": str(source.resolve_source(source.runtime_public_path)),
    }
    if any(
        getattr(prepared, field_name) != expected
        for field_name, expected in expected_source_paths.items()
    ):
        raise ValueError("prepared recovery source binding mismatch")
    assert_recovery_paths_separate(
        source,
        working_root=expected_working_root,
        delivery_root=delivery_root,
        state_root=expected_state_root,
    )
    campaign_path = Path(prepared.campaign_path).resolve(strict=True)
    store = RecoveryCampaignStore(campaign_path)
    existing_campaign = store.load()
    if campaign_path != canonical_recovery_campaign_path(
        expected_state_root,
        existing_campaign.freeze,
    ).resolve(strict=False):
        raise ValueError("prepared recovery campaign path is not canonical")
    if (
        prepared.campaign_id != existing_campaign.campaign_id
        or prepared.freeze_sha256 != existing_campaign.freeze.freeze_sha256
    ):
        raise ValueError("prepared recovery campaign binding mismatch")
    predecessor_campaign_path = (
        Path(prepared.predecessor_campaign_path).resolve(strict=True)
        if prepared.predecessor_campaign_path is not None
        else None
    )
    predecessor_roots: tuple[Path, Path, Path] | None = None
    if predecessor_campaign_path is not None:
        predecessor = RecoveryCampaignStore(predecessor_campaign_path).load()
        predecessor_roots = _predecessor_roots(
            predecessor_campaign_path,
            predecessor,
        )
        prepared_predecessor_roots = (
            prepared.predecessor_delivery_root,
            prepared.predecessor_working_root,
            prepared.predecessor_state_root,
        )
        if any(value is None for value in prepared_predecessor_roots) or tuple(
            Path(str(value)).resolve(strict=False)
            for value in prepared_predecessor_roots
        ) != predecessor_roots:
            raise ValueError("prepared predecessor roots mismatch")
    elif (
        existing_campaign.freeze.predecessor_binding is not None
        or any(
            value is not None
            for value in (
                prepared.predecessor_delivery_root,
                prepared.predecessor_working_root,
                prepared.predecessor_state_root,
            )
        )
    ):
        raise ValueError("prepared recovery predecessor binding is incomplete")
    protocol = FrozenBenchmarkProtocol.model_validate_json(
        Path(prepared.protocol_path).read_text(encoding="utf-8")
    )
    reference = BenchmarkReference.model_validate_json(
        Path(prepared.reference_path).read_text(encoding="utf-8")
    )
    request = CreateRunRequest.model_validate_json(
        Path(prepared.visible_input_path).read_text(encoding="utf-8")
    )
    if request.case is None:
        raise ValueError("prepared recovery has no explicit case")
    data_paths = tuple(
        DatasetRegistry().resolve(dataset_ref)
        for dataset_ref in request.case.dataset_refs
    )
    if (
        (delivery_root / RECOVERY_DELIVERY_MANIFEST).exists()
        or (delivery_root / RECOVERY_DELIVERY_MANIFEST).is_symlink()
    ):
        verify_recovery_delivery_manifest(
            delivery_root,
            expected_campaign=existing_campaign,
        )
    backend = ProductionRecoveryBackend(
        source_config_path=Path(prepared.source_config_path),
        protocol=protocol,
        working_root=Path(prepared.working_root),
        delivery_root=Path(prepared.delivery_root),
        state_root=Path(prepared.state_root),
        predecessor_campaign_path=predecessor_campaign_path,
    )
    runner = LocalRecoveryRunner(
        store=store,
        backend=backend,
        protocol=protocol,
        reference=reference,
        source_artifact_root=Path(prepared.source_artifact_root),
        delivery_root=Path(prepared.delivery_root),
        visible_input_path=Path(prepared.visible_input_path),
        data_paths=data_paths,
        reference_path=Path(prepared.reference_path),
        reference_summary_path=Path(prepared.reference_summary_path),
        protected_official_roots=(
            Path(source.output_dir),
            Path(source.working_dir),
            Path(source.official_state_root),
            *(predecessor_roots or ()),
        ),
        predecessor_campaign_path=predecessor_campaign_path,
    )
    campaign = await runner.run()
    write_recovery_delivery(
        campaign,
        delivery_root=delivery_root,
        working_root=Path(prepared.working_root),
    )
    verify_recovery_delivery_manifest(
        delivery_root,
        expected_campaign=campaign,
    )
    return campaign


def write_recovery_delivery(
    campaign: RecoveryCampaign,
    *,
    delivery_root: Path,
    working_root: Path | None = None,
) -> None:
    verify_recovery_campaign(campaign)
    delivery_root.mkdir(parents=True, exist_ok=True)
    manifest_path = delivery_root / RECOVERY_DELIVERY_MANIFEST
    if manifest_path.exists() or manifest_path.is_symlink():
        verify_recovery_delivery_manifest(
            delivery_root,
            expected_campaign=campaign,
        )
        return
    rejected_terminal_evidence = _load_rejected_terminal_evidence(
        campaign,
        working_root=working_root,
    )
    evidence = _collect_delivery_evidence(
        campaign,
        delivery_root=delivery_root,
        rejected_terminal_evidence=rejected_terminal_evidence,
    )
    manifest_payload = campaign.model_dump(mode="json", by_alias=True)
    _write_replace(
        delivery_root / "recovery-manifest.json",
        {
            "provenance_scope": "seen_case_recovery_non_official",
            "official": False,
            "campaign": manifest_payload,
            "campaign_sha256": campaign.campaign_sha256,
            "selected_round_id": next(
                (
                    item.round_id
                    for item in campaign.rounds
                    if item.status == "hard_gate_qualified"
                ),
                None,
            ),
            "selection_rule": "first_hard_gate_qualified_round_only",
            "artifact_index": "recovery-artifact-index.json",
            "rejected_terminal_evidence": (
                "rejected-terminal-evidence.json"
                if rejected_terminal_evidence is not None
                else None
            ),
        },
    )
    comparison = campaign.comparison
    invalidated_round_started = bool(
        campaign.invalidation is not None
        and campaign.invalidation.reservation_scope == "round"
    )
    predecessor_known_usage = (
        campaign.predecessor_carryover.known_usage
        if campaign.predecessor_carryover is not None
        and campaign.predecessor_carryover.known_usage is not None
        else RecoveryUsage()
    )
    predecessor_unknown_calls = (
        (
            campaign.predecessor_carryover.unknown_llm_calls
            if campaign.predecessor_carryover.unknown_llm_calls is not None
            else campaign.predecessor_carryover.conservative_llm_calls
        )
        if campaign.predecessor_carryover is not None
        else 0
    )
    total_usage = RecoveryUsage(
        llm_calls=cumulative_llm_calls(campaign),
        input_tokens=(
            campaign.prior_usage.usage.input_tokens
            + predecessor_known_usage.input_tokens
            + sum(item.usage.input_tokens for item in campaign.rounds)
            + (
                comparison.qwen_single_pass.input_tokens
                + comparison.agent_laboratory.input_tokens
                + comparison.blind_reviews.input_tokens
                if comparison is not None
                else 0
            )
        ),
        output_tokens=(
            campaign.prior_usage.usage.output_tokens
            + predecessor_known_usage.output_tokens
            + sum(item.usage.output_tokens for item in campaign.rounds)
            + (
                comparison.qwen_single_pass.output_tokens
                + comparison.agent_laboratory.output_tokens
                + comparison.blind_reviews.output_tokens
                if comparison is not None
                else 0
            )
        ),
        wall_time_seconds=(
            campaign.prior_usage.usage.wall_time_seconds
            + predecessor_known_usage.wall_time_seconds
            + sum(item.usage.wall_time_seconds for item in campaign.rounds)
            + (
                comparison.qwen_single_pass.wall_time_seconds
                + comparison.agent_laboratory.wall_time_seconds
                + comparison.blind_reviews.wall_time_seconds
                if comparison is not None
                else 0
            )
        ),
        technical_failures=tuple(
            [*campaign.prior_usage.usage.technical_failures]
            + [*predecessor_known_usage.technical_failures]
            + [
                failure
                for item in campaign.rounds
                for failure in item.usage.technical_failures
            ]
            + (
                [
                    *comparison.qwen_single_pass.technical_failures,
                    *comparison.agent_laboratory.technical_failures,
                    *comparison.blind_reviews.technical_failures,
                ]
                if comparison is not None
                else []
            )
        ),
    )
    _write_replace(
        delivery_root / "cumulative-resource-usage.json",
        {
            **total_usage.model_dump(mode="json"),
            "total_call_ceiling": campaign.total_call_ceiling,
            "token_usage_status": campaign.cumulative_token_usage_status,
            "prior_official_calls": campaign.prior_usage.usage.llm_calls,
            "recovery_round_calls": sum(item.usage.llm_calls for item in campaign.rounds),
            "comparison_calls": (
                comparison.qwen_single_pass.llm_calls
                + comparison.agent_laboratory.llm_calls
                + comparison.blind_reviews.llm_calls
                if comparison is not None
                else 0
            ),
            "predecessor_carryover_calls": (
                campaign.predecessor_carryover.conservative_llm_calls
                if campaign.predecessor_carryover is not None
                else 0
            ),
            "predecessor_started_round_count": (
                campaign.predecessor_carryover.started_round_count
                if campaign.predecessor_carryover is not None
                else 0
            ),
            "predecessor_known_usage": (
                predecessor_known_usage.model_dump(mode="json")
                if campaign.predecessor_carryover is not None
                else None
            ),
            "predecessor_unknown_calls": predecessor_unknown_calls,
            "conservative_invalidation_calls": (
                campaign.invalidation.conservative_llm_call_charge
                if campaign.invalidation is not None
                else 0
            ),
            "started_round_count": (
                (
                    campaign.predecessor_carryover.started_round_count
                    if campaign.predecessor_carryover is not None
                    else 0
                )
                + len(campaign.rounds)
                + (1 if invalidated_round_started else 0)
            ),
            "rejected_terminal_known_usage": (
                rejected_terminal_evidence["reported_usage"]
                if rejected_terminal_evidence is not None
                else None
            ),
        },
    )
    all_round_receipts = {
        item.round_id: [
            receipt.model_dump(mode="json") for receipt in item.receipts
        ]
        for item in campaign.rounds
    }
    if rejected_terminal_evidence is not None:
        rejected_round_id = rejected_terminal_evidence["round_id"]
        all_round_receipts[
            f"rejected_terminal_evidence:{rejected_round_id}"
        ] = rejected_terminal_evidence["receipts"]
    _write_replace(
        delivery_root / "all-round-receipts.json",
        all_round_receipts,
    )
    _write_replace(
        delivery_root / "comparison-receipts.json",
        [
            receipt.model_dump(mode="json")
            for receipt in (comparison.receipts if comparison is not None else ())
        ],
    )
    _write_replace(
        delivery_root / "round-statuses.json",
        evidence["round_statuses"],
    )
    _write_replace(
        delivery_root / "hard-metrics-by-round.json",
        evidence["hard_metrics_by_round"],
    )
    _write_replace(
        delivery_root / "fault-and-ablation-by-round.json",
        evidence["fault_and_ablation_by_round"],
    )
    _write_replace(
        delivery_root / "selected-round.json",
        evidence["selection"],
    )
    _write_replace(
        delivery_root / "recovery-artifact-index.json",
        evidence["artifact_index"],
    )
    if rejected_terminal_evidence is not None:
        _write_replace(
            delivery_root / "rejected-terminal-evidence.json",
            rejected_terminal_evidence,
        )
    selected = evidence["selection"]
    comparison_evidence = evidence["artifact_index"]["comparison"]
    hard_passed = sum(
        bool(value) for value in selected.get("hard_metric_results", {}).values()
    )
    hard_total = len(selected.get("hard_metric_results", {}))
    ablations_passed = sum(
        bool(value)
        for value in selected.get("ablation_target_degradation_results", {}).values()
    )
    comparison_claim = comparison_evidence.get("claim_condition_met")
    report = [
        "# Task3 同案例恢复评测报告",
        "",
        "- 原正式 benchmark：失败且不可重跑，原封存记录未被修改。",
        "- 本次性质：同一已见企业面板案例上的运行可靠性与修复验证，不是正式、隐藏或无偏 benchmark。",
        "- 盲评性质：模型盲评，不是人工同行评审。",
        f"- Campaign 终态：`{campaign.protocol_status}`。",
        f"- 累计模型调用：{total_usage.llm_calls}/120（包含原失败正式评测 {campaign.prior_usage.usage.llm_calls} 次）。",
        f"- Token 用量状态：`{campaign.cumulative_token_usage_status}`（lower_bound 不写成精确值）。",
        f"- 首个硬门合格轮：`{selected.get('selected_round_id') or '无'}`。",
        f"- 硬指标通过：{hard_passed}/{hard_total or 8}。",
        f"- 六项消融目标退化：{ablations_passed}/{len(selected.get('ablation_target_degradation_results', {})) or 6}。",
    ]
    if campaign.predecessor_carryover is not None:
        report.append(
            "- 前序 Campaign 保守结转："
            f"{campaign.predecessor_carryover.conservative_llm_calls} 次；"
            f"已计入 {campaign.predecessor_carryover.started_round_count} 个启动轮。"
        )
        report.append(
            "- 前序可核验用量："
            f"{predecessor_known_usage.llm_calls} 次调用；"
            f"未知用量仅按 {predecessor_unknown_calls} 次保守计数，"
            "不伪造 token 或耗时。"
        )
    if rejected_terminal_evidence is not None:
        report.append(
            "- 被拒绝终态证据："
            f"`{rejected_terminal_evidence['round_id']}` 未成为 finalized round；"
            f"保留 {len(rejected_terminal_evidence['receipts'])} 份真实调用 receipt，"
            f"Campaign 仍按 {rejected_terminal_evidence['campaign_invalidation']['conservative_llm_call_charge']} 次保守扣费。"
        )
    elif invalidated_round_started:
        assert campaign.invalidation is not None
        report.append(
            "- 未知终态证据：已启动轮没有可验证的 round-result，"
            f"仍按 {campaign.invalidation.conservative_llm_call_charge} 次保守扣费。"
        )
    qualified = next(
        (item for item in campaign.rounds if item.status == "hard_gate_qualified"),
        None,
    )
    if qualified is None:
        report.append("- 结论：未获得全部硬指标合格轮，不作比较优势宣称。")
    elif comparison is None or comparison.status != "completed":
        reason = (
            comparison.technical_failure
            if comparison is not None
            else "comparison_not_started"
        )
        report.append(
            "- 结论：已见案例硬指标合格，但比较未完成"
            f"（`{reason}`），不作优势宣称。"
        )
    elif comparison_claim is True:
        report.append(
            "- 结论：通过全部绝对硬指标，相对 Agent Laboratory "
            "至少一项严格更优，且模型盲评 overall 中位数更高、"
            "soundness 不低；允许限定声明“在本已见案例上科学可靠性更高”。"
        )
    else:
        report.append(
            "- 结论：已见案例硬指标合格，但相对优势或模型盲评"
            "门槛未全部满足；不声明“科学可靠性更高”。"
        )
    _write_text_replace(delivery_root / "中文恢复评测报告.md", "\n".join(report) + "\n")
    _seal_recovery_delivery_manifest(campaign, delivery_root=delivery_root)
    verify_recovery_delivery_manifest(
        delivery_root,
        expected_campaign=campaign,
    )


def verify_recovery_delivery_manifest(
    delivery_root: Path,
    *,
    expected_campaign: RecoveryCampaign | None = None,
) -> dict[str, Any]:
    """Re-read every delivered file and verify the canonical root seal."""

    root = delivery_root.resolve(strict=True)
    manifest_path = root / RECOVERY_DELIVERY_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("recovery delivery manifest is missing or unsafe")
    manifest = _load_json(manifest_path)
    required_fields = {
        "manifest_version",
        "provenance_scope",
        "official",
        "campaign_id",
        "campaign_sha256",
        "file_sha256",
        "manifest_sha256",
    }
    if set(manifest) != required_fields:
        raise ValueError("recovery delivery manifest fields are invalid")
    unsigned = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if (
        manifest.get("manifest_version") != 1
        or manifest.get("provenance_scope")
        != "seen_case_recovery_non_official"
        or manifest.get("official") is not False
        or not isinstance(manifest.get("campaign_id"), str)
        or not manifest.get("campaign_id")
        or not _is_sha256(manifest.get("campaign_sha256"))
        or not _is_sha256(manifest.get("manifest_sha256"))
        or canonical_sha256(unsigned) != manifest.get("manifest_sha256")
    ):
        raise ValueError("recovery delivery manifest seal is invalid")
    if expected_campaign is not None:
        verify_recovery_campaign(expected_campaign)
        if (
            manifest.get("campaign_id") != expected_campaign.campaign_id
            or manifest.get("campaign_sha256") != expected_campaign.campaign_sha256
        ):
            raise ValueError("recovery delivery manifest is bound to another campaign")
    recorded = manifest.get("file_sha256")
    if not isinstance(recorded, dict) or not recorded:
        raise ValueError("recovery delivery manifest has no file registry")
    if any(
        not isinstance(relative, str)
        or not relative
        or relative == RECOVERY_DELIVERY_MANIFEST
        or not _is_sha256(value)
        for relative, value in recorded.items()
    ):
        raise ValueError("recovery delivery manifest file registry is invalid")
    actual = _delivery_file_sha256(root)
    if set(actual) != set(recorded):
        raise ValueError("recovery delivery file registry mismatch")
    for relative, expected_hash in recorded.items():
        if actual[relative] != expected_hash:
            raise ValueError(f"recovery delivery file hash mismatch: {relative}")
    return manifest


def _seal_recovery_delivery_manifest(
    campaign: RecoveryCampaign,
    *,
    delivery_root: Path,
) -> None:
    file_sha256 = _delivery_file_sha256(delivery_root.resolve(strict=True))
    unsigned = {
        "manifest_version": 1,
        "provenance_scope": "seen_case_recovery_non_official",
        "official": False,
        "campaign_id": campaign.campaign_id,
        "campaign_sha256": campaign.campaign_sha256,
        "file_sha256": file_sha256,
    }
    _write_once(
        delivery_root / RECOVERY_DELIVERY_MANIFEST,
        {**unsigned, "manifest_sha256": canonical_sha256(unsigned)},
    )


def _delivery_file_sha256(delivery_root: Path) -> dict[str, str]:
    root = delivery_root.resolve(strict=True)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("recovery delivery cannot contain symlinks")
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("recovery delivery file escapes its root")
        relative = resolved.relative_to(root).as_posix()
        if relative == RECOVERY_DELIVERY_MANIFEST:
            continue
        files[relative] = _file_sha256(resolved)
    return dict(sorted(files.items()))


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _load_rejected_terminal_evidence(
    campaign: RecoveryCampaign,
    *,
    working_root: Path | None,
) -> dict[str, Any] | None:
    """Load receipt-backed terminal work that the Campaign correctly rejected."""

    invalidation = campaign.invalidation
    if (
        working_root is None
        or invalidation is None
        or invalidation.reservation_scope != "round"
        or invalidation.reservation_id is None
    ):
        return None
    round_id = f"round-{len(campaign.rounds) + 1:02d}"
    if not working_root.exists():
        return None
    if working_root.is_symlink() or not working_root.is_dir():
        raise ValueError("rejected terminal working_root is unsafe")
    root = working_root.resolve(strict=True)
    round_root = (root / campaign.campaign_id / round_id).resolve(strict=False)
    if not round_root.is_relative_to(root):
        raise ValueError("rejected terminal evidence escapes working_root")
    result_path = round_root / "round-result.json"
    if not result_path.exists():
        return None
    started_path = round_root / "round-started.json"
    database_path = round_root / "hypoweaver.db"
    for path in (result_path, started_path, database_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("rejected terminal evidence source is missing or unsafe")

    result = LocalRecoveryRoundResult.model_validate_json(
        result_path.read_text(encoding="utf-8")
    )
    if result.status != "technical_failed" or result.packet is not None:
        raise ValueError("rejected terminal evidence is not a technical failure")
    started = _load_json(started_path)
    expected_started = {
        "event": "model_facing_round_started",
        "campaign_id": campaign.campaign_id,
        "round_id": round_id,
        "reservation_id": invalidation.reservation_id,
        "call_limit": invalidation.conservative_llm_call_charge,
    }
    if any(started.get(key) != value for key, value in expected_started.items()):
        raise ValueError("rejected terminal start marker binding mismatch")
    if len(result.receipts) > invalidation.conservative_llm_call_charge:
        raise ValueError("rejected terminal receipt count exceeds conservative charge")

    source_receipts, database_usage = _read_rejected_model_usage(database_path)
    mapped_receipts = map_model_call_receipts(
        source_receipts,
        campaign_id=campaign.campaign_id,
        round_id=round_id,
        require_complete=False,
    )
    if mapped_receipts != result.receipts:
        raise ValueError("rejected terminal receipt source binding mismatch")
    receipt_input_tokens = sum(item.input_tokens for item in result.receipts)
    receipt_output_tokens = sum(item.output_tokens for item in result.receipts)
    if (
        result.usage.llm_calls != len(result.receipts)
        or result.usage.input_tokens != receipt_input_tokens
        or result.usage.output_tokens != receipt_output_tokens
    ):
        raise ValueError("rejected terminal receipt usage mismatch")

    receipt_wall_time = sum(
        max(
            0.0,
            (
                datetime.fromisoformat(item.call_completed_at)
                - datetime.fromisoformat(item.call_started_at)
            ).total_seconds(),
        )
        for item in result.receipts
    )
    if abs(result.usage.wall_time_seconds - receipt_wall_time) > 1e-6:
        raise ValueError("rejected terminal receipt wall-time mismatch")
    receipt_failures = tuple(
        str(item.error_type)
        for item in result.receipts
        if item.error_type is not None
    )
    reported_failures = tuple(result.usage.technical_failures)
    reported_counter = Counter(reported_failures)
    receipt_counter = Counter(receipt_failures)
    relative_root = round_root.relative_to(root).as_posix()
    return {
        "evidence_version": 1,
        "evidence_kind": "rejected_terminal_evidence",
        "admission_status": "rejected_not_finalized_round",
        "campaign_id": campaign.campaign_id,
        "campaign_sha256": campaign.campaign_sha256,
        "freeze_sha256": campaign.freeze.freeze_sha256,
        "round_id": round_id,
        "reservation_id": invalidation.reservation_id,
        "campaign_invalidation": {
            "reason": invalidation.reason,
            "invalidation_sha256": invalidation.invalidation_sha256,
            "unknown_call_evidence": invalidation.unknown_call_evidence,
            "conservative_llm_call_charge": (
                invalidation.conservative_llm_call_charge
            ),
        },
        "terminal_result": {
            "status": result.status,
            "reason_code": result.reason_code,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
        },
        "source_files": {
            "round_result": {
                "path_within_working_root": f"{relative_root}/round-result.json",
                "sha256": _file_sha256(result_path),
            },
            "round_started": {
                "path_within_working_root": f"{relative_root}/round-started.json",
                "sha256": _file_sha256(started_path),
            },
            "run_database": {
                "path_within_working_root": f"{relative_root}/hypoweaver.db",
                "sha256": _file_sha256(database_path),
            },
        },
        "reported_usage": result.usage.model_dump(mode="json"),
        "receipt_derived_usage": {
            "llm_calls": len(result.receipts),
            "input_tokens": receipt_input_tokens,
            "output_tokens": receipt_output_tokens,
            "wall_time_seconds": receipt_wall_time,
            "technical_failures": list(receipt_failures),
        },
        "database_usage": database_usage,
        "usage_diagnostics": {
            "call_count_match": True,
            "input_tokens_match": True,
            "output_tokens_match": True,
            "wall_time_match": True,
            "technical_failures_match": reported_counter == receipt_counter,
            "reported_only_failures": list(
                (reported_counter - receipt_counter).elements()
            ),
            "receipt_only_failures": list(
                (receipt_counter - reported_counter).elements()
            ),
        },
        "receipts": [item.model_dump(mode="json") for item in result.receipts],
    }


def _read_rejected_model_usage(
    database_path: Path,
) -> tuple[list[ModelCallReceipt], dict[str, Any]]:
    uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute("SELECT payload FROM runs").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError("rejected terminal run database is unreadable") from error
    if len(rows) != 1:
        raise ValueError("rejected terminal run database must contain one run")
    try:
        state = json.loads(rows[0][0])
        if state.get("status") != "failed":
            raise ValueError("rejected terminal source run is not failed")
        usage = state["artifacts"]["model_usage"]["payload"]
        raw_receipts = usage["call_receipts"]
        receipts = [ModelCallReceipt.model_validate(item) for item in raw_receipts]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("rejected terminal model usage is malformed") from error
    if (
        int(usage.get("llm_calls", -1)) != len(receipts)
        or int(usage.get("input_tokens", -1))
        != sum(item.input_tokens for item in receipts)
        or int(usage.get("output_tokens", -1))
        != sum(item.output_tokens for item in receipts)
    ):
        raise ValueError("rejected terminal database usage mismatch")
    return receipts, {
        "llm_calls": int(usage["llm_calls"]),
        "input_tokens": int(usage["input_tokens"]),
        "output_tokens": int(usage["output_tokens"]),
        "wall_time_seconds": float(usage.get("wall_time_seconds", 0) or 0),
        "technical_failures": [
            str(item) for item in usage.get("technical_failures", [])
        ],
    }


def _collect_delivery_evidence(
    campaign: RecoveryCampaign,
    *,
    delivery_root: Path,
    rejected_terminal_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    round_statuses: list[dict[str, Any]] = []
    hard_by_round: dict[str, Any] = {}
    fault_by_round: dict[str, Any] = {}
    artifact_rounds: list[dict[str, Any]] = []
    selected_record = next(
        (item for item in campaign.rounds if item.status == "hard_gate_qualified"),
        None,
    )
    for record in campaign.rounds:
        round_statuses.append(record.model_dump(mode="json"))
        artifact_entry: dict[str, Any] = {
            "round_id": record.round_id,
            "status": record.status,
            "receipts_file": "all-round-receipts.json",
        }
        if record.benchmark_packet_sha256 is not None:
            root = delivery_root / campaign.campaign_id / record.round_id
            packet_path = root / "hypoweaver_packet.json"
            hard_path = root / "hard_metrics.json"
            replay_path = root / "fault_replay.json"
            packet = BenchmarkPacket.model_validate_json(
                packet_path.read_text(encoding="utf-8")
            )
            hard = HardMetricReport.model_validate_json(
                hard_path.read_text(encoding="utf-8")
            )
            replay = FaultReplayReport.model_validate_json(
                replay_path.read_text(encoding="utf-8")
            )
            if (
                packet.packet_sha256 != record.benchmark_packet_sha256
                or canonical_sha256(hard.model_dump(mode="json"))
                != record.hard_metric_report_sha256
                or canonical_sha256(replay.model_dump(mode="json"))
                != record.fault_replay_sha256
            ):
                raise ValueError("recovery round delivery evidence hash mismatch")
            hard_by_round[record.round_id] = hard.model_dump(mode="json")
            fault_by_round[record.round_id] = replay.model_dump(mode="json")
            artifact_entry["files"] = {
                "hypoweaver_packet": _relative_delivery_path(
                    packet_path,
                    delivery_root,
                ),
                "hard_metrics": _relative_delivery_path(hard_path, delivery_root),
                "fault_and_ablations": _relative_delivery_path(
                    replay_path,
                    delivery_root,
                ),
            }
        artifact_rounds.append(artifact_entry)
    if rejected_terminal_evidence is not None:
        round_statuses.append(
            {
                "round_id": rejected_terminal_evidence["round_id"],
                "status": "rejected_terminal_evidence",
                "finalized_round": False,
                "receipt_count": len(rejected_terminal_evidence["receipts"]),
                "reported_usage": rejected_terminal_evidence["reported_usage"],
                "campaign_invalidation": rejected_terminal_evidence[
                    "campaign_invalidation"
                ],
                "evidence_file": "rejected-terminal-evidence.json",
            }
        )
    elif (
        campaign.invalidation is not None
        and campaign.invalidation.reservation_scope == "round"
    ):
        round_statuses.append(
            {
                "round_id": f"round-{len(campaign.rounds) + 1:02d}",
                "status": "invalidated_unknown_terminal_evidence",
                "finalized_round": False,
                "receipt_count": 0,
                "campaign_invalidation": {
                    "reason": campaign.invalidation.reason,
                    "invalidation_sha256": (
                        campaign.invalidation.invalidation_sha256
                    ),
                    "unknown_call_evidence": (
                        campaign.invalidation.unknown_call_evidence
                    ),
                    "conservative_llm_call_charge": (
                        campaign.invalidation.conservative_llm_call_charge
                    ),
                },
            }
        )

    selection = {
        "selection_rule": "first_hard_gate_qualified_round_only",
        "selected_round_id": (
            selected_record.round_id if selected_record is not None else None
        ),
        "selected_round_index": (
            selected_record.round_index if selected_record is not None else None
        ),
        "benchmark_packet_sha256": (
            selected_record.benchmark_packet_sha256
            if selected_record is not None
            else None
        ),
        "hard_metric_results": (
            selected_record.hard_metric_results if selected_record is not None else {}
        ),
        "ablation_target_degradation_results": (
            selected_record.ablation_target_degradation_results
            if selected_record is not None
            else {}
        ),
    }

    comparison_entry: dict[str, Any] = {
        "status": (
            campaign.comparison.status
            if campaign.comparison is not None
            else "not_started"
        ),
        "technical_failure": (
            campaign.comparison.technical_failure
            if campaign.comparison is not None
            else None
        ),
        "claim_condition_met": None,
        "files": {},
    }
    if campaign.comparison is not None and campaign.comparison.status == "completed":
        comparison_root = delivery_root / campaign.campaign_id / "comparison"
        manifest_path = comparison_root / "delivery_manifest.json"
        manifest = BenchmarkDeliveryManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if (
            manifest.official
            or manifest.manifest_sha256
            != campaign.comparison.delivery_manifest_sha256
            or canonical_sha256(
                manifest.model_dump(mode="json", exclude={"manifest_sha256"})
            )
            != manifest.manifest_sha256
        ):
            raise ValueError("recovery comparison delivery manifest mismatch")
        mandatory = {
            "neutral_packets/qwen_single_pass.json",
            "neutral_packets/agent_laboratory.json",
            "neutral_packets/hypoweaver.json",
            "hard_metrics.json",
            "ablations.json",
            "blind_reviews.json",
            *(f"blind_reviews/review-{index}.json" for index in range(1, 6)),
            "resource_usage.json",
            "comparison_report_zh.md",
        }
        if not mandatory.issubset(manifest.file_sha256):
            raise ValueError("recovery comparison delivery is incomplete")
        for relative, expected_hash in manifest.file_sha256.items():
            path = (comparison_root / relative).resolve(strict=True)
            if not path.is_relative_to(comparison_root.resolve()):
                raise ValueError("recovery comparison artifact escapes delivery root")
            if _file_sha256(path) != expected_hash:
                raise ValueError("recovery comparison artifact hash mismatch")
        packets = {
            name: BenchmarkPacket.model_validate_json(
                (comparison_root / relative).read_text(encoding="utf-8")
            )
            for name, relative in {
                "qwen_single_pass": "neutral_packets/qwen_single_pass.json",
                "agent_laboratory": "neutral_packets/agent_laboratory.json",
                "hypoweaver": "neutral_packets/hypoweaver.json",
            }.items()
        }
        if any(packet.system_id != name for name, packet in packets.items()):
            raise ValueError("recovery comparison neutral packet identity mismatch")
        blind = PairedReviewSummary.model_validate_json(
            (comparison_root / "blind_reviews.json").read_text(encoding="utf-8")
        )
        if len(blind.reviews) != 5 or any(
            review.call_receipt is None for review in blind.reviews
        ):
            raise ValueError("recovery comparison requires five receipt-bound reviews")
        comparison_entry = {
            "status": "completed",
            "technical_failure": None,
            "claim_condition_met": manifest.claim_condition_met,
            "delivery_manifest_sha256": manifest.manifest_sha256,
            "files": {
                "delivery_manifest": _relative_delivery_path(
                    manifest_path,
                    delivery_root,
                ),
                "neutral_packets": {
                    name: _relative_delivery_path(
                        comparison_root / relative,
                        delivery_root,
                    )
                    for name, relative in {
                        "qwen_single_pass": "neutral_packets/qwen_single_pass.json",
                        "agent_laboratory": "neutral_packets/agent_laboratory.json",
                        "hypoweaver": "neutral_packets/hypoweaver.json",
                    }.items()
                },
                "hard_metrics": _relative_delivery_path(
                    comparison_root / "hard_metrics.json",
                    delivery_root,
                ),
                "ablations": _relative_delivery_path(
                    comparison_root / "ablations.json",
                    delivery_root,
                ),
                "blind_summary": _relative_delivery_path(
                    comparison_root / "blind_reviews.json",
                    delivery_root,
                ),
                "blind_reviews": [
                    _relative_delivery_path(
                        comparison_root / "blind_reviews" / f"review-{index}.json",
                        delivery_root,
                    )
                    for index in range(1, 6)
                ],
                "resource_usage": _relative_delivery_path(
                    comparison_root / "resource_usage.json",
                    delivery_root,
                ),
                "comparison_report": _relative_delivery_path(
                    comparison_root / "comparison_report_zh.md",
                    delivery_root,
                ),
            },
        }
        if manifest.claim_condition_met and (
            selected_record is None
            or not all(selected_record.hard_metric_results.values())
            or not all(
                selected_record.ablation_target_degradation_results.values()
            )
        ):
            raise ValueError("comparison claim condition conflicts with selected round")

    return {
        "round_statuses": round_statuses,
        "hard_metrics_by_round": hard_by_round,
        "fault_and_ablation_by_round": fault_by_round,
        "selection": selection,
        "artifact_index": {
            "provenance_scope": "seen_case_recovery_non_official",
            "campaign_id": campaign.campaign_id,
            "campaign_artifact_root": campaign.campaign_id,
            "rounds": artifact_rounds,
            "rejected_terminal_evidence": (
                {
                    "round_id": rejected_terminal_evidence["round_id"],
                    "status": "rejected_not_finalized_round",
                    "receipt_count": len(rejected_terminal_evidence["receipts"]),
                    "evidence_file": "rejected-terminal-evidence.json",
                    "receipts_file": "all-round-receipts.json",
                }
                if rejected_terminal_evidence is not None
                else None
            ),
            "comparison": comparison_entry,
            "top_level_files": {
                "campaign": "recovery-manifest.json",
                "protocol": "campaign-protocol.json",
                "prior_usage": "prior-official-usage-import.json",
                "cumulative_usage": "cumulative-resource-usage.json",
                "round_statuses": "round-statuses.json",
                "round_receipts": "all-round-receipts.json",
                "comparison_receipts": "comparison-receipts.json",
                "rejected_terminal_evidence": (
                    "rejected-terminal-evidence.json"
                    if rejected_terminal_evidence is not None
                    else None
                ),
                "selection": "selected-round.json",
                "report": "中文恢复评测报告.md",
            },
        },
    }


def _relative_delivery_path(path: Path, delivery_root: Path) -> str:
    resolved_root = delivery_root.resolve()
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("recovery artifact escapes delivery root")
    return resolved.relative_to(resolved_root).as_posix()


def _import_failed_source_usage(source: Any) -> Any:
    output_root = Path(source.output_dir).resolve(strict=True)
    state = _load_json(output_root / ".official-benchmark-state.json")
    failure = _load_json(output_root / "official_failure.json")
    run_manifest = _load_json(output_root / ".official-benchmark-run-manifest.json")
    _verify_failed_source_binding(
        source,
        output_root=output_root,
        state=state,
        failure=failure,
        run_manifest=run_manifest,
    )
    ledger_path = Path(f"{source.output_dir}-failed-delivery") / "resource_usage.json"
    ledger = _load_json(ledger_path)
    totals = ledger.get("known_totals") or {}
    if int(totals.get("llm_calls", 0) or 0) != 22:
        raise ValueError("source failed attempt ledger must contain exactly 22 calls")
    receipt_hashes = _source_receipt_hashes(Path(source.working_dir))
    if len(receipt_hashes) != 21:
        raise ValueError("source failed attempt must expose exactly 21 of 22 receipts")
    technical_failures = tuple(
        str(item)
        for section in ("agent_laboratory", "hypoweaver")
        for item in (ledger.get(section) or {}).get("technical_failures", [])
    )
    usage = RecoveryUsage(
        llm_calls=22,
        input_tokens=int(totals.get("input_tokens_lower_bound", 0) or 0),
        output_tokens=int(totals.get("output_tokens_lower_bound", 0) or 0),
        wall_time_seconds=float(ledger.get("official_attempt_elapsed_seconds", 0) or 0),
        technical_failures=technical_failures,
    )
    binding = OfficialAttemptBinding(
        attempt_id=str(state["attempt_id"]),
        run_manifest_sha256=str(state["run_manifest_sha256"]),
        begun_at=str(state["begun_at"]),
    )
    return import_prior_usage_from_ledger(
        binding,
        source_official_holdout_lock_id=str(state["holdout_lock_id"]),
        usage=usage,
        resource_ledger_sha256=_file_sha256(ledger_path),
        verified_receipt_sha256=receipt_hashes,
        token_usage_status="lower_bound",
    )


def _verify_failed_source_binding(
    source: Any,
    *,
    output_root: Path,
    state: dict[str, Any],
    failure: dict[str, Any],
    run_manifest: dict[str, Any],
) -> None:
    protocol_path = source.resolve_source(source.protocol_path)
    protocol = FrozenBenchmarkProtocol.model_validate_json(
        protocol_path.read_text(encoding="utf-8")
    )
    verify_protocol(protocol)
    unsigned_manifest = {
        key: value
        for key, value in run_manifest.items()
        if key != "run_manifest_sha256"
    }
    manifest_sha256 = canonical_sha256(unsigned_manifest)
    holdout_lock_id = canonical_sha256(
        {
            "lock_version": 1,
            "case_id": protocol.case_id,
            "visible_input_sha256": protocol.visible_input_sha256,
            "data_sha256": protocol.data_sha256,
        }
    )
    identity = {
        "attempt_id": state.get("attempt_id"),
        "run_manifest_sha256": state.get("run_manifest_sha256"),
        "begun_at": state.get("begun_at"),
    }
    expected_output = str(output_root)
    if (
        state.get("status") != "failed"
        or failure.get("status") != "failed"
        or state.get("protocol_sha256") != protocol.protocol_sha256
        or state.get("holdout_lock_id") != holdout_lock_id
        or state.get("output_dir") != expected_output
        or run_manifest.get("run_manifest_sha256") != manifest_sha256
        or identity["run_manifest_sha256"] != manifest_sha256
        or run_manifest.get("protocol_sha256") != protocol.protocol_sha256
        or run_manifest.get("holdout_lock_id") != holdout_lock_id
        or run_manifest.get("output_dir") != expected_output
        or run_manifest.get("artifact_root")
        != str(Path(source.artifact_root).resolve(strict=True))
        or run_manifest.get("source_artifact_paths")
        != source.source_artifact_paths
        or run_manifest.get("configuration_artifact_paths")
        != source.configuration_artifact_paths
        or run_manifest.get("source_sha256") != protocol.source_sha256
        or run_manifest.get("configuration_sha256")
        != protocol.configuration_sha256
        or any(run_manifest.get(key) != value for key, value in identity.items())
        or any(failure.get(key) != value for key, value in identity.items())
        or failure.get("error_type") != state.get("error_type")
    ):
        raise ValueError("source formal attempt binding or run manifest is invalid")
    state_root = Path(source.official_state_root).resolve(strict=True)
    canonical_path = (state_root / f"{holdout_lock_id}.json").resolve(strict=True)
    if not canonical_path.is_relative_to(state_root):
        raise ValueError("source formal canonical state escapes its state root")
    canonical_state = _load_json(canonical_path)
    if canonical_state != state:
        raise ValueError("source formal local and canonical states disagree")


def _same_prior_usage_binding(left: Any, right: Any) -> bool:
    excluded = {"imported_at", "import_sha256"}
    return left.model_dump(mode="json", exclude=excluded) == right.model_dump(
        mode="json",
        exclude=excluded,
    )


def _source_receipt_hashes(working_root: Path) -> tuple[str, ...]:
    hashes: list[str] = []
    database = working_root / "hypoweaver.db"
    states = RunRepository(database).list()
    if len(states) != 1:
        raise ValueError("source failed HypoWeaver database must contain one run")
    envelope = states[0].artifacts.get("model_usage") or {}
    payload = envelope.get("payload") or {}
    raw_hypoweaver = payload.get("call_receipts") or []
    if int(payload.get("llm_calls", 0) or 0) != len(raw_hypoweaver):
        raise ValueError("source HypoWeaver receipt ledger mismatch")
    for raw in raw_hypoweaver:
        ModelCallReceipt.model_validate(raw)
        hashes.append(canonical_sha256(raw))
    usage_paths = list((working_root / "agent-laboratory").glob("*/output/*/*/model_usage.json"))
    if len(usage_paths) != 1:
        raise ValueError("source Agent Laboratory usage ledger is ambiguous")
    agent_usage = _load_json(usage_paths[0])
    raw_agent = agent_usage.get("call_receipts") or []
    if int(agent_usage.get("llm_calls", 0) or 0) != len(raw_agent):
        raise ValueError("source Agent Laboratory receipt ledger mismatch")
    hashes.extend(canonical_sha256(item) for item in raw_agent)
    if len(set(hashes)) != len(hashes):
        raise ValueError("source receipt hashes are not unique")
    return tuple(hashes)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if canonical_sha256(existing) != canonical_sha256(payload):
            raise ValueError(f"recovery preparation artifact is append-only: {path.name}")
        return
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(rendered)


def _write_replace(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_text_replace(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _predecessor_roots(
    campaign_path: Path,
    campaign: RecoveryCampaign,
) -> tuple[Path, Path, Path]:
    campaign_path = campaign_path.resolve(strict=True)
    state_root = campaign_path.parent.resolve(strict=True)
    if not state_root.name.endswith("-state"):
        raise ValueError("predecessor state root is not canonical")
    delivery_name = state_root.name.removesuffix("-state")
    if not delivery_name:
        raise ValueError("predecessor state root has no delivery identity")
    delivery_root = state_root.with_name(delivery_name).resolve(strict=False)
    working_root = state_root.with_name(f"{delivery_name}-work").resolve(
        strict=False
    )
    expected_campaign_path = canonical_recovery_campaign_path(
        state_root,
        campaign.freeze,
    ).resolve(strict=False)
    if campaign_path != expected_campaign_path:
        raise ValueError("predecessor campaign path is not canonical")
    predecessor_prepared_path = delivery_root / "recovery-run-config.json"
    if predecessor_prepared_path.is_file():
        predecessor_prepared = PreparedRecoveryConfiguration.model_validate_json(
            predecessor_prepared_path.read_text(encoding="utf-8")
        )
        if (
            Path(predecessor_prepared.delivery_root).resolve(strict=False)
            != delivery_root
            or Path(predecessor_prepared.working_root).resolve(strict=False)
            != working_root
            or Path(predecessor_prepared.state_root).resolve(strict=False)
            != state_root
            or Path(predecessor_prepared.campaign_path).resolve(strict=False)
            != campaign_path
            or predecessor_prepared.campaign_id != campaign.campaign_id
            or predecessor_prepared.freeze_sha256 != campaign.freeze.freeze_sha256
        ):
            raise ValueError("predecessor prepared configuration binding mismatch")
    return delivery_root, working_root, state_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or run the non-formal Task3 seen-case recovery campaign."
    )
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--source-config", type=Path, default=DEFAULT_SOURCE_CONFIG)
    parser.add_argument("--delivery-root", type=Path, default=DEFAULT_DELIVERY_ROOT)
    parser.add_argument("--predecessor-campaign", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        campaign = prepare_recovery_campaign(
            source_config_path=args.source_config,
            delivery_root=args.delivery_root,
            predecessor_campaign_path=args.predecessor_campaign,
        )
    else:
        if args.predecessor_campaign is not None:
            raise ValueError(
                "run uses the predecessor path sealed by the prepare command"
            )
        campaign = asyncio.run(
            run_prepared_recovery(delivery_root=args.delivery_root)
        )
    print(campaign.model_dump_json(indent=2, by_alias=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
