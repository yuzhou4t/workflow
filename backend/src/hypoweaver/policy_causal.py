from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from linearmodels.iv import AbsorbingLS
from scipy.stats import chi2, norm

from .models import ModelSpec


POLICY_PRIMARY_IMPLEMENTATION_ID = "linearmodels-absorbingls-policy-did-v2"
POLICY_REPRODUCTION_IMPLEMENTATION_ID = "numpy-multiway-within-policy-did-v2"
_REMOTE_PRE_TERM = "event_remote_pre"
_EVENT_TERM_SCALING = "binary_group_year_contrast"

_POLICY_DESIGN_REQUIRED_KEYS = frozenset(
    {
        "group_field",
        "time_field",
        "policy_start_year",
        "policy_start_weight",
        "exposure_name",
        "fixed_effects",
        "cluster_fields",
        "event_reference_year",
        "event_years",
        "event_term_scaling",
    }
)
_POLICY_DESIGN_OPTIONAL_KEYS = frozenset(
    {
        "policy_start_month",
        "post_start_weight",
        "cluster_composition",
        "event_remote_pre_years",
        "placebo_start_year",
        "placebo_repetitions",
        "permutation_scheme",
        "permutation_unit_field",
        "random_seed",
        "group_assignment_mode",
    }
)
_WITHIN_TOLERANCE = 1e-8
_WITHIN_MAX_ITERATIONS = 10_000


class PolicyCausalError(ValueError):
    """Base error for a rejected policy-causal contract or estimation."""


class PolicyContractError(PolicyCausalError):
    """Raised when ModelSpec.parameters does not contain a valid contract."""


class PolicyEstimationError(PolicyCausalError):
    """Raised when frozen data cannot identify the requested model."""


@dataclass(frozen=True)
class PolicyDesign:
    group_field: str
    time_field: str
    policy_start_year: int
    policy_start_month: int | None
    policy_start_weight: float
    post_start_weight: float
    exposure_name: str
    fixed_effects: tuple[str, ...]
    cluster_fields: tuple[str, ...]
    cluster_composition: str
    event_reference_year: int
    event_years: tuple[int, ...]
    event_remote_pre_years: tuple[int, ...]
    event_term_scaling: str
    placebo_start_year: int | None
    placebo_repetitions: int
    permutation_scheme: str
    permutation_unit_field: str | None
    random_seed: int
    group_assignment_mode: str


@dataclass(frozen=True)
class _FitResult:
    estimates: tuple[dict[str, Any], ...]
    terms: tuple[str, ...]
    covariance: np.ndarray
    nobs: int
    dropped_terms: tuple[str, ...]
    diagnostics: Mapping[str, Any]


@dataclass(frozen=True)
class _PreparedData:
    frame: pd.DataFrame
    rows_input: int
    rows_dropped: int
    observed_years: tuple[int, ...]
    missing_calendar_years: tuple[int, ...]
    cluster_codes: np.ndarray
    cluster_count: int
    design: PolicyDesign
    sample_flow: Mapping[str, Any]


def parse_policy_design(model: ModelSpec) -> PolicyDesign:
    """Parse and strongly validate the code-owned ``policy_design`` contract.

    The nested dictionary is intentionally strict. Unknown keys and silent
    disagreement with the surrounding ModelSpec are rejected before any data are
    read, so the executor never has to infer an estimand from prose.
    """

    raw = model.parameters.get("policy_design")
    if not isinstance(raw, dict):
        raise PolicyContractError(
            "ModelSpec.parameters.policy_design must be a dictionary."
        )
    keys = frozenset(raw)
    missing = sorted(_POLICY_DESIGN_REQUIRED_KEYS - keys)
    unknown = sorted(keys - _POLICY_DESIGN_REQUIRED_KEYS - _POLICY_DESIGN_OPTIONAL_KEYS)
    if missing:
        raise PolicyContractError(
            "policy_design is missing required keys: " + ", ".join(missing)
        )
    if unknown:
        raise PolicyContractError(
            "policy_design contains unknown keys: " + ", ".join(unknown)
        )

    group_field = _strict_name(raw["group_field"], "group_field")
    time_field = _strict_name(raw["time_field"], "time_field")
    exposure_name = _strict_name(raw["exposure_name"], "exposure_name")
    policy_start_year = _strict_int(raw["policy_start_year"], "policy_start_year")
    policy_start_month = _optional_int(
        raw.get("policy_start_month"), "policy_start_month"
    )
    if policy_start_month is not None and not 1 <= policy_start_month <= 12:
        raise PolicyContractError("policy_start_month must be between 1 and 12.")
    policy_start_weight = _strict_weight(
        raw["policy_start_weight"], "policy_start_weight", allow_zero=True
    )
    post_start_weight = _strict_weight(
        raw.get("post_start_weight", 1.0),
        "post_start_weight",
        allow_zero=False,
    )
    fixed_effects = _strict_name_list(raw["fixed_effects"], "fixed_effects")
    cluster_fields = _strict_name_list(raw["cluster_fields"], "cluster_fields")
    cluster_composition = raw.get("cluster_composition", "interaction")
    if cluster_composition != "interaction":
        raise PolicyContractError(
            "cluster_composition must be exactly 'interaction' in policy-did-v2."
        )
    event_reference_year = _strict_int(
        raw["event_reference_year"], "event_reference_year"
    )
    event_years = _strict_year_list(raw["event_years"], "event_years")
    event_remote_pre_years = _strict_year_list(
        raw.get("event_remote_pre_years", []),
        "event_remote_pre_years",
    )
    event_term_scaling = raw["event_term_scaling"]
    if event_term_scaling != _EVENT_TERM_SCALING:
        raise PolicyContractError(
            "event_term_scaling must be exactly "
            f"{_EVENT_TERM_SCALING!r} in policy-did-v2."
        )
    placebo_start_year = _optional_int(
        raw.get("placebo_start_year"), "placebo_start_year"
    )
    placebo_repetitions = _strict_int(
        raw.get("placebo_repetitions"), "placebo_repetitions"
    )
    permutation_scheme = raw.get(
        "permutation_scheme", "assignment_unit_label"
    )
    if permutation_scheme not in {
        "assignment_unit_label",
        "rowwise_exposure",
    }:
        raise PolicyContractError(
            "permutation_scheme must be assignment_unit_label or "
            "rowwise_exposure in policy-did-v2."
        )
    permutation_unit_field = (
        _strict_name(raw["permutation_unit_field"], "permutation_unit_field")
        if raw.get("permutation_unit_field") is not None
        else None
    )
    if permutation_scheme == "assignment_unit_label" and permutation_unit_field is None:
        raise PolicyContractError(
            "assignment_unit_label requires permutation_unit_field."
        )
    random_seed = _strict_int(raw.get("random_seed"), "random_seed")
    group_assignment_mode = raw.get(
        "group_assignment_mode", "observed_time_varying"
    )
    if group_assignment_mode not in {
        "observed_time_varying",
        "fixed_last_pre_policy",
        "stable_entities_only",
    }:
        raise PolicyContractError(
            "group_assignment_mode must be observed_time_varying, "
            "fixed_last_pre_policy, or stable_entities_only."
        )

    if group_field == time_field:
        raise PolicyContractError("group_field and time_field must be different.")
    if exposure_name in {group_field, time_field}:
        raise PolicyContractError(
            "exposure_name must be a derived field, not group_field or time_field."
        )
    if permutation_unit_field == time_field:
        raise PolicyContractError(
            "permutation_unit_field cannot be the time field."
        )
    if policy_start_weight > post_start_weight:
        raise PolicyContractError(
            "policy_start_weight cannot exceed post_start_weight."
        )
    if len(fixed_effects) < 2:
        raise PolicyContractError("fixed_effects must contain at least two fields.")
    if fixed_effects[0] == time_field or time_field not in fixed_effects:
        raise PolicyContractError(
            "fixed_effects must start with the entity field and include time_field."
        )
    if tuple(model.fixed_effects) != fixed_effects:
        raise PolicyContractError(
            "policy_design.fixed_effects must exactly match ModelSpec.fixed_effects."
        )
    if not cluster_fields:
        raise PolicyContractError("cluster_fields must not be empty.")
    if event_reference_year >= policy_start_year:
        raise PolicyContractError(
            "event_reference_year must be a pre-policy absolute year."
        )
    if event_reference_year in event_years:
        raise PolicyContractError(
            "event_years must not contain event_reference_year."
        )
    if event_reference_year in event_remote_pre_years:
        raise PolicyContractError(
            "event_remote_pre_years must not contain event_reference_year."
        )
    if not event_years:
        raise PolicyContractError("event_years must not be empty.")
    if event_remote_pre_years and max(event_remote_pre_years) >= min(event_years):
        raise PolicyContractError(
            "event_remote_pre_years must precede every explicit event_year."
        )
    if any(year >= policy_start_year for year in event_remote_pre_years):
        raise PolicyContractError(
            "event_remote_pre_years must all precede policy_start_year."
        )
    if not any(
        year < policy_start_year
        for year in (*event_remote_pre_years, *event_years)
    ):
        raise PolicyContractError(
            "the event design must request at least one non-reference pre-policy year."
        )
    if not any(year >= policy_start_year for year in event_years):
        raise PolicyContractError(
            "event_years must request at least one policy or post-policy year."
        )
    if placebo_start_year is not None and placebo_start_year >= policy_start_year:
        raise PolicyContractError(
            "placebo_start_year must precede policy_start_year."
        )
    if placebo_repetitions < 1 or placebo_repetitions > 500:
        raise PolicyContractError("placebo_repetitions must be in [1, 500].")
    if random_seed < 0 or random_seed > 2**32 - 1:
        raise PolicyContractError("random_seed must be in [0, 2**32 - 1].")
    if not model.outcome:
        raise PolicyContractError("policy DID ModelSpec must freeze one outcome.")
    if len(model.controls) != len(set(model.controls)):
        raise PolicyContractError("ModelSpec.controls must not contain duplicates.")
    if exposure_name in model.controls:
        raise PolicyContractError("exposure_name cannot also be a control.")
    if model.outcome in {
        group_field,
        time_field,
        exposure_name,
        *model.controls,
        *fixed_effects,
    }:
        raise PolicyContractError(
            "outcome must be distinct from policy fields, controls, and fixed effects."
        )
    if model.treatments_or_exposures != [exposure_name]:
        raise PolicyContractError(
            "ModelSpec.treatments_or_exposures must contain only exposure_name."
        )
    if "did" not in model.estimator.casefold() and "difference" not in model.estimator.casefold():
        raise PolicyContractError(
            "ModelSpec.estimator must explicitly identify a DID estimator."
        )
    if (
        model.standard_error_strategy is None
        or "cluster" not in model.standard_error_strategy.casefold()
    ):
        raise PolicyContractError(
            "policy DID requires an explicitly clustered standard_error_strategy."
        )

    return PolicyDesign(
        group_field=group_field,
        time_field=time_field,
        policy_start_year=policy_start_year,
        policy_start_month=policy_start_month,
        policy_start_weight=policy_start_weight,
        post_start_weight=post_start_weight,
        exposure_name=exposure_name,
        fixed_effects=fixed_effects,
        cluster_fields=cluster_fields,
        cluster_composition=cluster_composition,
        event_reference_year=event_reference_year,
        event_years=event_years,
        event_remote_pre_years=event_remote_pre_years,
        event_term_scaling=event_term_scaling,
        placebo_start_year=placebo_start_year,
        placebo_repetitions=placebo_repetitions,
        permutation_scheme=permutation_scheme,
        permutation_unit_field=permutation_unit_field,
        random_seed=random_seed,
        group_assignment_mode=group_assignment_mode,
    )


