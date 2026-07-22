from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from .models import (
    AnalysisPlan,
    ClaimRecord,
    CriticIssue,
    EvidenceObject,
    EvidenceRegistry,
    ExecutionRecord,
    Hypothesis,
    ModelSpec,
    PlannedStep,
    ReproductionAudit,
    ResearchRun,
    ScientificAudit,
    TestRole,
)
from .policy_causal import parse_policy_design


ENTERPRISE_PANEL_REGISTRY_VERSION = "enterprise-panel-v1"
POLICY_DID_REGISTRY_VERSION = "policy-did-v2"

THREAT_KEY_SAMPLE_FLOW = "panel.key_sample_flow"
THREAT_MISSINGNESS_WITHIN_VARIANCE = "panel.missingness_within_variance"
THREAT_FE_CLUSTER_FEASIBILITY = "panel.fe_cluster_feasibility"
THREAT_ALTERNATIVE_MEASUREMENT = "panel.alternative_measurement"
THREAT_LEAD_PLACEBO = "panel.lead_placebo"
THREAT_SAMPLE_OUTLIER_SENSITIVITY = "panel.sample_outlier_sensitivity"
THREAT_MECHANISM_INTERACTION_BOUNDARY = "panel.mechanism_interaction_boundary"
THREAT_INDEPENDENT_REPLICATION = "panel.independent_replication"
THREAT_POLICY_SUPPORT = "policy.group_time_support"
THREAT_POLICY_EVENT_STUDY = "policy.event_study_pretrends"
THREAT_POLICY_PLACEBO = "policy.placebo_timing"
THREAT_POLICY_GROUP_FIXED_PRE = "policy.group_fixed_last_pre"
THREAT_POLICY_GROUP_STABLE_ONLY = "policy.group_stable_entities_only"
THREAT_POLICY_ENTITY_CLUSTER = "policy.entity_cluster_sensitivity"
THREAT_POLICY_PERMUTATION_PLACEBO = "policy.permutation_placebo"
THREAT_POLICY_ALTERNATIVE_OUTCOME = "policy.alternative_outcome"
THREAT_POLICY_INDEPENDENT_REPLICATION = "policy.independent_replication"

RunType = Literal[
    "baseline",
    "diagnostic",
    "robustness",
    "falsification",
    "mechanism",
    "heterogeneity",
    "replication",
]
PlanSection = Literal[
    "diagnostics",
    "robustness_tests",
    "falsification_tests",
    "mechanism_tests",
]
NotExecutedReasonCode = Literal[
    "budget_exhausted",
    "not_executable",
    "dependency_failed",
    "external_replication_pending",
    "fixture_only",
]


class TestDagError(ValueError):
    pass


def validate_policy_did_execution_plan(plan: AnalysisPlan) -> ModelSpec:
    """Reject an invalid policy-did-v2 plan before any data are read."""

    if plan.method_family != "policy_causal":
        raise TestDagError("policy-did-v2 only supports policy_causal plans")
    if len(plan.baseline_models) != 1:
        raise TestDagError("policy-did-v2 requires exactly one baseline model")
    if plan.design_only:
        raise TestDagError("policy-did-v2 requires design_only=false")
    if plan.check_registry_version != POLICY_DID_REGISTRY_VERSION:
        raise TestDagError(
            "policy-did-v2 requires check_registry_version=policy-did-v2"
        )
    baseline = plan.baseline_models[0]
    parse_policy_design(baseline)
    return baseline


@dataclass(frozen=True)
class EnterprisePanelThreat:
    threat_id: str
    name: str
    test_role: TestRole
    plan_section: PlanSection
    rationale: str
    required_by_default: bool = True


@dataclass(frozen=True)
class ScheduledTest:
    run_type: RunType
    step: PlannedStep

    @property
    def required(self) -> bool:
        return self.step.required_for_admission or self.run_type == "baseline"


@dataclass(frozen=True)
class BudgetedSchedule:
    selected: tuple[ScheduledTest, ...]
    omitted: tuple[ScheduledTest, ...]


ENTERPRISE_PANEL_THREATS: tuple[EnterprisePanelThreat, ...] = (
    EnterprisePanelThreat(
        threat_id=THREAT_KEY_SAMPLE_FLOW,
        name="主键、重复项、单例与样本流诊断",
        test_role="diagnostic",
        plan_section="diagnostics",
        rationale="确认面板主键唯一性并保留单例与样本流变化。",
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_MISSINGNESS_WITHIN_VARIANCE,
        name="缺失与组内变异诊断",
        test_role="diagnostic",
        plan_section="diagnostics",
        rationale="确认核心变量有可用于固定效应识别的组内变异。",
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_FE_CLUSTER_FEASIBILITY,
        name="固定效应、聚类与有限样本修正诊断",
        test_role="diagnostic",
        plan_section="diagnostics",
        rationale="核验固定效应、聚类层级与有限样本修正可执行。",
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_ALTERNATIVE_MEASUREMENT,
        name="替代结果或解释变量稳健性检验",
        test_role="robustness",
        plan_section="robustness_tests",
        rationale="使用冻结的替代测量检验结果是否依赖单一口径。",
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_LEAD_PLACEBO,
        name="前导或安慰剂证伪检验",
        test_role="falsification",
        plan_section="falsification_tests",
        rationale="检验未来信息或安慰剂结果是否错误地产生显著关系。",
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_SAMPLE_OUTLIER_SENSITIVITY,
        name="样本与异常值敏感性检验",
        test_role="robustness",
        plan_section="robustness_tests",
        rationale="使用预注册样本规则检验结果是否由异常值或样本选择驱动。",
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_MECHANISM_INTERACTION_BOUNDARY,
        name="机制、交互与边界检验",
        test_role="robustness",
        plan_section="mechanism_tests",
        rationale="仅对机制主张核验冻结的机制变量、交互项和边界条件。",
        required_by_default=False,
    ),
    EnterprisePanelThreat(
        threat_id=THREAT_INDEPENDENT_REPLICATION,
        name="独立统计实现复算",
        test_role="replication",
        plan_section="robustness_tests",
        rationale="使用不同实现复算所有估计型步骤并比较样本流、系数与标准误。",
    ),
)

