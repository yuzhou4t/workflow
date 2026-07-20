from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .benchmark_evaluator import (
    evaluate_hard_metrics,
    seal_benchmark_packet,
    summarize_paired_reviews,
    verify_benchmark_packet,
)
from .benchmark_faults import replay_ablations
from .benchmark_models import (
    BenchmarkDeliveryManifest,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    BenchmarkUsageReport,
    FrozenBenchmarkProtocol,
    HardMetricReport,
    OfficialAttemptBinding,
    OfficialCallReceipt,
    PairedReviewSummary,
)
from .models import utc_now
from .seal import canonical_sha256


OFFICIAL_STATE_FILE = ".official-benchmark-state.json"
OFFICIAL_DELIVERY_LOCK_FILE = ".official-delivery-claimed"
OFFICIAL_RUN_MANIFEST_FILE = ".official-benchmark-run-manifest.json"
OFFICIAL_FAILURE_FILE = "official_failure.json"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT.parent
DEFAULT_OFFICIAL_STATE_ROOT = (
    PROJECT_ROOT / "backend" / "var" / "benchmarks" / "official-attempts"
)

_IGNORED_ARTIFACT_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_IGNORED_ARTIFACT_FILE_NAMES = {".DS_Store"}
_IGNORED_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}


def seal_protocol(protocol: FrozenBenchmarkProtocol) -> FrozenBenchmarkProtocol:
    payload = protocol.model_dump(mode="json", exclude={"protocol_sha256"})
    return protocol.model_copy(update={"protocol_sha256": canonical_sha256(payload)})


def verify_protocol(protocol: FrozenBenchmarkProtocol) -> None:
    if not protocol.protocol_sha256:
        raise ValueError("benchmark protocol is not frozen")
    payload = protocol.model_dump(mode="json", exclude={"protocol_sha256"})
    expected_hashes = {canonical_sha256(payload)}
    if (
        not protocol.source_artifact_paths
        and not protocol.configuration_artifact_paths
    ):
        legacy_payload = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "source_artifact_paths",
                "configuration_artifact_paths",
            }
        }
        expected_hashes.add(canonical_sha256(legacy_payload))
    if protocol.protocol_sha256 not in expected_hashes:
        raise ValueError("benchmark protocol sha256 mismatch")


def freeze_protocol(protocol: FrozenBenchmarkProtocol, target: Path) -> FrozenBenchmarkProtocol:
    if target.exists():
        raise FileExistsError(f"frozen protocol already exists: {target}")
    frozen = seal_protocol(protocol.model_copy(update={"protocol_sha256": None}))
    _write_json(target, frozen.model_dump(mode="json"), replace=False)
    return frozen


def begin_official_attempt(
    protocol: FrozenBenchmarkProtocol,
    output_dir: Path,
    *,
    state_root: Path | None = None,
    artifact_root: Path | None = None,
) -> Path:
    """Atomically reserve the one official attempt before any real model call."""

    verify_protocol(protocol)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / OFFICIAL_STATE_FILE
    if not state_path.exists() and any(output_dir.iterdir()):
        raise RuntimeError("official benchmark output directory must be empty before begin")
    snapshot = _snapshot_protocol_artifacts(
        protocol,
        artifact_root=artifact_root or DEFAULT_ARTIFACT_ROOT,
        output_dir=output_dir,
    )
    _assert_protocol_artifact_hashes(protocol, snapshot)
    run_manifest = _seal_run_manifest(
        {
            "manifest_version": 1,
            "attempt_id": secrets.token_hex(32),
            "begun_at": utc_now(),
            "protocol_sha256": protocol.protocol_sha256,
            "holdout_lock_id": official_holdout_lock_id(protocol),
            "output_dir": str(output_dir.resolve()),
            **snapshot,
        }
    )
    attempt = _attempt_binding_from_manifest(run_manifest)
    canonical_state_path = _canonical_state_path(protocol, state_root)
    _claim_official_attempt(
        canonical_state_path,
        protocol,
        output_dir=output_dir,
        attempt=attempt,
    )
    try:
        run_manifest_path = output_dir / OFFICIAL_RUN_MANIFEST_FILE
        _write_json(run_manifest_path, run_manifest, replace=False)
        os.chmod(run_manifest_path, 0o400)
        _claim_official_attempt(
            state_path,
            protocol,
            output_dir=output_dir,
            attempt=attempt,
        )
    except Exception:
        _write_json(
            canonical_state_path,
            _official_state_payload(
                protocol,
                output_dir=output_dir,
                status="failed",
                error_type="LocalStateReservationError",
                attempt=attempt,
            ),
        )
        raise
    return state_path


