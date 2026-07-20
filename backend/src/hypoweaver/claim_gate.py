from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal, Mapping, Sequence

from .models import (
    AnalysisPlan,
    ClaimGateReport,
    ClaimGateResult,
    ClaimLedger,
    ClaimRecord,
    ClaimStrength,
    EvidenceObject,
    EvidenceRegistry,
    FormalResearchContract,
    Hypothesis,
    ReproductionAudit,
    ResearchPackage,
    ResearchRun,
    ScientificAudit,
)
from .seal import canonical_sha256
from .test_dag import (
    ENTERPRISE_PANEL_REGISTRY_VERSION,
    ENTERPRISE_PANEL_THREAT_BY_ID,
    POLICY_DID_REGISTRY_VERSION,
    THREAT_INDEPENDENT_REPLICATION,
    THREAT_MECHANISM_INTERACTION_BOUNDARY,
    THREAT_POLICY_INDEPENDENT_REPLICATION,
    required_checks_for_claim,
    schedule_test_dag,
    stable_claim_id,
)


class ClaimGateError(ValueError):
    pass


_STRENGTH_RANK: Mapping[ClaimStrength, float] = {
    "prohibited": 0,
    "insufficient": 1,
    "preliminary": 2,
    "mixed": 2.5,
    "associational": 3,
    "causal_cautious": 4,
    "causal_strong": 5,
}

_SAFE_CAUSAL_DISCLAIMERS = (
    re.compile(r"不支持(?:任何)?因果(?:解释|推断|结论|表述)?"),
    re.compile(r"不能(?:被)?解释为因果(?:关系|效应)?"),
    re.compile(r"不应(?:被)?(?:解释|解读|视为|表述)为因果(?:关系|效应|结论)?"),
    re.compile(
        r"不能(?:被)?(?:确认|认定|证明|识别)(?:为)?"
        r"[^。；！？!?;]*?因果(?:关系|效应|结论)?"
    ),
    re.compile(r"仅(?:能)?(?:表明|支持|解释为)?(?:稳健的|初步的)?关联(?:性|关系|证据)?"),
    re.compile(
        r"(?:与|和)[^。；！？!?;]*?"
        r"(?:提高|提升|降低|改善|增加|减少)"
        r"[^。；！？!?;]*?存在(?:统计(?:上)?)?关联"
    ),
    re.compile(
        r"(?:未发现|没有发现|尚无证据表明)[^。；！？!?;]*?"
        r"(?:影响|导致|促进|抑制|造成|引发|驱动|提高|提升|降低|改善|"
        r"增加|减少|有助于|促使|推动|加剧|削弱|缓解|改变|带来)"
        r"[^。；！？!?;]*?(?:的)?证据"
    ),
    re.compile(
        r"(?:does not|do not|cannot|can not) support (?:a |an )?causal "
        r"(?:interpretation|claim|conclusion)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\brather than\s+(?:(?:a|an|the)\s+)?causal effects?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cannot|can\s+not|can't|could\s+not|couldn't)\s+be\s+"
        r"(?:confirmed|established|identified|interpreted)\s+as\s+"
        r"(?:(?:a|an|the)\s+)?causal effects?\b",
        re.IGNORECASE,
    ),
)

_CAUSAL_ASSERTION = re.compile(
    r"影响(?:了|着)?|导致|促进|抑制|造成|引发|驱动|"
    r"使得|促使|使(?!用)|有助于|推动|加剧|削弱|缓解|改变|带来|"
    r"因果效应|处理效应|"
    r"(?:提高|提升|降低|改善|增加|减少)(?:了|着)?|"
    r"\b(?:causes?|caused|impacts?|impacted|leads? to|led to|promotes?|"
    r"inhibits?|drives?|causal effects?)\b",
    re.IGNORECASE,
)