ENTERPRISE_PANEL_THREAT_BY_ID = {
    item.threat_id: item for item in ENTERPRISE_PANEL_THREATS
}

_SECTION_RUN_TYPE: dict[PlanSection, RunType] = {
    "diagnostics": "diagnostic",
    "robustness_tests": "robustness",
    "falsification_tests": "falsification",
    "mechanism_tests": "mechanism",
}
_TERMINAL_EXECUTION_STATUSES = {
    "succeeded",
    "failed",
    "cancelled",
    "not_executed",
    "fixture_only",
}
_SIMPLE_SAMPLE_FILTER_RE = re.compile(
    r"^\s*[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*\s*"
    r"(?:==|!=|>=|<=|>|<)\s*(?:[-+]?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")\s*$"
)


def stable_claim_id(hypothesis_id: str) -> str:
    value = hypothesis_id.strip()
    if not value:
        raise TestDagError("hypothesis_id cannot be empty")
    return f"claim-{value}"


def stable_claim_ids(
    hypotheses: Sequence[Hypothesis | str],
) -> list[str]:
    hypothesis_ids = [
        item.hypothesis_id if isinstance(item, Hypothesis) else item
        for item in hypotheses
    ]
    claim_ids = [stable_claim_id(item) for item in hypothesis_ids]
    if len(claim_ids) != len(set(claim_ids)):
        raise TestDagError("hypothesis ids must map to unique stable claim ids")
    return claim_ids


def compile_enterprise_panel_test_dag(
    plan: AnalysisPlan,
    hypotheses: Sequence[Hypothesis | str],
    reviewer_issues: Sequence[CriticIssue] = (),
    *,
    mechanism_hypothesis_ids: Sequence[str] | None = None,
) -> AnalysisPlan:
    """Bind an enterprise-panel plan to the frozen threat registry.

    The compiler uses only structured fields. It never parses reviewer prose to
    guess a threat, variable, parameter, or executable repair.
    """

    if plan.method_family not in {"panel_association", "mechanism_boundary"}:
        raise TestDagError(
            "enterprise-panel-v1 only supports panel_association and "
            "mechanism_boundary plans"
        )

    hypothesis_ids = [
        item.hypothesis_id if isinstance(item, Hypothesis) else item
        for item in hypotheses
    ]
    claim_ids = stable_claim_ids(hypotheses)
    hypothesis_to_claim = dict(zip(hypothesis_ids, claim_ids, strict=True))
    if mechanism_hypothesis_ids is None:
        mechanism_hypothesis_ids = tuple(
            item.hypothesis_id
            for item in hypotheses
            if isinstance(item, Hypothesis) and bool(item.mechanism)
        )
    mechanism_claim_ids = {
        hypothesis_to_claim[item]
        for item in mechanism_hypothesis_ids
        if item in hypothesis_to_claim
    }
    unknown_mechanism_ids = set(mechanism_hypothesis_ids) - set(hypothesis_to_claim)
    if unknown_mechanism_ids:
        raise TestDagError(
            "unknown mechanism hypothesis ids: "
            + ", ".join(sorted(unknown_mechanism_ids))
        )

    compiled = plan.model_copy(deep=True)
    _validate_unique_step_ids(compiled)

    compiled.baseline_models = [
        model.model_copy(
            update={
                "target_claim_ids": (
                    _canonical_targets(
                        model.target_claim_ids,
                        claim_ids,
                        hypothesis_to_claim,
                    )
                    or list(claim_ids)
                ),
                "required_for_admission": True,
                "priority": "required",
            }
        )
        for model in compiled.baseline_models
    ]
    baseline_exposures = (
        list(compiled.baseline_models[0].treatments_or_exposures)
        if len(compiled.baseline_models) == 1
        else []
    )

    for section in _SECTION_RUN_TYPE:
        normalized: list[PlannedStep] = []
        for step in getattr(compiled, section):
            threat_id = step.threat_id or _infer_structured_threat_id(section, step)
            if threat_id is None:
                normalized.append(
                    _normalize_unregistered_step(
                        section,
                        step,
                        claim_ids,
                        hypothesis_to_claim,
                    )
                )
                continue
            threat = ENTERPRISE_PANEL_THREAT_BY_ID.get(threat_id)
            if threat is None:
                normalized.append(
                    _unknown_threat_placeholder(
                        threat_id=threat_id,
                        issue_id=None,
                        section=section,
                        claim_ids=claim_ids,
                        original=step,
                    )
                )
                continue
            registered_step = _normalize_registered_step(
                    step,
                    threat,
                    claim_ids,
                    hypothesis_to_claim,
                    mechanism_claim_ids,
                )
            if (
                threat.threat_id == THREAT_ALTERNATIVE_MEASUREMENT
                and registered_step.parameters.get("alternative_exposure")
                and not registered_step.parameters.get("replaces_exposure")
            ):
                if len(baseline_exposures) == 1:
                    registered_step = registered_step.model_copy(
                        update={
                            "parameters": {
                                **registered_step.parameters,
                                "replaces_exposure": baseline_exposures[0],
                            }
                        }
                    )
                else:
                    registered_step = registered_step.model_copy(
                        update={
                            "not_executable_reason": (
                                "alternative_exposure 未冻结 replaces_exposure，"
                                "且基准暴露变量不唯一；代码不猜测方向比较映射。"
                            )
                        }
                    )
            normalized.append(registered_step)
        setattr(compiled, section, normalized)

    open_issues = [item for item in reviewer_issues if item.status == "open"]
    known_issue_ids: dict[str, list[str]] = {}
    unknown_issues: list[CriticIssue] = []
    for issue in open_issues:
        if issue.threat_id in ENTERPRISE_PANEL_THREAT_BY_ID:
            known_issue_ids.setdefault(str(issue.threat_id), []).append(issue.issue_id)
        else:
            unknown_issues.append(issue)

    baseline = compiled.baseline_models[0] if compiled.baseline_models else None
    for threat in ENTERPRISE_PANEL_THREATS:
        section_steps: list[PlannedStep] = getattr(compiled, threat.plan_section)
        matches = [item for item in section_steps if item.threat_id == threat.threat_id]
        if not matches:
            section_steps.append(
                _default_registry_step(
                    threat,
                    baseline,
                    claim_ids,
                    mechanism_claim_ids,
                )
            )
            matches = [section_steps[-1]]
        issue_ids = known_issue_ids.get(threat.threat_id, [])
        if issue_ids:
            first = matches[0]
            updated = first.model_copy(
                update={
                    "source_issue_ids": list(
                        dict.fromkeys([*first.source_issue_ids, *issue_ids])
                    ),
                    "priority": "required",
                    "required_for_admission": True,
                }
            )
            section_steps[section_steps.index(first)] = updated

    for issue in unknown_issues:
        section = _section_for_issue(issue)
        placeholder_threat_id = issue.threat_id or f"unmapped:{issue.issue_id}"
        _upsert_unknown_threat_placeholder(
            compiled,
            section,
            _unknown_threat_placeholder(
                threat_id=placeholder_threat_id,
                issue_id=issue.issue_id,
                section=section,
                claim_ids=claim_ids,
            ),
        )

    compiled.check_registry_version = ENTERPRISE_PANEL_REGISTRY_VERSION
    _validate_unique_step_ids(compiled)
    return compiled