def construct_policy_exposure(
    frame: pd.DataFrame,
    design: PolicyDesign,
    *,
    start_year: int | None = None,
) -> pd.Series:
    """Construct group-by-policy exposure without adding or imputing rows."""

    selected_start = design.policy_start_year if start_year is None else start_year
    if design.group_field not in frame or design.time_field not in frame:
        raise PolicyEstimationError(
            "data are missing group_field or time_field for exposure construction."
        )
    groups = pd.to_numeric(frame[design.group_field], errors="coerce")
    times = pd.to_numeric(frame[design.time_field], errors="coerce")
    if groups.isna().any() or times.isna().any():
        raise PolicyEstimationError(
            "group_field and time_field must be numeric after sample cleaning."
        )
    invalid_groups = sorted(set(groups.unique()) - {0.0, 1.0})
    if invalid_groups:
        raise PolicyEstimationError("group_field must contain only 0 and 1.")
    weights = np.select(
        [times < selected_start, times == selected_start, times > selected_start],
        [0.0, design.policy_start_weight, design.post_start_weight],
        default=np.nan,
    )
    return pd.Series(
        groups.to_numpy(dtype=float) * weights,
        index=frame.index,
        name=design.exposure_name,
        dtype=float,
    )


def estimate_policy_model(path: Path, model: ModelSpec) -> dict[str, Any]:
    """Run the primary AbsorbingLS DID, event study, and optional placebo."""

    return _estimate_policy_model(
        path,
        model,
        implementation_id=POLICY_PRIMARY_IMPLEMENTATION_ID,
        fitter=_fit_absorbing_ls,
    )


def estimate_policy_baseline(path: Path, model: ModelSpec) -> dict[str, Any]:
    """Run only the requested baseline model for an alternative outcome step."""

    return _estimate_policy_model(
        path,
        model,
        implementation_id=POLICY_PRIMARY_IMPLEMENTATION_ID,
        fitter=_fit_absorbing_ls,
        include_event_study=False,
        include_placebo=False,
        include_permutation_placebo=False,
    )


def estimate_policy_core(path: Path, model: ModelSpec) -> dict[str, Any]:
    """Run baseline, event study and fake timing without a permutation draw."""

    return _estimate_policy_model(
        path,
        model,
        implementation_id=POLICY_PRIMARY_IMPLEMENTATION_ID,
        fitter=_fit_absorbing_ls,
        include_permutation_placebo=False,
    )


def estimate_policy_permutation(path: Path, model: ModelSpec) -> dict[str, Any]:
    """Run the frozen baseline plus only its assignment permutation."""

    return _estimate_policy_model(
        path,
        model,
        implementation_id=POLICY_PRIMARY_IMPLEMENTATION_ID,
        fitter=_fit_absorbing_ls,
        include_event_study=False,
        include_placebo=False,
        include_permutation_placebo=True,
    )


def reproduce_policy_model(path: Path, model: ModelSpec) -> dict[str, Any]:
    """Independently reproduce the frozen DID with NumPy within transforms."""

    return _estimate_policy_model(
        path,
        model,
        implementation_id=POLICY_REPRODUCTION_IMPLEMENTATION_ID,
        fitter=_fit_numpy_within,
        include_permutation_placebo=False,
    )


def reproduce_policy_baseline(path: Path, model: ModelSpec) -> dict[str, Any]:
    """Independently run only an alternative-outcome baseline model."""

    return _estimate_policy_model(
        path,
        model,
        implementation_id=POLICY_REPRODUCTION_IMPLEMENTATION_ID,
        fitter=_fit_numpy_within,
        include_event_study=False,
        include_placebo=False,
        include_permutation_placebo=False,
    )