_CLAUSE_START = r"(?:\A\s*|(?:[.!?;]|\n)\s*)"
_DISCOURSE_MARKER = (
    r"(?:(?:however|moreover|therefore|thus|accordingly),?\s+)?"
)
_EN_POLICY_DIRECTIONAL_ASSERTION = re.compile(
    _CLAUSE_START
    + _DISCOURSE_MARKER
    + r"(?P<assertion>"
    r"(?:(?:the|a|an)\s+)?"
    r"(?:policy|treatment|intervention|program|reform)\s+"
    r"(?:"
    r"(?:(?:(?:do|does|did)\s+not|don't|doesn't|didn't)\s+"
    r"(?:(?:statistically\s+)?"
    r"(?:significantly|materially|substantially)\s+)?"
    r"(?:reduce|lower|raise))"
    r"|"
    r"(?:(?:statistically\s+)?"
    r"(?:significantly|materially|substantially|directly)\s+)?"
    r"(?:reduce[sd]?|lower(?:s|ed)?|raise[sd]?)"
    r")\b(?!-|\s+form\b)"
    r")",
    re.IGNORECASE,
)
_EN_CAUSAL_INTERPRETATION_ASSERTION = re.compile(
    _CLAUSE_START
    + _DISCOURSE_MARKER
    + r"(?P<assertion>"
    r"(?:this|"
    r"(?:this|these|the)\s+"
    r"(?:evidence|results?|findings?|estimates?|analysis|study|design)"
    r")\s+"
    r"(?:(?:strongly|clearly|directly|robustly)\s+)?"
    r"support(?:s|ed)?\s+"
    r"(?:(?:a|the)\s+)?causal interpretation\b"
    r")",
    re.IGNORECASE,
)

_PRELIMINARY_CALIBRATION = re.compile(
    r"初步|有限|尚待|尚需|未完成|不完整|暂不能|证据不足"
)
_MIXED_CALIBRATION = re.compile(
    r"混合|不一致|反向|证伪|未稳健|部分支持|证据有限"
)
_UNPROTECTED_CLAIM_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:[eE][-+]?\d+)?%?(?![A-Za-z0-9_])"
)


def causal_wording_violations(
    text: str,
    max_allowed_strength: ClaimStrength,
) -> list[str]:
    """Return unauthorized empirical causal predicates in ``text``.

    Explicit non-causal disclaimers are removed before matching. A zero-effect
    assertion such as ``不导致`` is intentionally *not* a disclaimer: it is still
    a causal empirical claim and therefore remains detectable.
    """

    if max_allowed_strength in {"causal_strong", "causal_cautious"}:
        return []
    remaining = text
    for pattern in _SAFE_CAUSAL_DISCLAIMERS:
        remaining = pattern.sub(" ", remaining)
    violations = [
        match.group(0) for match in _CAUSAL_ASSERTION.finditer(remaining)
    ]
    for pattern in (
        _EN_POLICY_DIRECTIONAL_ASSERTION,
        _EN_CAUSAL_INTERPRETATION_ASSERTION,
    ):
        violations.extend(
            match.group("assertion") for match in pattern.finditer(remaining)
        )
    return list(dict.fromkeys(violations))


def permitted_h3_decisions(claim: ClaimRecord) -> tuple[str, ...]:
    if claim.admission_status == "admitted" and claim.allowed_strength not in {
        "insufficient",
        "prohibited",
    }:
        return ("approve", "downgrade", "reject", "hold")
    if claim.admission_status == "downgrade_required" and claim.allowed_strength not in {
        "insufficient",
        "prohibited",
    }:
        return ("downgrade", "reject", "hold")
    return ("reject", "hold")


def validate_h3_claim_decision(
    claim: ClaimRecord,
    decision: Literal["approve", "downgrade", "reject", "hold"],
    final_text: str | None = None,
) -> None:
    allowed = permitted_h3_decisions(claim)
    if decision not in allowed:
        raise ClaimGateError(
            f"H3 decision {decision!r} is not allowed for {claim.claim_id}; "
            f"allowed={','.join(allowed)}"
        )
    if decision in {"reject", "hold"}:
        return
    text = (final_text or claim.final_text or claim.claim_text).strip()
    if not text:
        raise ClaimGateError(f"H3 decision for {claim.claim_id} requires final text")
    effective_cap = _tighter_strength(
        claim.allowed_strength,
        claim.max_allowed_strength or claim.allowed_strength,
    )
    violations = causal_wording_violations(text, effective_cap)
    if violations:
        raise ClaimGateError(
            f"unauthorized causal wording for {claim.claim_id}: "
            + ", ".join(violations)
        )
    if _UNPROTECTED_CLAIM_NUMBER.search(text):
        raise ClaimGateError(
            f"Claim {claim.claim_id} contains an unprotected numeric value; "
            "statistical numbers must come from Execution-backed Manuscript statements"
        )
    if effective_cap == "preliminary" and not _PRELIMINARY_CALIBRATION.search(text):
        raise ClaimGateError(
            f"preliminary Claim {claim.claim_id} requires explicit calibration"
        )
    if effective_cap == "mixed" and not _MIXED_CALIBRATION.search(text):
        raise ClaimGateError(
            f"mixed Claim {claim.claim_id} must disclose conflicting evidence"
        )


