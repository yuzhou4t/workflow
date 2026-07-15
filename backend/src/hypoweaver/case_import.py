from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import AsyncIterable
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic import Field

from .models import CaseSubmission, DatasetRef, Hypothesis, StrictModel, VariableSpec


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "backend" / "var" / "datasets.json"
DEFAULT_UPLOAD_ROOT = PROJECT_ROOT / "backend" / "var" / "uploads"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
HIDDEN_SUFFIXES = {".pdf", ".doc", ".docx", ".do", ".r", ".rmd", ".py", ".ipynb", ".log"}
HIDDEN_PATH_MARKERS = {"hidden", "reference", "references", "gold", "原始论文"}


class CaseImportError(ValueError):
    pass


class LocalCaseImportRequest(StrictModel):
    path: str = Field(min_length=1)


class SafeImportReport(StrictModel):
    registered_dataset_id: str
    main_data_filename: str
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=1)
    year_min: int | None = None
    year_max: int | None = None
    hidden_file_count: int = Field(ge=0)
    excluded_file_count: int = Field(ge=0)
    requires_human_confirmation: bool = True
    human_review_items: list[str]


class LocalCaseImportResponse(StrictModel):
    case_submission: CaseSubmission
    import_report: SafeImportReport


class _CsvInspection(StrictModel):
    sha256: str
    size_bytes: int = Field(ge=0)
    columns: list[str]
    row_count: int = Field(ge=0)
    year_min: int | None = None
    year_max: int | None = None
    processed_definitions: dict[str, str] = Field(default_factory=dict)


