from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = PROJECT_ROOT.parent / "benchmark-cases"
SOURCE = (
    BENCHMARK_ROOT
    / "case_001_green_finance_sustainable_development"
    / "01_model_input"
    / "main_data.csv"
)
TARGET = (
    BENCHMARK_ROOT
    / "case_001_green_finance_spatial_method_aligned"
    / "01_model_input"
)
EARTH_RADIUS_KM = 6371.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    frame = pd.read_csv(SOURCE)
    coordinates_path = TARGET / "province_capital_coordinates.csv"
    coordinates = pd.read_csv(coordinates_path)
    province_order = list(dict.fromkeys(frame["province"].astype(str)))
    if province_order != coordinates["province"].astype(str).tolist():
        raise ValueError("coordinate rows must match the main-data province order")
    if (frame["SD"] <= 0).any():
        raise ValueError("lnSD requires strictly positive SD values")

    output = frame.copy()
    output.insert(output.columns.get_loc("SD"), "lnSD", np.log(output["SD"]))
    output.insert(
        output.columns.get_loc("GF"),
        "zGF",
        (output["GF"] - output["GF"].mean()) / output["GF"].std(ddof=1),
    )
    output.insert(
        output.columns.get_loc("FDI"),
        "zFDI",
        (output["FDI"] - output["FDI"].mean()) / output["FDI"].std(ddof=1),
    )
    main_path = TARGET / "main_data.csv"
    output.to_csv(main_path, index=False, float_format="%.12g")

    latitudes = np.deg2rad(coordinates["latitude"].to_numpy(float))
    longitudes = np.deg2rad(coordinates["longitude"].to_numpy(float))
    delta_lat = latitudes[:, None] - latitudes[None, :]
    delta_lon = longitudes[:, None] - longitudes[None, :]
    haversine = (
        np.sin(delta_lat / 2) ** 2
        + np.cos(latitudes[:, None])
        * np.cos(latitudes[None, :])
        * np.sin(delta_lon / 2) ** 2
    )
    distances = 2 * EARTH_RADIUS_KM * np.arctan2(
        np.sqrt(haversine),
        np.sqrt(1 - haversine),
    )
    weights = 1 / (1 + distances / 1000)
    np.fill_diagonal(weights, 0.0)
    weights /= weights.sum(axis=1, keepdims=True)
    weights_path = TARGET / "spatial_weights.csv"
    weights_frame = pd.DataFrame(weights, columns=province_order)
    weights_frame.insert(0, "spatial_id", province_order)
    weights_frame.to_csv(weights_path, index=False, float_format="%.12f")

    metadata = {
        "asset_status": "reconstructed_public_rule",
        "matrix_scope": "30 province-level administrative capitals present in main_data.csv",
        "coordinate_source": "https://simplemaps.com/data/cn-cities/world-cities-cn.csv",
        "coordinate_license": "MIT",
        "coordinate_snapshot_date": "2026-07-15",
        "distance": "Haversine great-circle distance in kilometers",
        "weight_formula": "off_diagonal=1/(1+distance_km/1000); diagonal=0; row_standardized=true",
        "earth_radius_km": EARTH_RADIUS_KM,
        "known_ambiguity": (
            "The disclosed prose mentions pilot provinces, while the disclosed formula is an all-pairs "
            "province matrix. This asset implements the explicit all-pairs formula and is not claimed "
            "to be an author's original matrix."
        ),
        "source_main_data_sha256": sha256(SOURCE),
        "coordinate_sha256": sha256(coordinates_path),
        "main_data_sha256": sha256(main_path),
        "spatial_weights_sha256": sha256(weights_path),
        "row_sum_max_error": float(np.max(np.abs(weights.sum(axis=1) - 1))),
    }
    metadata_path = TARGET / "spatial_weights_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    case_root = TARGET.parent
    visible_files = sorted(path for path in TARGET.iterdir() if path.is_file())
    manifest = {
        "case_id": "case_001_green_finance_spatial_method_aligned",
        "track": "method_aligned_blind",
        "visible_files": [
            {
                "path": str(path.relative_to(case_root)),
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for path in visible_files
        ],
        "hidden_reference": (
            "Shared with case_001_green_finance_sustainable_development and available only after sealing."
        ),
    }
    (case_root / "case_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
