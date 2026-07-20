from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from .claim_gate import causal_wording_violations
from .models import (
    AnalysisPlan,
    ClaimLedger,
    ExecutionRecord,
    ManuscriptPackage,
    ManuscriptSection,
    ManuscriptSectionDraft,
    ManuscriptStatement,
    ProtectedValue,
    ResearchPackage,
    ResearchRun,
    ReproductionAudit,
    TRACEABLE_MANUSCRIPT_SECTION_IDS,
    VerifiedPassageRef,
)


class ManuscriptIRError(ValueError):
    """Raised when manuscript text cannot be compiled from verified sources."""


STATEMENT_ANCHOR_RE = re.compile(r"\[\[STATEMENT:([A-Za-z0-9_.:-]+)\]\]")
VALUE_ANCHOR_RE = re.compile(r"\[\[VALUE:([A-Za-z0-9_.:-]+)\]\]")
BARE_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:[eE][-+]?\d+)?%?(?![A-Za-z0-9_])"
)
WRITER_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?%?"
)
YEAR_LITERAL_RE = re.compile(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)")
FORMAL_CITATION_PATTERNS = (
    re.compile(r"\[[0-9,;\-\s]+\]"),
    re.compile(r"[\(\uff08][^\)\uff09]{0,80}(?:19|20)\d{2}[^\)\uff09]{0,80}[\)\uff09]"),
    re.compile(r"\b(?:doi\s*:|https?://|et\s+al\.)", re.IGNORECASE),
)
EMPIRICAL_JUDGMENT_PATTERNS = (
    re.compile(r"(?:\u7ed3\u679c|\u56de\u5f52|\u68c0\u9a8c|\u8bc1\u636e)(?:\u663e\u793a|\u8868\u660e|\u53d1\u73b0|\u652f\u6301|\u62d2\u7edd)"),
    re.compile(r"\u672c\u7814\u7a76(?:\u7ed3\u679c)?(?:\u663e\u793a|\u8868\u660e|\u53d1\u73b0|\u8bc1\u5b9e|\u652f\u6301|\u62d2\u7edd)"),
    re.compile(r"(?:\u7edf\u8ba1)?\u663e\u8457|\u4e0d\u663e\u8457|\u672a\u8fbe\u5230[^\u3002\uff1b\n]{0,24}\u663e\u8457"),
    re.compile(r"\u7cfb\u6570|\u6807\u51c6\u8bef|\u7f6e\u4fe1\u533a\u95f4|p\s*(?:\u503c|=)|\u6837\u672c\u91cf|\u62df\u5408\u6307\u6807|R(?:2|\u00b2)"),
)
TRACEABLE_EMPIRICAL_PREDICATE_PATTERNS = (
    re.compile(r"\u8d8a(?:\u9ad8|\u4f4e|\u5927|\u5c0f)[^\u3002\uff1b\n]{0,80}\u8d8a(?:\u9ad8|\u4f4e|\u5927|\u5c0f)"),
    re.compile(r"(?:\u5448|\u5b58\u5728|\u5177\u6709)[^\u3002\uff1b\n]{0,20}(?:\u6b63\u5411|\u8d1f\u5411)?(?:\u76f8\u5173|\u5173\u8054|\u5173\u7cfb)"),
    re.compile(r"(?:\u6b63\u5411|\u8d1f\u5411)(?:\u76f8\u5173|\u5173\u8054|\u5173\u7cfb|\u5f71\u54cd|\u4f5c\u7528)"),
    re.compile(
        r"(?:影响|导致|促进|抑制|造成|引发|驱动|使得|促使|有助于|推动|"
        r"加剧|削弱|缓解|改变|带来|提高|提升|降低|改善|增加|减少)"
    ),
)

# Code-owned allowlist for policy diagnostics that may enter manuscript text.
# Every label is fixed here; diagnostic keys and arbitrary string values are
# never copied into the manuscript.
_POLICY_DIAGNOSTIC_FACT_SPECS = {
    "check-policy-support": (
        (
            "group_switcher_count",
            "group_switcher_entities",
            "count",
            "政策支持诊断中，组别变化的实体数",
        ),
    ),
    "check-policy-event-study": (
        (
            "joint_pretrend_p_value",
            "joint_pretrend_p_value",
            "p_value",
            "政策前事件系数联合零假设检验的 p 值",
        ),
    ),
    "check-policy-permutation-placebo": (
        (
            "permutation_repetitions_completed",
            "repetitions_completed",
            "count",
            "随机置换安慰剂实际完成的置换次数",
        ),
        (
            "permutation_empirical_p_value",
            "empirical_p_value",
            "p_value",
            "随机置换安慰剂的双侧经验 p 值",
        ),
    ),
}

_PLAN_STEP_COLLECTIONS = (
    "estimands",
    "sample_rules",
    "variable_construction",
    "baseline_models",
    "diagnostics",
    "robustness_tests",
    "falsification_tests",
    "mechanism_tests",
    "heterogeneity_tests",
)

_REPRODUCTION_SHARED_COMPONENT_LABELS = {
    "policy_causal analysis-table preparation": "政策分析表准备",
    "policy event/placebo regressor construction": "事件研究和安慰剂变量构造",
}
_END_TO_END_REPRODUCTION_RE = re.compile(
    r"(?:(?:端到端|全流程|完整流程).{0,12}(?:独立复现|独立复算)|"
    r"(?:独立复现|独立复算).{0,12}(?:端到端|全流程|完整流程))"
)
_REPRODUCTION_NEGATION_RE = re.compile(
    r"(?:不得|不能|不可|并非|不是|不属于|不构成|不代表|未达到|并未|没有)"
)


def allowed_writer_year_literals(
    package: ResearchPackage,
    plan: AnalysisPlan,
) -> set[str]:
    """Return year tokens sourced only from the visible package and frozen plan."""

    years: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                visit(item)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                visit(item)
            return
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int):
            text = str(value)
            if YEAR_LITERAL_RE.fullmatch(text):
                years.add(text)
            return
        if isinstance(value, str):
            years.update(YEAR_LITERAL_RE.findall(value))

    visit(package.model_dump(mode="json"))
    visit(plan.model_dump(mode="json"))
    return years


