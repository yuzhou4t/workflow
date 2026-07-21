from __future__ import annotations

import math
import re
from numbers import Integral, Real
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BeforeValidator, Field, StrictStr, StringConstraints, model_validator

from ..models import StrictModel


RecipeId = Literal[
    "coefficient_forest",
    "sample_flow",
    "event_study",
    "grouped_time_series",
    "heterogeneity_forest",
    "specification_curve",
    "descriptive_statistics",
    "correlation_heatmap",
    "distribution_histogram",
    "box_plot",
    "scatter_plot",
    "spatial_choropleth",
    "mechanism_evidence_graph",
]
SampleScope = Literal[
    "frozen_source_rows",
    "prepared_estimation_sample",
    "upstream_aggregate",
]

RECIPE_IDS: tuple[RecipeId, ...] = (
    "coefficient_forest",
    "sample_flow",
    "event_study",
    "grouped_time_series",
    "heterogeneity_forest",
    "specification_curve",
    "descriptive_statistics",
    "correlation_heatmap",
    "distribution_histogram",
    "box_plot",
    "scatter_plot",
    "spatial_choropleth",
    "mechanism_evidence_graph",
)


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("value must be a real number, not bool or a numeric string")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("value must be finite")
    return number


def _strict_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError("value must be an integer, not bool or a numeric string")
    return int(value)


FiniteFloat = Annotated[float, BeforeValidator(_finite_number)]
Probability = Annotated[FiniteFloat, Field(ge=0, le=1)]
NonNegativeFloat = Annotated[FiniteFloat, Field(ge=0)]
StrictInteger = Annotated[int, BeforeValidator(_strict_integer)]
NonNegativeInt = Annotated[StrictInteger, Field(ge=0)]
PositiveInt = Annotated[StrictInteger, Field(gt=0)]
NonEmptyStr = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Sha256 = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]

_UNAUTHORIZED_MECHANISM_LABEL = re.compile(
    r"显著|证明|导致|造成|因果|支持|成立|促进|抑制|影响|"
    r"\b(?:causes?|causal|proves?|significant|supports?|promotes?|"
    r"increases?|decreases?|effects?)\b",
    re.IGNORECASE,
)


class EstimateInterval(StrictModel):
    coefficient: FiniteFloat
    ci_lower: FiniteFloat
    ci_upper: FiniteFloat

    @model_validator(mode="after")
    def validate_interval(self) -> "EstimateInterval":
        if self.ci_lower > self.ci_upper:
            raise ValueError("ci_lower must not exceed ci_upper")
        if not self.ci_lower <= self.coefficient <= self.ci_upper:
            raise ValueError("confidence interval must contain the point estimate")
        return self


class CoefficientForestRecord(EstimateInterval):
    term: NonEmptyStr
    execution_id: NonEmptyStr
    p_value: Probability | None = None
    sample_size: PositiveInt | None = None


class SampleFlowData(StrictModel):
    rows_input: NonNegativeInt
    rows_used: NonNegativeInt
    rows_dropped: NonNegativeInt

    @model_validator(mode="after")
    def validate_closed_flow(self) -> "SampleFlowData":
        if self.rows_input != self.rows_used + self.rows_dropped:
            raise ValueError("sample flow must close: rows_input = rows_used + rows_dropped")
        return self


class EventStudyPoint(EstimateInterval):
    relative_time: FiniteFloat
    event_year: StrictInteger | None = None
    execution_id: NonEmptyStr