def compile_policy_did_test_dag(
    plan: AnalysisPlan,
    hypotheses: Sequence[Hypothesis | str],
) -> AnalysisPlan:
    """Bind an executable policy plan to the minimal DID admission registry."""

    if plan.method_family != "policy_causal":
        raise TestDagError("policy-did-v2 only supports policy_causal plans")
    claim_ids = stable_claim_ids(hypotheses)
    if len(plan.baseline_models) != 1:
        raise TestDagError("policy-did-v2 requires exactly one baseline model")
    compiled = plan.model_copy(deep=True)
    _validate_unique_step_ids(compiled)
    compiled.baseline_models = [
        compiled.baseline_models[0].model_copy(
            update={
                "target_claim_ids": list(claim_ids),
                "priority": "required",
                "required_for_admission": True,
            }
        )
    ]

    threat_by_step = {
        "check-policy-support": (THREAT_POLICY_SUPPORT, "diagnostic", True),
        "check-policy-alternative-outcome": (
            THREAT_POLICY_ALTERNATIVE_OUTCOME,
            "robustness",
            True,
        ),
        "check-policy-group-fixed-pre": (
            THREAT_POLICY_GROUP_FIXED_PRE,
            "robustness",
            True,
        ),
        "check-policy-group-stable-only": (
            THREAT_POLICY_GROUP_STABLE_ONLY,
            "robustness",
            False,
        ),
        "check-policy-cluster-entity": (
            THREAT_POLICY_ENTITY_CLUSTER,
            "robustness",
            True,
        ),
        "check-policy-event-study": (
            THREAT_POLICY_EVENT_STUDY,
            "falsification",
            True,
        ),
        "check-policy-placebo-time": (
            THREAT_POLICY_PLACEBO,
            "falsification",
            True,
        ),
        "check-policy-permutation-placebo": (
            THREAT_POLICY_PERMUTATION_PLACEBO,
            "falsification",
            True,
        ),
        "check-policy-independent-replication": (
            THREAT_POLICY_INDEPENDENT_REPLICATION,
            "replication",
            True,
        ),
    }
    seen: set[str] = set()
    for section in (
        "diagnostics",
        "robustness_tests",
        "falsification_tests",
        "mechanism_tests",
        "heterogeneity_tests",
    ):
        normalized: list[PlannedStep] = []
        for step in getattr(compiled, section):
            binding = threat_by_step.get(step.step_id)
            if binding is None:
                normalized.append(
                    step.model_copy(
                        update={
                            "target_claim_ids": list(claim_ids),
                            "required_for_admission": False,
                            "test_role": step.test_role or "exploratory",
                        }
                    )
                )
                continue
            threat_id, test_role, required = binding
            seen.add(step.step_id)
            normalized.append(
                step.model_copy(
                    update={
                        "threat_id": threat_id,
                        "target_claim_ids": list(claim_ids),
                        "test_role": test_role,
                        "required_for_admission": required,
                        "priority": "required" if required else "recommended",
                    }
                )
            )
        setattr(compiled, section, normalized)
    required_steps = {
        "check-policy-support",
        "check-policy-group-fixed-pre",
        "check-policy-group-stable-only",
        "check-policy-event-study",
        "check-policy-placebo-time",
        "check-policy-permutation-placebo",
        "check-policy-independent-replication",
    }
    if not required_steps.issubset(seen):
        raise TestDagError(
            "policy-did-v2 is missing required checks: "
            + ", ".join(sorted(required_steps - seen))
        )
    compiled.check_registry_version = POLICY_DID_REGISTRY_VERSION
    _validate_unique_step_ids(compiled)
    return compiled


def schedule_test_dag(plan: AnalysisPlan) -> list[ScheduledTest]:
    """Return the frozen execution order, with independent replication last."""

    _validate_unique_step_ids(plan)
    diagnostics = [ScheduledTest("diagnostic", item) for item in plan.diagnostics]
    baselines = [ScheduledTest("baseline", item) for item in plan.baseline_models]
    additional = [
        *[ScheduledTest("robustness", item) for item in plan.robustness_tests],
        *[ScheduledTest("falsification", item) for item in plan.falsification_tests],
        *[ScheduledTest("mechanism", item) for item in plan.mechanism_tests],
        *[ScheduledTest("heterogeneity", item) for item in plan.heterogeneity_tests],
    ]
    replication = [item for item in additional if item.step.test_role == "replication"]
    additional = [item for item in additional if item.step.test_role != "replication"]

    required_diagnostics = [item for item in diagnostics if item.required]
    optional_diagnostics = [item for item in diagnostics if not item.required]
    required_additional = [item for item in additional if item.required]
    optional_additional = [item for item in additional if not item.required]
    return [
        *required_diagnostics,
        *baselines,
        *required_additional,
        *optional_diagnostics,
        *optional_additional,
        *[
            ScheduledTest("replication", item.step)
            for item in replication
        ],
    ]