def fail_official_attempt(
    protocol: FrozenBenchmarkProtocol,
    output_dir: Path,
    error: BaseException,
    *,
    state_root: Path | None = None,
) -> Path:
    """Seal a pre-delivery failure without exposing exception messages or retrying."""

    verify_protocol(protocol)
    state_path = output_dir / OFFICIAL_STATE_FILE
    canonical_state_path = _canonical_state_path(protocol, state_root)
    local_state = _read_official_state(state_path)
    canonical_state = _read_official_state(canonical_state_path)
    expected_output_dir = str(output_dir.resolve())
    identity_fields = (
        "protocol_sha256",
        "holdout_lock_id",
        "output_dir",
        "attempt_id",
        "run_manifest_sha256",
        "begun_at",
    )
    if any(
        local_state.get(field) != canonical_state.get(field)
        for field in identity_fields
    ):
        raise RuntimeError("official benchmark state binding mismatch")
    if (
        local_state.get("protocol_sha256") != protocol.protocol_sha256
        or local_state.get("output_dir") != expected_output_dir
    ):
        raise RuntimeError("official benchmark state is bound to another attempt")
    if local_state.get("status") == "completed":
        raise RuntimeError("completed official benchmark cannot be marked failed")
    attempt = OfficialAttemptBinding(
        attempt_id=str(local_state.get("attempt_id", "")),
        run_manifest_sha256=str(local_state.get("run_manifest_sha256", "")),
        begun_at=str(local_state.get("begun_at", "")),
    )
    payload = _official_state_payload(
        protocol,
        output_dir=output_dir,
        status="failed",
        error_type=type(error).__name__,
        attempt=attempt,
    )
    _write_json(
        output_dir / OFFICIAL_FAILURE_FILE,
        {
            "status": "failed",
            "error_type": type(error).__name__,
            "failed_at": utc_now(),
            **attempt.model_dump(mode="json"),
        },
    )
    _write_official_state_pair(
        state_path=state_path,
        canonical_state_path=canonical_state_path,
        payload=payload,
    )
    return state_path


def run_benchmark_delivery(
    *,
    protocol: FrozenBenchmarkProtocol,
    reference: BenchmarkReference,
    qwen_packet: BenchmarkPacket,
    agent_laboratory_packet: BenchmarkPacket,
    hypoweaver_packet: BenchmarkPacket,
    blind_summary: PairedReviewSummary,
    output_dir: Path,
    official: bool,
    technical_failures: list[str] | None = None,
    official_state_root: Path | None = None,
) -> BenchmarkDeliveryManifest:
    """Compile the frozen packets into the complete T5 delivery bundle.

    Official mode only accepts a running state created by
    ``begin_official_attempt`` before any model call. A completed or failed
    attempt cannot be retried in the same output directory.
    """

    verify_protocol(protocol)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / OFFICIAL_STATE_FILE
    canonical_state_path: Path | None = None
    official_attempt: OfficialAttemptBinding | None = None
    if official:
        canonical_state_path, official_attempt = _claim_official_delivery(
            output_dir,
            protocol,
            state_root=official_state_root,
        )

    try:
        _validate_reference(protocol, reference)
        packets = {
            "qwen_single_pass": qwen_packet,
            "agent_laboratory": agent_laboratory_packet,
            "hypoweaver": hypoweaver_packet,
        }
        _validate_packets(protocol, packets)
        _validate_blind_summary(
            blind_summary, hypoweaver_packet, agent_laboratory_packet
        )
        if official:
            if official_attempt is None:
                raise RuntimeError("official benchmark attempt binding is unavailable")
            _validate_official_receipts(
                official_attempt,
                packets,
                blind_summary,
            )
        replay = replay_ablations(hypoweaver_packet)
        full_report = evaluate_hard_metrics(
            hypoweaver_packet,
            reference,
            fault_outcomes=replay.full_system_outcomes,
            clean_false_block_count=replay.clean_false_block_count,
        )
        comparison_reports = {
            "qwen_single_pass": evaluate_hard_metrics(
                qwen_packet,
                reference,
                fault_outcomes=[],
            ),
            "agent_laboratory": evaluate_hard_metrics(
                agent_laboratory_packet,
                reference,
                fault_outcomes=[],
            ),
            "hypoweaver": full_report,
        }
        usage = _usage_report(
            protocol,
            qwen_packet,
            agent_laboratory_packet,
            hypoweaver_packet,
            blind_summary,
            technical_failures or [],
        )
        if not usage.within_budget:
            raise ValueError("benchmark resource usage exceeds the frozen call budget")
        if official and usage.blind_review_calls != 5:
            raise ValueError("official benchmark requires five blind review calls")

        claim_condition = _claim_condition(
            full_report,
            comparison_reports["agent_laboratory"],
            blind_summary,
            hypoweaver_packet,
            agent_laboratory_packet,
        )
        files = _write_delivery_files(
            output_dir=output_dir,
            protocol=protocol,
            packets=packets,
            reports=comparison_reports,
            replay=replay.model_dump(mode="json"),
            blind_summary=blind_summary,
            usage=usage,
            claim_condition=claim_condition,
        )
        file_hashes = {
            str(path.relative_to(output_dir)): _file_sha256(path)
            for path in files
        }
        if official:
            _verify_official_run_manifest(
                output_dir,
                protocol,
                expected_manifest_sha256=official_attempt.run_manifest_sha256,
            )
        manifest = BenchmarkDeliveryManifest(
            protocol_sha256=protocol.protocol_sha256 or "",
            case_id=protocol.case_id,
            official=official,
            file_sha256=file_hashes,
            all_hard_gates_passed=full_report.all_hard_gates_passed,
            claim_condition_met=claim_condition,
        )
        manifest = manifest.model_copy(
            update={
                "manifest_sha256": canonical_sha256(
                    manifest.model_dump(mode="json", exclude={"manifest_sha256"})
                )
            }
        )
        manifest_path = output_dir / "delivery_manifest.json"
        _write_json(manifest_path, manifest.model_dump(mode="json"))
        if official:
            _write_official_state_pair(
                state_path=state_path,
                canonical_state_path=canonical_state_path,
                payload=_official_state_payload(
                    protocol,
                    output_dir=output_dir,
                    status="completed",
                    manifest_sha256=manifest.manifest_sha256,
                    attempt=official_attempt,
                ),
            )
        return manifest
    except Exception as error:
        if official:
            _write_official_state_pair(
                state_path=state_path,
                canonical_state_path=canonical_state_path,
                payload=_official_state_payload(
                    protocol,
                    output_dir=output_dir,
                    status="failed",
                    error_type=type(error).__name__,
                    attempt=official_attempt,
                ),
            )
        raise