def _estimate_policy_model(
    path: Path,
    model: ModelSpec,
    *,
    implementation_id: str,
    fitter: Callable[[_PreparedData, str, Sequence[str]], _FitResult],
    include_event_study: bool = True,
    include_placebo: bool = True,
    include_permutation_placebo: bool = True,
) -> dict[str, Any]:
    design = parse_policy_design(model)
    prepared = _prepare_data(Path(path), model, design)
    frame = prepared.frame
    frame[design.exposure_name] = construct_policy_exposure(frame, design)
    baseline_terms = [design.exposure_name, *model.controls]
    baseline = fitter(prepared, model.outcome or "", baseline_terms)
    if design.exposure_name not in baseline.terms:
        raise PolicyEstimationError(
            "the frozen policy exposure is absorbed or collinear in the baseline model."
        )

    event_study = (
        _run_event_study(prepared, model, fitter)
        if include_event_study
        else {
            "status": "not_requested",
            "reference_year": design.event_reference_year,
            "estimates": [],
        }
    )
    placebo = _run_placebo(prepared, model, fitter) if include_placebo else None
    primary_estimate = next(
        item for item in baseline.estimates if item["term"] == design.exposure_name
    )
    permutation_placebo = (
        _run_permutation_placebo(
            prepared,
            model,
            observed_coefficient=float(primary_estimate["coefficient"]),
        )
        if include_permutation_placebo
        else None
    )
    group_counts = {
        str(int(group)): int(count)
        for group, count in frame[design.group_field].value_counts().sort_index().items()
    }
    entity_field = design.fixed_effects[0]
    entity_groups = frame.groupby(entity_field, observed=True)[design.group_field].max()
    group_switcher_entities = int(
        (
            frame.groupby(entity_field, observed=True)[design.group_field]
            .nunique()
            .gt(1)
        ).sum()
    )
    entity_sizes = frame.groupby(entity_field, observed=True).size()
    entity_year_support = frame.groupby(entity_field, observed=True)[
        design.time_field
    ].agg(["min", "max"])
    diagnostics = {
        "rows_input": prepared.rows_input,
        "rows_used": int(len(frame)),
        "rows_dropped": prepared.rows_dropped,
        "entity_count": int(frame[entity_field].nunique()),
        "treated_entity_count": int((entity_groups == 1).sum()),
        "control_entity_count": int((entity_groups == 0).sum()),
        "group_switcher_entities": group_switcher_entities,
        "source_group_switcher_entities": int(
            prepared.sample_flow.get("source_group_switcher_entities", 0)
        ),
        "analysis_group_switcher_entities": group_switcher_entities,
        "group_assignment_mode": design.group_assignment_mode,
        "rows_dropped_for_group_assignment": int(
            prepared.sample_flow.get("rows_dropped_for_group_assignment", 0)
        ),
        "rows_dropped_for_missing_model_fields": int(
            prepared.sample_flow.get("rows_dropped_for_missing_model_fields", 0)
        ),
        "entities_dropped_no_pre_policy_group": int(
            prepared.sample_flow.get("entities_dropped_no_pre_policy_group", 0)
        ),
        "entities_dropped_as_group_switchers": int(
            prepared.sample_flow.get("entities_dropped_as_group_switchers", 0)
        ),
        "singleton_entities": int(entity_sizes.eq(1).sum()),
        "entities_spanning_policy": int(
            (
                entity_year_support["min"].lt(design.policy_start_year)
                & entity_year_support["max"].ge(design.policy_start_year)
            ).sum()
        ),
        "group_row_counts": group_counts,
        "observed_years": list(prepared.observed_years),
        "missing_calendar_years": list(prepared.missing_calendar_years),
        "calendar_years_imputed": [],
        "policy_start_year": design.policy_start_year,
        "policy_start_month": design.policy_start_month,
        "policy_start_weight": design.policy_start_weight,
        "post_start_weight": design.post_start_weight,
        "fixed_effects": list(design.fixed_effects),
        "cluster_fields": list(design.cluster_fields),
        "cluster_composition": design.cluster_composition,
        "cluster_count": prepared.cluster_count,
        "cluster_size_min": int(
            prepared.sample_flow.get("cluster_size_min", 0)
        ),
        "cluster_size_median": float(
            prepared.sample_flow.get("cluster_size_median", 0.0)
        ),
        "cluster_size_max": int(
            prepared.sample_flow.get("cluster_size_max", 0)
        ),
        "singleton_cluster_count": int(
            prepared.sample_flow.get("singleton_cluster_count", 0)
        ),
        "singleton_cluster_share": float(
            prepared.sample_flow.get("singleton_cluster_share", 0.0)
        ),
        "cluster_includes_time_field": design.time_field in design.cluster_fields,
        "entities_spanning_multiple_clusters": int(
            prepared.sample_flow.get("entities_spanning_multiple_clusters", 0)
        ),
        "fixed_effect_level_counts": dict(
            prepared.sample_flow.get("fixed_effect_level_counts", {})
        ),
        "fixed_effect_singleton_level_counts": dict(
            prepared.sample_flow.get("fixed_effect_singleton_level_counts", {})
        ),
        "placebo_repetitions": design.placebo_repetitions,
        "permutation_scheme": design.permutation_scheme,
        "random_seed": design.random_seed,
        "baseline_fit": dict(baseline.diagnostics),
    }
    return {
        "implementation_id": implementation_id,
        "model_step_id": model.step_id,
        "outcome": model.outcome,
        "exposure": design.exposure_name,
        "primary_estimate": primary_estimate,
        "estimates": list(baseline.estimates),
        "event_study": event_study,
        "placebo": placebo,
        "permutation_placebo": permutation_placebo,
        "diagnostics": diagnostics,
    }


def _prepare_data(
    path: Path,
    model: ModelSpec,
    design: PolicyDesign,
) -> _PreparedData:
    if not path.is_file():
        raise PolicyEstimationError(f"policy DID CSV does not exist: {path}")
    outcome = model.outcome or ""
    required = list(
        dict.fromkeys(
            [
                outcome,
                design.group_field,
                design.time_field,
                *model.controls,
                *design.fixed_effects,
                *design.cluster_fields,
                *(
                    [design.permutation_unit_field]
                    if design.permutation_unit_field is not None
                    else []
                ),
            ]
        )
    )
    frame = _read_csv(path, required)
    missing = [field for field in required if field not in frame]
    if missing:
        raise PolicyEstimationError(
            "data are missing frozen policy DID fields: " + ", ".join(missing)
        )
    rows_input = len(frame)
    numeric_fields = list(
        dict.fromkeys(
            [outcome, design.group_field, design.time_field, *model.controls]
        )
    )
    for field in numeric_fields:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    entity_field = design.fixed_effects[0]
    source_support = frame.dropna(
        subset=[entity_field, design.time_field, design.group_field]
    ).copy()
    source_times = source_support[design.time_field].to_numpy(dtype=float)
    if not np.all(np.equal(source_times, np.floor(source_times))):
        raise PolicyEstimationError("time_field must contain integer absolute years.")
    source_support[design.time_field] = source_times.astype(np.int64)
    invalid_source_groups = sorted(
        set(source_support[design.group_field].unique()) - {0.0, 1.0}
    )
    if invalid_source_groups:
        raise PolicyEstimationError("group_field must contain only 0 and 1.")
    source_group_counts = source_support.groupby(
        entity_field, observed=True
    )[design.group_field].nunique()
    source_group_switchers = int(source_group_counts.gt(1).sum())
    source_entities = set(source_support[entity_field].unique())
    dropped_no_pre = 0
    dropped_switchers = 0

    if design.group_assignment_mode == "fixed_last_pre_policy":
        pre_policy = source_support.loc[
            source_support[design.time_field] < design.policy_start_year
        ].sort_values([entity_field, design.time_field])
        last_pre = pre_policy.drop_duplicates(entity_field, keep="last").set_index(
            entity_field
        )[design.group_field]
        eligible_entities = set(last_pre.index)
        dropped_no_pre = len(source_entities - eligible_entities)
        frame = frame.loc[frame[entity_field].isin(eligible_entities)].copy()
        frame[design.group_field] = frame[entity_field].map(last_pre)
    elif design.group_assignment_mode == "stable_entities_only":
        stable_entities = set(source_group_counts[source_group_counts.eq(1)].index)
        dropped_switchers = len(source_entities - stable_entities)
        stable_group = (
            source_support.loc[source_support[entity_field].isin(stable_entities)]
            .drop_duplicates(entity_field, keep="first")
            .set_index(entity_field)[design.group_field]
        )
        frame = frame.loc[frame[entity_field].isin(stable_entities)].copy()
        frame[design.group_field] = frame[entity_field].map(stable_group)

    rows_after_group_assignment = len(frame)
    frame = frame.dropna(subset=required).copy()
    rows_dropped_missing = rows_after_group_assignment - len(frame)
    rows_dropped = rows_input - len(frame)
    if frame.empty:
        raise PolicyEstimationError("no complete observations remain for policy DID.")
    times = frame[design.time_field].to_numpy(dtype=float)
    if not np.all(np.equal(times, np.floor(times))):
        raise PolicyEstimationError("time_field must contain integer absolute years.")
    frame[design.time_field] = times.astype(np.int64)
    groups = set(frame[design.group_field].unique())
    if groups != {0.0, 1.0}:
        raise PolicyEstimationError(
            "the estimation sample must contain both group_field values 0 and 1."
        )
    frame[design.group_field] = frame[design.group_field].astype(np.int8)
    duplicate_rows = int(
        frame.duplicated([entity_field, design.time_field], keep=False).sum()
    )
    if duplicate_rows:
        raise PolicyEstimationError(
            f"entity-time key contains {duplicate_rows} duplicate rows."
        )
    observed_years = tuple(
        int(year) for year in sorted(frame[design.time_field].unique())
    )
    if design.policy_start_year not in observed_years:
        raise PolicyEstimationError("policy_start_year is absent from the data.")
    if design.event_reference_year not in observed_years:
        raise PolicyEstimationError("event_reference_year is absent from the data.")
    if not any(year < design.policy_start_year for year in observed_years):
        raise PolicyEstimationError("policy DID requires at least one pre-policy year.")
    if not any(year > design.policy_start_year for year in observed_years):
        raise PolicyEstimationError("policy DID requires at least one post-policy year.")
    reference_groups = set(
        frame.loc[
            frame[design.time_field] == design.event_reference_year,
            design.group_field,
        ].unique()
    )
    if reference_groups != {0, 1}:
        raise PolicyEstimationError(
            "event_reference_year must contain treated and control observations."
        )
    for field in design.fixed_effects:
        if frame[field].nunique() < 2:
            raise PolicyEstimationError(
                f"fixed effect {field!r} has fewer than two levels."
            )
    cluster_codes = _interaction_codes(frame, design.cluster_fields)
    cluster_count = int(np.unique(cluster_codes).size)
    if cluster_count < 2:
        raise PolicyEstimationError(
            "interaction clustering requires at least two observed clusters."
        )
    if len(frame) <= len(model.controls) + 2:
        raise PolicyEstimationError("too few observations remain for policy DID.")
    frame = frame.reset_index(drop=True)
    cluster_codes = _interaction_codes(frame, design.cluster_fields)
    cluster_sizes = pd.Series(cluster_codes).value_counts()
    fixed_effect_level_counts = {
        field: int(frame[field].nunique()) for field in design.fixed_effects
    }
    fixed_effect_singleton_level_counts = {
        field: int(frame[field].value_counts(dropna=False).eq(1).sum())
        for field in design.fixed_effects
    }
    entity_cluster_counts = (
        pd.DataFrame(
            {
                "entity": frame[entity_field].to_numpy(),
                "cluster": cluster_codes,
            }
        )
        .groupby("entity", observed=True)["cluster"]
        .nunique()
    )
    missing_calendar_years = tuple(
        year
        for year in range(min(observed_years), max(observed_years) + 1)
        if year not in observed_years
    )
    return _PreparedData(
        frame=frame,
        rows_input=rows_input,
        rows_dropped=rows_dropped,
        observed_years=observed_years,
        missing_calendar_years=missing_calendar_years,
        cluster_codes=cluster_codes,
        cluster_count=cluster_count,
        design=design,
        sample_flow={
            "source_group_switcher_entities": source_group_switchers,
            "rows_dropped_for_group_assignment": (
                rows_input - rows_after_group_assignment
            ),
            "rows_dropped_for_missing_model_fields": rows_dropped_missing,
            "entities_dropped_no_pre_policy_group": dropped_no_pre,
            "entities_dropped_as_group_switchers": dropped_switchers,
            "cluster_size_min": int(cluster_sizes.min()),
            "cluster_size_median": float(cluster_sizes.median()),
            "cluster_size_max": int(cluster_sizes.max()),
            "singleton_cluster_count": int(cluster_sizes.eq(1).sum()),
            "singleton_cluster_share": float(cluster_sizes.eq(1).mean()),
            "entities_spanning_multiple_clusters": int(
                entity_cluster_counts.gt(1).sum()
            ),
            "fixed_effect_level_counts": fixed_effect_level_counts,
            "fixed_effect_singleton_level_counts": (
                fixed_effect_singleton_level_counts
            ),
        },
    )