def select_test_dag_with_budget(
    plan: AnalysisPlan,
    max_executions: int,
) -> BudgetedSchedule:
    if max_executions < 0:
        raise TestDagError("max_executions cannot be negative")
    scheduled = schedule_test_dag(plan)
    executable = [
        item for item in scheduled if item.step.not_executable_reason is None
    ]
    required = [item for item in executable if item.required]
    optional = [item for item in executable if not item.required]
    selected_candidates = [*required, *optional]
    selected_ids = {
        item.step.step_id for item in selected_candidates[:max_executions]
    }
    selected = tuple(item for item in scheduled if item.step.step_id in selected_ids)
    omitted = tuple(item for item in scheduled if item.step.step_id not in selected_ids)
    return BudgetedSchedule(selected=selected, omitted=omitted)


def select_primary_test_dag_with_budget(
    plan: AnalysisPlan,
    max_executions: int,
) -> BudgetedSchedule:
    """Apply the frozen budget to primary steps; replication is a separate service."""

    if max_executions < 0:
        raise TestDagError("max_executions cannot be negative")
    scheduled = [
        item for item in schedule_test_dag(plan) if item.run_type != "replication"
    ]
    executable = [
        item for item in scheduled if item.step.not_executable_reason is None
    ]
    required = [item for item in executable if item.required]
    optional = [item for item in executable if not item.required]
    selected_ids = {
        item.step.step_id
        for item in [*required, *optional][:max_executions]
    }
    return BudgetedSchedule(
        selected=tuple(
            item for item in scheduled if item.step.step_id in selected_ids
        ),
        omitted=tuple(
            item for item in scheduled if item.step.step_id not in selected_ids
        ),
    )


def finalize_test_dag_executions(
    plan: AnalysisPlan,
    executions: Sequence[ExecutionRecord],
    *,
    reason_codes: Mapping[str, NotExecutedReasonCode] | None = None,
    reasons: Mapping[str, str] | None = None,
) -> list[ExecutionRecord]:
    """Ensure every executable frozen step has exactly one terminal record."""

    scheduled = schedule_test_dag(plan)
    known = {item.step.step_id: item for item in scheduled}
    by_step: dict[str, ExecutionRecord] = {}
    for execution in executions:
        if execution.plan_step_id not in known:
            raise TestDagError(
                f"execution references an unknown frozen step: {execution.plan_step_id}"
            )
        if execution.plan_step_id in by_step:
            raise TestDagError(
                f"frozen step has multiple execution records: {execution.plan_step_id}"
            )
        if execution.execution_status not in _TERMINAL_EXECUTION_STATUSES:
            raise TestDagError(
                f"frozen step is not terminal: {execution.plan_step_id}"
            )
        by_step[execution.plan_step_id] = execution

    reason_codes = reason_codes or {}
    reasons = reasons or {}
    finalized: list[ExecutionRecord] = []
    for scheduled_test in scheduled:
        step = scheduled_test.step
        execution = by_step.get(step.step_id)
        if execution is not None:
            reason_code = execution.not_executed_reason_code
            if execution.execution_status in {"not_executed", "fixture_only"}:
                reason_code = reason_code or reason_codes.get(step.step_id)
                reason_code = reason_code or (
                    "not_executable"
                    if step.not_executable_reason
                    else "fixture_only"
                    if execution.execution_status == "fixture_only"
                    else "dependency_failed"
                )
            finalized.append(
                execution.model_copy(
                    update={
                        "check_id": execution.check_id or step.step_id,
                        "not_executed_reason_code": reason_code,
                    }
                )
            )
            continue
        reason_code = reason_codes.get(
            step.step_id,
            "not_executable" if step.not_executable_reason else "dependency_failed",
        )
        reason = reasons.get(
            step.step_id,
            step.not_executable_reason
            or "冻结步骤没有得到执行结果，已显式关闭为 not_executed。",
        )
        finalized.append(
            ExecutionRecord(
                execution_id=f"execution-{step.step_id}-not-executed",
                run_type=scheduled_test.run_type,
                plan_step_id=step.step_id,
                check_id=step.step_id,
                execution_status="not_executed",
                not_executed_reason_code=reason_code,
                error=reason,
                warnings=[reason],
            )
        )
    return finalized


def required_checks_for_claim(
    plan: AnalysisPlan,
    claim: ClaimRecord,
) -> list[str]:
    """Derive admission checks exclusively from the frozen plan.

    ``ClaimRecord.required_check_ids`` is model-authored candidate metadata and
    must never be able to remove a frozen falsification or replication check.
    The claim record contributes only its code-frozen stable identifier here;
    claim type and targets are defined by the plan's structured step targets.
    """

    required: list[str] = []
    for item in schedule_test_dag(plan):
        step = item.step
        if not step.required_for_admission:
            continue
        if step.target_claim_ids and claim.claim_id not in step.target_claim_ids:
            continue
        if (
            step.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
            and not step.target_claim_ids
        ):
            # A legacy target-less mechanism step is ambiguous. It must not be
            # assigned using an LLM-authored claim_type; upgraded plans freeze
            # explicit mechanism targets before admission.
            continue
        required.append(step.step_id)
    return list(dict.fromkeys(required))


