from __future__ import annotations

import math
import re
from collections import Counter
from statistics import median
from typing import Any

from .benchmark_models import (
    BenchmarkPacket,
    BenchmarkReference,
    FaultOutcome,
    HardMetric,
    HardMetricReport,
    NeurIPSReview,
    NormalizedStatement,
    PairedReviewSummary,
    TERMINAL_EXECUTION_STATUSES,
)
from .claim_gate import causal_wording_violations
from .models import TRACEABLE_MANUSCRIPT_SECTION_IDS
from .manuscript_ir import (
    BARE_NUMBER_RE,
    EMPIRICAL_JUDGMENT_PATTERNS,
    ManuscriptIRError,
    format_protected_value,
)
from .seal import canonical_sha256


def seal_benchmark_packet(packet: BenchmarkPacket) -> BenchmarkPacket:
    payload = packet.model_dump(mode="json", exclude={"packet_sha256"})
    return packet.model_copy(update={"packet_sha256": canonical_sha256(payload)})


def verify_benchmark_packet(packet: BenchmarkPacket) -> None:
    if not packet.packet_sha256:
        raise ValueError("benchmark packet is not sealed")
    expected = canonical_sha256(
        packet.model_dump(mode="json", exclude={"packet_sha256"})
    )
    if expected != packet.packet_sha256:
        raise ValueError("benchmark packet sha256 mismatch")


def _rate_metric(
    metric_id: str,
    numerator: int,
    denominator: int,
    *,
    target: str,
    passed: bool,
    evidence: list[str],
) -> HardMetric:
    value = round(numerator / denominator, 6) if denominator else 1.0
    return HardMetric(
        metric_id=metric_id,
        numerator=numerator,
        denominator=denominator,
        value=value,
        target=target,
        passed=passed,
        evidence=evidence,
    )


def _design_checks(packet: BenchmarkPacket, reference: BenchmarkReference) -> list[tuple[str, bool]]:
    expected = reference.expected_design
    actual = packet.design.model_dump(mode="json")
    checks: list[tuple[str, bool]] = []
    for key, expected_value in expected.items():
        if key not in actual:
            checks.append((key, False))
            continue
        actual_value = actual[key]
        if isinstance(expected_value, list):
            checks.append((key, set(actual_value or []) == set(expected_value)))
        else:
            checks.append((key, actual_value == expected_value))
    checks.append(("visible_input_sha256", packet.visible_input_sha256 == reference.visible_input_sha256))
    checks.append(("data_sha256", packet.data_sha256 == reference.data_sha256))
    return checks