def _subset_prepared_data(
    prepared: _PreparedData,
    mask: pd.Series,
) -> _PreparedData:
    """Create an estimation-ready subset without changing the frozen source frame."""

    frame = prepared.frame.loc[mask].copy().reset_index(drop=True)
    if frame.empty:
        raise PolicyEstimationError("the frozen policy subset contains no rows.")
    design = prepared.design
    observed_years = tuple(
        int(year) for year in sorted(frame[design.time_field].unique())
    )
    missing_calendar_years = tuple(
        year
        for year in range(min(observed_years), max(observed_years) + 1)
        if year not in observed_years
    )
    cluster_codes = _interaction_codes(frame, design.cluster_fields)
    return _PreparedData(
        frame=frame,
        rows_input=prepared.rows_input,
        rows_dropped=prepared.rows_dropped + len(prepared.frame) - len(frame),
        observed_years=observed_years,
        missing_calendar_years=missing_calendar_years,
        cluster_codes=cluster_codes,
        cluster_count=int(np.unique(cluster_codes).size),
        design=design,
        sample_flow=prepared.sample_flow,
    )


def _run_event_study(
    prepared: _PreparedData,
    model: ModelSpec,
    fitter: Callable[[_PreparedData, str, Sequence[str]], _FitResult],
) -> dict[str, Any]:
    frame = prepared.frame
    design = prepared.design
    policy_year_term = f"event_{design.policy_start_year}"
    scaling_diagnostics = {
        "event_term_scaling": design.event_term_scaling,
        "policy_year_event_term": policy_year_term,
        "policy_year_event_requested": (
            design.policy_start_year in design.event_years
        ),
        "policy_year_event_regressor_weight": 1.0,
        "baseline_policy_start_weight": design.policy_start_weight,
        "policy_year_event_uses_baseline_policy_start_weight": False,
        "policy_year_event_coefficient_directly_comparable_to_baseline": False,
        "policy_year_event_comparability_note": (
            f"{policy_year_term} is a binary treated-by-year contrast, not the "
            "baseline per-unit policy_exposure coefficient; coefficient magnitudes "
            "must not be compared directly."
        ),
    }
    available: list[int] = []
    unavailable: list[int] = []
    event_terms: list[str] = []
    available_remote_pre: list[int] = []
    unavailable_remote_pre: list[int] = []
    for year in design.event_remote_pre_years:
        year_groups = set(
            frame.loc[frame[design.time_field] == year, design.group_field].unique()
        )
        if year not in prepared.observed_years or year_groups != {0, 1}:
            unavailable_remote_pre.append(year)
            continue
        available_remote_pre.append(year)
    if available_remote_pre:
        frame[_REMOTE_PRE_TERM] = (
            frame[design.time_field].isin(available_remote_pre)
            & frame[design.group_field].eq(1)
        ).astype(float)
        event_terms.append(_REMOTE_PRE_TERM)
    for year in design.event_years:
        year_groups = set(
            frame.loc[frame[design.time_field] == year, design.group_field].unique()
        )
        if year not in prepared.observed_years or year_groups != {0, 1}:
            unavailable.append(year)
            continue
        term = f"event_{year}"
        frame[term] = (
            (frame[design.time_field] == year) & (frame[design.group_field] == 1)
        ).astype(float)
        available.append(year)
        event_terms.append(term)
    if design.event_remote_pre_years and unavailable_remote_pre:
        return {
            "status": "not_executed",
            **scaling_diagnostics,
            "reference_year": design.event_reference_year,
            "requested_event_years": list(design.event_years),
            "generated_event_years": [],
            "unavailable_event_years": unavailable,
            "requested_remote_pre_years": list(design.event_remote_pre_years),
            "generated_remote_pre_years": [],
            "unavailable_remote_pre_years": unavailable_remote_pre,
            "remote_pre_term": None,
            "remote_pre_requested": True,
            "remote_pre_status": "incomplete",
            "remote_pre_complete": False,
            "collinear_remote_pre": False,
            "estimates": [],
            "joint_pretrend": {
                "status": "not_testable",
                "reason": (
                    "one or more frozen remote-pre years lack treated/control support"
                ),
                "terms": [],
            },
            "reason": "the frozen remote-pre bin is incomplete",
        }
    if not event_terms:
        return {
            "status": "not_executed",
            **scaling_diagnostics,
            "reference_year": design.event_reference_year,
            "requested_event_years": list(design.event_years),
            "generated_event_years": [],
            "unavailable_event_years": unavailable,
            "requested_remote_pre_years": list(design.event_remote_pre_years),
            "generated_remote_pre_years": [],
            "unavailable_remote_pre_years": unavailable_remote_pre,
            "remote_pre_term": None,
            "remote_pre_requested": bool(design.event_remote_pre_years),
            "remote_pre_status": (
                "incomplete" if design.event_remote_pre_years else "not_applicable"
            ),
            "remote_pre_complete": not design.event_remote_pre_years,
            "collinear_remote_pre": False,
            "estimates": [],
            "joint_pretrend": {
                "status": "not_testable",
                "reason": "no requested event year has treated and control support",
            },
        }
    fit = fitter(prepared, model.outcome or "", [*event_terms, *model.controls])
    kept_event_terms = [term for term in event_terms if term in fit.terms]
    estimates: list[dict[str, Any]] = []
    for item in fit.estimates:
        term = str(item["term"])
        if term not in kept_event_terms:
            continue
        if term == _REMOTE_PRE_TERM:
            estimates.append(
                {
                    **item,
                    "event_bin": "remote_pre",
                    "event_years": list(available_remote_pre),
                    "event_year": None,
                    "relative_year": None,
                    "event_term_scaling": design.event_term_scaling,
                    "regressor_weight": 1.0,
                }
            )
            continue
        event_year = int(term.removeprefix("event_"))
        estimates.append(
            {
                **item,
                "event_year": event_year,
                "relative_year": event_year - design.policy_start_year,
                "event_term_scaling": design.event_term_scaling,
                "regressor_weight": 1.0,
            }
        )
    pretrend_terms = (
        [_REMOTE_PRE_TERM] if _REMOTE_PRE_TERM in kept_event_terms else []
    ) + [
        term
        for term in kept_event_terms
        if term != _REMOTE_PRE_TERM
        and int(term.removeprefix("event_")) < design.policy_start_year
    ]
    joint_pretrend = _joint_zero_test(fit, pretrend_terms)
    remote_pre_kept = _REMOTE_PRE_TERM in kept_event_terms
    if design.event_remote_pre_years and not remote_pre_kept:
        return {
            "status": "not_executed",
            **scaling_diagnostics,
            "reference_year": design.event_reference_year,
            "requested_event_years": list(design.event_years),
            "generated_event_years": [],
            "unavailable_event_years": unavailable,
            "collinear_event_years": [
                year for year in available if f"event_{year}" not in kept_event_terms
            ],
            "requested_remote_pre_years": list(design.event_remote_pre_years),
            "generated_remote_pre_years": [],
            "unavailable_remote_pre_years": unavailable_remote_pre,
            "remote_pre_term": None,
            "remote_pre_requested": True,
            "remote_pre_status": "collinear",
            "remote_pre_complete": False,
            "collinear_remote_pre": True,
            "estimates": [],
            "joint_pretrend": {
                "status": "not_testable",
                "reason": "the frozen remote-pre term was absorbed or collinear",
                "terms": [],
            },
            "fit_diagnostics": dict(fit.diagnostics),
            "reason": "the frozen remote-pre term was absorbed or collinear",
        }
    return {
        "status": "succeeded",
        **scaling_diagnostics,
        "reference_year": design.event_reference_year,
        "requested_event_years": list(design.event_years),
        "generated_event_years": [
            year for year in available if f"event_{year}" in kept_event_terms
        ],
        "unavailable_event_years": unavailable,
        "collinear_event_years": [
            year for year in available if f"event_{year}" not in kept_event_terms
        ],
        "requested_remote_pre_years": list(design.event_remote_pre_years),
        "generated_remote_pre_years": (
            list(available_remote_pre) if remote_pre_kept else []
        ),
        "unavailable_remote_pre_years": unavailable_remote_pre,
        "remote_pre_term": _REMOTE_PRE_TERM if remote_pre_kept else None,
        "remote_pre_requested": bool(design.event_remote_pre_years),
        "remote_pre_status": (
            "complete" if design.event_remote_pre_years else "not_applicable"
        ),
        "remote_pre_complete": (
            not design.event_remote_pre_years or remote_pre_kept
        ),
        "collinear_remote_pre": bool(available_remote_pre and not remote_pre_kept),
        "estimates": estimates,
        "joint_pretrend": joint_pretrend,
        "fit_diagnostics": dict(fit.diagnostics),
    }