def build_evidence_registry(
    plan: AnalysisPlan,
    research_run: ResearchRun,
    claims: Sequence[ClaimRecord],
    *,
    reproduction_audit: ReproductionAudit | None = None,
    scientific_audit: ScientificAudit | None = None,
    alpha: float = 0.05,
) -> EvidenceRegistry:
    """Compile code-owned terminal facts into claim-scoped evidence.

    ``ScientificAudit`` is deliberately advisory. Its free-text verdict is kept
    as a separate artifact for human review, but cannot manufacture or override
    the execution, reproduction, or contract facts in this registry.
    """

    if not 0 < alpha < 1:
        raise TestDagError("alpha must be between zero and one")
    claim_ids = [item.claim_id for item in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise TestDagError("claim ids must be unique")
    claim_by_id = {item.claim_id: item for item in claims}
    scheduled = schedule_test_dag(plan)
    step_by_id = {item.step.step_id: item.step for item in scheduled}
    baseline_estimates = _baseline_estimates(research_run)
    baseline_target_terms = {
        term
        for model in plan.baseline_models
        for term in model.treatments_or_exposures
    }

    evidence: list[EvidenceObject] = []

    def append(
        *,
        claim_id: str,
        check_id: str,
        execution_id: str | None,
        source_kind: Literal[
            "execution", "reproduction", "scientific_audit", "contract"
        ],
        status: Literal["supporting", "opposing", "incomplete", "invalid"],
        reason: str,
    ) -> None:
        evidence.append(
            EvidenceObject(
                evidence_id=(
                    f"evidence-{research_run.research_run_id}-{len(evidence) + 1:04d}"
                ),
                claim_id=claim_id,
                check_id=check_id,
                execution_id=execution_id,
                source_kind=source_kind,
                status=status,
                reason=reason,
            )
        )

    for execution in research_run.executions:
        step = step_by_id.get(execution.plan_step_id)
        if step is not None and step.test_role == "replication":
            # The independent ReproductionAudit is authoritative for this
            # frozen check. A terminal placeholder in the primary ResearchRun
            # must not downgrade a successful independent reproduction.
            continue
        if step is None:
            targets = claim_ids
            check_id = execution.check_id or execution.plan_step_id
        else:
            targets = _target_claim_ids(step, claims)
            check_id = step.step_id
        status, reason = _execution_evidence_status(
            execution,
            step,
            baseline_estimates,
            baseline_target_terms,
            fixture_only=research_run.fixture_only,
            alpha=alpha,
        )
        for claim_id in targets:
            if claim_id in claim_by_id:
                append(
                    claim_id=claim_id,
                    check_id=check_id,
                    execution_id=execution.execution_id,
                    source_kind="execution",
                    status=status,
                    reason=reason,
                )

    replication_steps = [
        item.step for item in scheduled if item.step.test_role == "replication"
    ]
    for step in replication_steps:
        status, reason = _reproduction_evidence_status(reproduction_audit)
        for claim_id in _target_claim_ids(step, claims):
            append(
                claim_id=claim_id,
                check_id=step.step_id,
                execution_id=(
                    reproduction_audit.replication_run_id
                    if reproduction_audit is not None
                    else None
                ),
                source_kind="reproduction",
                status=status,
                reason=reason,
            )

    _ = scientific_audit

    required_check_ids = [
        item.step.step_id
        for item in scheduled
        if item.step.required_for_admission
    ]
    return EvidenceRegistry(
        registry_version=(
            plan.check_registry_version or ENTERPRISE_PANEL_REGISTRY_VERSION
        ),
        case_id=research_run.case_id,
        research_run_id=research_run.research_run_id,
        required_check_ids=list(dict.fromkeys(required_check_ids)),
        evidence=evidence,
    )


def _normalize_registered_step(
    step: PlannedStep,
    threat: EnterprisePanelThreat,
    claim_ids: Sequence[str],
    hypothesis_to_claim: Mapping[str, str],
    mechanism_claim_ids: set[str],
) -> PlannedStep:
    targets = _canonical_targets(
        step.target_claim_ids,
        claim_ids,
        hypothesis_to_claim,
    )
    if not targets:
        targets = (
            sorted(mechanism_claim_ids)
            if threat.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY
            else list(claim_ids)
        )
    required = step.required_for_admission or threat.required_by_default
    if threat.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY:
        required = bool(targets)
    parameters = dict(step.parameters)
    not_executable_reason = step.not_executable_reason
    if threat.threat_id == THREAT_LEAD_PLACEBO:
        parameters["alpha"] = 0.05
    if threat.threat_id == THREAT_FE_CLUSTER_FEASIBILITY:
        parameters["wild_cluster_bootstrap_replications"] = 999
        parameters["wild_cluster_bootstrap_seed"] = 20260720
    if threat.threat_id == THREAT_SAMPLE_OUTLIER_SENSITIVITY:
        unsupported = set(parameters).intersection(
            {"winsor_limits", "trim_quantiles", "outlier_rule"}
        )
        sample_filter = parameters.get("sample_filter")
        invalid_filter = "sample_filter" in parameters and (
            not isinstance(sample_filter, str)
            or not _SIMPLE_SAMPLE_FILTER_RE.fullmatch(sample_filter)
        )
        if unsupported or invalid_filter:
            not_executable_reason = (
                "当前执行器只支持单个冻结 sample_filter，语法为字段与常量的简单比较；"
                "winsor_limits、trim_quantiles、outlier_rule 或复合表达式不被猜测执行。"
            )
    return step.model_copy(
        update={
            "threat_id": threat.threat_id,
            "target_claim_ids": targets,
            "test_role": threat.test_role,
            "required_for_admission": required,
            "priority": "required" if required else step.priority,
            "parameters": parameters,
            "not_executable_reason": not_executable_reason,
        }
    )


def _normalize_unregistered_step(
    section: PlanSection,
    step: PlannedStep,
    claim_ids: Sequence[str],
    hypothesis_to_claim: Mapping[str, str],
) -> PlannedStep:
    targets = _canonical_targets(
        step.target_claim_ids,
        claim_ids,
        hypothesis_to_claim,
    )
    not_executable_reason = step.not_executable_reason
    if (
        section == "robustness_tests"
        and not is_estimative_test_step(step, "robustness")
    ):
        not_executable_reason = not_executable_reason or (
            "该稳健性步骤没有冻结当前执行器支持的替代变量或 sample_filter；"
            "代码不会把未知参数静默当作基准模型重新估计。"
        )
    return step.model_copy(
        update={
            "target_claim_ids": targets,
            "not_executable_reason": not_executable_reason,
        }
    )


def is_estimative_test_step(step: PlannedStep, run_type: RunType) -> bool:
    """Return whether a frozen non-baseline step changes an estimable model."""

    if step.not_executable_reason:
        return False
    if run_type == "robustness":
        return any(
            key in step.parameters
            for key in (
                "alternative_outcome",
                "alternative_exposure",
                "sample_filter",
            )
        )
    if run_type == "falsification":
        return any(
            key in step.parameters
            for key in (
                "alternative_outcome",
                "placebo_outcome",
                "alternative_exposure",
                "lead_exposure",
                "policy_event_study",
                "policy_placebo",
            )
        )
    if run_type == "mechanism":
        return any(
            key in step.parameters
            for key in (
                "mediator",
                "moderator",
                "mechanism_variable",
            )
        )
    if run_type == "heterogeneity":
        return (
            "subgroup_variable" in step.parameters
            and "subgroup_value" in step.parameters
        )
    return False


def _canonical_targets(
    targets: Sequence[str],
    claim_ids: Sequence[str],
    hypothesis_to_claim: Mapping[str, str],
) -> list[str]:
    known = set(claim_ids)
    canonical: list[str] = []
    for target in targets:
        value = hypothesis_to_claim.get(target, target)
        if value not in known:
            raise TestDagError(f"step references an unknown claim: {target}")
        canonical.append(value)
    return list(dict.fromkeys(canonical))


def _default_registry_step(
    threat: EnterprisePanelThreat,
    baseline: ModelSpec | None,
    claim_ids: Sequence[str],
    mechanism_claim_ids: set[str],
) -> PlannedStep:
    fields = _baseline_fields(baseline)
    parameters: dict[str, object] = {}
    not_executable_reason: str | None = None
    targets = list(claim_ids)
    required = threat.required_by_default

    if threat.threat_id == THREAT_KEY_SAMPLE_FLOW:
        parameters = {"checks": ["duplicate_primary_key", "singleton_rows", "sample_flow"]}
        fields = list(baseline.fixed_effects) if baseline is not None else []
    elif threat.threat_id == THREAT_MISSINGNESS_WITHIN_VARIANCE:
        within_fields = (
            list(baseline.treatments_or_exposures) if baseline is not None else []
        )
        parameters = {
            "checks": [
                "missingness",
                *[f"within_variance({item})" for item in within_fields],
            ]
        }
    elif threat.threat_id == THREAT_FE_CLUSTER_FEASIBILITY:
        parameters = {
            "checks": [
                "fixed_effects",
                "cluster_level",
                "finite_sample_correction",
                "wild_cluster_bootstrap",
            ],
            "wild_cluster_bootstrap_replications": 999,
            "wild_cluster_bootstrap_seed": 20260720,
        }
    elif threat.threat_id == THREAT_ALTERNATIVE_MEASUREMENT:
        not_executable_reason = (
            "未冻结 alternative_outcome 或 alternative_exposure；代码不猜测替代变量。"
        )
    elif threat.threat_id == THREAT_LEAD_PLACEBO:
        not_executable_reason = (
            "未冻结 lead_exposure 或 placebo_outcome；代码不猜测证伪变量。"
        )
        parameters = {"alpha": 0.05}
    elif threat.threat_id == THREAT_SAMPLE_OUTLIER_SENSITIVITY:
        not_executable_reason = (
            "未冻结样本筛选、缩尾或异常值敏感性规则；代码不猜测阈值。"
        )
    elif threat.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY:
        targets = sorted(mechanism_claim_ids)
        required = bool(targets)
        if not targets:
            not_executable_reason = "当前没有声明机制主张，因此该检查不参与其他 Claim 准入。"
        else:
            not_executable_reason = (
                "未冻结 mediator、moderator 或 interaction_term；代码不猜测机制变量。"
            )
    elif threat.threat_id == THREAT_INDEPENDENT_REPLICATION:
        parameters = {"implementation": "independent_within_transform"}

    return PlannedStep(
        step_id=f"check-{threat.threat_id.replace('.', '-')}",
        name=threat.name,
        priority="required" if required else "recommended",
        rationale=threat.rationale,
        required_data_fields=fields,
        parameters=parameters,
        threat_id=threat.threat_id,
        target_claim_ids=targets,
        test_role=threat.test_role,
        required_for_admission=required,
        not_executable_reason=not_executable_reason,
    )


def _unknown_threat_placeholder(
    *,
    threat_id: str,
    issue_id: str | None,
    section: PlanSection,
    claim_ids: Sequence[str],
    original: PlannedStep | None = None,
) -> PlannedStep:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in threat_id
    ).strip("-") or "unknown"
    reason = (
        f"Reviewer threat_id '{threat_id}' 不在 {ENTERPRISE_PANEL_REGISTRY_VERSION}；"
        "已显式标记为 not_executable，代码不解析 required_fix 猜测修复。"
    )
    if original is not None:
        return original.model_copy(
            update={
                "threat_id": threat_id,
                "target_claim_ids": list(claim_ids),
                "test_role": original.test_role or "exploratory",
                "required_for_admission": True,
                "priority": "required",
                "source_issue_ids": list(
                    dict.fromkeys(
                        [*original.source_issue_ids, *([issue_id] if issue_id else [])]
                    )
                ),
                "not_executable_reason": reason,
            }
        )
    return PlannedStep(
        step_id=f"check-unregistered-{safe}-{issue_id or 'plan'}",
        name="未注册 Reviewer 威胁",
        priority="required",
        rationale="保留 Reviewer 问题但不以自然语言猜测可执行分析。",
        threat_id=threat_id,
        target_claim_ids=list(claim_ids),
        test_role="exploratory",
        required_for_admission=True,
        source_issue_ids=[issue_id] if issue_id else [],
        not_executable_reason=reason,
    )