def scrub_writer_numbers(
    value: Any,
    *,
    allowed_numeric_literals: set[str] | frozenset[str] = frozenset(),
    identifier_keys: set[str] | frozenset[str] = frozenset(),
) -> Any:
    """Redact numeric text except source-authorized literals and identifiers."""

    allowed = set(allowed_numeric_literals)
    identifiers = set(identifier_keys)

    def scrub(item: Any, key: str | None = None) -> Any:
        if isinstance(item, Mapping):
            return {
                item_key: scrub(item_value, str(item_key))
                for item_key, item_value in item.items()
            }
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            return [scrub(element, key) for element in item]
        if isinstance(item, bool) or item is None:
            return item
        if isinstance(item, (int, float)):
            rendered = str(item)
            return item if rendered in allowed else "[受保护数值已移除]"
        if isinstance(item, str) and key not in identifiers:
            return WRITER_NUMBER_RE.sub(
                lambda match: (
                    match.group(0)
                    if match.group(0) in allowed
                    else "[受保护数值已移除]"
                ),
                item,
            )
        return item

    return scrub(value)


def reproduction_scope_overclaim(
    text: str,
    reproduction_audit: ReproductionAudit | None,
) -> bool:
    """Detect end-to-end independence claims beyond the audited scope."""

    if (
        reproduction_audit is None
        or reproduction_audit.independence_scope == "end_to_end"
    ):
        return False
    return any(
        _END_TO_END_REPRODUCTION_RE.search(sentence)
        and not _REPRODUCTION_NEGATION_RE.search(sentence)
        for sentence in re.split(r"[。！？；\n]", text)
    )


def format_protected_value(value_kind: str, raw_value: Any) -> str:
    if value_kind in {"claim_text", "passage_quote"}:
        if not isinstance(raw_value, str):
            raise ManuscriptIRError(f"{value_kind} must be text")
        return raw_value
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ManuscriptIRError(f"{value_kind} must be numeric")
    value = float(raw_value)
    if not math.isfinite(value):
        raise ManuscriptIRError(f"{value_kind} must be finite")
    if value_kind in {"count", "year"}:
        if not value.is_integer():
            raise ManuscriptIRError(f"{value_kind} must be an integer")
        return str(int(value))
    if value_kind in {"coefficient", "standard_error", "interval_bound"}:
        return f"{value:.4f}"
    if value_kind == "p_value":
        if not 0 <= value <= 1:
            raise ManuscriptIRError("p_value must be between zero and one")
        return "<0.001" if value < 0.001 else f"{value:.3f}"
    if value_kind == "fit_statistic":
        return f"{value:.3f}"
    raise ManuscriptIRError(f"unsupported protected value kind: {value_kind}")


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _protected_value(
    *,
    value_kind: str,
    source_kind: str,
    source_id: str,
    source_path: str,
    raw_value: Any,
) -> ProtectedValue:
    return ProtectedValue(
        value_id=_stable_id(
            "value", source_kind, source_id, source_path, value_kind
        ),
        value_kind=value_kind,
        source_kind=source_kind,
        source_id=source_id,
        source_path=source_path,
        raw_value=raw_value,
        rendered_value=format_protected_value(value_kind, raw_value),
    )


def _safe_label(value: Any) -> str:
    return (
        str(value)
        .replace("[[", "\uff3b\uff3b")
        .replace("]]", "\uff3d\uff3d")
        .replace("\n", " ")
        .strip()
        or "\u672a\u547d\u540d\u9879"
    )


def _frozen_step_labels(plan: AnalysisPlan | None) -> dict[str, str]:
    if plan is None:
        return {}
    labels: dict[str, str] = {}
    for collection_name in _PLAN_STEP_COLLECTIONS:
        for step in getattr(plan, collection_name):
            labels[step.step_id] = _safe_label(step.name)
    return labels


def _execution_label(
    execution: ExecutionRecord,
    frozen_step_labels: Mapping[str, str],
) -> str:
    return _safe_label(
        frozen_step_labels.get(execution.plan_step_id)
        or (
            frozen_step_labels.get(execution.check_id)
            if execution.check_id is not None
            else None
        )
        or execution.check_id
        or execution.plan_step_id
    )


def _estimate_statement(
    execution_id: str,
    execution_label: str,
    execution_index: int,
    estimate_index: int,
    estimate: Mapping[str, Any],
) -> ManuscriptStatement | None:
    value_specs: list[tuple[str, str, Any]] = []
    for field, kind in (
        ("coefficient", "coefficient"),
        ("standard_error", "standard_error"),
        ("p_value", "p_value"),
    ):
        if estimate.get(field) is not None:
            value_specs.append((field, kind, estimate[field]))
    interval = estimate.get("confidence_interval_95")
    if isinstance(interval, Sequence) and not isinstance(interval, (str, bytes)):
        if len(interval) == 2 and interval[0] is not None and interval[1] is not None:
            value_specs.extend(
                (
                    ("confidence_interval_95/0", "interval_bound", interval[0]),
                    ("confidence_interval_95/1", "interval_bound", interval[1]),
                )
            )
    if not value_specs:
        return None
    values = [
        _protected_value(
            value_kind=kind,
            source_kind="execution",
            source_id=execution_id,
            source_path=(
                f"/executions/{execution_index}/estimates/{estimate_index}/{field}"
            ),
            raw_value=raw,
        )
        for field, kind, raw in value_specs
    ]
    values_by_kind: dict[str, list[ProtectedValue]] = {}
    for value in values:
        values_by_kind.setdefault(value.value_kind, []).append(value)
    fragments: list[str] = []
    if "coefficient" in values_by_kind:
        fragments.append(
            f"\u7cfb\u6570\u4e3a [[VALUE:{values_by_kind['coefficient'][0].value_id}]]"
        )
    if "standard_error" in values_by_kind:
        fragments.append(
            f"\u6807\u51c6\u8bef\u4e3a [[VALUE:{values_by_kind['standard_error'][0].value_id}]]"
        )
    if "interval_bound" in values_by_kind:
        lower, upper = values_by_kind["interval_bound"]
        fragments.append(
            "95% \u7f6e\u4fe1\u533a\u95f4\u4e3a "
            f"[[VALUE:{lower.value_id}]] \u81f3 [[VALUE:{upper.value_id}]]"
        )
    if "p_value" in values_by_kind:
        fragments.append(
            f"p \u503c\u4e3a [[VALUE:{values_by_kind['p_value'][0].value_id}]]"
        )
    term = _safe_label(estimate.get("term", "\u672a\u547d\u540d\u9879"))
    return ManuscriptStatement(
        statement_id=_stable_id(
            "statement-estimate", execution_id, estimate_index
        ),
        statement_kind="estimate_fact",
        text_template=(
            f"冻结步骤“{execution_label}”中，{term} 的"
            + "，".join(fragments)
            + "。"
        ),
        protected_values=values,
        execution_ids=[execution_id],
    )