def _contract_execution_checks(
    packet: BenchmarkPacket,
    reference: BenchmarkReference,
) -> list[tuple[str, bool]]:
    """Check only execution facts explicitly present in the neutral packet."""

    checks: list[tuple[str, bool]] = []
    planned = packet.design.planned_check_ids
    required = packet.design.required_check_ids
    planned_set = set(planned)
    check_ids = [execution.check_id for execution in packet.executions]
    execution_ids = [execution.execution_id for execution in packet.executions]
    checks.extend(
        (
            (
                "design.source_artifact_sha256",
                _is_sha256(packet.design.source_artifact_sha256),
            ),
            (
                "design.contract_sha256",
                _is_sha256(packet.design.contract_sha256),
            ),
            ("design.planned_check_ids_unique", len(planned) == len(planned_set)),
            ("design.required_check_ids_unique", len(required) == len(set(required))),
            ("design.required_checks_planned", set(required).issubset(planned_set)),
            ("execution.execution_ids_unique", len(execution_ids) == len(set(execution_ids))),
            ("execution.check_ids_unique", len(check_ids) == len(set(check_ids))),
            (
                "execution.check_ids_known",
                all(check_id in planned_set for check_id in check_ids),
            ),
            (
                "execution.check_ids_complete",
                set(check_ids) == planned_set,
            ),
            (
                "design.check_threat_ids_known",
                set(packet.design.check_threat_ids).issubset(planned_set),
            ),
            (
                "design.required_threat_ids_present",
                set(reference.required_threat_ids).issubset(
                    set(packet.design.check_threat_ids.values())
                ),
            ),
            (
                "design.reference_required_checks_present",
                set(reference.required_check_ids).issubset(set(required)),
            ),
        )
    )
    if packet.system_id in {"hypoweaver", "hypoweaver_ablation"}:
        checks.append(
            (
                "native.formal_research_contract",
                bool(packet.design.contract_sha256)
                and packet.native_artifact_sha256.get(
                    "formal_research_contract"
                )
                == packet.design.contract_sha256,
            )
        )

    expected_fixed_effects = reference.expected_design.get("fixed_effects")
    expected_cluster = reference.expected_design.get("standard_error_strategy")
    successful = [
        execution
        for execution in packet.executions
        if execution.execution_status == "succeeded"
    ]
    observed_contract_hashes: list[str] = []
    for execution in successful:
        prefix = f"execution.{execution.execution_id}"
        checks.append((f"{prefix}.contract_sha256", bool(execution.contract_sha256)))
        if packet.system_id in {"hypoweaver", "hypoweaver_ablation"}:
            checks.extend(
                (
                    (f"{prefix}.implementation_id", bool(execution.implementation_id)),
                    (
                        f"{prefix}.implementation_version",
                        bool(execution.implementation_version),
                    ),
                    (f"{prefix}.code_sha256", _is_sha256(execution.code_sha256)),
                    (
                        f"{prefix}.environment_sha256",
                        _is_sha256(execution.environment_sha256),
                    ),
                    (
                        f"{prefix}.source_artifact_sha256",
                        _is_sha256(execution.source_artifact_sha256),
                    ),
                )
            )
        checks.append(
            (
                f"{prefix}.data_sha256",
                bool(execution.data_sha256)
                and execution.data_sha256 == packet.data_sha256
                and execution.data_sha256 == reference.data_sha256,
            )
        )
        if execution.contract_sha256:
            observed_contract_hashes.append(execution.contract_sha256)
        checks.append(
            (
                f"{prefix}.contract_sha256_expected",
                execution.contract_sha256 == packet.design.contract_sha256
                and (
                    reference.expected_contract_sha256 is None
                    or execution.contract_sha256
                    == reference.expected_contract_sha256
                ),
            )
        )
        if execution.estimates:
            if expected_fixed_effects is not None:
                checks.append(
                    (
                        f"{prefix}.fixed_effects",
                        execution.fixed_effects == list(expected_fixed_effects),
                    )
                )
            if expected_cluster is not None:
                checks.append(
                    (
                        f"{prefix}.standard_error_strategy",
                        execution.standard_error_strategy == expected_cluster,
                    )
                )
    checks.append(
        (
            "execution.contract_sha256_consistent",
            bool(successful)
            and len(observed_contract_hashes) == len(successful)
            and len(set(observed_contract_hashes)) == 1,
        )
    )
    return checks


def _resolve_source_path(execution: dict[str, Any], source_path: str) -> Any:
    if not source_path.startswith("/"):
        raise ValueError("source_path must be an absolute JSON Pointer")
    value: Any = execution
    for raw in source_path.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise KeyError(source_path)
    return value