def _run_placebo(
    prepared: _PreparedData,
    model: ModelSpec,
    fitter: Callable[[_PreparedData, str, Sequence[str]], _FitResult],
) -> dict[str, Any] | None:
    design = prepared.design
    if design.placebo_start_year is None:
        return None
    if design.placebo_start_year not in prepared.observed_years:
        return {
            "status": "not_executed",
            "placebo_start_year": design.placebo_start_year,
            "reason": "placebo_start_year is absent; no year was imputed",
        }
    placebo_prepared = _subset_prepared_data(
        prepared,
        prepared.frame[design.time_field].lt(design.policy_start_year),
    )
    placebo_frame = placebo_prepared.frame
    pseudo_pre = placebo_frame[design.time_field].lt(design.placebo_start_year)
    pseudo_post = placebo_frame[design.time_field].ge(design.placebo_start_year)
    control = placebo_frame[design.group_field].eq(0)
    treated = placebo_frame[design.group_field].eq(1)
    support_counts = {
        "control_pre": int((control & pseudo_pre).sum()),
        "treated_pre": int((treated & pseudo_pre).sum()),
        "control_post": int((control & pseudo_post).sum()),
        "treated_post": int((treated & pseudo_post).sum()),
    }
    pseudo_pre_support = bool(
        support_counts["control_pre"] and support_counts["treated_pre"]
    )
    pseudo_post_support = bool(
        support_counts["control_post"] and support_counts["treated_post"]
    )
    entity_field = design.fixed_effects[0]
    entity_groups = placebo_frame.groupby(entity_field, observed=True)[
        design.group_field
    ].max()
    group_switcher_entities = int(
        (
            placebo_frame.groupby(entity_field, observed=True)[design.group_field]
            .nunique()
            .gt(1)
        ).sum()
    )
    entity_sizes = placebo_frame.groupby(entity_field, observed=True).size()
    entity_year_support = placebo_frame.groupby(entity_field, observed=True)[
        design.time_field
    ].agg(["min", "max"])
    cluster_sizes = pd.Series(placebo_prepared.cluster_codes).value_counts()
    entity_cluster_counts = (
        pd.DataFrame(
            {
                "entity": placebo_frame[entity_field].to_numpy(),
                "cluster": placebo_prepared.cluster_codes,
            }
        )
        .groupby("entity", observed=True)["cluster"]
        .nunique()
    )
    placebo_diagnostics = {
        "policy_start_year": design.policy_start_year,
        "placebo_start_year": design.placebo_start_year,
        "sample_start_year": int(placebo_frame[design.time_field].min()),
        "sample_end_year": int(placebo_frame[design.time_field].max()),
        "rows_after_sample_filter": int(len(placebo_frame)),
        "rows_used": int(len(placebo_frame)),
        "rows_dropped": int(placebo_prepared.rows_dropped),
        "rows_excluded_at_or_after_true_policy": int(
            prepared.frame[design.time_field].ge(design.policy_start_year).sum()
        ),
        "true_policy_contamination_rows": int(
            placebo_frame[design.time_field].ge(design.policy_start_year).sum()
        ),
        "pseudo_period_group_row_counts": support_counts,
        "pseudo_pre_support": pseudo_pre_support,
        "pseudo_post_support": pseudo_post_support,
        "entity_count": int(placebo_frame[entity_field].nunique()),
        "treated_entity_count": int((entity_groups == 1).sum()),
        "control_entity_count": int((entity_groups == 0).sum()),
        "group_switcher_entities": group_switcher_entities,
        "analysis_group_switcher_entities": group_switcher_entities,
        "singleton_entities": int(entity_sizes.eq(1).sum()),
        "entities_spanning_policy": int(
            (
                entity_year_support["min"].lt(design.policy_start_year)
                & entity_year_support["max"].ge(design.policy_start_year)
            ).sum()
        ),
        "group_row_counts": {
            str(int(group)): int(count)
            for group, count in (
                placebo_frame[design.group_field]
                .value_counts()
                .sort_index()
                .items()
            )
        },
        "observed_years": list(placebo_prepared.observed_years),
        "missing_calendar_years": list(placebo_prepared.missing_calendar_years),
        "calendar_years_imputed": [],
        "time_period_count": len(placebo_prepared.observed_years),
        "cluster_count": placebo_prepared.cluster_count,
        "cluster_size_min": int(cluster_sizes.min()),
        "cluster_size_median": float(cluster_sizes.median()),
        "cluster_size_max": int(cluster_sizes.max()),
        "singleton_cluster_count": int(cluster_sizes.eq(1).sum()),
        "singleton_cluster_share": float(cluster_sizes.eq(1).mean()),
        "entities_spanning_multiple_clusters": int(
            entity_cluster_counts.gt(1).sum()
        ),
        "fixed_effect_level_counts": {
            field: int(placebo_frame[field].nunique())
            for field in design.fixed_effects
        },
        "fixed_effect_singleton_level_counts": {
            field: int(
                placebo_frame[field].value_counts(dropna=False).eq(1).sum()
            )
            for field in design.fixed_effects
        },
    }
    if not pseudo_pre_support or not pseudo_post_support:
        return {
            "status": "not_executed",
            **placebo_diagnostics,
            "reason": (
                "fake timing requires treated and control observations in both "
                "pseudo-pre and pseudo-post periods before the true policy year"
            ),
        }
    placebo_term = f"placebo_exposure_{design.placebo_start_year}"
    placebo_frame[placebo_term] = construct_policy_exposure(
        placebo_frame,
        design,
        start_year=design.placebo_start_year,
    ).to_numpy(dtype=float)
    fit = fitter(
        placebo_prepared,
        model.outcome or "",
        [placebo_term, *model.controls],
    )
    estimate = next(
        (item for item in fit.estimates if item["term"] == placebo_term), None
    )
    if estimate is None:
        return {
            "status": "not_executed",
            **placebo_diagnostics,
            "reason": "placebo exposure was absorbed or collinear",
        }
    return {
        "status": "succeeded",
        **placebo_diagnostics,
        "estimate": estimate,
        "random_seed": design.random_seed,
        "fit_diagnostics": dict(fit.diagnostics),
    }