def _policy_diagnostic_statements(
    execution_id: str,
    execution_index: int,
    plan_step_id: str,
    diagnostics: Mapping[str, Any],
) -> list[ManuscriptStatement]:
    statements: list[ManuscriptStatement] = []
    structured_permutation = (
        plan_step_id == "check-policy-permutation-placebo"
        and diagnostics.get("scheme") is not None
    )
    for fact_id, source_field, value_kind, fixed_label in (
        _POLICY_DIAGNOSTIC_FACT_SPECS.get(plan_step_id, ())
    ):
        if structured_permutation:
            # The composite statement below binds the target sample, unit count,
            # repetitions, p-value resolution and interpretation boundary in one
            # source-checked disclosure. Avoid repeating the legacy scalar facts.
            continue
        raw_value = diagnostics.get(source_field)
        if raw_value is None:
            continue
        value = _protected_value(
            value_kind=value_kind,
            source_kind="execution",
            source_id=execution_id,
            source_path=(
                f"/executions/{execution_index}/diagnostic_results/{source_field}"
            ),
            raw_value=raw_value,
        )
        statements.append(
            ManuscriptStatement(
                statement_id=_stable_id(
                    "statement-policy-diagnostic", execution_id, fact_id
                ),
                statement_kind="diagnostic_fact",
                text_template=(
                    f"{fixed_label}为 [[VALUE:{value.value_id}]]。"
                ),
                protected_values=[value],
                execution_ids=[execution_id],
            )
        )
    statements.extend(
        _policy_composite_diagnostic_statements(
            execution_id,
            execution_index,
            plan_step_id,
            diagnostics,
        )
    )
    return statements


def _diagnostic_value(
    execution_id: str,
    execution_index: int,
    diagnostics: Mapping[str, Any],
    field: str,
    value_kind: str,
) -> ProtectedValue:
    if field not in diagnostics or diagnostics[field] is None:
        raise ManuscriptIRError(
            f"{field} is required for a code-owned policy disclosure"
        )
    return _protected_value(
        value_kind=value_kind,
        source_kind="execution",
        source_id=execution_id,
        source_path=(
            f"/executions/{execution_index}/diagnostic_results/{field}"
        ),
        raw_value=diagnostics[field],
    )


def _diagnostic_year_values(
    execution_id: str,
    execution_index: int,
    diagnostics: Mapping[str, Any],
    field: str,
) -> list[ProtectedValue]:
    raw_years = diagnostics.get(field)
    if (
        not isinstance(raw_years, Sequence)
        or isinstance(raw_years, (str, bytes))
        or not raw_years
    ):
        raise ManuscriptIRError(
            f"{field} must be a non-empty year list for a code-owned policy disclosure"
        )
    return [
        _protected_value(
            value_kind="year",
            source_kind="execution",
            source_id=execution_id,
            source_path=(
                f"/executions/{execution_index}/diagnostic_results/{field}/{index}"
            ),
            raw_value=year,
        )
        for index, year in enumerate(raw_years)
    ]


def _fixed_pre_attrition_statement(
    execution_id: str,
    execution_index: int,
    diagnostics: Mapping[str, Any],
) -> ManuscriptStatement:
    if diagnostics.get("group_assignment_mode") != "fixed_last_pre_policy":
        raise ManuscriptIRError(
            "fixed-pre disclosure requires group_assignment_mode=fixed_last_pre_policy"
        )
    values = {
        field: _diagnostic_value(
            execution_id,
            execution_index,
            diagnostics,
            field,
            "count",
        )
        for field in (
            "rows_used",
            "rows_input",
            "rows_dropped_for_group_assignment",
            "entities_dropped_no_pre_policy_group",
        )
    }
    rows_used = int(values["rows_used"].raw_value)
    rows_input = int(values["rows_input"].raw_value)
    rows_dropped = int(
        values["rows_dropped_for_group_assignment"].raw_value
    )
    entities_dropped = int(
        values["entities_dropped_no_pre_policy_group"].raw_value
    )
    if (
        not (0 < rows_used <= rows_input)
        or rows_dropped < 0
        or rows_used + rows_dropped != rows_input
        or entities_dropped < 0
    ):
        raise ManuscriptIRError("fixed-pre attrition diagnostics are inconsistent")
    if rows_used < rows_input or rows_dropped or entities_dropped:
        boundary = (
            "该步骤改变了样本构成，不能表述为同一样本稳健性检验。"
        )
    else:
        boundary = "该步骤没有改变冻结估计样本。"
    return ManuscriptStatement(
        statement_id=_stable_id(
            "statement-policy-disclosure", execution_id, "fixed-pre-attrition"
        ),
        statement_kind="diagnostic_fact",
        text_template=(
            "政策前最后观测固定分组使用 "
            f"[[VALUE:{values['rows_used'].value_id}]]/"
            f"[[VALUE:{values['rows_input'].value_id}]] 行；"
            "分组环节删去 "
            f"[[VALUE:{values['rows_dropped_for_group_assignment'].value_id}]] 行，"
            "并因缺少政策前组别删去 "
            f"[[VALUE:{values['entities_dropped_no_pre_policy_group'].value_id}]] "
            f"个实体。{boundary}"
        ),
        protected_values=list(values.values()),
        execution_ids=[execution_id],
    )