def _upsert_unknown_threat_placeholder(
    plan: AnalysisPlan,
    section: PlanSection,
    placeholder: PlannedStep,
) -> None:
    existing_location: tuple[PlanSection, int, PlannedStep] | None = None
    for candidate_section in _SECTION_RUN_TYPE:
        for index, step in enumerate(getattr(plan, candidate_section)):
            if step.step_id != placeholder.step_id:
                continue
            existing_location = (candidate_section, index, step)
            break
        if existing_location is not None:
            break

    if existing_location is None:
        getattr(plan, section).append(placeholder)
        return

    existing_section, index, existing = existing_location
    if existing.threat_id in ENTERPRISE_PANEL_THREAT_BY_ID:
        raise TestDagError(
            "unknown reviewer placeholder step id collides with a registered check: "
            + placeholder.step_id
        )
    if existing.threat_id != placeholder.threat_id:
        raise TestDagError(
            "unknown reviewer placeholder step id maps to different threat ids: "
            + placeholder.step_id
        )
    getattr(plan, existing_section)[index] = existing.model_copy(
        update={
            "threat_id": existing.threat_id or placeholder.threat_id,
            "target_claim_ids": list(
                dict.fromkeys(
                    [*existing.target_claim_ids, *placeholder.target_claim_ids]
                )
            ),
            "test_role": existing.test_role or "exploratory",
            "required_for_admission": True,
            "priority": "required",
            "source_issue_ids": list(
                dict.fromkeys(
                    [*existing.source_issue_ids, *placeholder.source_issue_ids]
                )
            ),
            "not_executable_reason": (
                existing.not_executable_reason
                or placeholder.not_executable_reason
            ),
        }
    )