def _run_permutation_placebo(
    prepared: _PreparedData,
    model: ModelSpec,
    *,
    observed_coefficient: float,
) -> dict[str, Any]:
    """Run a deterministic frozen-assignment permutation with batched FWL.

    ``assignment_unit_label`` preserves every row belonging to an assignment
    unit and shuffles only the unit-level treated label.  The legacy
    ``rowwise_exposure`` scheme remains available for backward compatibility,
    but is explicitly labelled as a weak signal check. Fixed effects and
    controls are re-partialled for every draw while outcome residualization is
    reused.
    """

    frame = prepared.frame
    design = prepared.design
    outcome = model.outcome or ""
    fixed_effect_codes = [
        pd.factorize(frame[field], sort=True)[0].astype(np.int64)
        for field in design.fixed_effects
    ]
    base_values = np.column_stack(
        [frame[outcome].to_numpy(dtype=float)]
        + [frame[field].to_numpy(dtype=float) for field in model.controls]
        + [frame[design.exposure_name].to_numpy(dtype=float)]
    )
    base_within, base_iterations = _alternating_multiway_demean(
        base_values,
        fixed_effect_codes,
    )
    y_within = base_within[:, 0]
    control_count = len(model.controls)
    controls_within = base_within[:, 1 : 1 + control_count]
    exposure_within = base_within[:, -1]
    if control_count:
        kept_controls = _independent_columns(controls_within)
        controls_within = controls_within[:, kept_controls]
        y_residual = y_within - controls_within @ np.linalg.lstsq(
            controls_within,
            y_within,
            rcond=None,
        )[0]
        exposure_residual = exposure_within - controls_within @ np.linalg.lstsq(
            controls_within,
            exposure_within,
            rcond=None,
        )[0]
    else:
        y_residual = y_within
        exposure_residual = exposure_within
    observed_denominator = float(exposure_residual @ exposure_residual)
    if observed_denominator <= np.finfo(float).eps:
        raise PolicyEstimationError(
            "permutation placebo exposure is absorbed after fixed effects and controls."
        )
    fwl_observed = float(
        (exposure_residual @ y_residual) / observed_denominator
    )
    if not np.isclose(
        fwl_observed,
        observed_coefficient,
        rtol=1e-6,
        atol=1e-8,
    ):
        raise PolicyEstimationError(
            "permutation FWL observed coefficient does not match the frozen baseline."
        )

    rng = np.random.default_rng(design.random_seed)
    raw_exposure = frame[design.exposure_name].to_numpy(dtype=float)
    permutation_unit_field: str | None = None
    permutation_unit_count: int | None = None
    treated_permutation_unit_count: int | None = None
    unit_codes: np.ndarray | None = None
    unit_labels: np.ndarray | None = None
    policy_weights: np.ndarray | None = None
    if design.permutation_scheme == "assignment_unit_label":
        permutation_unit_field = design.permutation_unit_field
        if permutation_unit_field is None:
            raise PolicyEstimationError(
                "assignment-unit permutation is missing its frozen unit field."
            )
        unit_codes, unit_values = pd.factorize(
            frame[permutation_unit_field],
            sort=True,
        )
        if np.any(unit_codes < 0):
            raise PolicyEstimationError(
                "permutation_unit_field contains missing values after sample cleaning."
            )
        unit_group_counts = (
            pd.DataFrame(
                {
                    "unit_code": unit_codes,
                    "group": frame[design.group_field].to_numpy(dtype=np.int8),
                }
            )
            .groupby("unit_code", observed=True)["group"]
            .nunique()
        )
        if unit_group_counts.gt(1).any():
            raise PolicyEstimationError(
                "assignment_unit_label requires one frozen group label per "
                "permutation unit; use a fixed grouping sensitivity first."
            )
        unit_labels = (
            pd.DataFrame(
                {
                    "unit_code": unit_codes,
                    "group": frame[design.group_field].to_numpy(dtype=np.int8),
                }
            )
            .drop_duplicates("unit_code")
            .sort_values("unit_code")["group"]
            .to_numpy(dtype=float)
        )
        times = frame[design.time_field].to_numpy(dtype=float)
        policy_weights = np.select(
            [
                times < design.policy_start_year,
                times == design.policy_start_year,
                times > design.policy_start_year,
            ],
            [0.0, design.policy_start_weight, design.post_start_weight],
            default=np.nan,
        )
        regenerated = unit_labels[unit_codes] * policy_weights
        if not np.allclose(regenerated, raw_exposure, rtol=0.0, atol=0.0):
            raise PolicyEstimationError(
                "frozen assignment-unit labels do not reconstruct the observed exposure."
            )
        permutation_unit_count = int(len(unit_values))
        treated_permutation_unit_count = int(np.count_nonzero(unit_labels == 1))
    coefficients: list[float] = []
    batch_size = 8
    maximum_iterations = base_iterations
    for start in range(0, design.placebo_repetitions, batch_size):
        size = min(batch_size, design.placebo_repetitions - start)
        if design.permutation_scheme == "assignment_unit_label":
            assert unit_codes is not None
            assert unit_labels is not None
            assert policy_weights is not None
            shuffled = np.column_stack(
                [
                    rng.permutation(unit_labels)[unit_codes] * policy_weights
                    for _ in range(size)
                ]
            )
        else:
            shuffled = np.column_stack(
                [rng.permutation(raw_exposure) for _ in range(size)]
            )
        shuffled_within, iterations = _alternating_multiway_demean(
            shuffled,
            fixed_effect_codes,
        )
        maximum_iterations = max(maximum_iterations, iterations)
        if control_count:
            shuffled_within = shuffled_within - controls_within @ np.linalg.lstsq(
                controls_within,
                shuffled_within,
                rcond=None,
            )[0]
        denominators = np.sum(shuffled_within * shuffled_within, axis=0)
        if np.any(denominators <= np.finfo(float).eps):
            raise PolicyEstimationError(
                "a permuted exposure was absorbed after fixed effects and controls."
            )
        batch_coefficients = (
            shuffled_within.T @ y_residual
        ) / denominators
        coefficients.extend(float(value) for value in batch_coefficients)

    null = np.asarray(coefficients, dtype=float)
    extreme_count = int(
        np.count_nonzero(np.abs(null) >= abs(observed_coefficient))
    )
    empirical_p_value = float(
        (extreme_count + 1) / (design.placebo_repetitions + 1)
    )
    serialized = json.dumps(
        [format(value, ".17g") for value in null],
        separators=(",", ":"),
    ).encode("utf-8")
    quantiles = np.quantile(null, [0.025, 0.25, 0.5, 0.75, 0.975])
    return {
        "status": "succeeded",
        "scheme": design.permutation_scheme,
        "permutation_unit_field": permutation_unit_field,
        "permutation_unit_count": permutation_unit_count,
        "treated_permutation_unit_count": treated_permutation_unit_count,
        "statistic": "coefficient",
        "repetitions_requested": design.placebo_repetitions,
        "repetitions_completed": int(len(null)),
        "random_seed": design.random_seed,
        "observed_coefficient": observed_coefficient,
        "fwl_observed_coefficient": fwl_observed,
        "extreme_count": extreme_count,
        "empirical_p_value": empirical_p_value,
        "null_mean": float(null.mean()),
        "null_standard_deviation": float(null.std(ddof=1)),
        "null_quantiles": {
            "q025": float(quantiles[0]),
            "q25": float(quantiles[1]),
            "q50": float(quantiles[2]),
            "q75": float(quantiles[3]),
            "q975": float(quantiles[4]),
        },
        "distribution_sha256": hashlib.sha256(serialized).hexdigest(),
        "within_iterations_max": maximum_iterations,
        "interpretation_boundary": (
            "Assignment-unit labels were permuted while preserving the treated-unit "
            "count and complete unit rows. This is a randomization-style sensitivity "
            "analysis only when assignment units are exchangeable under the frozen design."
            if design.permutation_scheme == "assignment_unit_label"
            else "Row-wise exposure permutation is a signal sanity check, not a "
            "randomized policy-assignment test."
        ),
    }