class EventStudyData(StrictModel):
    points: list[EventStudyPoint] = Field(min_length=1)
    reference_period: FiniteFloat | None = None
    joint_pretrend_p_value: Probability | None = None

    @model_validator(mode="after")
    def validate_points(self) -> "EventStudyData":
        identities = [
            (point.execution_id, point.relative_time) for point in self.points
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("event-study execution/relative-time points must be unique")
        if self.reference_period is not None and any(
            point.relative_time == self.reference_period for point in self.points
        ):
            raise ValueError("reference_period must be omitted from estimated points")
        event_years = [
            point.event_year for point in self.points if point.event_year is not None
        ]
        if len(event_years) != len(set(event_years)):
            raise ValueError("event_year values must be unique when supplied")
        return self


class GroupedTimeSeriesRecord(StrictModel):
    period: FiniteFloat
    period_label: NonEmptyStr | None = None
    series: NonEmptyStr
    value: FiniteFloat
    n: PositiveInt | None = None


class GroupedTimeSeriesData(StrictModel):
    value_name: NonEmptyStr
    time_variable: NonEmptyStr
    series_variable: NonEmptyStr
    series_labels: dict[str, NonEmptyStr]
    sample_scope: SampleScope
    intervention_period: FiniteFloat | None = None
    records: list[GroupedTimeSeriesRecord] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_records(self) -> "GroupedTimeSeriesData":
        identities = [(item.series, item.period) for item in self.records]
        if len(identities) != len(set(identities)):
            raise ValueError("time-series series/period rows must be unique")
        counts: dict[str, int] = {}
        labels: dict[float, str] = {}
        for item in self.records:
            counts[item.series] = counts.get(item.series, 0) + 1
            if item.period_label is not None:
                previous = labels.setdefault(item.period, item.period_label)
                if previous != item.period_label:
                    raise ValueError("one period cannot have conflicting labels")
        if any(count < 2 for count in counts.values()):
            raise ValueError("every time series requires at least two periods")
        series = set(counts)
        if set(self.series_labels) != series:
            raise ValueError("series_labels must cover every series exactly")
        if len(set(self.series_labels.values())) != len(self.series_labels):
            raise ValueError("series_labels must be unique")
        period_sets = {
            series_name: {
                item.period
                for item in self.records
                if item.series == series_name
            }
            for series_name in series
        }
        if len({frozenset(periods) for periods in period_sets.values()}) != 1:
            raise ValueError(
                "grouped time series must share the same observed periods"
            )
        if self.intervention_period is not None and self.intervention_period not in {
            item.period for item in self.records
        }:
            raise ValueError("intervention_period must be an observed period")
        return self


class HeterogeneityForestRecord(EstimateInterval):
    subgroup: NonEmptyStr
    subgroup_variable: NonEmptyStr
    term: NonEmptyStr
    execution_id: NonEmptyStr
    sample_size: PositiveInt | None = None


class SpecificationCurvePoint(EstimateInterval):
    specification: NonEmptyStr
    run_type: Literal["baseline", "robustness"]
    execution_id: NonEmptyStr


class SpecificationCurveData(StrictModel):
    term: NonEmptyStr
    points: list[SpecificationCurvePoint] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_specifications(self) -> "SpecificationCurveData":
        specifications = [point.specification for point in self.points]
        if len(specifications) != len(set(specifications)):
            raise ValueError("specification labels must be unique")
        execution_ids = [point.execution_id for point in self.points]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("specification execution_ids must be unique")
        if sum(point.run_type == "baseline" for point in self.points) != 1:
            raise ValueError("specification curve requires exactly one baseline")
        return self


class DescriptiveStatisticsRecord(StrictModel):
    variable: NonEmptyStr
    sample_scope: SampleScope
    n: PositiveInt
    missing: NonNegativeInt
    mean: FiniteFloat
    std: NonNegativeFloat
    min: FiniteFloat
    q1: FiniteFloat
    median: FiniteFloat
    q3: FiniteFloat
    max: FiniteFloat

    @model_validator(mode="after")
    def validate_summary(self) -> "DescriptiveStatisticsRecord":
        if not self.min <= self.q1 <= self.median <= self.q3 <= self.max:
            raise ValueError("descriptive five-number summary must be ordered")
        if not self.min <= self.mean <= self.max:
            raise ValueError("mean must lie between min and max")
        return self


class CorrelationHeatmapData(StrictModel):
    variables: list[NonEmptyStr] = Field(min_length=2, max_length=50)
    matrix: list[list[FiniteFloat]] = Field(min_length=2, max_length=50)
    method: Literal["pearson", "spearman"]
    sample_policy: Literal["listwise_complete"]
    sample_scope: SampleScope
    n: PositiveInt

    @model_validator(mode="after")
    def validate_matrix(self) -> "CorrelationHeatmapData":
        size = len(self.variables)
        if len(set(self.variables)) != size:
            raise ValueError("correlation variables must be unique")
        if len(self.matrix) != size or any(len(row) != size for row in self.matrix):
            raise ValueError("correlation matrix must be square and match variables")
        for row in self.matrix:
            if any(value < -1 or value > 1 for value in row):
                raise ValueError("correlations must lie in [-1, 1]")
        for index in range(size):
            if not math.isclose(
                self.matrix[index][index], 1.0, rel_tol=0.0, abs_tol=1e-12
            ):
                raise ValueError("correlation matrix diagonal must equal 1")
            for other in range(index + 1, size):
                if not math.isclose(
                    self.matrix[index][other],
                    self.matrix[other][index],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    raise ValueError("correlation matrix must be symmetric")
        array = np.asarray(self.matrix, dtype=float)
        minimum_eigenvalue = float(np.linalg.eigvalsh(array).min())
        if minimum_eigenvalue < -1e-10:
            raise ValueError("correlation matrix must be positive semidefinite")
        if int(np.linalg.matrix_rank(array, tol=1e-10)) > self.n - 1:
            raise ValueError("correlation matrix rank cannot exceed n - 1")
        return self


class HistogramBin(StrictModel):
    lower: FiniteFloat
    upper: FiniteFloat
    count: NonNegativeInt

    @model_validator(mode="after")
    def validate_interval(self) -> "HistogramBin":
        if self.lower >= self.upper:
            raise ValueError("histogram bin lower must be smaller than upper")
        return self


class DistributionHistogramData(StrictModel):
    variable: NonEmptyStr
    sample_scope: SampleScope
    bins: list[HistogramBin] = Field(min_length=1, max_length=200)
    binning_rule: NonEmptyStr
    n: NonNegativeInt

    @model_validator(mode="after")
    def validate_bins(self) -> "DistributionHistogramData":
        for previous, current in zip(self.bins, self.bins[1:]):
            if current.lower < previous.lower:
                raise ValueError("histogram bins must be sorted by lower bound")
            if current.lower < previous.upper:
                raise ValueError("histogram bins must not overlap")
        if sum(item.count for item in self.bins) != self.n:
            raise ValueError("histogram bin counts must sum to n")
        return self


class BoxPlotGroup(StrictModel):
    group: NonEmptyStr
    whisker_low: FiniteFloat
    q1: FiniteFloat
    median: FiniteFloat
    q3: FiniteFloat
    whisker_high: FiniteFloat
    n: PositiveInt

    @model_validator(mode="after")
    def validate_five_numbers(self) -> "BoxPlotGroup":
        if not (
            self.whisker_low
            <= self.q1
            <= self.median
            <= self.q3
            <= self.whisker_high
        ):
            raise ValueError("box-plot five-number summary must be ordered")
        return self


class BoxPlotData(StrictModel):
    variable: NonEmptyStr
    group_variable: NonEmptyStr
    whisker_rule: Literal["tukey_1_5_iqr"]
    sample_scope: SampleScope
    groups: list[BoxPlotGroup] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_groups(self) -> "BoxPlotData":
        groups = [item.group for item in self.groups]
        if len(groups) != len(set(groups)):
            raise ValueError("box-plot groups must be unique")
        return self


class ScatterPoint(StrictModel):
    x: FiniteFloat
    y: FiniteFloat
    n: PositiveInt | None = None
    label: NonEmptyStr | None = None


class ScatterPlotData(StrictModel):
    x_variable: NonEmptyStr
    y_variable: NonEmptyStr
    sample_scope: SampleScope
    grain: Literal["aggregate", "group", "bin"]
    points: list[ScatterPoint] = Field(min_length=8, max_length=500)


def _segments_intersect(
    first: tuple[list[float], list[float]],
    second: tuple[list[float], list[float]],
) -> bool:
    a, b = first
    c, d = second

    def cross(
        start: list[float],
        end: list[float],
        point: list[float],
    ) -> float:
        return (end[0] - start[0]) * (point[1] - start[1]) - (
            end[1] - start[1]
        ) * (point[0] - start[0])

    def on_segment(
        start: list[float],
        end: list[float],
        point: list[float],
    ) -> bool:
        return (
            min(start[0], end[0]) - 1e-12
            <= point[0]
            <= max(start[0], end[0]) + 1e-12
            and min(start[1], end[1]) - 1e-12
            <= point[1]
            <= max(start[1], end[1]) + 1e-12
        )

    orientations = (
        cross(a, b, c),
        cross(a, b, d),
        cross(c, d, a),
        cross(c, d, b),
    )
    if orientations[0] * orientations[1] < 0 and orientations[2] * orientations[3] < 0:
        return True
    return any(
        math.isclose(value, 0.0, rel_tol=0.0, abs_tol=1e-12)
        and on_segment(start, end, point)
        for value, start, end, point in (
            (orientations[0], a, b, c),
            (orientations[1], a, b, d),
            (orientations[2], c, d, a),
            (orientations[3], c, d, b),
        )
    )


class SpatialRegion(StrictModel):
    region_id: NonEmptyStr
    label: NonEmptyStr
    value: FiniteFloat
    polygon: list[list[FiniteFloat]] = Field(min_length=4, max_length=1_000)

    @model_validator(mode="after")
    def validate_polygon(self) -> "SpatialRegion":
        if any(len(position) != 2 for position in self.polygon):
            raise ValueError("polygon positions must be [longitude, latitude]")
        for longitude, latitude in self.polygon:
            if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                raise ValueError("polygon coordinates must be valid EPSG:4326 positions")
            if abs(latitude) > 85:
                raise ValueError(
                    "spatial renderer supports latitudes only within [-85, 85]"
                )
        longitudes = [position[0] for position in self.polygon]
        if max(longitudes) - min(longitudes) > 180:
            raise ValueError(
                "antimeridian-crossing polygons require upstream normalization"
            )
        if self.polygon[0] != self.polygon[-1]:
            raise ValueError("polygon ring must be closed")
        if len({tuple(position) for position in self.polygon[:-1]}) < 3:
            raise ValueError("polygon ring requires at least three distinct vertices")
        vertices = self.polygon[:-1]
        signed_area = sum(
            start[0] * end[1] - end[0] * start[1]
            for start, end in zip(
                vertices,
                [*vertices[1:], vertices[0]],
                strict=True,
            )
        ) / 2
        if math.isclose(signed_area, 0.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("polygon ring must have non-zero area")
        segments = list(
            zip(self.polygon[:-1], self.polygon[1:], strict=True)
        )
        for first_index, first in enumerate(segments):
            for second_index in range(first_index + 1, len(segments)):
                if second_index == first_index + 1 or (
                    first_index == 0 and second_index == len(segments) - 1
                ):
                    continue
                if _segments_intersect(first, segments[second_index]):
                    raise ValueError("polygon ring must not self-intersect")
        return self


class SpatialChoroplethData(StrictModel):
    crs: Literal["EPSG:4326"]
    value_name: NonEmptyStr
    geometry_source_sha256: Sha256
    value_source_sha256: Sha256
    regions: list[SpatialRegion] = Field(min_length=1, max_length=5_000)

    @model_validator(mode="after")
    def validate_regions(self) -> "SpatialChoroplethData":
        region_ids = [region.region_id for region in self.regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("spatial region_id values must be unique")
        longitudes = [
            position[0]
            for region in self.regions
            for position in region.polygon
        ]
        if max(longitudes) - min(longitudes) > 180:
            raise ValueError(
                "spatial region collection requires upstream antimeridian normalization"
            )
        if sum(len(region.polygon) for region in self.regions) > 20_000:
            raise ValueError(
                "spatial input exceeds 20,000 simplified polygon vertices"
            )
        return self


class MechanismNode(StrictModel):
    node_id: NonEmptyStr
    label: NonEmptyStr

    @model_validator(mode="after")
    def validate_neutral_label(self) -> "MechanismNode":
        if _UNAUTHORIZED_MECHANISM_LABEL.search(self.label):
            raise ValueError("mechanism node label contains conclusion language")
        return self


class MechanismEdge(StrictModel):
    edge_id: NonEmptyStr
    source: NonEmptyStr
    target: NonEmptyStr
    edge_kind: Literal["hypothesized"]
    label: NonEmptyStr

    @model_validator(mode="after")
    def validate_hypothesis_edge(self) -> "MechanismEdge":
        if self.source == self.target:
            raise ValueError("mechanism graph edges cannot be self-loops")
        if _UNAUTHORIZED_MECHANISM_LABEL.search(self.label):
            raise ValueError("mechanism edge label contains conclusion language")
        return self


class MechanismEvidenceGraphData(StrictModel):
    nodes: list[MechanismNode] = Field(min_length=2)
    edges: list[MechanismEdge] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> "MechanismEvidenceGraphData":
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("mechanism node_id values must be unique")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("mechanism edge_id values must be unique")
        known_nodes = set(node_ids)
        for edge in self.edges:
            if edge.source not in known_nodes or edge.target not in known_nodes:
                raise ValueError("mechanism edges must reference declared nodes")
        return self


RecipeData = dict[str, Any] | list[dict[str, Any]]

_LIST_RECIPE_MODELS: dict[str, type[StrictModel]] = {
    "coefficient_forest": CoefficientForestRecord,
    "heterogeneity_forest": HeterogeneityForestRecord,
    "descriptive_statistics": DescriptiveStatisticsRecord,
}

_OBJECT_RECIPE_MODELS: dict[str, type[StrictModel]] = {
    "sample_flow": SampleFlowData,
    "event_study": EventStudyData,
    "grouped_time_series": GroupedTimeSeriesData,
    "specification_curve": SpecificationCurveData,
    "correlation_heatmap": CorrelationHeatmapData,
    "distribution_histogram": DistributionHistogramData,
    "box_plot": BoxPlotData,
    "scatter_plot": ScatterPlotData,
    "spatial_choropleth": SpatialChoroplethData,
    "mechanism_evidence_graph": MechanismEvidenceGraphData,
}


def _validate_list_recipe(recipe_id: str, data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError(f"{recipe_id} data must be a list")
    if not data:
        raise ValueError(f"{recipe_id} data must not be empty")
    model = _LIST_RECIPE_MODELS[recipe_id]
    records = [model.model_validate(item) for item in data]

    if recipe_id == "coefficient_forest":
        identities = [
            (item.execution_id, item.term)
            for item in records
            if isinstance(item, CoefficientForestRecord)
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("coefficient forest execution/term rows must be unique")
    elif recipe_id == "heterogeneity_forest":
        heterogeneity = [
            item for item in records if isinstance(item, HeterogeneityForestRecord)
        ]
        if len(heterogeneity) < 2:
            raise ValueError("heterogeneity forest requires at least two subgroups")
        if len({item.subgroup_variable for item in heterogeneity}) != 1:
            raise ValueError("heterogeneity rows must use one subgroup_variable")
        if len({item.term for item in heterogeneity}) != 1:
            raise ValueError("heterogeneity rows must estimate one comparable term")
        identities = [
            (item.subgroup_variable, item.subgroup, item.term)
            for item in heterogeneity
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("heterogeneity subgroup rows must be unique")
        execution_ids = [item.execution_id for item in heterogeneity]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("heterogeneity execution_ids must be unique")
    elif recipe_id == "descriptive_statistics":
        variables = [
            item.variable
            for item in records
            if isinstance(item, DescriptiveStatisticsRecord)
        ]
        if len(variables) != len(set(variables)):
            raise ValueError("descriptive-statistics variables must be unique")
        scopes = {
            item.sample_scope
            for item in records
            if isinstance(item, DescriptiveStatisticsRecord)
        }
        if len(scopes) != 1:
            raise ValueError("descriptive-statistics rows must share one sample_scope")

    return [item.model_dump(mode="json") for item in records]


def validate_recipe_data(recipe_id: str, data: Any) -> RecipeData:
    """Validate and normalize one recipe's JSON-compatible input data."""

    if not isinstance(recipe_id, str) or recipe_id not in RECIPE_IDS:
        raise ValueError(f"unknown recipe_id: {recipe_id!r}")
    if recipe_id in _LIST_RECIPE_MODELS:
        return _validate_list_recipe(recipe_id, data)
    if not isinstance(data, dict):
        raise ValueError(f"{recipe_id} data must be an object")
    model = _OBJECT_RECIPE_MODELS[recipe_id]
    return model.model_validate(data).model_dump(mode="json")


def recipe_data_snapshot(recipe_id: str, data: Any) -> dict[str, Any]:
    """Return the canonical FigureArtifact snapshot for validated recipe data."""

    normalized = validate_recipe_data(recipe_id, data)
    if isinstance(normalized, list):
        return {"records": normalized}
    return normalized