class DatasetRegistry:
    """Private, server-side map from opaque dataset ids to local source files."""

    def __init__(self, path: Path | None = None) -> None:
        configured_path = os.getenv("HYPOWEAVER_DATASET_REGISTRY_PATH")
        self.path = (
            Path(configured_path).expanduser()
            if path is None and configured_path
            else (path or DEFAULT_REGISTRY_PATH)
        )
        self._lock = threading.Lock()

    def register(self, dataset_ref: DatasetRef, source_path: Path) -> None:
        with self._lock:
            records = self._load()
            records[dataset_ref.dataset_id] = {
                "source_path": str(source_path.resolve()),
                "sha256": dataset_ref.sha256,
                "filename": dataset_ref.filename,
                "size_bytes": dataset_ref.size_bytes,
            }
            self._write(records)

    def resolve(self, dataset_ref: DatasetRef) -> Path:
        """Resolve an opaque dataset id and verify its immutable registry metadata."""
        with self._lock:
            record = self._load().get(dataset_ref.dataset_id)
        if record is None:
            raise CaseImportError(f"dataset id is not registered: {dataset_ref.dataset_id}")
        if (
            record.get("sha256") != dataset_ref.sha256
            or record.get("filename") != dataset_ref.filename
            or record.get("size_bytes") != dataset_ref.size_bytes
        ):
            raise CaseImportError("dataset reference does not match the private registry")
        source_path = Path(str(record.get("source_path", "")))
        if not source_path.is_file():
            raise CaseImportError("registered dataset file is unavailable")
        return source_path

    def _load(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        os.chmod(self.path, 0o600)
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise CaseImportError("dataset registry is not a JSON object")
        return loaded

    def _write(self, records: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".datasets-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(records, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()


class CaseUploadStore:
    """Persists one user-selected CSV without exposing a client filesystem path."""

    def __init__(self, root: Path | None = None) -> None:
        configured_root = os.getenv("HYPOWEAVER_UPLOAD_ROOT")
        self.root = (
            Path(configured_root).expanduser()
            if root is None and configured_root
            else (root or DEFAULT_UPLOAD_ROOT)
        )

    async def save(self, filename: str, chunks: AsyncIterable[bytes]) -> Path:
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename or Path(safe_name).suffix.casefold() != ".csv":
            raise CaseImportError("only a single CSV analysis file can be uploaded")

        upload_dir = self.root / str(uuid4())
        upload_dir.mkdir(parents=True, mode=0o700)
        destination = upload_dir / safe_name
        size = 0
        try:
            with destination.open("xb") as handle:
                os.chmod(destination, 0o600)
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise CaseImportError("CSV file exceeds the 512 MiB upload limit")
                    handle.write(chunk)
            if size == 0:
                raise CaseImportError("uploaded CSV is empty")
            return destination
        except Exception:
            destination.unlink(missing_ok=True)
            upload_dir.rmdir()
            raise


class LocalCaseImporter:
    def __init__(self, registry: DatasetRegistry | None = None) -> None:
        self.registry = registry or DatasetRegistry()

    def import_folder(self, folder: str | Path) -> LocalCaseImportResponse:
        root = Path(folder).expanduser()
        if not root.exists() or not root.is_dir():
            raise CaseImportError("case folder does not exist or is not a directory")
        root = root.resolve()

        files = [path for path in root.rglob("*") if path.is_file()]
        hidden_files = [path for path in files if _is_hidden_reference(path, root)]
        visible_files = [path for path in files if path not in hidden_files]
        csv_files = [path for path in visible_files if path.suffix.casefold() == ".csv"]
        if not csv_files:
            raise CaseImportError("case folder contains no supported CSV analysis data")

        main_data = min(csv_files, key=_main_csv_sort_key)
        inspection = _inspect_csv(main_data)
        roles = _infer_roles(inspection.columns)
        outcome = next((name for name in inspection.columns if roles[name] == "outcome"), None)
        exposure = next(
            (name for name in inspection.columns if roles[name] in {"exposure", "treatment"}),
            None,
        )
        if outcome is None:
            raise CaseImportError(
                "could not infer an outcome variable from the CSV header; add a supported outcome name or enter the case manually"
            )

        dataset_id = f"ds_{inspection.sha256[:16]}"
        dataset_ref = DatasetRef(
            dataset_id=dataset_id,
            role="main",
            filename=main_data.name,
            mime_type="text/csv",
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
        )
        self.registry.register(dataset_ref, main_data)

        case_title, research_question, statement = _research_text(
            main_data.stem, outcome, exposure
        )
        variables = [
            _variable_spec(
                name,
                roles[name],
                inspection.processed_definitions.get(name),
            )
            for name in inspection.columns
            if roles[name] != "unknown"
        ]
        has_id = any(variable.role == "id" for variable in variables)
        has_time = any(variable.role == "time" for variable in variables)
        sample_period = (
            f"{inspection.year_min}—{inspection.year_max}"
            if inspection.year_min is not None and inspection.year_max is not None
            else None
        )
        case = CaseSubmission(
            case_id=f"case_{inspection.sha256[:12]}",
            title=case_title,
            research_question=research_question,
            hypotheses=[
                Hypothesis(
                    hypothesis_id="H1",
                    statement=statement,
                    expected_direction="unspecified",
                )
            ],
            unit_of_analysis="企业—年度" if has_id and has_time else None,
            sample_period=sample_period,
            data_structure_hint="panel" if has_id and has_time else "unknown",
            variables=variables,
            dataset_refs=[dataset_ref],
            constraints=["变量角色仅依据数据表头保守推断，进入分析前须在 H1 人工确认。"],
        )

        excluded_files = [path for path in visible_files if path != main_data]
        review_items = [
            "请在 H1 确认研究问题、假设与变量角色。",
            "请确认样本筛选、缺失值和异常值处理规则。",
        ]
        normalized_columns = {_normalized(name) for name in inspection.columns}
        if {"sdlaw", "esgw"}.issubset(normalized_columns):
            review_items.append("数据同时包含原始与 _w 处理口径；系统默认使用一套 _w 字段，避免同一构念重复入模，请在 H1 确认该口径。")
        if not has_id or not has_time:
            review_items.append("请补充或确认面板数据的实体与时间主键。")
        if exposure is None:
            review_items.append("请补充或确认核心解释变量。")

        return LocalCaseImportResponse(
            case_submission=case,
            import_report=SafeImportReport(
                registered_dataset_id=dataset_id,
                main_data_filename=main_data.name,
                row_count=inspection.row_count,
                column_count=len(inspection.columns),
                year_min=inspection.year_min,
                year_max=inspection.year_max,
                hidden_file_count=len(hidden_files),
                excluded_file_count=len(excluded_files),
                human_review_items=review_items,
            ),
        )


def _is_hidden_reference(path: Path, root: Path) -> bool:
    if path.suffix.casefold() in HIDDEN_SUFFIXES:
        return True
    relative_parts = path.relative_to(root).parts[:-1]
    return any(
        any(marker in part.casefold() for marker in HIDDEN_PATH_MARKERS)
        for part in relative_parts
    )


def _main_csv_sort_key(path: Path) -> tuple[int, int, int, str]:
    is_canonical = path.name.casefold() == "main_data.csv"
    looks_like_data = "数据" in path.stem or "data" in path.stem.casefold()
    return (
        0 if is_canonical else 1,
        0 if looks_like_data else 1,
        -path.stat().st_size,
        path.name.casefold(),
    )


def _inspect_csv(path: Path) -> _CsvInspection:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)

    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            columns, row_count, year_min, year_max = _read_csv_metadata(path, encoding)
            break
        except UnicodeDecodeError as error:
            last_error = error
    else:
        raise CaseImportError("CSV encoding must be UTF-8 or GB18030") from last_error

    if not columns:
        raise CaseImportError("CSV header is empty")
    if len(set(columns)) != len(columns):
        raise CaseImportError("CSV header contains duplicate column names")
    return _CsvInspection(
        sha256=hasher.hexdigest(),
        size_bytes=path.stat().st_size,
        columns=columns,
        row_count=row_count,
        year_min=year_min,
        year_max=year_max,
        processed_definitions=_infer_processed_definitions(path, columns, encoding),
    )


def _read_csv_metadata(
    path: Path, encoding: str
) -> tuple[list[str], int, int | None, int | None]:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        try:
            columns = [column.strip() for column in next(reader)]
        except StopIteration:
            return [], 0, None, None
        if any(not column for column in columns):
            raise CaseImportError("CSV header contains an empty column name")
        year_index = next(
            (index for index, name in enumerate(columns) if _normalized(name) in {"year", "年份", "年度"}),
            None,
        )
        years: list[int] = []
        row_count = 0
        for row in reader:
            if not row:
                continue
            row_count += 1
            if year_index is not None and year_index < len(row):
                try:
                    value = float(row[year_index])
                    year = int(value)
                    if value == year and 1000 <= year <= 3000:
                        years.append(year)
                except (TypeError, ValueError):
                    pass
        return columns, row_count, min(years, default=None), max(years, default=None)


def _normalized(name: str) -> str:
    return re.sub(r"[\s_.\-]+", "", name).casefold()


def _infer_roles(columns: list[str]) -> dict[str, str]:
    id_names = {
        "id",
        "firmid",
        "companyid",
        "entityid",
        "stkcd",
        "stockcode",
        "证券代码",
        "股票代码",
        "企业代码",
    }
    time_names = {"year", "time", "年份", "年度"}
    outcome_names = {
        "sd",
        "sdla",
        "outcome",
        "dependent",
        "dependentvariable",
        "depvar",
        "y",
    }
    exposure_names = {"gf", "esg", "esgscore", "exposure", "x"}
    treatment_names = {"treat", "treatment", "treatpost", "did"}
    control_names = {
        "size",
        "board",
        "lev",
        "roa",
        "growth",
        "cf",
        "fa",
        "top1",
        "q",
        "indr",
        "age",
        "ci",
        "mh",
        "dual",
        "soe",
        "ana",
        "ins",
    }
    roles: dict[str, str] = {}
    for name in columns:
        normalized = _normalized(name)
        if normalized in id_names:
            role = "id"
        elif normalized in time_names:
            role = "time"
        elif normalized in outcome_names:
            role = "outcome"
        elif normalized in exposure_names:
            role = "exposure"
        elif normalized in treatment_names:
            role = "treatment"
        elif normalized in control_names:
            role = "control"
        else:
            role = "unknown"
        roles[name] = role

    # A table may contain both a raw variable and its processed `_w` version.
    # Keep one executable measurement set instead of placing both versions of
    # the same construct in the baseline model. H1 still exposes this choice.
    normalized_columns = {_normalized(name): name for name in columns}
    for name in columns:
        normalized = _normalized(name)
        if not normalized.endswith("w"):
            continue
        raw_name = normalized_columns.get(normalized[:-1])
        if raw_name and roles[raw_name] in {"outcome", "exposure", "control"}:
            roles[name] = roles[raw_name]
            roles[raw_name] = "unknown"
    return roles


_VARIABLE_LABELS = {
    "year": "年份",
    "证券代码": "企业证券代码",
    "sd": "省级可持续发展指数",
    "gf": "绿色金融综合指数",
    "sdla": "企业短债长用程度",
    "esg": "企业 ESG 表现",
    "size": "企业规模",
    "board": "董事会特征",
    "lev": "资产负债率",
    "roa": "总资产收益率",
    "growth": "企业成长性",
    "cf": "现金流",
    "fa": "固定资产特征",
    "top1": "第一大股东持股比例",
    "q": "企业市场价值指标",
    "indr": "独立董事特征",
    "age": "企业年龄",
    "ci": "资本密集度",
    "mh": "管理层持股特征",
    "dual": "两职合一",
    "soe": "国有企业标识",
    "ana": "分析师关注",
    "ins": "机构投资者持股",
}


def _variable_spec(
    name: str,
    role: str,
    processed_definition: str | None = None,
) -> VariableSpec:
    normalized = _normalized(name)
    processed = normalized.endswith("w") and normalized[:-1] in _VARIABLE_LABELS
    base = normalized[:-1] if processed else normalized
    label = _VARIABLE_LABELS.get(base, name)
    if role == "id" and base not in _VARIABLE_LABELS:
        label = "观测实体标识"
    definition = f"{label}字段。"
    if processed_definition:
        definition += processed_definition
    elif processed:
        definition += "当前采用数据表中的 _w 处理版本；具体处理规则需在 H1 结合变量字典确认。"
    elif role in {"outcome", "exposure", "control"}:
        definition += "具体计算口径需在 H1 结合变量字典确认。"
    return VariableSpec(
        name=name,
        label=label,
        role=role,
        definition=definition,
        source=(
            "用户导入的主分析数据表；处理口径已由同表原始列逐行校验，原始数据来源待补充。"
            if processed_definition
            else "用户导入的主分析数据表；原始数据来源待变量字典确认。"
        ),
    )


def _infer_processed_definitions(
    path: Path,
    columns: list[str],
    encoding: str,
) -> dict[str, str]:
    by_normalized = {_normalized(name): name for name in columns}
    pairs: list[tuple[str, str]] = []
    for processed in columns:
        normalized = _normalized(processed)
        if not normalized.endswith("w"):
            continue
        raw = by_normalized.get(normalized[:-1])
        if raw:
            pairs.append((raw, processed))
    if not pairs:
        return {}

    selected = [name for pair in pairs for name in pair]
    frame = pd.read_csv(
        path,
        encoding=encoding,
        usecols=lambda name: name in selected,
    )
    definitions: dict[str, str] = {}
    for raw_name, processed_name in pairs:
        raw = pd.to_numeric(frame[raw_name], errors="coerce")
        processed = pd.to_numeric(frame[processed_name], errors="coerce")
        valid = raw.notna() & processed.notna()
        if not valid.any():
            continue
        raw_values = raw[valid]
        processed_values = processed[valid]
        lower = float(processed_values.min())
        upper = float(processed_values.max())
        clipped = raw_values.clip(lower=lower, upper=upper)
        matches_clip = bool(
            np.isclose(
                clipped.to_numpy(dtype=float),
                processed_values.to_numpy(dtype=float),
                rtol=1e-9,
                atol=1e-12,
            ).all()
        )
        if not matches_clip:
            continue
        changed = int(
            (~np.isclose(
                raw_values.to_numpy(dtype=float),
                processed_values.to_numpy(dtype=float),
                rtol=1e-9,
                atol=1e-12,
            )).sum()
        )
        if changed:
            below = int((raw_values < lower).sum())
            above = int((raw_values > upper).sum())
            definitions[processed_name] = (
                f"经同表 {raw_name} 与 {processed_name} 逐行校验，"
                f"该列等价于将原始值截尾至 [{lower:.8g}, {upper:.8g}]；"
                f"下端 {below} 行、上端 {above} 行被调整。"
            )
        else:
            definitions[processed_name] = (
                f"经同表 {raw_name} 与 {processed_name} 逐行校验，两列在全部非缺失观测上数值一致。"
            )
    return definitions


def _research_text(
    data_stem: str, outcome: str, exposure: str | None
) -> tuple[str, str, str]:
    outcome_name = _normalized(outcome).removesuffix("w")
    exposure_name = _normalized(exposure).removesuffix("w") if exposure else None
    if outcome_name == "sdla" and exposure_name == "esg":
        return (
            "企业ESG表现与短债长用",
            "企业 ESG 表现是否与企业短债长用程度存在系统性关联？",
            "企业 ESG 表现与企业短债长用程度存在系统性关联。",
        )
    if outcome_name == "sd" and exposure_name == "gf":
        return (
            "绿色金融与省级可持续发展",
            "绿色金融水平是否与省级可持续发展存在系统性关联？",
            "绿色金融水平与省级可持续发展存在系统性关联。",
        )
    safe_title = re.sub(r"(?:[-_—]?(数据|data))$", "", data_stem, flags=re.IGNORECASE)
    if exposure:
        return (
            safe_title or "待确认研究案例",
            f"{exposure} 是否与 {outcome} 存在系统性关联？",
            f"{exposure} 与 {outcome} 存在系统性关联。",
        )
    return (
        safe_title or "待确认研究案例",
        f"哪些因素影响 {outcome}？",
        f"{outcome} 受案例中核心解释变量影响。",
    )
