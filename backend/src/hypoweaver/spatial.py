from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


SPATIAL_WEIGHTS_FILENAME = "spatial_weights.csv"


def is_spatial_weights_filename(filename: str) -> bool:
    return filename.casefold() == SPATIAL_WEIGHTS_FILENAME


@dataclass(frozen=True)
class SpatialWeights:
    labels: tuple[str, ...]
    matrix: np.ndarray

    @classmethod
    def from_csv(cls, path: Path) -> "SpatialWeights":
        frame = pd.read_csv(path, encoding="utf-8-sig")
        if frame.empty or frame.columns[0] != "spatial_id":
            raise ValueError("空间权重矩阵首列必须命名为 spatial_id。")
        row_labels = tuple(frame.iloc[:, 0].astype(str).str.strip())
        column_labels = tuple(str(value).strip() for value in frame.columns[1:])
        if not row_labels or len(set(row_labels)) != len(row_labels):
            raise ValueError("空间权重矩阵行标签必须非空且唯一。")
        if row_labels != column_labels:
            raise ValueError("空间权重矩阵的行列标签及顺序必须完全一致。")
        matrix = frame.iloc[:, 1:].apply(pd.to_numeric, errors="coerce").to_numpy(float)
        if matrix.shape != (len(row_labels), len(row_labels)):
            raise ValueError("空间权重矩阵必须是方阵。")
        if not np.isfinite(matrix).all() or (matrix < 0).any():
            raise ValueError("空间权重矩阵只能包含有限的非负数值。")
        if not np.allclose(np.diag(matrix), 0.0, atol=1e-12):
            raise ValueError("空间权重矩阵对角线必须为 0。")
        if not np.allclose(matrix.sum(axis=1), 1.0, atol=1e-8):
            raise ValueError("空间权重矩阵必须预先按行标准化。")
        return cls(labels=row_labels, matrix=matrix)

    def aligned(self, labels: list[str]) -> np.ndarray:
        normalized = [str(value).strip() for value in labels]
        if len(set(normalized)) != len(normalized):
            raise ValueError("主数据空间标识存在重复标签。")
        if set(normalized) != set(self.labels):
            missing = sorted(set(self.labels) - set(normalized))
            extra = sorted(set(normalized) - set(self.labels))
            details = []
            if missing:
                details.append("主数据缺少：" + "、".join(missing))
            if extra:
                details.append("矩阵缺少：" + "、".join(extra))
            raise ValueError("空间权重矩阵与主数据标签不一致；" + "；".join(details))
        positions = {label: index for index, label in enumerate(self.labels)}
        order = [positions[label] for label in normalized]
        return self.matrix[np.ix_(order, order)]