def _validate_reference(
    protocol: FrozenBenchmarkProtocol,
    reference: BenchmarkReference,
) -> None:
    if canonical_sha256(reference.model_dump(mode="json")) != protocol.reference_sha256:
        raise ValueError("benchmark reference does not match the frozen protocol")
    if (
        reference.case_id != protocol.case_id
        or reference.visible_input_sha256 != protocol.visible_input_sha256
        or reference.data_sha256 != protocol.data_sha256
    ):
        raise ValueError("benchmark reference input identity mismatch")


def _validate_packets(
    protocol: FrozenBenchmarkProtocol,
    packets: dict[str, BenchmarkPacket],
) -> None:
    for expected_system, packet in packets.items():
        verify_benchmark_packet(packet)
        if packet.system_id != expected_system:
            raise ValueError(
                f"expected {expected_system} packet, received {packet.system_id}"
            )
        if (
            packet.case_id != protocol.case_id
            or packet.visible_input_sha256 != protocol.visible_input_sha256
            or packet.data_sha256 != protocol.data_sha256
        ):
            raise ValueError(f"{expected_system} packet input identity mismatch")


def _validate_blind_summary(
    summary: PairedReviewSummary,
    hypoweaver: BenchmarkPacket,
    agent_laboratory: BenchmarkPacket,
) -> None:
    if summary.case_id != hypoweaver.case_id:
        raise ValueError("blind summary case does not match benchmark packets")
    if (
        summary.packet_a_id != hypoweaver.packet_id
        or summary.packet_b_id != agent_laboratory.packet_id
    ):
        raise ValueError("blind summary must compare HypoWeaver and Agent Laboratory")
    if len(summary.reviews) != 5:
        raise ValueError("blind summary must contain exactly five reviews")
    if {review.system_assignment for review in summary.reviews} != {
        "A_B",
        "B_A",
    }:
        raise ValueError("blind summary must balance both anonymous system mappings")
    recomputed = summarize_paired_reviews(
        summary.case_id,
        summary.packet_a_id,
        summary.packet_b_id,
        summary.reviews,
    )
    if (
        summary.median_scores != recomputed.median_scores
        or summary.interquartile_ranges != recomputed.interquartile_ranges
        or summary.preference_counts != recomputed.preference_counts
    ):
        raise ValueError("blind summary aggregate does not match its sealed reviews")


def _validate_official_receipts(
    binding: OfficialAttemptBinding,
    packets: dict[str, BenchmarkPacket],
    summary: PairedReviewSummary,
) -> None:
    begun_at = datetime.fromisoformat(binding.begun_at)
    call_ids: set[str] = set()

    for system_id, packet in packets.items():
        if datetime.fromisoformat(packet.sealed_at) < begun_at:
            raise ValueError(
                f"official {system_id} packet predates begin_official_attempt"
            )
        receipts = packet.official_receipts
        if len(receipts) != packet.resource_usage.llm_calls:
            raise ValueError(
                f"official {system_id} packet receipts must match actual model calls"
            )
        if system_id == "qwen_single_pass":
            if len(receipts) != 1 or packet.resource_usage.llm_calls != 1:
                raise ValueError(
                    "official Qwen single-pass baseline requires exactly one call"
                )
            if receipts[0].provider != "qwen":
                raise ValueError(
                    "official Qwen single-pass baseline requires the qwen provider"
                )
            if receipts[0].model != packet.model_id:
                raise ValueError(
                    "official Qwen single-pass receipt model does not match its packet"
                )
        for receipt in receipts:
            _validate_official_call_receipt(receipt, binding, begun_at, call_ids)

    if len(summary.reviews) != 5:
        raise ValueError("official benchmark requires exactly five blind reviews")
    for review in summary.reviews:
        if review.resource_usage.llm_calls != 1:
            raise ValueError("each official blind review must record exactly one call")
        receipt = review.official_receipt
        if receipt is None:
            raise ValueError("official blind review is missing its call receipt")
        if receipt.provider != "qwen":
            raise ValueError("official blind reviews require the qwen provider")
        _validate_official_call_receipt(receipt, binding, begun_at, call_ids)