def _fit_absorbing_ls(
    prepared: _PreparedData,
    outcome: str,
    terms: Sequence[str],
) -> _FitResult:
    frame = prepared.frame
    absorb = pd.DataFrame(index=frame.index)
    for field in prepared.design.fixed_effects:
        absorb[field] = pd.Categorical(frame[field])
    exog = frame[list(terms)].astype(float)
    try:
        result = AbsorbingLS(
            frame[outcome].astype(float),
            exog,
            absorb=absorb,
            drop_absorbed=True,
        ).fit(
            cov_type="clustered",
            clusters=pd.Series(prepared.cluster_codes, index=frame.index),
            debiased=True,
            method="hdfe",
            absorb_options={
                "compute_degrees": False,
                "residualize_method": "map",
                "options": {
                    "transform": "symmetric",
                    "acceleration": "cg",
                    "tol": _WITHIN_TOLERANCE,
                    "iteration_limit": _WITHIN_MAX_ITERATIONS,
                },
            },
            use_cache=True,
        )
    except Exception as error:
        raise PolicyEstimationError(f"AbsorbingLS policy DID failed: {error}") from error
    kept_terms = tuple(str(term) for term in result.params.index)
    if not kept_terms:
        raise PolicyEstimationError("all policy DID regressors were absorbed.")
    covariance = np.asarray(
        result.cov.loc[list(kept_terms), list(kept_terms)],
        dtype=float,
    )
    absorbed_degrees = _absorbed_degrees(frame, prepared.design.fixed_effects)
    residual_degrees = int(result.nobs) - len(kept_terms) - absorbed_degrees
    if residual_degrees <= 0:
        raise PolicyEstimationError(
            "absorbed fixed effects and regressors exhaust residual degrees of freedom."
        )
    absorbed_df_correction = (
        (int(result.nobs) - len(kept_terms)) / residual_degrees
    )
    covariance *= absorbed_df_correction
    corrected_standard_errors = np.sqrt(
        np.clip(np.diag(covariance), 0.0, np.inf)
    )
    estimates = tuple(
        _estimate_record(
            term,
            float(result.params[term]),
            float(corrected_standard_errors[index]),
            int(result.nobs),
        )
        for index, term in enumerate(kept_terms)
    )
    return _FitResult(
        estimates=estimates,
        terms=kept_terms,
        covariance=covariance,
        nobs=int(result.nobs),
        dropped_terms=tuple(term for term in terms if term not in kept_terms),
        diagnostics={
            "absorbed_effects": list(prepared.design.fixed_effects),
            "cluster_count": prepared.cluster_count,
            "covariance": "clustered_interaction_debiased",
            "absorbed_degrees_of_freedom": absorbed_degrees,
            "absorbed_df_correction": absorbed_df_correction,
            "dropped_terms": [term for term in terms if term not in kept_terms],
            "residual_degrees_of_freedom": residual_degrees,
        },
    )


def _fit_numpy_within(
    prepared: _PreparedData,
    outcome: str,
    terms: Sequence[str],
) -> _FitResult:
    frame = prepared.frame
    values = np.column_stack(
        [frame[outcome].to_numpy(dtype=float)]
        + [frame[term].to_numpy(dtype=float) for term in terms]
    )
    fixed_effect_codes = [
        pd.factorize(frame[field], sort=True)[0].astype(np.int64)
        for field in prepared.design.fixed_effects
    ]
    within, iterations = _alternating_multiway_demean(values, fixed_effect_codes)
    y = within[:, 0]
    x_all = within[:, 1:]
    kept_indexes = _independent_columns(x_all)
    kept_terms = tuple(terms[index] for index in kept_indexes)
    x = x_all[:, kept_indexes]
    if not kept_terms:
        raise PolicyEstimationError("all policy DID regressors were absorbed.")
    coefficients = np.linalg.lstsq(x, y, rcond=None)[0]
    residuals = y - x @ coefficients
    covariance = _interaction_clustered_covariance(
        x,
        residuals,
        prepared.cluster_codes,
    )
    absorbed_degrees = _absorbed_degrees(frame, prepared.design.fixed_effects)
    residual_degrees = len(frame) - len(kept_terms) - absorbed_degrees
    if residual_degrees <= 0:
        raise PolicyEstimationError(
            "absorbed fixed effects and regressors exhaust residual degrees of freedom."
        )
    absorbed_df_correction = (
        (len(frame) - len(kept_terms)) / residual_degrees
    )
    covariance *= absorbed_df_correction
    standard_errors = np.sqrt(np.clip(np.diag(covariance), 0.0, np.inf))
    estimates = tuple(
        _estimate_record(
            term,
            float(coefficients[index]),
            float(standard_errors[index]),
            len(frame),
        )
        for index, term in enumerate(kept_terms)
    )
    return _FitResult(
        estimates=estimates,
        terms=kept_terms,
        covariance=covariance,
        nobs=len(frame),
        dropped_terms=tuple(
            term for index, term in enumerate(terms) if index not in kept_indexes
        ),
        diagnostics={
            "absorbed_effects": list(prepared.design.fixed_effects),
            "cluster_count": prepared.cluster_count,
            "covariance": "manual_interaction_cluster_finite_sample",
            "absorbed_degrees_of_freedom": absorbed_degrees,
            "absorbed_df_correction": absorbed_df_correction,
            "within_iterations": iterations,
            "dropped_terms": [
                term for index, term in enumerate(terms) if index not in kept_indexes
            ],
            "residual_degrees_of_freedom": residual_degrees,
        },
    )


def _absorbed_degrees(
    frame: pd.DataFrame,
    fixed_effects: Sequence[str],
) -> int:
    """Return the frozen conservative rank used for absorbed-effect correction."""

    if not fixed_effects:
        return 0
    levels = sum(int(frame[field].nunique()) for field in fixed_effects)
    return levels - (len(fixed_effects) - 1)


def _joint_zero_test(fit: _FitResult, terms: Sequence[str]) -> dict[str, Any]:
    available = [term for term in terms if term in fit.terms]
    if not available:
        return {
            "status": "not_testable",
            "reason": "no estimable pre-policy event coefficient was requested",
            "terms": [],
        }
    indexes = [fit.terms.index(term) for term in available]
    coefficients_by_term = {
        str(item["term"]): float(item["coefficient"]) for item in fit.estimates
    }
    coefficients = np.asarray(
        [coefficients_by_term[term] for term in available], dtype=float
    )
    covariance = fit.covariance[np.ix_(indexes, indexes)]
    statistic = float(coefficients.T @ np.linalg.pinv(covariance) @ coefficients)
    degrees = int(np.linalg.matrix_rank(covariance))
    if degrees < 1:
        return {
            "status": "not_testable",
            "reason": "pre-policy covariance matrix has zero rank",
            "terms": available,
        }
    return {
        "status": "tested",
        "null_hypothesis": "all requested pre-policy event coefficients equal zero",
        "terms": available,
        "statistic": statistic,
        "degrees_of_freedom": degrees,
        "p_value": float(chi2.sf(statistic, degrees)),
    }