def _section_for_issue(issue: CriticIssue) -> PlanSection:
    if issue.dimension == "causal":
        return "falsification_tests"
    if issue.dimension == "measurement":
        return "robustness_tests"
    if issue.dimension == "reproducibility":
        return "robustness_tests"
    return "diagnostics"


def _infer_structured_threat_id(
    section: PlanSection,
    step: PlannedStep,
) -> str | None:
    keys = set(step.parameters)
    if section == "falsification_tests" and keys.intersection(
        {"lead_exposure", "placebo_outcome"}
    ):
        return THREAT_LEAD_PLACEBO
    if section == "robustness_tests" and keys.intersection(
        {"alternative_outcome", "alternative_exposure"}
    ):
        return THREAT_ALTERNATIVE_MEASUREMENT
    if section == "robustness_tests" and keys.intersection(
        {"sample_filter", "winsor_limits", "trim_quantiles", "outlier_rule"}
    ):
        return THREAT_SAMPLE_OUTLIER_SENSITIVITY
    if section == "mechanism_tests" and keys.intersection(
        {"mediator", "moderator", "mechanism_variable", "interaction_term"}
    ):
        return THREAT_MECHANISM_INTERACTION_BOUNDARY
    if step.test_role == "replication":
        return THREAT_INDEPENDENT_REPLICATION
    checks = {str(item) for item in step.parameters.get("checks", [])}
    if section == "diagnostics" and checks.intersection(
        {"duplicate_primary_key", "singleton_rows", "sample_flow"}
    ):
        return THREAT_KEY_SAMPLE_FLOW
    if section == "diagnostics" and (
        "missingness" in checks
        or any(item.startswith("within_variance(") for item in checks)
    ):
        return THREAT_MISSINGNESS_WITHIN_VARIANCE
    if section == "diagnostics" and checks.intersection(
        {"fixed_effects", "cluster_level", "finite_sample_correction"}
    ):
        return THREAT_FE_CLUSTER_FEASIBILITY
    return None


def _baseline_fields(baseline: ModelSpec | None) -> list[str]:
    if baseline is None:
        return []
    return list(
        dict.fromkeys(
            [
                *baseline.fixed_effects,
                *([baseline.outcome] if baseline.outcome else []),
                *baseline.treatments_or_exposures,
                *baseline.controls,
            ]
        )
    )


def _validate_unique_step_ids(plan: AnalysisPlan) -> None:
    steps: list[PlannedStep] = [
        *plan.estimands,
        *plan.sample_rules,
        *plan.variable_construction,
        *plan.baseline_models,
        *plan.diagnostics,
        *plan.robustness_tests,
        *plan.falsification_tests,
        *plan.mechanism_tests,
        *plan.heterogeneity_tests,
    ]
    ids = [item.step_id for item in steps]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise TestDagError("plan step ids must be globally unique: " + ", ".join(duplicates))


def _target_claim_ids(
    step: PlannedStep,
    claims: Sequence[ClaimRecord],
) -> list[str]:
    known = {item.claim_id for item in claims}
    if step.target_claim_ids:
        return [item for item in step.target_claim_ids if item in known]
    if step.threat_id == THREAT_MECHANISM_INTERACTION_BOUNDARY:
        return [item.claim_id for item in claims if item.claim_type == "mechanism"]
    return [item.claim_id for item in claims]


def _baseline_estimates(research_run: ResearchRun) -> dict[str, float]:
    for execution in research_run.executions:
        if execution.run_type != "baseline" or execution.execution_status != "succeeded":
            continue
        return {
            str(item["term"]): float(item["coefficient"])
            for item in execution.estimates
            if "term" in item and _is_number(item.get("coefficient"))
        }
    return {}