def _validate_official_call_receipt(
    receipt: OfficialCallReceipt,
    binding: OfficialAttemptBinding,
    begun_at: datetime,
    call_ids: set[str],
) -> None:
    if (
        receipt.attempt_id != binding.attempt_id
        or receipt.run_manifest_sha256 != binding.run_manifest_sha256
    ):
        raise ValueError("official call receipt is bound to another attempt")
    if receipt.provider != "qwen":
        raise ValueError("fixture or manual providers cannot produce official receipts")
    if datetime.fromisoformat(receipt.call_started_at) < begun_at:
        raise ValueError("official call receipt predates begin_official_attempt")
    if receipt.call_id in call_ids:
        raise ValueError("official call receipts cannot be reused")
    call_ids.add(receipt.call_id)


def _usage_report(
    protocol: FrozenBenchmarkProtocol,
    qwen: BenchmarkPacket,
    agent_laboratory: BenchmarkPacket,
    hypoweaver: BenchmarkPacket,
    blind_summary: PairedReviewSummary,
    technical_failures: list[str],
) -> BenchmarkUsageReport:
    blind_usage = BenchmarkResourceUsage(
        llm_calls=sum(
            review.resource_usage.llm_calls for review in blind_summary.reviews
        ),
        input_tokens=sum(
            review.resource_usage.input_tokens for review in blind_summary.reviews
        ),
        output_tokens=sum(
            review.resource_usage.output_tokens for review in blind_summary.reviews
        ),
        wall_time_seconds=sum(
            review.resource_usage.wall_time_seconds
            for review in blind_summary.reviews
        ),
        technical_failures=[
            failure
            for review in blind_summary.reviews
            for failure in review.resource_usage.technical_failures
        ],
    )
    blind_calls = blind_usage.llm_calls
    total = (
        qwen.resource_usage.llm_calls
        + agent_laboratory.resource_usage.llm_calls
        + hypoweaver.resource_usage.llm_calls
        + blind_calls
    )
    budget = protocol.call_budget
    within = (
        qwen.resource_usage.llm_calls == 1
        and hypoweaver.resource_usage.llm_calls <= budget.hypoweaver_max_calls
        and agent_laboratory.resource_usage.llm_calls
        <= budget.agent_laboratory_max_calls
        and blind_calls == budget.blind_review_calls
        and total <= budget.total_max_calls
    )
    return BenchmarkUsageReport(
        qwen_single_pass=qwen.resource_usage,
        hypoweaver=hypoweaver.resource_usage,
        agent_laboratory=agent_laboratory.resource_usage,
        blind_reviews=blind_usage,
        blind_review_calls=blind_calls,
        total_llm_calls=total,
        within_budget=within,
        technical_failures=[
            *technical_failures,
            *blind_usage.technical_failures,
        ],
    )


def _metric(report: HardMetricReport, metric_id: str) -> tuple[float, bool]:
    item = next(metric for metric in report.metrics if metric.metric_id == metric_id)
    value = item.value if item.denominator else 0.0
    return value, item.passed


def _claim_condition(
    full_report: HardMetricReport,
    agent_report: HardMetricReport,
    summary: PairedReviewSummary,
    hypoweaver: BenchmarkPacket,
    agent_laboratory: BenchmarkPacket,
) -> bool:
    method_full, _ = _metric(full_report, "contract_execution_fidelity")
    method_agent, _ = _metric(agent_report, "contract_execution_fidelity")
    numeric_full, _ = _metric(full_report, "protected_numeric_consistency")
    numeric_agent, _ = _metric(agent_report, "protected_numeric_consistency")
    strict_dimensions = (
        "fatal_fault_detection_rate",
        "required_evidence_completion",
        "statement_traceability",
    )
    strictly_better = any(
        _metric(full_report, metric_id)[0] > _metric(agent_report, metric_id)[0]
        for metric_id in strict_dimensions
    )
    full_causal = _metric(full_report, "causal_overreach_escape_count")[0]
    agent_causal = _metric(agent_report, "causal_overreach_escape_count")[0]
    strictly_better = strictly_better or full_causal < agent_causal

    hypoweaver_label = (
        "A" if summary.packet_a_id == hypoweaver.packet_id else "B"
    )
    agent_label = (
        "A" if summary.packet_a_id == agent_laboratory.packet_id else "B"
    )
    blind_ok = (
        summary.median_scores[hypoweaver_label]["overall"]
        > summary.median_scores[agent_label]["overall"]
        and summary.median_scores[hypoweaver_label]["soundness"]
        >= summary.median_scores[agent_label]["soundness"]
    )
    return (
        full_report.all_hard_gates_passed
        and method_full >= method_agent
        and numeric_full >= numeric_agent
        and strictly_better
        and blind_ok
    )