def _is_sha256(value: str | None) -> bool:
    return bool(
        value
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _rendered_value_occurrences(text: str, rendered: str) -> int:
    return len(
        re.findall(
            rf"(?<![+\-\d.]){re.escape(rendered)}(?![\d.])",
            text,
        )
    )


def protected_numeric_consistency(
    packet: BenchmarkPacket,
) -> tuple[int, int, list[str]]:
    """Validate normalized values with the production Manuscript IR formatter."""

    execution_payloads = {
        execution.execution_id: execution.model_dump(mode="json")
        for execution in packet.executions
    }
    numeric_kinds = {
        "count",
        "coefficient",
        "standard_error",
        "interval_bound",
        "p_value",
        "fit_statistic",
        "year",
    }
    occurrence_expectations = Counter(
        statement.text
        for statement in packet.statements
        if any(
            str(value.get("value_kind")) in numeric_kinds
            for value in statement.protected_values
        )
    )
    occurrence_matches = {
        text: bool(text) and packet.manuscript_text.count(text) == expected
        for text, expected in occurrence_expectations.items()
    }
    total = 0
    valid = 0
    failures: list[str] = []
    for statement in packet.statements:
        for protected in statement.protected_values:
            value_kind = str(protected.get("value_kind"))
            if value_kind not in numeric_kinds:
                continue
            total += 1
            failure_id = str(protected.get("value_id", statement.statement_id))
            try:
                if str(protected.get("source_kind", "execution")) != "execution":
                    raise ValueError("numeric values must point to an execution")
                source = execution_payloads[str(protected["source_id"])]
                raw = _resolve_source_path(source, str(protected["source_path"]))
                rendered = format_protected_value(value_kind, raw)
                rendered_counts = Counter(
                    str(item.get("rendered_value"))
                    for item in statement.protected_values
                    if str(item.get("value_kind")) in numeric_kinds
                )
                if (
                    raw == protected.get("raw_value")
                    and rendered == protected.get("rendered_value")
                    and _rendered_value_occurrences(statement.text, rendered)
                    >= rendered_counts[rendered]
                    and occurrence_matches.get(statement.text, False)
                ):
                    valid += 1
                else:
                    failures.append(failure_id)
            except (
                KeyError,
                ValueError,
                TypeError,
                IndexError,
                ManuscriptIRError,
            ):
                failures.append(failure_id)
    return valid, total, failures


def _statement_occurrence_id(statement: NormalizedStatement) -> str:
    return (
        f"{statement.section_id}:{statement.statement_id}"
        if statement.section_id
        else statement.statement_id
    )


def _traceability_body_audit(
    packet: BenchmarkPacket,
    empirical: list[NormalizedStatement],
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    expected_occurrences = Counter(statement.text for statement in empirical)
    for text, expected in expected_occurrences.items():
        if not text or packet.manuscript_text.count(text) != expected:
            failures.append("manuscript:statement_occurrence_mismatch")
            break

    residual = packet.manuscript_text
    for text in {statement.text for statement in packet.statements if statement.text}:
        residual = residual.replace(text, " ")
    if BARE_NUMBER_RE.search(residual):
        failures.append("manuscript:unprotected_numeric_text")
    if any(pattern.search(residual) for pattern in EMPIRICAL_JUDGMENT_PATTERNS):
        failures.append("manuscript:untracked_empirical_judgment")
    if any(claim.text and claim.text in residual for claim in packet.claims):
        failures.append("manuscript:untracked_authorized_claim")
    if not empirical:
        failures.append("manuscript:missing_empirical_statement_registry")
    return not failures, list(dict.fromkeys(failures))


def _manuscript_causal_overreaches(packet: BenchmarkPacket) -> list[str]:
    """Detect causal prose that is not supplied by an authorized statement."""

    if packet.manuscript_section_texts:
        if packet.system_id in {"hypoweaver", "hypoweaver_ablation"}:
            sections = {
                section_id: text
                for section_id, text in packet.manuscript_section_texts.items()
                if section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
            }
        else:
            sections = packet.manuscript_section_texts
    else:
        sections = {"manuscript": packet.manuscript_text}

    failures: list[str] = []
    for section_id, section_text in sections.items():
        residual = section_text
        for statement in packet.statements:
            if statement.text and (
                statement.section_id in {None, section_id}
                or section_id in {"report", "manuscript"}
            ):
                residual = residual.replace(statement.text, " ")
        for predicate in causal_wording_violations(residual, "associational"):
            failures.append(f"manuscript:{section_id}:{predicate}")
    return list(dict.fromkeys(failures))


def _is_unauthorized_causal(text: str, strength: str) -> bool:
    known_strengths = {
        "causal_strong",
        "causal_cautious",
        "associational",
        "preliminary",
        "mixed",
        "insufficient",
        "prohibited",
    }
    effective_strength = strength if strength in known_strengths else "prohibited"
    return bool(causal_wording_violations(text, effective_strength))


def evaluate_hard_metrics(
    packet: BenchmarkPacket,
    reference: BenchmarkReference,
    *,
    fault_outcomes: list[FaultOutcome],
    clean_false_block_count: int = 0,
) -> HardMetricReport:
    verify_benchmark_packet(packet)
    if packet.case_id != reference.case_id:
        raise ValueError("benchmark packet case does not match reference")

    metrics: list[HardMetric] = []
    design_checks = [
        *_design_checks(packet, reference),
        *_contract_execution_checks(packet, reference),
    ]
    design_passed = sum(1 for _, passed in design_checks if passed)
    metrics.append(
        _rate_metric(
            "contract_execution_fidelity",
            design_passed,
            len(design_checks),
            target="100%",
            passed=design_passed == len(design_checks),
            evidence=[name for name, passed in design_checks if not passed],
        )
    )

    by_check = {execution.check_id: execution for execution in packet.executions}
    expected_terminal = set(packet.design.required_check_ids) | set(
        reference.required_check_ids
    )
    terminal = [
        check_id
        for check_id in expected_terminal
        if check_id in by_check
        and by_check[check_id].execution_status in TERMINAL_EXECUTION_STATUSES
    ]
    missing_terminal = sorted(expected_terminal - set(terminal))
    metrics.append(
        _rate_metric(
            "required_step_terminal_rate",
            len(terminal),
            len(expected_terminal),
            target="100%",
            passed=not missing_terminal,
            evidence=missing_terminal,
        )
    )

    evidence_complete: list[str] = []
    incomplete_evidence: list[str] = []
    for check_id in sorted(expected_terminal):
        execution = by_check.get(check_id)
        if execution is None:
            incomplete_evidence.append(f"{check_id}:missing")
        elif execution.execution_status != "succeeded":
            incomplete_evidence.append(
                f"{check_id}:{execution.execution_status}"
            )
        elif not execution.estimates and not execution.diagnostics:
            incomplete_evidence.append(
                f"{check_id}:succeeded_without_evidence"
            )
        else:
            evidence_complete.append(check_id)
    metrics.append(
        _rate_metric(
            "required_evidence_completion",
            len(evidence_complete),
            len(expected_terminal),
            target="100%",
            passed=not incomplete_evidence,
            evidence=incomplete_evidence,
        )
    )

    expected_fault_ids = {
        "duplicate_merge_inflation",
        "time_leakage_or_lead_misuse",
        "unit_amplification",
        "variable_timing_shift",
        "wrong_clustering",
        "significant_subgroup_cherry_pick",
        "table_text_mismatch",
        "association_to_causation",
        "deleted_null_or_failure_branch",
    }
    fault_by_id = {outcome.fault_id: outcome for outcome in fault_outcomes}
    detected = [
        fault_id
        for fault_id in expected_fault_ids
        if fault_id in fault_by_id
        and fault_by_id[fault_id].detected
        and fault_by_id[fault_id].action != "missed"
    ]
    missed_faults = sorted(expected_fault_ids - set(detected))
    metrics.append(
        _rate_metric(
            "fatal_fault_detection_rate",
            len(detected),
            len(expected_fault_ids),
            target="9/9",
            passed=not missed_faults,
            evidence=missed_faults,
        )
    )
    metrics.append(
        HardMetric(
            metric_id="clean_false_block_count",
            numerator=clean_false_block_count,
            denominator=max(1, len(reference.clean_packet_ids)),
            value=float(clean_false_block_count),
            target="0",
            passed=clean_false_block_count == 0,
            evidence=[] if clean_false_block_count == 0 else [str(clean_false_block_count)],
        )
    )

    protected_valid, protected_total, protected_failures = (
        protected_numeric_consistency(packet)
    )
    metrics.append(
        _rate_metric(
            "protected_numeric_consistency",
            protected_valid,
            protected_total,
            target="100%",
            passed=protected_total > 0 and protected_valid == protected_total,
            evidence=protected_failures,
        )
    )

    eligible_claims = [
        claim
        for claim in packet.claims
        if claim.admission_status not in {"rejected", "prohibited"}
    ]
    known_claims = {claim.claim_id for claim in eligible_claims}
    known_executions = {execution.execution_id for execution in packet.executions}
    empirical = [
        statement
        for statement in packet.statements
        if statement.statement_kind != "citation"
    ]

    def is_traceable(statement: NormalizedStatement) -> bool:
        if statement.statement_kind == "authorized_claim":
            return bool(statement.claim_ids) and set(statement.claim_ids).issubset(
                known_claims
            )
        if statement.statement_kind in {
            "estimate_fact",
            "sample_fact",
            "diagnostic_fact",
        }:
            return bool(statement.execution_ids) and set(
                statement.execution_ids
            ).issubset(known_executions)
        return False

    occurrence_counts = Counter(statement.text for statement in empirical)
    traceable = [
        statement
        for statement in empirical
        if is_traceable(statement)
        and bool(statement.text)
        and packet.manuscript_text.count(statement.text)
        == occurrence_counts[statement.text]
    ]
    untraceable = sorted(
        _statement_occurrence_id(statement)
        for statement in empirical
        if statement not in traceable
    )
    body_traceable, body_failures = _traceability_body_audit(packet, empirical)
    traceability_total = len(empirical) + 1
    traceability_valid = len(traceable) + int(body_traceable)
    metrics.append(
        _rate_metric(
            "statement_traceability",
            traceability_valid,
            traceability_total,
            target="100%",
            passed=(
                bool(empirical)
                and len(traceable) == len(empirical)
                and body_traceable
            ),
            evidence=[*untraceable, *body_failures],
        )
    )

    overreaches = [
        claim.claim_id
        for claim in eligible_claims
        if _is_unauthorized_causal(claim.text, claim.strength)
    ]
    overreaches.extend(_manuscript_causal_overreaches(packet))
    metrics.append(
        HardMetric(
            metric_id="causal_overreach_escape_count",
            numerator=len(overreaches),
            denominator=max(1, len(packet.claims)),
            value=float(len(overreaches)),
            target="0",
            passed=not overreaches,
            evidence=overreaches,
        )
    )

    reproduced = set(packet.reproduction.covered_check_ids)
    expected_reproduction = {
        execution.check_id
        for execution in packet.executions
        if execution.execution_status == "succeeded"
        and execution.estimates
        and execution.run_type != "replication"
    } | set(reference.independently_reproducible_check_ids)
    replication_ok = (
        packet.reproduction.mode == "independent_implementation"
        and packet.reproduction.status == "matched"
        and packet.reproduction.primary_implementation_id
        and packet.reproduction.replication_implementation_id
        and packet.reproduction.primary_implementation_id
        != packet.reproduction.replication_implementation_id
    )
    matched_reproduction = expected_reproduction & reproduced if replication_ok else set()
    missing_reproduction = sorted(expected_reproduction - matched_reproduction)
    metrics.append(
        _rate_metric(
            "independent_replication_rate",
            len(matched_reproduction),
            len(expected_reproduction),
            target="100%",
            passed=replication_ok and not missing_reproduction,
            evidence=missing_reproduction,
        )
    )

    return HardMetricReport(
        report_id=f"hard-{packet.packet_id}",
        case_id=packet.case_id,
        packet_id=packet.packet_id,
        metrics=metrics,
        all_hard_gates_passed=all(metric.passed for metric in metrics),
    )


def _percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_paired_reviews(
    case_id: str,
    packet_a_id: str,
    packet_b_id: str,
    reviews: list[NeurIPSReview],
) -> PairedReviewSummary:
    if len(reviews) != 5 or {item.sample_index for item in reviews} != {1, 2, 3, 4, 5}:
        raise ValueError("paired benchmark requires exactly five distinct review samples")
    dimensions = (
        "quality",
        "significance",
        "clarity",
        "soundness",
        "presentation",
        "contribution",
        "overall",
    )
    scores: dict[str, dict[str, list[int]]] = {
        "A": {dimension: [] for dimension in dimensions},
        "B": {dimension: [] for dimension in dimensions},
    }
    for review in reviews:
        ratings_for_packet_a = (
            review.ratings_a
            if review.system_assignment == "A_B"
            else review.ratings_b
        )
        ratings_for_packet_b = (
            review.ratings_b
            if review.system_assignment == "A_B"
            else review.ratings_a
        )
        for dimension in dimensions:
            scores["A"][dimension].append(
                int(getattr(ratings_for_packet_a, dimension))
            )
            scores["B"][dimension].append(
                int(getattr(ratings_for_packet_b, dimension))
            )
    medians: dict[str, dict[str, float]] = {"A": {}, "B": {}}
    iqrs: dict[str, dict[str, float]] = {"A": {}, "B": {}}
    for label in ("A", "B"):
        for dimension in dimensions:
            values = scores[label][dimension]
            medians[label][dimension] = float(median(values)) if values else 0.0
            iqrs[label][dimension] = (
                _percentile(values, 0.75) - _percentile(values, 0.25)
                if values
                else 0.0
            )
    preference_counts = {"A": 0, "B": 0, "tie": 0}
    for review in reviews:
        if review.preferred_label == "tie":
            preference_counts["tie"] += 1
        elif review.system_assignment == "A_B":
            preference_counts[review.preferred_label] += 1
        else:
            preference_counts[
                "B" if review.preferred_label == "A" else "A"
            ] += 1
    return PairedReviewSummary(
        case_id=case_id,
        packet_a_id=packet_a_id,
        packet_b_id=packet_b_id,
        reviews=reviews,
        median_scores=medians,
        interquartile_ranges=iqrs,
        preference_counts=preference_counts,
    )