def _event_design_disclosure_statement(
    execution_id: str,
    execution_index: int,
    diagnostics: Mapping[str, Any],
) -> ManuscriptStatement | None:
    fragments: list[str] = []
    protected_values: list[ProtectedValue] = []

    remote_requested = diagnostics.get("remote_pre_requested")
    if remote_requested is not None:
        if remote_requested is not True:
            if remote_requested is not False:
                raise ManuscriptIRError("remote_pre_requested must be boolean")
        else:
            if (
                diagnostics.get("remote_pre_status") != "complete"
                or diagnostics.get("remote_pre_complete") is not True
                or diagnostics.get("remote_pre_term") != "event_remote_pre"
                or diagnostics.get("collinear_remote_pre") is not False
                or diagnostics.get("unavailable_remote_pre_years") != []
            ):
                raise ManuscriptIRError(
                    "requested remote-pre disclosure requires a complete, non-collinear bin"
                )
            requested_years = _diagnostic_year_values(
                execution_id,
                execution_index,
                diagnostics,
                "requested_remote_pre_years",
            )
            generated_years = diagnostics.get("generated_remote_pre_years")
            if generated_years != [value.raw_value for value in requested_years]:
                raise ManuscriptIRError(
                    "generated_remote_pre_years must match the frozen requested years"
                )
            protected_values.extend(requested_years)
            rendered_years = "、".join(
                f"[[VALUE:{value.value_id}]]" for value in requested_years
            )
            fragments.append(
                "远端政策前合并项状态为完整，覆盖 "
                f"{rendered_years} 年，并作为事件期组间差异系数进入联合政策前检验"
            )

    policy_year_requested = diagnostics.get("policy_year_event_requested")
    if policy_year_requested is not None:
        if policy_year_requested is not True:
            if policy_year_requested is not False:
                raise ManuscriptIRError(
                    "policy_year_event_requested must be boolean"
                )
        else:
            policy_year = _diagnostic_value(
                execution_id,
                execution_index,
                diagnostics,
                "policy_start_year",
                "year",
            )
            expected_term = f"event_{int(policy_year.raw_value)}"
            if (
                diagnostics.get("event_term_scaling")
                != "binary_group_year_contrast"
                or diagnostics.get("policy_year_event_term") != expected_term
                or diagnostics.get(
                    "policy_year_event_coefficient_directly_comparable_to_baseline"
                )
                is not False
            ):
                raise ManuscriptIRError(
                    "policy-year event disclosure has inconsistent scaling or comparability"
                )
            protected_values.append(policy_year)
            fragments.append(
                f"政策年 [[VALUE:{policy_year.value_id}]] 的事件项是二元处理组×年份对比，"
                "不是基准 policy_exposure 的每单位系数，两者数值不可直接比较"
            )

    if not fragments:
        return None
    return ManuscriptStatement(
        statement_id=_stable_id(
            "statement-policy-disclosure", execution_id, "event-design"
        ),
        statement_kind="diagnostic_fact",
        text_template="；".join(fragments) + "。",
        protected_values=protected_values,
        execution_ids=[execution_id],
    )


def _clean_fake_time_statement(
    execution_id: str,
    execution_index: int,
    diagnostics: Mapping[str, Any],
) -> ManuscriptStatement:
    if diagnostics.get("status") != "succeeded":
        raise ManuscriptIRError(
            "fake-time disclosure requires status=succeeded"
        )
    values = {
        field: _diagnostic_value(
            execution_id,
            execution_index,
            diagnostics,
            field,
            value_kind,
        )
        for field, value_kind in (
            ("sample_start_year", "year"),
            ("sample_end_year", "year"),
            ("policy_start_year", "year"),
            ("rows_used", "count"),
            ("rows_excluded_at_or_after_true_policy", "count"),
            ("true_policy_contamination_rows", "count"),
        )
    }
    if (
        int(values["sample_start_year"].raw_value)
        > int(values["sample_end_year"].raw_value)
        or int(values["sample_end_year"].raw_value)
        >= int(values["policy_start_year"].raw_value)
        or int(values["rows_used"].raw_value) <= 0
        or int(values["rows_excluded_at_or_after_true_policy"].raw_value) < 0
        or int(values["true_policy_contamination_rows"].raw_value) != 0
        or diagnostics.get("pseudo_pre_support") is not True
        or diagnostics.get("pseudo_post_support") is not True
    ):
        raise ManuscriptIRError(
            "fake-time diagnostics do not establish a clean pre-policy placebo sample"
        )
    return ManuscriptStatement(
        statement_id=_stable_id(
            "statement-policy-disclosure", execution_id, "clean-fake-time"
        ),
        statement_kind="diagnostic_fact",
        text_template=(
            "真政策起始年为 "
            f"[[VALUE:{values['policy_start_year'].value_id}]] 年；"
            "伪政策时点检验仅使用 "
            f"[[VALUE:{values['sample_start_year'].value_id}]]—"
            f"[[VALUE:{values['sample_end_year'].value_id}]] 年的 "
            f"[[VALUE:{values['rows_used'].value_id}]] 行；"
            "排除真政策期及以后 "
            f"[[VALUE:{values['rows_excluded_at_or_after_true_policy'].value_id}]] "
            "行，真政策期污染行为 "
            f"[[VALUE:{values['true_policy_contamination_rows'].value_id}]]，"
            "因此该伪时点结果不含真政策期观察。"
        ),
        protected_values=list(values.values()),
        execution_ids=[execution_id],
    )