def _write_delivery_files(
    *,
    output_dir: Path,
    protocol: FrozenBenchmarkProtocol,
    packets: dict[str, BenchmarkPacket],
    reports: dict[str, HardMetricReport],
    replay: dict[str, Any],
    blind_summary: PairedReviewSummary,
    usage: BenchmarkUsageReport,
    claim_condition: bool,
) -> list[Path]:
    written: list[Path] = []
    protocol_path = output_dir / "frozen_protocol.json"
    _write_json(protocol_path, protocol.model_dump(mode="json"))
    written.append(protocol_path)

    packet_dir = output_dir / "neutral_packets"
    for system_id, packet in packets.items():
        path = packet_dir / f"{system_id}.json"
        _write_json(path, packet.model_dump(mode="json"))
        written.append(path)

    hard_path = output_dir / "hard_metrics.json"
    _write_json(
        hard_path,
        {key: report.model_dump(mode="json") for key, report in reports.items()},
    )
    written.append(hard_path)
    ablation_path = output_dir / "ablations.json"
    _write_json(ablation_path, replay)
    written.append(ablation_path)
    blind_path = output_dir / "blind_reviews.json"
    _write_json(blind_path, blind_summary.model_dump(mode="json"))
    written.append(blind_path)
    review_dir = output_dir / "blind_reviews"
    for review in blind_summary.reviews:
        path = review_dir / f"review-{review.sample_index}.json"
        _write_json(path, review.model_dump(mode="json"))
        written.append(path)
    usage_path = output_dir / "resource_usage.json"
    _write_json(usage_path, usage.model_dump(mode="json"))
    written.append(usage_path)
    report_path = output_dir / "comparison_report_zh.md"
    _write_text(
        report_path,
        _render_chinese_report(
            reports,
            replay,
            blind_summary,
            usage,
            claim_condition,
            protocol.call_budget.total_max_calls,
        ),
    )
    written.append(report_path)
    return written


def _render_chinese_report(
    reports: dict[str, HardMetricReport],
    replay: dict[str, Any],
    blind: PairedReviewSummary,
    usage: BenchmarkUsageReport,
    claim_condition: bool,
    total_call_budget: int = 46,
) -> str:
    full = reports["hypoweaver"]
    passed = sum(metric.passed for metric in full.metrics)
    ablations = replay.get("ablations", [])
    degraded = sum(bool(item.get("target_fault_degraded")) for item in ablations)
    conclusion = (
        "满足本次冻结企业面板案例的限定宣称条件。"
        if claim_condition
        else "未满足限定宣称条件，不作科学可靠性更高的结论。"
    )
    return (
        "# Task3 企业面板配对评测报告\n\n"
        "本报告只适用于本次冻结的企业面板案例，不外推到其他方法或科研任务。\n\n"
        f"- HypoWeaver 硬指标通过：{passed}/{len(full.metrics)}\n"
        f"- 九类故障识别：{sum(item['detected'] for item in replay['full_system_outcomes'])}/9\n"
        f"- 六项消融的目标故障劣化：{degraded}/6\n"
        f"- 模型盲评样本：{len(blind.reviews)} 次（非独立人工同行评审）\n"
        f"- 模型调用总数：{usage.total_llm_calls}/{total_call_budget}\n\n"
        f"结论：{conclusion}\n"
    )


def hash_protocol_artifacts(
    *,
    artifact_root: Path,
    source_artifact_paths: dict[str, list[str]],
    configuration_artifact_paths: list[str],
) -> tuple[dict[str, str], str]:
    """Hash explicit, static source/configuration artifacts for protocol creation."""

    snapshot = _snapshot_artifacts(
        artifact_root=artifact_root,
        source_artifact_paths=source_artifact_paths,
        configuration_artifact_paths=configuration_artifact_paths,
        output_dir=None,
    )
    return snapshot["source_sha256"], snapshot["configuration_sha256"]