def apply_claim_gate(
    candidate_ledger: ClaimLedger,
    plan: AnalysisPlan,
    research_run: ResearchRun,
    evidence_registry: EvidenceRegistry,
    hypotheses: Sequence[Hypothesis],
    *,
    contract: FormalResearchContract,
    reproduction_audit: ReproductionAudit | None,
    scientific_audit: ScientificAudit | None,
    research_package: ResearchPackage | None = None,
) -> tuple[ClaimLedger, ClaimGateReport]:
    """Deterministically compile an LLM candidate ledger into admissible claims.

    The function performs no I/O, uses no model and uses no random identifiers.
    All output identifiers are derived from canonical input hashes.
    """

    expected = {stable_claim_id(item.hypothesis_id): item for item in hypotheses}
    expected_ids = set(expected)
    scheduled = schedule_test_dag(plan)
    step_by_id = {item.step.step_id: item.step for item in scheduled}
    known_check_ids = set(step_by_id)
    execution_by_id = {item.execution_id: item for item in research_run.executions}
    replication_run_id = reproduction_audit.replication_run_id if reproduction_audit else None
    known_run_ids = {research_run.research_run_id, *execution_by_id}
    if replication_run_id:
        known_run_ids.add(replication_run_id)

    binding_notes: list[str] = []
    if candidate_ledger.case_id != research_run.case_id:
        binding_notes.append(
            "Candidate ClaimLedger case_id was non-authoritative and code-rebound "
            f"from {candidate_ledger.case_id!r} to {research_run.case_id!r}."
        )

    systemic_reasons = _systemic_gate_reasons(
        candidate_ledger,
        plan,
        research_run,
        evidence_registry,
        expected_ids,
        known_check_ids,
        execution_by_id,
        contract,
        reproduction_audit,
        scientific_audit,
        research_package,
    )

    candidates_by_id: dict[str, list[ClaimRecord]] = defaultdict(list)
    for claim in candidate_ledger.claims:
        candidates_by_id[claim.claim_id].append(claim)

    evidence_by_claim_check: dict[str, dict[str, list[EvidenceObject]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for evidence in evidence_registry.evidence:
        evidence_by_claim_check[evidence.claim_id][evidence.check_id].append(evidence)

    gated_claims: list[ClaimRecord] = []
    results: list[ClaimGateResult] = []
    excluded = list(candidate_ledger.excluded_findings)

    for claim_id, hypothesis in expected.items():
        candidates = candidates_by_id.get(claim_id, [])
        if len(candidates) != 1:
            reason = (
                "Candidate Claim is missing."
                if not candidates
                else "Candidate Claim id is duplicated."
            )
            claim = _rejected_placeholder(claim_id, hypothesis, reason)
            gated_claims.append(claim)
            results.append(
                ClaimGateResult(
                    claim_id=claim_id,
                    admission_status="rejected",
                    max_allowed_strength="prohibited",
                    reasons=[reason],
                )
            )
            continue

        candidate = candidates[0]
        claim_reasons: list[str] = []
        if candidate.hypothesis_id not in {None, hypothesis.hypothesis_id}:
            claim_reasons.append("Candidate Claim references the wrong hypothesis_id.")
        unknown_runs = set(candidate.supporting_runs + candidate.opposing_runs) - known_run_ids
        if unknown_runs:
            claim_reasons.append(
                "Candidate Claim references unknown Execution ids: "
                + ", ".join(sorted(unknown_runs))
            )
        unknown_candidate_checks = (
            set(candidate.required_check_ids) - known_check_ids
        )
        if unknown_candidate_checks:
            claim_reasons.append(
                "Candidate Claim references unknown Check ids: "
                + ", ".join(sorted(unknown_candidate_checks))
            )
        expected_type = (
            "mechanism"
            if any(
                item.step.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
                and claim_id in item.step.target_claim_ids
                for item in scheduled
            )
            else "causal"
            if plan.method_family == "policy_causal"
            else "associational"
        )
        required_check_ids = required_checks_for_claim(
            plan,
            candidate.model_copy(update={"claim_type": expected_type}),
        )

        if systemic_reasons:
            admission_status = "prohibited"
            cap: ClaimStrength = "prohibited"
            claim_reasons.extend(systemic_reasons)
            code_evidence_status = "not_tested"
        elif claim_reasons:
            admission_status = "rejected"
            cap = "prohibited"
            code_evidence_status = "not_tested"
        else:
            statuses: list[str] = []
            for check_id in required_check_ids:
                items = evidence_by_claim_check[claim_id].get(check_id, [])
                statuses.extend(item.status for item in items)
                if not items:
                    claim_reasons.append(f"Required check {check_id} has no evidence.")
                elif any(item.status == "invalid" for item in items):
                    claim_reasons.extend(
                        item.reason for item in items if item.status == "invalid"
                    )
                elif any(item.status == "opposing" for item in items):
                    claim_reasons.extend(
                        item.reason for item in items if item.status == "opposing"
                    )
                elif any(item.status == "incomplete" for item in items):
                    claim_reasons.extend(
                        item.reason for item in items if item.status == "incomplete"
                    )

            if "invalid" in statuses:
                admission_status = "prohibited"
                cap = "prohibited"
                code_evidence_status = "not_tested"
            elif "opposing" in statuses:
                admission_status = "downgrade_required"
                cap = "mixed"
                code_evidence_status = "mixed"
            elif "incomplete" in statuses or any(
                not evidence_by_claim_check[claim_id].get(check_id)
                for check_id in required_check_ids
            ):
                admission_status = "downgrade_required"
                cap = "preliminary"
                code_evidence_status = "inconclusive"
            else:
                admission_status = "admitted"
                cap = (
                    "causal_cautious"
                    if plan.method_family == "policy_causal"
                    else "associational"
                )
                code_evidence_status = "supported"

        wording = causal_wording_violations(candidate.claim_text, cap)
        if candidate.final_text:
            wording.extend(causal_wording_violations(candidate.final_text, cap))
        if wording:
            if admission_status != "prohibited":
                admission_status = "rejected"
            cap = "prohibited"
            code_evidence_status = "not_tested"
            claim_reasons.append(
                "Candidate Claim contains unauthorized causal wording: "
                + ", ".join(dict.fromkeys(wording))
            )

        supporting_runs = _evidence_run_ids(
            evidence_by_claim_check[claim_id], "supporting"
        )
        opposing_runs = _evidence_run_ids(
            evidence_by_claim_check[claim_id], "opposing"
        )
        final_evidence_status = _tightened_evidence_status(
            code_evidence_status,
            candidate.evidence_status,
        )
        if candidate.evidence_status != code_evidence_status:
            claim_reasons.append(
                "Model-authored evidence_status disagreed with the code-owned "
                "Evidence Registry and was retained only as an advisory assessment."
            )
        actual_strength = _tighter_strength(candidate.allowed_strength, cap)
        gated_claim = candidate.model_copy(
            update={
                "hypothesis_id": hypothesis.hypothesis_id,
                "claim_type": expected_type,
                "required_check_ids": required_check_ids,
                "admission_status": admission_status,
                "max_allowed_strength": cap,
                "allowed_strength": actual_strength,
                "gate_reasons": list(dict.fromkeys(claim_reasons)),
                "evidence_status": final_evidence_status,
                "supporting_runs": supporting_runs,
                "opposing_runs": opposing_runs,
                "approval_status": "pending",
                "final_text": None,
            }
        )
        gated_claims.append(gated_claim)
        results.append(
            ClaimGateResult(
                claim_id=claim_id,
                admission_status=admission_status,
                max_allowed_strength=cap,
                reasons=list(dict.fromkeys(claim_reasons)),
            )
        )

    for unknown_claim in candidate_ledger.claims:
        if unknown_claim.claim_id in expected_ids:
            continue
        reason = "Candidate Claim id is not one of the code-frozen stable Claim ids."
        excluded.append(unknown_claim.claim_text)
        results.append(
            ClaimGateResult(
                claim_id=unknown_claim.claim_id,
                admission_status="rejected",
                max_allowed_strength="prohibited",
                reasons=[reason],
            )
        )

    gate_payload = {
        "candidate_ledger": candidate_ledger.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "research_run": research_run.model_dump(mode="json"),
        "evidence_registry": evidence_registry.model_dump(mode="json"),
        "reproduction_audit": (
            reproduction_audit.model_dump(mode="json")
            if reproduction_audit is not None
            else None
        ),
        "scientific_audit": (
            scientific_audit.model_dump(mode="json")
            if scientific_audit is not None
            else None
        ),
    }
    gate_id = f"claim-gate-{canonical_sha256(gate_payload)[:20]}"
    report = ClaimGateReport(
        gate_id=gate_id,
        case_id=research_run.case_id,
        research_run_id=research_run.research_run_id,
        registry_version=evidence_registry.registry_version,
        results=results,
    )
    gated_ledger = candidate_ledger.model_copy(
        update={
            "ledger_id": f"gated-{candidate_ledger.ledger_id}",
            "case_id": research_run.case_id,
            "research_run_id": research_run.research_run_id,
            "claims": gated_claims,
            "excluded_findings": list(dict.fromkeys(excluded)),
            "unresolved_issues": list(
                dict.fromkeys(
                    [
                        *candidate_ledger.unresolved_issues,
                        *binding_notes,
                        *systemic_reasons,
                        *[
                            reason
                            for claim in gated_claims
                            for reason in claim.gate_reasons
                        ],
                    ]
                )
            ),
        }
    )
    return gated_ledger, report


def code_owned_claims_for_registry(
    candidate_ledger: ClaimLedger,
    plan: AnalysisPlan,
    hypotheses: Sequence[Hypothesis],
) -> list[ClaimRecord]:
    """Return one code-owned, stable Claim shell per frozen hypothesis.

    This is used only to scope Evidence Registry construction. Duplicate,
    missing and invented candidate ids remain visible to ``apply_claim_gate``
    and are rejected there.
    """

    shells = {
        item.claim_id: item
        for item in code_owned_claim_shells(plan, hypotheses)
    }
    first_by_id: dict[str, ClaimRecord] = {}
    for candidate in candidate_ledger.claims:
        first_by_id.setdefault(candidate.claim_id, candidate)
    claims: list[ClaimRecord] = []
    for hypothesis in hypotheses:
        claim_id = stable_claim_id(hypothesis.hypothesis_id)
        candidate = first_by_id.get(claim_id)
        claim_type = shells[claim_id].claim_type
        if candidate is None:
            claims.append(shells[claim_id])
        else:
            claims.append(
                candidate.model_copy(
                    update={
                        "claim_id": claim_id,
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "claim_type": claim_type,
                    }
                )
            )
    return claims


def code_owned_claim_shells(
    plan: AnalysisPlan,
    hypotheses: Sequence[Hypothesis],
) -> list[ClaimRecord]:
    """Create stable Claim scopes before any model-authored assessment exists."""

    scheduled = schedule_test_dag(plan)
    claims: list[ClaimRecord] = []
    for hypothesis in hypotheses:
        claim_id = stable_claim_id(hypothesis.hypothesis_id)
        claim_type = (
            "mechanism"
            if any(
                item.step.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
                and claim_id in item.step.target_claim_ids
                for item in scheduled
            )
            else "causal"
            if plan.method_family == "policy_causal"
            else "associational"
        )
        claims.append(
            _rejected_placeholder(
                claim_id,
                hypothesis,
                "Awaiting code-owned Claim Gate compilation.",
            ).model_copy(update={"claim_type": claim_type})
        )
    return claims


def _systemic_gate_reasons(
    candidate_ledger: ClaimLedger,
    plan: AnalysisPlan,
    research_run: ResearchRun,
    registry: EvidenceRegistry,
    expected_claim_ids: set[str],
    known_check_ids: set[str],
    execution_by_id: Mapping[str, object],
    contract: FormalResearchContract,
    reproduction_audit: ReproductionAudit | None,
    scientific_audit: ScientificAudit | None,
    research_package: ResearchPackage | None,
) -> list[str]:
    reasons: list[str] = []
    allowed_registry = (
        POLICY_DID_REGISTRY_VERSION
        if plan.method_family == "policy_causal"
        else ENTERPRISE_PANEL_REGISTRY_VERSION
    )
    if plan.method_family not in {
        "policy_causal",
        "panel_association",
        "mechanism_boundary",
    }:
        reasons.append("Claim Gate does not support the frozen method family.")
    if plan.check_registry_version != allowed_registry:
        reasons.append(f"AnalysisPlan is not bound to {allowed_registry}.")
    if research_run.fixture_only or research_run.execution_status == "fixture_only":
        reasons.append("Fixture output is prohibited from supporting empirical Claims.")
    if research_run.execution_status in {"failed", "cancelled", "not_executed"}:
        reasons.append("ResearchRun did not complete an empirical execution.")
    if candidate_ledger.research_run_id != research_run.research_run_id:
        reasons.append("Candidate ClaimLedger research_run_id does not match ResearchRun.")
    if registry.registry_version != allowed_registry:
        reasons.append("EvidenceRegistry version is unknown.")
    if registry.case_id != research_run.case_id:
        reasons.append("EvidenceRegistry case_id does not match ResearchRun.")
    if registry.research_run_id != research_run.research_run_id:
        reasons.append("EvidenceRegistry research_run_id does not match ResearchRun.")

    if contract.case_id != research_run.case_id:
        reasons.append("FormalResearchContract case_id does not match ResearchRun.")
    if research_run.contract_hash != contract.approved_plan_hash:
        reasons.append("ResearchRun contract hash does not match the frozen plan hash.")
    if contract.approved_plan_hash != canonical_sha256(
        contract.approved_plan.model_dump(mode="json")
    ):
        reasons.append("FormalResearchContract approved plan hash is invalid.")
    if contract.approved_plan.model_dump(mode="json") != plan.model_dump(mode="json"):
        reasons.append("Claim Gate plan differs from the plan embedded in the contract.")
    if research_run.plan_version != contract.approved_plan.plan_version:
        reasons.append("ResearchRun plan_version does not match the contract.")
    if [item.sha256 for item in contract.dataset_refs] != contract.data_hashes:
        reasons.append("FormalResearchContract data hashes do not match dataset_refs.")
    if research_package is not None:
        if canonical_sha256(research_package.model_dump(mode="json")) != contract.research_package_hash:
            reasons.append("ResearchPackage hash does not match the contract.")
        if research_package.case_id != contract.case_id:
            reasons.append("ResearchPackage case_id does not match the contract.")

    execution_ids = [item.execution_id for item in research_run.executions]
    if len(execution_ids) != len(set(execution_ids)):
        reasons.append("ResearchRun execution_id values are not unique.")
    plan_step_ids = [item.plan_step_id for item in research_run.executions]
    unknown_plan_steps = set(plan_step_ids) - known_check_ids
    if unknown_plan_steps:
        reasons.append(
            "ResearchRun references unknown frozen steps: "
            + ", ".join(sorted(unknown_plan_steps))
        )
    for execution in research_run.executions:
        if execution.check_id and execution.check_id != execution.plan_step_id:
            reasons.append(
                f"Execution {execution.execution_id} has a mismatched check_id."
            )
        if execution.execution_status != "succeeded":
            continue
        provenance = execution.provenance
        if provenance is None:
            reasons.append(
                f"Succeeded Execution {execution.execution_id} has no provenance."
            )
            continue
        if not all(
            (
                provenance.implementation_id,
                provenance.implementation_version,
                provenance.code_sha256,
                provenance.environment_sha256,
            )
        ):
            reasons.append(
                f"Succeeded Execution {execution.execution_id} has incomplete provenance."
            )
        if provenance.contract_sha256 != canonical_sha256(contract.model_dump(mode="json")):
            reasons.append(
                f"Execution {execution.execution_id} contract provenance hash is invalid."
            )
        if provenance.data_sha256 != contract.data_hashes:
            reasons.append(
                f"Execution {execution.execution_id} data provenance hashes are invalid."
            )

    evidence_ids = [item.evidence_id for item in registry.evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        reasons.append("EvidenceRegistry evidence_id values are not unique.")
    for evidence in registry.evidence:
        if evidence.claim_id not in expected_claim_ids:
            reasons.append(
                f"Evidence {evidence.evidence_id} references an unknown Claim."
            )
        if evidence.check_id not in known_check_ids:
            reasons.append(
                f"Evidence {evidence.evidence_id} references an unknown Check."
            )
        if evidence.source_kind == "execution":
            execution = execution_by_id.get(evidence.execution_id or "")
            if execution is None:
                reasons.append(
                    f"Evidence {evidence.evidence_id} references an unknown Execution."
                )
            elif (
                evidence.check_id != getattr(execution, "plan_step_id", None)
                and evidence.check_id != getattr(execution, "check_id", None)
            ):
                reasons.append(
                    f"Evidence {evidence.evidence_id} does not match its Execution Check."
                )
        if evidence.source_kind == "reproduction" and (
            reproduction_audit is None
            or evidence.execution_id != reproduction_audit.replication_run_id
        ):
            reasons.append(
                f"Evidence {evidence.evidence_id} has an invalid reproduction reference."
            )

    if scientific_audit is None:
        reasons.append("Scientific audit is missing.")

    replication_steps = {
        item.step.step_id
        for item in schedule_test_dag(plan)
        if item.step.threat_id
        in {THREAT_INDEPENDENT_REPLICATION, THREAT_POLICY_INDEPENDENT_REPLICATION}
    }
    estimated_steps = {
        item.plan_step_id
        for item in research_run.executions
        if item.execution_status == "succeeded" and item.estimates
    }
    if reproduction_audit is None:
        reasons.append("Independent reproduction is missing.")
    else:
        if reproduction_audit.primary_run_id != research_run.research_run_id:
            reasons.append("Independent reproduction references the wrong primary run.")
        if not reproduction_audit.replication_run_id:
            reasons.append("Independent reproduction has no replication run id.")
        if reproduction_audit.mode != "independent_implementation":
            reasons.append("Reproduction is a same-implementation rerun, not independent.")
        if reproduction_audit.status != "matched":
            reasons.append(
                f"Independent reproduction status is {reproduction_audit.status}."
            )
        if (
            not reproduction_audit.primary_implementation_id
            or not reproduction_audit.replication_implementation_id
            or reproduction_audit.primary_implementation_id
            == reproduction_audit.replication_implementation_id
        ):
            reasons.append("Independent reproduction implementation ids are missing or equal.")
        primary_implementation_ids = {
            item.provenance.implementation_id
            for item in research_run.executions
            if item.execution_status == "succeeded" and item.provenance is not None
        }
        if (
            reproduction_audit.primary_implementation_id
            and reproduction_audit.primary_implementation_id
            not in primary_implementation_ids
        ):
            reasons.append(
                "Reproduction primary implementation id does not match Execution provenance."
            )
        missing_coverage = estimated_steps - set(
            reproduction_audit.covered_plan_step_ids
        )
        if missing_coverage:
            reasons.append(
                "Independent reproduction does not cover estimated steps: "
                + ", ".join(sorted(missing_coverage))
            )
        if not replication_steps:
            reasons.append("AnalysisPlan has no independent reproduction check.")

    return list(dict.fromkeys(reasons))


def _tighter_strength(
    first: ClaimStrength,
    second: ClaimStrength,
) -> ClaimStrength:
    return first if _STRENGTH_RANK[first] <= _STRENGTH_RANK[second] else second


def _evidence_run_ids(
    by_check: Mapping[str, Sequence[EvidenceObject]],
    status: Literal["supporting", "opposing"],
) -> list[str]:
    return list(
        dict.fromkeys(
            item.execution_id
            for items in by_check.values()
            for item in items
            if item.status == status and item.execution_id is not None
        )
    )


def _tightened_evidence_status(
    code_status: str,
    candidate_status: str,
) -> str:
    """Return the registry status; model-authored status is advisory only."""

    _ = candidate_status
    return code_status


def _rejected_placeholder(
    claim_id: str,
    hypothesis: Hypothesis,
    reason: str,
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        hypothesis_id=hypothesis.hypothesis_id,
        claim_text=hypothesis.statement,
        evidence_status="not_tested",
        allowed_strength="prohibited",
        supporting_runs=[],
        opposing_runs=[],
        scope="冻结合同定义的样本、时期与变量口径",
        robustness_status="rejected_by_claim_gate",
        unresolved_risks=[reason],
        claim_type="unspecified",
        admission_status="rejected",
        max_allowed_strength="prohibited",
        gate_reasons=[reason],
    )