def _alternating_multiway_demean(
    values: np.ndarray,
    fixed_effect_codes: Sequence[np.ndarray],
) -> tuple[np.ndarray, int]:
    """Residualize by an independently implemented accelerated projection.

    The primary path delegates absorption to pyhdfe through AbsorbingLS.  This
    reproduction path implements the symmetric projection and conjugate-gradient
    updates directly with NumPy, retaining a separate code path while avoiding
    the pathological convergence of unaccelerated alternating demeaning in
    heavily nested four-way fixed effects.
    """

    if not fixed_effect_codes:
        return np.asarray(values, dtype=float).copy(), 0

    def symmetric_projection(matrix: np.ndarray) -> np.ndarray:
        transformed = matrix
        for codes in (*fixed_effect_codes, *reversed(fixed_effect_codes)):
            transformed = _group_demean(transformed, codes)
        return transformed

    current = np.asarray(values, dtype=float).copy()
    residual = symmetric_projection(current) - current
    squared_residual = np.sum(residual * residual, axis=0, keepdims=True)
    direction = residual.copy()
    epsilon = np.finfo(float).eps
    for iteration in range(1, _WITHIN_MAX_ITERATIONS + 1):
        previous = current.copy()
        operator_direction = direction - symmetric_projection(direction)
        denominator = np.sum(
            direction * operator_direction,
            axis=0,
            keepdims=True,
        )
        active = (
            squared_residual > epsilon
        ) & (np.abs(denominator) > epsilon)
        if not bool(np.all(active)):
            # Degenerate columns are already at (or numerically indistinguishable
            # from) the fixed point. A plain projection closes them safely.
            inactive = ~active.ravel()
            if bool(np.any(inactive)):
                current[:, inactive] = symmetric_projection(current[:, inactive])
            if not bool(np.any(active)):
                if float(np.max(np.abs(current - previous))) <= _WITHIN_TOLERANCE:
                    return current, iteration
                residual = symmetric_projection(current) - current
                squared_residual = np.sum(
                    residual * residual,
                    axis=0,
                    keepdims=True,
                )
                direction = residual.copy()
                continue

        alpha = np.zeros_like(squared_residual)
        alpha[active] = squared_residual[active] / denominator[active]
        current += direction * alpha
        residual -= operator_direction * alpha
        next_squared_residual = np.sum(
            residual * residual,
            axis=0,
            keepdims=True,
        )
        beta = np.zeros_like(squared_residual)
        beta[active] = next_squared_residual[active] / squared_residual[active]
        direction = residual + direction * beta
        squared_residual = next_squared_residual
        if float(np.max(np.abs(current - previous))) <= _WITHIN_TOLERANCE:
            return current, iteration
    raise PolicyEstimationError(
        f"multi-way within transform did not converge in {_WITHIN_MAX_ITERATIONS} iterations."
    )


def _group_demean(values: np.ndarray, codes: np.ndarray) -> np.ndarray:
    group_count = int(codes.max()) + 1
    counts = np.bincount(codes, minlength=group_count).astype(float)
    totals = np.zeros((group_count, values.shape[1]), dtype=float)
    np.add.at(totals, codes, values)
    return values - totals[codes] / counts[codes, None]


def _independent_columns(values: np.ndarray) -> list[int]:
    kept: list[int] = []
    current_rank = 0
    for index in range(values.shape[1]):
        candidate = values[:, [*kept, index]]
        rank = int(np.linalg.matrix_rank(candidate))
        if rank > current_rank:
            kept.append(index)
            current_rank = rank
    return kept


def _interaction_clustered_covariance(
    x: np.ndarray,
    residuals: np.ndarray,
    cluster_codes: np.ndarray,
) -> np.ndarray:
    nobs, nvar = x.shape
    groups = np.unique(cluster_codes)
    if len(groups) <= 1:
        raise PolicyEstimationError(
            "manual interaction-cluster covariance requires at least two clusters."
        )
    if nobs <= nvar:
        raise PolicyEstimationError(
            "too few observations for manual interaction-cluster covariance."
        )
    bread = np.linalg.pinv(x.T @ x)
    scores = x * residuals[:, None]
    group_scores = np.zeros((len(groups), nvar), dtype=float)
    # _interaction_codes returns dense factor codes, so one indexed reduction
    # computes all 111k+ cluster scores without a quadratic Boolean-mask loop.
    np.add.at(group_scores, cluster_codes, scores)
    meat = group_scores.T @ group_scores
    correction = (len(groups) / (len(groups) - 1)) * ((nobs - 1) / (nobs - nvar))
    covariance = bread @ meat @ bread * correction
    return (covariance + covariance.T) / 2


def _interaction_codes(frame: pd.DataFrame, fields: Sequence[str]) -> np.ndarray:
    index = pd.MultiIndex.from_frame(frame[list(fields)], names=list(fields))
    codes, _ = pd.factorize(index, sort=True)
    return codes.astype(np.int64)


def _estimate_record(
    term: str,
    coefficient: float,
    standard_error: float,
    nobs: int,
    *,
    p_value: float | None = None,
) -> dict[str, Any]:
    if standard_error > 0 and np.isfinite(standard_error):
        t_statistic = coefficient / standard_error
        resolved_p_value = (
            float(2 * norm.sf(abs(t_statistic)))
            if p_value is None
            else float(p_value)
        )
        confidence_interval = [
            float(coefficient - 1.959963984540054 * standard_error),
            float(coefficient + 1.959963984540054 * standard_error),
        ]
    else:
        t_statistic = None
        resolved_p_value = None
        confidence_interval = [None, None]
    return {
        "term": term,
        "coefficient": float(coefficient),
        "standard_error": float(standard_error),
        "t_statistic": None if t_statistic is None else float(t_statistic),
        "p_value": resolved_p_value,
        "confidence_interval_95": confidence_interval,
        "nobs": int(nobs),
    }


def _read_csv(path: Path, required: Sequence[str]) -> pd.DataFrame:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            header = pd.read_csv(path, encoding=encoding, nrows=0)
            missing = [field for field in required if field not in header.columns]
            if missing:
                raise PolicyEstimationError(
                    "data are missing frozen policy DID fields: " + ", ".join(missing)
                )
            return pd.read_csv(path, encoding=encoding, usecols=list(required))
        except UnicodeDecodeError as error:
            last_error = error
    raise PolicyEstimationError(
        "policy DID CSV encoding must be UTF-8 or GB18030."
    ) from last_error


def _strict_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PolicyContractError(f"{field} must be a non-empty trimmed string.")
    return value


def _strict_name_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PolicyContractError(f"{field} must be a JSON list of field names.")
    names = tuple(_strict_name(item, field) for item in value)
    if len(names) != len(set(names)):
        raise PolicyContractError(f"{field} must not contain duplicates.")
    return names


def _strict_year_list(value: Any, field: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise PolicyContractError(f"{field} must be a JSON list of absolute years.")
    years = tuple(_strict_int(item, field) for item in value)
    if tuple(sorted(set(years))) != years:
        raise PolicyContractError(f"{field} must be unique and sorted ascending.")
    return years


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyContractError(f"{field} must be an integer.")
    return value


def _optional_int(value: Any, field: str) -> int | None:
    return None if value is None else _strict_int(value, field)


def _strict_weight(value: Any, field: str, *, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyContractError(f"{field} must be numeric.")
    resolved = float(value)
    lower_ok = resolved >= 0 if allow_zero else resolved > 0
    if not np.isfinite(resolved) or not lower_ok or resolved > 1:
        interval = "[0, 1]" if allow_zero else "(0, 1]"
        raise PolicyContractError(f"{field} must be in {interval}.")
    return resolved


__all__ = [
    "POLICY_PRIMARY_IMPLEMENTATION_ID",
    "POLICY_REPRODUCTION_IMPLEMENTATION_ID",
    "PolicyCausalError",
    "PolicyContractError",
    "PolicyDesign",
    "PolicyEstimationError",
    "construct_policy_exposure",
    "estimate_policy_baseline",
    "estimate_policy_core",
    "estimate_policy_model",
    "estimate_policy_permutation",
    "parse_policy_design",
    "reproduce_policy_baseline",
    "reproduce_policy_model",
]