def _snapshot_protocol_artifacts(
    protocol: FrozenBenchmarkProtocol,
    *,
    artifact_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    required_sources = {
        "hypoweaver",
        "agent_laboratory",
        "benchmark_harness",
    }
    if (
        set(protocol.source_artifact_paths) != required_sources
        or any(not paths for paths in protocol.source_artifact_paths.values())
        or not protocol.configuration_artifact_paths
    ):
        raise ValueError(
            "official benchmark protocol must declare non-empty source and "
            "configuration artifact paths"
        )
    return _snapshot_artifacts(
        artifact_root=artifact_root,
        source_artifact_paths=protocol.source_artifact_paths,
        configuration_artifact_paths=protocol.configuration_artifact_paths,
        output_dir=output_dir,
    )


def _snapshot_artifacts(
    *,
    artifact_root: Path,
    source_artifact_paths: dict[str, list[str]],
    configuration_artifact_paths: list[str],
    output_dir: Path | None,
) -> dict[str, Any]:
    resolved_root = artifact_root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("benchmark artifact root must be a directory")
    source_files = {
        source_id: _hash_declared_paths(
            resolved_root,
            paths,
            output_dir=output_dir,
        )
        for source_id, paths in sorted(source_artifact_paths.items())
    }
    configuration_files = _hash_declared_paths(
        resolved_root,
        configuration_artifact_paths,
        output_dir=output_dir,
    )
    return {
        "artifact_root": str(resolved_root),
        "source_artifact_paths": source_artifact_paths,
        "configuration_artifact_paths": configuration_artifact_paths,
        "source_files": source_files,
        "configuration_files": configuration_files,
        "source_sha256": {
            source_id: canonical_sha256(files)
            for source_id, files in source_files.items()
        },
        "configuration_sha256": canonical_sha256(configuration_files),
    }


def _hash_declared_paths(
    artifact_root: Path,
    relative_paths: list[str],
    *,
    output_dir: Path | None,
) -> dict[str, str]:
    if not relative_paths:
        raise ValueError("benchmark artifact path group cannot be empty")
    hashes: dict[str, str] = {}
    for relative_path in relative_paths:
        candidate = artifact_root.joinpath(*relative_path.split("/"))
        _reject_symlink_components(artifact_root, candidate)
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError(
                f"benchmark artifact does not exist: {relative_path}"
            ) from error
        if not resolved_candidate.is_relative_to(artifact_root):
            raise ValueError("benchmark artifact escapes the artifact root")
        if output_dir is not None:
            resolved_output = output_dir.resolve()
            if resolved_output == resolved_candidate or (
                resolved_candidate.is_dir()
                and resolved_output.is_relative_to(resolved_candidate)
            ):
                raise ValueError(
                    "official output directory cannot overlap a frozen artifact directory"
                )
        if resolved_candidate.is_file():
            files = [resolved_candidate]
        elif resolved_candidate.is_dir():
            files = []
            for path in sorted(resolved_candidate.rglob("*")):
                if path.is_symlink():
                    raise ValueError(
                        f"benchmark artifacts cannot contain symbolic links: {path}"
                    )
                if _ignored_artifact_path(path, resolved_candidate):
                    continue
                if path.is_file():
                    files.append(path)
        else:
            raise ValueError(
                f"benchmark artifact must be a regular file or directory: {relative_path}"
            )
        for path in files:
            key = path.relative_to(artifact_root).as_posix()
            hashes[key] = _file_sha256(path)
    if not hashes:
        raise ValueError("benchmark artifact path group contains no source files")
    return dict(sorted(hashes.items()))


def _reject_symlink_components(artifact_root: Path, candidate: Path) -> None:
    current = artifact_root
    for part in candidate.relative_to(artifact_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"benchmark artifacts cannot contain symbolic links: {current}"
            )


def _ignored_artifact_path(path: Path, declared_root: Path) -> bool:
    relative = path.relative_to(declared_root)
    return (
        any(part in _IGNORED_ARTIFACT_DIRECTORY_NAMES for part in relative.parts)
        or path.name in _IGNORED_ARTIFACT_FILE_NAMES
        or path.suffix in _IGNORED_ARTIFACT_SUFFIXES
    )


def _assert_protocol_artifact_hashes(
    protocol: FrozenBenchmarkProtocol,
    snapshot: dict[str, Any],
) -> None:
    if snapshot["source_sha256"] != protocol.source_sha256:
        raise ValueError("benchmark source artifacts do not match the frozen protocol")
    if snapshot["configuration_sha256"] != protocol.configuration_sha256:
        raise ValueError(
            "benchmark configuration artifacts do not match the frozen protocol"
        )


def _seal_run_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "run_manifest_sha256": canonical_sha256(payload),
    }


def _attempt_binding_from_manifest(manifest: dict[str, Any]) -> OfficialAttemptBinding:
    return OfficialAttemptBinding(
        attempt_id=str(manifest.get("attempt_id", "")),
        run_manifest_sha256=str(manifest.get("run_manifest_sha256", "")),
        begun_at=str(manifest.get("begun_at", "")),
    )


def load_official_attempt_binding(output_dir: Path) -> OfficialAttemptBinding:
    """Read and self-verify the begin marker used by code-owned model runners."""

    path = output_dir / OFFICIAL_RUN_MANIFEST_FILE
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("official benchmark run manifest is unreadable") from error
    if not isinstance(manifest, dict):
        raise ValueError("official benchmark run manifest is unreadable")
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key != "run_manifest_sha256"
    }
    if canonical_sha256(unsigned) != manifest.get("run_manifest_sha256"):
        raise ValueError("official benchmark run manifest sha256 mismatch")
    binding = _attempt_binding_from_manifest(manifest)
    try:
        state = _read_official_state(output_dir / OFFICIAL_STATE_FILE)
    except RuntimeError as error:
        raise ValueError("official benchmark begin state is unavailable") from error
    if state.get("status") != "running" or any(
        state.get(field) != value
        for field, value in binding.model_dump(mode="json").items()
    ):
        raise ValueError("official benchmark begin state/manifest binding mismatch")
    return binding


def create_official_call_receipt(
    binding: OfficialAttemptBinding,
    *,
    provider: str,
    model: str,
    call_started_at: str,
    call_completed_at: str,
    raw_response: Any | None = None,
    raw_response_sha256: str | None = None,
) -> OfficialCallReceipt:
    """Bind one provider response to the already-created official attempt."""

    if (raw_response is None) == (raw_response_sha256 is None):
        raise ValueError(
            "provide exactly one of raw_response or raw_response_sha256"
        )
    if raw_response_sha256 is None:
        if isinstance(raw_response, bytes):
            response_sha256 = hashlib.sha256(raw_response).hexdigest()
        elif isinstance(raw_response, str):
            response_sha256 = hashlib.sha256(
                raw_response.encode("utf-8")
            ).hexdigest()
        else:
            response_sha256 = canonical_sha256(raw_response)
    else:
        response_sha256 = raw_response_sha256
    receipt = OfficialCallReceipt(
        attempt_id=binding.attempt_id,
        run_manifest_sha256=binding.run_manifest_sha256,
        provider=provider,
        model=model,
        response_sha256=response_sha256,
        call_started_at=call_started_at,
        call_completed_at=call_completed_at,
    )
    if datetime.fromisoformat(receipt.call_started_at) < datetime.fromisoformat(
        binding.begun_at
    ):
        raise ValueError("official call cannot predate begin_official_attempt")
    return receipt