def _permutation_design_statement(
    execution_id: str,
    execution_index: int,
    diagnostics: Mapping[str, Any],
) -> ManuscriptStatement:
    if (
        diagnostics.get("status") != "succeeded"
        or diagnostics.get("scheme") != "assignment_unit_label"
        or diagnostics.get("group_assignment_mode")
        != "fixed_last_pre_policy"
        or not isinstance(diagnostics.get("permutation_unit_field"), str)
        or not diagnostics.get("permutation_unit_field")
    ):
        raise ManuscriptIRError(
            "permutation disclosure requires a succeeded fixed-pre assignment-unit design"
        )
    values = {
        field: _diagnostic_value(
            execution_id,
            execution_index,
            diagnostics,
            field,
            value_kind,
        )
        for field, value_kind in (
            ("rows_used", "count"),
            ("rows_input", "count"),
            ("permutation_unit_count", "count"),
            ("treated_permutation_unit_count", "count"),
            ("repetitions_requested", "count"),
            ("repetitions_completed", "count"),
            ("extreme_count", "count"),
            ("empirical_p_value", "p_value"),
        )
    }
    rows_used = int(values["rows_used"].raw_value)
    rows_input = int(values["rows_input"].raw_value)
    units = int(values["permutation_unit_count"].raw_value)
    treated_units = int(values["treated_permutation_unit_count"].raw_value)
    requested = int(values["repetitions_requested"].raw_value)
    completed = int(values["repetitions_completed"].raw_value)
    extreme_count = int(values["extreme_count"].raw_value)
    empirical_p_value = float(values["empirical_p_value"].raw_value)
    expected_p_value = (extreme_count + 1) / (completed + 1)
    if (
        not (0 < rows_used <= rows_input)
        or not (0 < treated_units <= units)
        or requested <= 0
        or completed != requested
        or not (0 <= extreme_count <= completed)
        or not math.isclose(
            empirical_p_value,
            expected_p_value,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        raise ManuscriptIRError("permutation diagnostics are internally inconsistent")
    resolution = (
        "由于极端次数为零，本次经验 p 值等于当前重复次数与加一校正下"
        "可报告的最小分辨率。"
        if extreme_count == 0
        else "当前重复次数与加一校正共同限定了经验 p 值的最小分辨率。"
    )
    return ManuscriptStatement(
        statement_id=_stable_id(
            "statement-policy-disclosure", execution_id, "permutation-design"
        ),
        statement_kind="diagnostic_fact",
        text_template=(
            "分配单元标签置换以政策前最后观测固定分组样本为目标，使用 "
            f"[[VALUE:{values['rows_used'].value_id}]]/"
            f"[[VALUE:{values['rows_input'].value_id}]] 行、"
            f"[[VALUE:{values['permutation_unit_count'].value_id}]] 个分配单元"
            "（处理单元 "
            f"[[VALUE:{values['treated_permutation_unit_count'].value_id}]] 个），请求 "
            f"[[VALUE:{values['repetitions_requested'].value_id}]] 次并完成 "
            f"[[VALUE:{values['repetitions_completed'].value_id}]] 次置换；"
            f"极端次数为 [[VALUE:{values['extreme_count'].value_id}]]，"
            "加一校正的双侧经验 p 值为 "
            f"[[VALUE:{values['empirical_p_value'].value_id}]]。{resolution}"
            "该检验仅在分配单元于冻结设计下可交换时才可作随机化式"
            "敏感性解释，不能单独建立因果识别。"
        ),
        protected_values=list(values.values()),
        execution_ids=[execution_id],
    )


def _policy_composite_diagnostic_statements(
    execution_id: str,
    execution_index: int,
    plan_step_id: str,
    diagnostics: Mapping[str, Any],
) -> list[ManuscriptStatement]:
    if plan_step_id == "check-policy-group-fixed-pre":
        return [
            _fixed_pre_attrition_statement(
                execution_id, execution_index, diagnostics
            )
        ]
    if plan_step_id == "check-policy-event-study":
        statement = _event_design_disclosure_statement(
            execution_id, execution_index, diagnostics
        )
        return [statement] if statement is not None else []
    if plan_step_id == "check-policy-placebo-time":
        return [
            _clean_fake_time_statement(
                execution_id, execution_index, diagnostics
            )
        ]
    if (
        plan_step_id == "check-policy-permutation-placebo"
        and diagnostics.get("scheme") is not None
    ):
        return [
            _permutation_design_statement(
                execution_id, execution_index, diagnostics
            )
        ]
    return []


def reproduction_scope_disclosure(
    reproduction_audit: ReproductionAudit | None,
) -> str | None:
    if (
        reproduction_audit is None
        or reproduction_audit.independence_scope != "estimator_only"
    ):
        return None
    shared_labels = list(
        dict.fromkeys(
            _REPRODUCTION_SHARED_COMPONENT_LABELS.get(
                component, _safe_label(component)
            )
            for component in reproduction_audit.shared_components
        )
    )
    shared_text = (
        "、".join(shared_labels)
        if shared_labels
        else "审计记录所列数据准备与变量构造组件"
    )
    return (
        "本轮复算的独立性仅覆盖估计器与协方差实现；"
        f"{shared_text}仍与主流程共享，因此不得将该复算表述为端到端独立复现。"
    )


def _reproduction_scope_statement(
    reproduction_audit: ReproductionAudit | None,
) -> ManuscriptStatement | None:
    disclosure = reproduction_scope_disclosure(reproduction_audit)
    if disclosure is None or reproduction_audit is None:
        return None
    return ManuscriptStatement(
        statement_id=_stable_id(
            "statement-reproduction-scope",
            reproduction_audit.audit_id,
            reproduction_audit.independence_scope,
            *reproduction_audit.shared_components,
        ),
        statement_kind="diagnostic_fact",
        text_template=disclosure,
    )


def build_statement_registry(
    ledger: ClaimLedger,
    run: ResearchRun,
    verified_passages: Sequence[VerifiedPassageRef] = (),
    *,
    analysis_plan: AnalysisPlan | None = None,
    reproduction_audit: ReproductionAudit | None = None,
    allowed_estimate_terms: set[str] | None = None,
) -> tuple[ManuscriptStatement, ...]:
    """Build the canonical statement set without model calls or I/O."""

    statements: list[ManuscriptStatement] = []
    frozen_step_labels = _frozen_step_labels(analysis_plan)
    seen_sample_counts: set[str] = set()
    approved_claims = [
        claim
        for claim in ledger.claims
        if claim.approval_status in {"approved", "downgraded"}
    ]
    for claim_index, claim in enumerate(ledger.claims):
        if claim not in approved_claims:
            continue
        source_field = "final_text" if claim.final_text is not None else "claim_text"
        raw_text = claim.final_text or claim.claim_text
        value = _protected_value(
            value_kind="claim_text",
            source_kind="claim",
            source_id=claim.claim_id,
            source_path=f"/claims/{claim_index}/{source_field}",
            raw_value=raw_text,
        )
        statements.append(
            ManuscriptStatement(
                statement_id=_stable_id("statement-claim", claim.claim_id),
                statement_kind="authorized_claim",
                text_template=f"[[VALUE:{value.value_id}]]",
                protected_values=[value],
                claim_ids=[claim.claim_id],
            )
        )

    bound_run_ids = {
        run_id
        for claim in approved_claims
        for run_id in [*claim.supporting_runs, *claim.opposing_runs]
    }
    explicit_execution_ids = {
        run_id
        for run_id in bound_run_ids
        if run_id != run.research_run_id
    }
    all_executions_authorized = run.research_run_id in bound_run_ids
    for execution_index, execution in enumerate(run.executions):
        if execution.execution_status != "succeeded":
            continue
        if not approved_claims:
            continue
        execution_label = _execution_label(execution, frozen_step_labels)
        policy_diagnostic_statements = _policy_diagnostic_statements(
            execution.execution_id,
            execution_index,
            execution.plan_step_id,
            execution.diagnostic_results,
        )
        if (
            not all_executions_authorized
            and execution.execution_id not in explicit_execution_ids
        ):
            # Frozen policy diagnostics have a narrow, code-owned allowlist and
            # describe identification limits rather than supplying an estimate.
            # Keep these facts available even when Claim Gate binds only the
            # supporting/opposing estimate executions to the final claim.
            statements.extend(policy_diagnostic_statements)
            continue
        for estimate_index, estimate in enumerate(execution.estimates):
            term = str(estimate.get("term", ""))
            if allowed_estimate_terms is not None and term not in allowed_estimate_terms:
                continue
            statement = _estimate_statement(
                execution.execution_id,
                execution_label,
                execution_index,
                estimate_index,
                estimate,
            )
            if statement is not None:
                statements.append(statement)
        statements.extend(policy_diagnostic_statements)
        rows_used = execution.diagnostic_results.get("rows_used")
        if rows_used is not None:
            value = _protected_value(
                value_kind="count",
                source_kind="execution",
                source_id=execution.execution_id,
                source_path=(
                    f"/executions/{execution_index}/diagnostic_results/rows_used"
                ),
                raw_value=rows_used,
            )
            if value.rendered_value not in seen_sample_counts:
                seen_sample_counts.add(value.rendered_value)
                statements.append(
                    ManuscriptStatement(
                        statement_id=_stable_id(
                            "statement-sample", execution.execution_id
                        ),
                        statement_kind="sample_fact",
                        text_template=(
                            f"冻结步骤“{execution_label}”的有效样本量为 "
                            f"[[VALUE:{value.value_id}]]。"
                        ),
                        protected_values=[value],
                        execution_ids=[execution.execution_id],
                    )
                )
        for field in (
            "r_squared_model",
            "r_squared_within",
            "r_squared_between",
            "r_squared_overall",
            "r_squared_inclusive",
            "r_squared_adjusted_inclusive",
        ):
            raw = execution.diagnostic_results.get(field)
            if raw is None:
                continue
            value = _protected_value(
                value_kind="fit_statistic",
                source_kind="execution",
                source_id=execution.execution_id,
                source_path=(
                    f"/executions/{execution_index}/diagnostic_results/{field}"
                ),
                raw_value=raw,
            )
            statements.append(
                ManuscriptStatement(
                    statement_id=_stable_id(
                        "statement-diagnostic", execution.execution_id, field
                    ),
                    statement_kind="diagnostic_fact",
                    text_template=(
                        f"冻结步骤“{execution_label}”的 {field} 为 "
                        f"[[VALUE:{value.value_id}]]。"
                    ),
                    protected_values=[value],
                    execution_ids=[execution.execution_id],
                )
            )

    reproduction_statement = _reproduction_scope_statement(reproduction_audit)
    if approved_claims and reproduction_statement is not None:
        statements.append(reproduction_statement)

    for passage_index, passage in enumerate(verified_passages):
        value = _protected_value(
            value_kind="passage_quote",
            source_kind="passage",
            source_id=passage.passage_id,
            source_path=f"/passages/{passage_index}/citation_render",
            raw_value=passage.citation_render,
        )
        statements.append(
            ManuscriptStatement(
                statement_id=_stable_id("statement-citation", passage.passage_id),
                statement_kind="citation",
                text_template=f"[[VALUE:{value.value_id}]]",
                protected_values=[value],
                citation_passage_ids=[passage.passage_id],
            )
        )

    ids = [statement.statement_id for statement in statements]
    if len(ids) != len(set(ids)):
        raise ManuscriptIRError("statement registry contains duplicate ids")
    return tuple(statements)


def writer_statement_catalog(
    statements: Sequence[ManuscriptStatement],
) -> list[dict[str, Any]]:
    """Return an LLM-safe catalog with no protected text or statistical values."""

    return [
        {
            "statement_id": statement.statement_id,
            "statement_kind": statement.statement_kind,
            "claim_ids": list(statement.claim_ids),
            "execution_ids": list(statement.execution_ids),
            "instruction": "\u5982\u9700\u4f7f\u7528\u8be5\u4e8b\u5b9e\uff0c\u53ea\u8f93\u51fa\u5b8c\u6574 statement \u951a\u70b9\uff0c\u4e0d\u5f97\u624b\u6284\u5185\u5bb9\u6216\u6570\u5b57\u3002",
        }
        for statement in statements
    ]


def required_statements_by_section(
    statements: Sequence[ManuscriptStatement],
) -> dict[str, list[str]]:
    by_kind: dict[str, list[str]] = {}
    for statement in statements:
        by_kind.setdefault(statement.statement_kind, []).append(statement.statement_id)
    claims = by_kind.get("authorized_claim", [])
    samples = by_kind.get("sample_fact", [])
    reproduction_limits = [
        statement.statement_id
        for statement in statements
        if statement.statement_id.startswith("statement-reproduction-scope-")
    ]
    empirical = [
        statement.statement_id
        for statement in statements
        if statement.statement_kind
        in {"authorized_claim", "estimate_fact", "sample_fact", "diagnostic_fact"}
        and statement.statement_id not in reproduction_limits
    ]
    return {
        "abstract": [*claims, *samples],
        "introduction": [],
        "theory_hypotheses": [],
        "data_variables": samples,
        "research_design": [],
        "empirical_results": empirical,
        "discussion_limitations": [*claims, *reproduction_limits],
        "conclusion": claims,
    }


def _masked_anchors(template: str) -> str:
    return STATEMENT_ANCHOR_RE.sub(" ", template)


def _validate_writer_template(
    template: str,
    section_id: str,
    *,
    allowed_numeric_literals: set[str] | frozenset[str] = frozenset(),
) -> None:
    masked = _masked_anchors(template)
    if "[[STATEMENT:" in masked or "[[VALUE:" in masked:
        raise ManuscriptIRError("template contains a malformed or forbidden anchor")
    numeric_masked = BARE_NUMBER_RE.sub(
        lambda match: (
            " "
            if match.group(0) in allowed_numeric_literals
            else match.group(0)
        ),
        masked,
    )
    if BARE_NUMBER_RE.search(numeric_masked):
        raise ManuscriptIRError("writer template contains a bare numeric value")
    if any(pattern.search(masked) for pattern in FORMAL_CITATION_PATTERNS):
        raise ManuscriptIRError("writer template contains an unauthorized formal citation")
    if any(pattern.search(masked) for pattern in EMPIRICAL_JUDGMENT_PATTERNS):
        raise ManuscriptIRError("writer template contains a new empirical judgment")
    if section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS and any(
        pattern.search(masked)
        for pattern in TRACEABLE_EMPIRICAL_PREDICATE_PATTERNS
    ):
        raise ManuscriptIRError("writer template contains a new empirical judgment")
    if (
        section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
        and causal_wording_violations(masked, "associational")
    ):
        raise ManuscriptIRError("writer template contains an unauthorized causal assertion")


def render_statement(statement: ManuscriptStatement) -> str:
    values = {value.value_id: value for value in statement.protected_values}
    if len(values) != len(statement.protected_values):
        raise ManuscriptIRError(
            f"statement {statement.statement_id} contains duplicate protected values"
        )
    anchor_ids = VALUE_ANCHOR_RE.findall(statement.text_template)
    counts = Counter(anchor_ids)
    if set(anchor_ids) != set(values):
        raise ManuscriptIRError(
            f"statement {statement.statement_id} has missing or unknown value anchors"
        )
    if any(count != 1 for count in counts.values()):
        raise ManuscriptIRError(
            f"statement {statement.statement_id} repeats a protected value anchor"
        )
    rendered = statement.text_template
    for value_id, value in values.items():
        expected = format_protected_value(value.value_kind, value.raw_value)
        if value.rendered_value != expected:
            raise ManuscriptIRError(
                f"protected value {value.value_id} has an invalid rendering"
            )
        rendered = rendered.replace(f"[[VALUE:{value_id}]]", expected)
    if "[[VALUE:" in rendered:
        raise ManuscriptIRError(
            f"statement {statement.statement_id} contains a malformed value anchor"
        )
    return rendered


def compile_section_draft(
    draft: ManuscriptSectionDraft,
    statements: Sequence[ManuscriptStatement],
    *,
    title: str,
    required_statement_ids: Sequence[str],
    research_run_id: str | None = None,
    allowed_numeric_literals: set[str] | frozenset[str] = frozenset(),
) -> ManuscriptSection:
    registry = {statement.statement_id: statement for statement in statements}
    if len(registry) != len(statements):
        raise ManuscriptIRError("statement registry contains duplicate ids")
    required = list(required_statement_ids)
    if len(required) != len(set(required)):
        raise ManuscriptIRError("required statement ids contain duplicates")
    unknown_required = [statement_id for statement_id in required if statement_id not in registry]
    if unknown_required:
        raise ManuscriptIRError(
            "required statement is not in the registry: " + ", ".join(unknown_required)
        )
    anchors = STATEMENT_ANCHOR_RE.findall(draft.content_template)
    counts = Counter(anchors)
    unknown_anchors = [statement_id for statement_id in anchors if statement_id not in registry]
    if unknown_anchors:
        raise ManuscriptIRError(
            "unknown statement anchor: " + ", ".join(sorted(set(unknown_anchors)))
        )
    duplicates = [statement_id for statement_id, count in counts.items() if count != 1]
    if duplicates:
        raise ManuscriptIRError(
            "statement anchor must appear exactly once: " + ", ".join(duplicates)
        )
    missing = [statement_id for statement_id in required if counts[statement_id] != 1]
    extras = [statement_id for statement_id in anchors if statement_id not in set(required)]
    if missing:
        raise ManuscriptIRError(
            "missing required statement anchor: " + ", ".join(missing)
        )
    if extras:
        raise ManuscriptIRError(
            "unexpected statement anchor: " + ", ".join(sorted(set(extras)))
        )
    _validate_writer_template(
        draft.content_template,
        draft.section_id,
        allowed_numeric_literals=allowed_numeric_literals,
    )
    selected = [registry[statement_id] for statement_id in anchors]
    selected_by_id = {statement.statement_id: statement for statement in selected}

    def replace_statement_anchor(match: re.Match[str]) -> str:
        rendered = render_statement(selected_by_id[match.group(1)])
        suffix = match.string[match.end() :]
        next_character = next((char for char in suffix if not char.isspace()), "")
        if (
            rendered.endswith("。")
            and next_character
            and next_character in "，。；、！？：,.!?;:和及"
        ):
            rendered = rendered[:-1]
        return rendered

    content = STATEMENT_ANCHOR_RE.sub(
        replace_statement_anchor,
        draft.content_template,
    )
    if "[[STATEMENT:" in content:
        raise ManuscriptIRError("compiled content contains a malformed statement anchor")
    claim_ids = list(
        dict.fromkeys(
            claim_id for statement in selected for claim_id in statement.claim_ids
        )
    )
    execution_ids = list(
        dict.fromkeys(
            execution_id
            for statement in selected
            for execution_id in statement.execution_ids
        )
    )
    run_ids = ([research_run_id] if research_run_id and selected else []) + execution_ids
    return ManuscriptSection(
        section_id=draft.section_id,
        title=title,
        content_markdown=content,
        status="generated",
        claim_ids=claim_ids,
        run_ids=list(dict.fromkeys(run_ids)),
        content_template=draft.content_template,
        statements=selected,
    )


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ManuscriptIRError(f"invalid JSON Pointer: {pointer}")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as error:
                raise ManuscriptIRError(f"dangling JSON Pointer: {pointer}") from error
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ManuscriptIRError(f"dangling JSON Pointer: {pointer}")
    return current


def verify_statement_sources(
    statements: Sequence[ManuscriptStatement],
    ledger: ClaimLedger,
    run: ResearchRun,
    verified_passages: Sequence[VerifiedPassageRef] = (),
) -> None:
    roots = {
        "claim": ledger.model_dump(mode="json"),
        "execution": run.model_dump(mode="json"),
        "passage": {
            "passages": [passage.model_dump(mode="json") for passage in verified_passages]
        },
    }
    source_ids = {
        "claim": {claim.claim_id for claim in ledger.claims},
        "execution": {execution.execution_id for execution in run.executions},
        "passage": {passage.passage_id for passage in verified_passages},
    }
    for statement in statements:
        for value in statement.protected_values:
            if value.source_id not in source_ids[value.source_kind]:
                raise ManuscriptIRError(
                    f"protected value {value.value_id} has a dangling source id"
                )
            actual = _resolve_pointer(roots[value.source_kind], value.source_path)
            if actual != value.raw_value:
                raise ManuscriptIRError(
                    f"protected value {value.value_id} does not match its source"
                )
            expected = format_protected_value(value.value_kind, actual)
            if expected != value.rendered_value:
                raise ManuscriptIRError(
                    f"protected value {value.value_id} rendering does not match its source"
                )
        render_statement(statement)


def audit_manuscript_ir(
    manuscript: ManuscriptPackage,
    ledger: ClaimLedger,
    run: ResearchRun,
    verified_passages: Sequence[VerifiedPassageRef] = (),
    *,
    analysis_plan: AnalysisPlan | None = None,
    reproduction_audit: ReproductionAudit | None = None,
    allowed_estimate_terms: set[str] | None = None,
    allowed_numeric_literals: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    problems: list[str] = []
    if manuscript.ir_version != 1:
        return ["full manuscript must use Manuscript IR version 1"]
    canonical_sequence = build_statement_registry(
        ledger,
        run,
        verified_passages,
        analysis_plan=analysis_plan,
        reproduction_audit=reproduction_audit,
        allowed_estimate_terms=allowed_estimate_terms,
    )
    canonical = {
        statement.statement_id: statement for statement in canonical_sequence
    }
    required_by_section = required_statements_by_section(canonical_sequence)
    for section in manuscript.manuscript_sections:
        if section.status != "generated":
            continue
        if section.content_template is None:
            problems.append(f"{section.section_id}: missing content_template")
            continue
        stored_ids = [statement.statement_id for statement in section.statements]
        try:
            stored_empirical_ids = [
                statement_id
                for statement_id in stored_ids
                if statement_id in canonical
                and canonical[statement_id].statement_kind != "citation"
            ]
            expected_ids = required_by_section.get(section.section_id, [])
            if Counter(stored_empirical_ids) != Counter(expected_ids):
                raise ManuscriptIRError(
                    "stored statement ids differ from the rebuilt section requirements"
                )
            verify_statement_sources(section.statements, ledger, run, verified_passages)
            for statement in section.statements:
                expected = canonical.get(statement.statement_id)
                if expected is None:
                    raise ManuscriptIRError(
                        f"statement {statement.statement_id} is not in the rebuilt registry"
                    )
                if statement.model_dump(mode="json") != expected.model_dump(mode="json"):
                    raise ManuscriptIRError(
                        f"statement {statement.statement_id} was changed after registry build"
                    )
            rebuilt = compile_section_draft(
                ManuscriptSectionDraft(
                    section_id=section.section_id,
                    content_template=section.content_template,
                ),
                tuple(canonical.values()),
                title=section.title,
                required_statement_ids=stored_ids,
                research_run_id=run.research_run_id,
                allowed_numeric_literals=allowed_numeric_literals,
            )
            if rebuilt.content_markdown != section.content_markdown:
                raise ManuscriptIRError("compiled content differs from stored manuscript text")
            if rebuilt.claim_ids != section.claim_ids or rebuilt.run_ids != section.run_ids:
                raise ManuscriptIRError("compiled provenance differs from stored provenance")
        except ManuscriptIRError as error:
            problems.append(f"{section.section_id}: {error}")
    return problems


def rebuild_ir1_package(
    legacy: ManuscriptPackage,
    drafts: Sequence[ManuscriptSectionDraft],
    ledger: ClaimLedger,
    run: ResearchRun,
    *,
    required_by_section: Mapping[str, Sequence[str]],
    verified_passages: Sequence[VerifiedPassageRef] = (),
    analysis_plan: AnalysisPlan | None = None,
    reproduction_audit: ReproductionAudit | None = None,
    allowed_numeric_literals: set[str] | frozenset[str] = frozenset(),
) -> ManuscriptPackage:
    """Rebuild IR0 from fresh templates; legacy prose is never trusted or copied."""

    registry = build_statement_registry(
        ledger,
        run,
        verified_passages,
        analysis_plan=analysis_plan,
        reproduction_audit=reproduction_audit,
    )
    legacy_by_id = {section.section_id: section for section in legacy.manuscript_sections}
    sections = [
        compile_section_draft(
            draft,
            registry,
            title=legacy_by_id[draft.section_id].title,
            required_statement_ids=required_by_section.get(draft.section_id, ()),
            research_run_id=run.research_run_id,
            allowed_numeric_literals=allowed_numeric_literals,
        )
        for draft in drafts
    ]
    return legacy.model_copy(
        update={
            "version": legacy.version + 1,
            "manuscript_sections": sections,
            "audit_result": "not_run",
            "ir_version": 1,
        }
    )