def _execution_evidence_status(
    execution: ExecutionRecord,
    step: PlannedStep | None,
    baseline_estimates: Mapping[str, float],
    baseline_target_terms: set[str],
    *,
    fixture_only: bool,
    alpha: float,
) -> tuple[
    Literal["supporting", "opposing", "incomplete", "invalid"],
    str,
]:
    if fixture_only or execution.execution_status == "fixture_only":
        return "invalid", "Fixture execution cannot support an empirical claim."
    if execution.execution_status in {"planned", "queued", "running"}:
        return "invalid", "Evidence Registry received a non-terminal execution."
    if execution.execution_status in {"failed", "cancelled", "not_executed"}:
        return "incomplete", execution.error or "Required check was not completed."
    if execution.provenance is None:
        return "invalid", "Succeeded execution is missing required provenance hashes."
    if step is not None and step.threat_id == THREAT_POLICY_PERMUTATION_PLACEBO:
        empirical_p = execution.diagnostic_results.get("empirical_p_value")
        requested = execution.diagnostic_results.get("repetitions_requested")
        completed = execution.diagnostic_results.get("repetitions_completed")
        if not all(_is_number(value) for value in (empirical_p, requested, completed)):
            return "incomplete", "Permutation placebo lacks its frozen completion and p-value fields."
        if int(completed) != int(requested):
            return "incomplete", "Permutation placebo did not complete all frozen repetitions."
        scheme = str(
            execution.diagnostic_results.get("scheme") or "frozen_assignment"
        )
        if float(empirical_p) <= alpha:
            return (
                "supporting",
                f"Observed coefficient is extreme under the frozen {scheme} "
                f"permutation at alpha={alpha:.3f}; interpretation remains bound "
                "to the registered assignment assumptions.",
            )
        return (
            "opposing",
            f"Observed coefficient is not extreme under the frozen {scheme} "
            f"permutation at alpha={alpha:.3f}.",
        )
    if not execution.estimates and execution.run_type == "falsification":
        return (
            "incomplete",
            "Feasibility-only falsification does not count as outcome evidence.",
        )

    diagnostics = execution.diagnostic_results
    if diagnostics.get("feasible") is False:
        return "incomplete", "The frozen check was not feasible on the bound data."
    if step is not None and step.threat_id == THREAT_KEY_SAMPLE_FLOW:
        duplicate_rows = diagnostics.get("duplicate_primary_key_rows", 0)
        if _is_number(duplicate_rows) and float(duplicate_rows) > 0:
            return "opposing", "Duplicate panel keys violate the frozen sample contract."
    if step is not None and step.threat_id == THREAT_POLICY_SUPPORT:
        switchers = diagnostics.get("group_switcher_entities", 0)
        if _is_number(switchers) and float(switchers) > 0:
            return (
                "supporting",
                "The source policy-group field changes within entities; policy-did-v2 "
                "requires a separate fixed-pre-policy grouping sensitivity before admission.",
            )
    if step is not None and step.threat_id == THREAT_POLICY_EVENT_STUDY:
        policy_design = step.parameters.get("policy_design")
        requested_remote_pre = (
            policy_design.get("event_remote_pre_years", [])
            if isinstance(policy_design, dict)
            else []
        )
        if requested_remote_pre and diagnostics.get("remote_pre_complete") is not True:
            return (
                "incomplete",
                "Event study did not complete the frozen remote pre-period bin.",
            )
        joint_p = diagnostics.get("joint_pretrend_p_value")
        if not _is_number(joint_p):
            return "incomplete", "Event study has no joint pre-trend test."
        if float(joint_p) < alpha:
            return (
                "opposing",
                f"Joint policy pre-trend test rejects at frozen alpha={alpha:.3f}.",
            )
        return (
            "supporting",
            "Joint pre-trend test did not reject; this is not proof of parallel trends.",
        )
    if step is not None and step.threat_id == THREAT_POLICY_PLACEBO:
        contamination = diagnostics.get("true_policy_contamination_rows")
        if not _is_number(contamination):
            return (
                "incomplete",
                "Fake-time placebo lacks a true-policy contamination diagnostic.",
            )
        if int(contamination) != 0:
            return (
                "invalid",
                "Fake-time placebo includes observations from the true policy period.",
            )
        significant = [
            item
            for item in execution.estimates
            if _is_number(item.get("p_value")) and float(item["p_value"]) < alpha
        ]
        if significant:
            return (
                "opposing",
                f"Pre-policy fake-time estimate is significant at frozen alpha={alpha:.3f}.",
            )
        return (
            "supporting",
            "Pre-policy fake-time estimate is not significant at the frozen alpha.",
        )
    if execution.run_type == "falsification":
        significant = [
            item
            for item in execution.estimates
            if _is_number(item.get("p_value")) and float(item["p_value"]) < alpha
        ]
        if significant:
            return (
                "opposing",
                f"Lead/placebo estimate is significant at frozen alpha={alpha:.3f}.",
            )
    if execution.run_type == "robustness" and execution.estimates:
        for estimate in execution.estimates:
            term = str(estimate.get("term", ""))
            coefficient = estimate.get("coefficient")
            baseline_term = term
            if (
                step is not None
                and term == str(step.parameters.get("alternative_exposure", ""))
            ):
                baseline_term = str(step.parameters.get("replaces_exposure", ""))
            if (
                baseline_term in baseline_target_terms
                and baseline_term in baseline_estimates
                and _is_number(coefficient)
                and float(coefficient) * baseline_estimates[baseline_term] < 0
            ):
                return "opposing", "Robustness estimate reverses the baseline sign."
        if step is not None and step.threat_id == THREAT_POLICY_GROUP_FIXED_PRE:
            rows_input = execution.diagnostic_results.get("rows_input")
            rows_used = execution.diagnostic_results.get("rows_used")
            dropped_entities = execution.diagnostic_results.get(
                "entities_dropped_no_pre_policy_group"
            )
            if not all(
                _is_number(value)
                for value in (rows_input, rows_used, dropped_entities)
            ):
                return (
                    "incomplete",
                    "Fixed pre-policy grouping lacks frozen row/entity attrition diagnostics.",
                )
            return (
                "supporting",
                "Fixed pre-policy grouping preserved the baseline sign on "
                f"{int(rows_used)}/{int(rows_input)} rows after dropping "
                f"{int(dropped_entities)} entities without a pre-policy group; "
                "this changes sample composition and is not same-sample robustness.",
            )
    if step is not None and step.threat_id == THREAT_POLICY_GROUP_FIXED_PRE:
        return "incomplete", "Fixed pre-policy grouping produced no estimates."
    return "supporting", "Frozen check completed without a deterministic contradiction."


def _reproduction_evidence_status(
    audit: ReproductionAudit | None,
) -> tuple[
    Literal["supporting", "opposing", "incomplete", "invalid"],
    str,
]:
    if audit is None:
        return "incomplete", "Independent reproduction was not executed."
    if audit.mode != "independent_implementation":
        return "incomplete", "Same-implementation rerun is not independent reproduction."
    if (
        not audit.primary_implementation_id
        or not audit.replication_implementation_id
        or audit.primary_implementation_id == audit.replication_implementation_id
    ):
        return "invalid", "Independent reproduction implementation ids are missing or equal."
    scope_suffix = f"independence_scope={audit.independence_scope}"
    if audit.shared_components:
        scope_suffix += "; shared components: " + ", ".join(audit.shared_components)
    if audit.status == "matched":
        if audit.independence_scope == "estimator_only":
            shared = ", ".join(audit.shared_components) or "unspecified shared components"
            return (
                "supporting",
                "Independent estimator/covariance implementation matched within frozen "
                f"tolerances; independence_scope=estimator_only; shared components: {shared}. "
                "This is not end-to-end independent reproduction.",
            )
        return (
            "supporting",
            "Independent implementation matched within frozen tolerances; "
            f"independence_scope={audit.independence_scope}.",
        )
    if audit.status == "diverged":
        return (
            "opposing",
            "Independent implementation diverged from the primary results; "
            f"{scope_suffix}.",
        )
    if audit.status == "failed":
        return "invalid", f"Independent reproduction failed; {scope_suffix}."
    return (
        "incomplete",
        f"Independent reproduction was not applicable; {scope_suffix}.",
    )


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