def bind_official_packet_receipts(
    packet: BenchmarkPacket,
    receipts: list[OfficialCallReceipt],
) -> BenchmarkPacket:
    """Attach code-owned call receipts and reseal a neutral packet."""

    if len(receipts) != packet.resource_usage.llm_calls:
        raise ValueError("official packet receipts must match actual model calls")
    if not receipts:
        return seal_benchmark_packet(
            packet.model_copy(
                update={
                    "official_receipts": [],
                    "sealed_at": utc_now(),
                    "packet_sha256": None,
                }
            )
        )
    identities = {
        (receipt.attempt_id, receipt.run_manifest_sha256)
        for receipt in receipts
    }
    if len(identities) != 1:
        raise ValueError("official packet receipts must share one attempt binding")
    return seal_benchmark_packet(
        packet.model_copy(
            update={
                "official_receipts": receipts,
                "sealed_at": utc_now(),
                "packet_sha256": None,
            }
        )
    )


def _verify_official_run_manifest(
    output_dir: Path,
    protocol: FrozenBenchmarkProtocol,
    *,
    expected_manifest_sha256: str,
) -> None:
    path = output_dir / OFFICIAL_RUN_MANIFEST_FILE
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("official benchmark run manifest is unreadable") from error
    if not isinstance(manifest, dict):
        raise ValueError("official benchmark run manifest is unreadable")
    manifest_sha256 = manifest.get("run_manifest_sha256")
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key != "run_manifest_sha256"
    }
    if (
        manifest_sha256 != expected_manifest_sha256
        or canonical_sha256(unsigned) != manifest_sha256
        or not manifest.get("attempt_id")
        or not manifest.get("begun_at")
        or manifest.get("protocol_sha256") != protocol.protocol_sha256
        or manifest.get("holdout_lock_id") != official_holdout_lock_id(protocol)
        or manifest.get("output_dir") != str(output_dir.resolve())
        or manifest.get("source_artifact_paths") != protocol.source_artifact_paths
        or manifest.get("configuration_artifact_paths")
        != protocol.configuration_artifact_paths
    ):
        raise ValueError("official benchmark run manifest binding mismatch")
    current = _snapshot_protocol_artifacts(
        protocol,
        artifact_root=Path(str(manifest.get("artifact_root", ""))),
        output_dir=output_dir,
    )
    _assert_protocol_artifact_hashes(protocol, current)
    for key in (
        "artifact_root",
        "source_files",
        "configuration_files",
        "source_sha256",
        "configuration_sha256",
    ):
        if current[key] != manifest.get(key):
            raise ValueError("official benchmark source/configuration artifact drift")


def _canonical_state_path(
    protocol: FrozenBenchmarkProtocol,
    state_root: Path | None,
) -> Path:
    if not protocol.protocol_sha256:
        raise ValueError("benchmark protocol is not frozen")
    root = state_root if state_root is not None else DEFAULT_OFFICIAL_STATE_ROOT
    return root / f"{official_holdout_lock_id(protocol)}.json"


def official_holdout_lock_id(protocol: FrozenBenchmarkProtocol) -> str:
    """Stable one-shot key that source/config/reference edits cannot redraw."""

    return canonical_sha256(
        {
            "lock_version": 1,
            "case_id": protocol.case_id,
            "visible_input_sha256": protocol.visible_input_sha256,
            "data_sha256": protocol.data_sha256,
        }
    )


def _official_state_payload(
    protocol: FrozenBenchmarkProtocol,
    *,
    output_dir: Path,
    status: str,
    manifest_sha256: str | None = None,
    error_type: str | None = None,
    attempt: OfficialAttemptBinding | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "protocol_sha256": protocol.protocol_sha256,
        "holdout_lock_id": official_holdout_lock_id(protocol),
        "output_dir": str(output_dir.resolve()),
    }
    if manifest_sha256 is not None:
        payload["manifest_sha256"] = manifest_sha256
    if attempt is not None:
        payload.update(attempt.model_dump(mode="json"))
    if error_type is not None:
        payload["error_type"] = error_type
    return payload


def _claim_official_attempt(
    path: Path,
    protocol: FrozenBenchmarkProtocol,
    *,
    output_dir: Path,
    attempt: OfficialAttemptBinding,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        _official_state_payload(
            protocol,
            output_dir=output_dir,
            status="running",
            attempt=attempt,
        ),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RuntimeError("official hidden benchmark is one-shot and was already attempted") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _claim_official_delivery(
    output_dir: Path,
    protocol: FrozenBenchmarkProtocol,
    *,
    state_root: Path | None,
) -> tuple[Path, OfficialAttemptBinding]:
    state_path = output_dir / OFFICIAL_STATE_FILE
    if not state_path.is_file():
        raise RuntimeError(
            "official benchmark must call begin_official_attempt before any model call"
        )
    canonical_state_path = _canonical_state_path(protocol, state_root)
    local_state = _read_official_state(state_path)
    canonical_state = _read_official_state(canonical_state_path)
    expected_output_dir = str(output_dir.resolve())
    for state in (local_state, canonical_state):
        if state.get("protocol_sha256") != protocol.protocol_sha256:
            raise RuntimeError("official benchmark state is bound to another protocol")
        if state.get("holdout_lock_id") != official_holdout_lock_id(protocol):
            raise RuntimeError("official benchmark state is bound to another holdout case")
        if state.get("output_dir") != expected_output_dir:
            raise RuntimeError("official benchmark state is bound to another output directory")
        if state.get("status") != "running":
            raise RuntimeError(
                "official hidden benchmark is one-shot and was already attempted"
            )
    binding_payloads = {
        (
            state.get("attempt_id"),
            state.get("run_manifest_sha256"),
            state.get("begun_at"),
        )
        for state in (local_state, canonical_state)
    }
    if len(binding_payloads) != 1:
        raise RuntimeError("official benchmark state has no run manifest binding")
    attempt_id, run_manifest_sha256, begun_at = next(iter(binding_payloads))
    try:
        attempt = OfficialAttemptBinding(
            attempt_id=str(attempt_id or ""),
            run_manifest_sha256=str(run_manifest_sha256 or ""),
            begun_at=str(begun_at or ""),
        )
    except ValueError as error:
        raise RuntimeError(
            "official benchmark state has no run manifest binding"
        ) from error
    try:
        _verify_official_run_manifest(
            output_dir,
            protocol,
            expected_manifest_sha256=attempt.run_manifest_sha256,
        )
        if load_official_attempt_binding(output_dir) != attempt:
            raise ValueError(
                "official benchmark state/run manifest binding mismatch"
            )
    except Exception as error:
        _write_official_state_pair(
            state_path=state_path,
            canonical_state_path=canonical_state_path,
            payload=_official_state_payload(
                protocol,
                output_dir=output_dir,
                status="failed",
                error_type="ArtifactDriftError",
                attempt=attempt,
            ),
        )
        raise ValueError(
            "official benchmark source/configuration artifact drift"
        ) from error
    lock_path = output_dir / OFFICIAL_DELIVERY_LOCK_FILE
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise RuntimeError("official hidden benchmark delivery was already claimed") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write((protocol.protocol_sha256 or "") + "\n")
    _write_official_state_pair(
        state_path=state_path,
        canonical_state_path=canonical_state_path,
        payload=_official_state_payload(
            protocol,
            output_dir=output_dir,
            status="compiling",
            attempt=attempt,
        ),
    )
    return canonical_state_path, attempt


def _read_official_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("official benchmark state is unreadable") from error
    if not isinstance(state, dict):
        raise RuntimeError("official benchmark state is unreadable")
    return state


def _write_official_state_pair(
    *,
    state_path: Path,
    canonical_state_path: Path | None,
    payload: dict[str, Any],
) -> None:
    if canonical_state_path is None:
        raise RuntimeError("official benchmark canonical state is unavailable")
    _write_json(canonical_state_path, payload)
    _write_json(state_path, payload)


def _write_json(path: Path, payload: Any, *, replace: bool = True) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        replace=replace,
    )


def _write_text(path: Path, content: str) -> None:
    _atomic_write(path, content, replace=True)


def _atomic_write(path: Path, content: str, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as error:
            raise FileExistsError(path) from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_model(path: Path, model: type[Any]) -> Any:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze and compile Task3 T5 benchmark artifacts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--protocol", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    begin = subparsers.add_parser("begin")
    begin.add_argument("--protocol", type=Path, required=True)
    begin.add_argument("--output-dir", type=Path, required=True)
    begin.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="root for the relative source/configuration artifacts frozen in the protocol",
    )
    deliver = subparsers.add_parser("deliver")
    deliver.add_argument("--protocol", type=Path, required=True)
    deliver.add_argument("--reference", type=Path, required=True)
    deliver.add_argument("--qwen-packet", type=Path, required=True)
    deliver.add_argument("--agent-laboratory-packet", type=Path, required=True)
    deliver.add_argument("--hypoweaver-packet", type=Path, required=True)
    deliver.add_argument("--blind-summary", type=Path, required=True)
    deliver.add_argument("--output-dir", type=Path, required=True)
    deliver.add_argument("--official", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "freeze":
        protocol = _load_model(args.protocol, FrozenBenchmarkProtocol)
        freeze_protocol(protocol, args.output)
        return 0
    if args.command == "begin":
        begin_official_attempt(
            _load_model(args.protocol, FrozenBenchmarkProtocol),
            args.output_dir,
            artifact_root=args.artifact_root,
        )
        return 0
    run_benchmark_delivery(
        protocol=_load_model(args.protocol, FrozenBenchmarkProtocol),
        reference=_load_model(args.reference, BenchmarkReference),
        qwen_packet=_load_model(args.qwen_packet, BenchmarkPacket),
        agent_laboratory_packet=_load_model(
            args.agent_laboratory_packet, BenchmarkPacket
        ),
        hypoweaver_packet=_load_model(args.hypoweaver_packet, BenchmarkPacket),
        blind_summary=_load_model(args.blind_summary, PairedReviewSummary),
        output_dir=args.output_dir,
        official=args.official,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
