from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from hypoweaver.models import ModelSpec
from hypoweaver.policy_causal import (
    POLICY_PRIMARY_IMPLEMENTATION_ID,
    POLICY_REPRODUCTION_IMPLEMENTATION_ID,
    PolicyContractError,
    construct_policy_exposure,
    estimate_policy_core,
    estimate_policy_model,
    parse_policy_design,
    reproduce_policy_model,
)


class PolicyCausalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.csv_path = self.root / "policy_panel.csv"
        self._write_synthetic_panel(self.csv_path)
        self.model = _policy_model()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_strict_contract_parser_and_exposure_weights(self) -> None:
        design = parse_policy_design(self.model)
        frame = pd.DataFrame(
            {
                "treated": [0, 1, 1, 1],
                "year": [2018, 2018, 2019, 2021],
            }
        )

        exposure = construct_policy_exposure(frame, design)

        self.assertEqual(exposure.tolist(), [0.0, 0.0, 0.5, 1.0])
        self.assertEqual(design.policy_start_month, 7)
        self.assertEqual(design.cluster_composition, "interaction")

        invalid = self.model.model_copy(deep=True)
        invalid.parameters["policy_design"]["unregistered_option"] = True
        with self.assertRaisesRegex(PolicyContractError, "unknown keys"):
            parse_policy_design(invalid)

        mismatch = self.model.model_copy(deep=True)
        mismatch.parameters["policy_design"]["fixed_effects"] = ["city", "year"]
        with self.assertRaisesRegex(PolicyContractError, "exactly match"):
            parse_policy_design(mismatch)

    def test_primary_absorbing_ls_recovers_frozen_did_effect(self) -> None:
        result = estimate_policy_model(self.csv_path, self.model)

        self.assertEqual(result["implementation_id"], POLICY_PRIMARY_IMPLEMENTATION_ID)
        self.assertAlmostEqual(
            result["primary_estimate"]["coefficient"],
            2.25,
            delta=0.15,
        )
        self.assertEqual(result["primary_estimate"]["term"], "policy_exposure")
        self.assertGreater(result["diagnostics"]["treated_entity_count"], 0)
        self.assertGreater(result["diagnostics"]["control_entity_count"], 0)
        self.assertEqual(result["event_study"]["joint_pretrend"]["status"], "tested")
        self.assertEqual(result["placebo"]["status"], "succeeded")
        self.assertEqual(result["permutation_placebo"]["status"], "succeeded")
        self.assertEqual(
            result["permutation_placebo"]["repetitions_completed"],
            25,
        )
        json.dumps(result, allow_nan=False)

    def test_event_study_does_not_generate_unobserved_calendar_year(self) -> None:
        result = estimate_policy_model(self.csv_path, self.model)
        event_study = result["event_study"]

        self.assertIn(2020, event_study["requested_event_years"])
        self.assertIn(2020, event_study["unavailable_event_years"])
        self.assertNotIn(2020, event_study["generated_event_years"])
        self.assertNotIn(
            "event_2020",
            {item["term"] for item in event_study["estimates"]},
        )
        self.assertIn(2020, result["diagnostics"]["missing_calendar_years"])
        self.assertEqual(result["diagnostics"]["calendar_years_imputed"], [])
        self.assertFalse(event_study["remote_pre_requested"])
        self.assertEqual(event_study["remote_pre_status"], "not_applicable")

    def test_fake_timing_uses_only_pre_policy_rows_and_ignores_post_policy_results(
        self,
    ) -> None:
        first = estimate_policy_core(self.csv_path, self.model)["placebo"]

        self.assertEqual(first["true_policy_contamination_rows"], 0)
        self.assertEqual(first["sample_end_year"], 2018)
        self.assertEqual(first["rows_used"], 48 * 4)
        self.assertEqual(first["observed_years"], [2015, 2016, 2017, 2018])
        self.assertEqual(first["time_period_count"], 4)
        self.assertEqual(first["entity_count"], 48)
        self.assertEqual(first["group_row_counts"], {"0": 96, "1": 96})
        self.assertEqual(first["entities_spanning_policy"], 0)
        self.assertEqual(first["rows_excluded_at_or_after_true_policy"], 48 * 3)
        self.assertTrue(first["pseudo_pre_support"])
        self.assertTrue(first["pseudo_post_support"])
        self.assertEqual(
            first["pseudo_period_group_row_counts"],
            {
                "control_pre": 48,
                "treated_pre": 48,
                "control_post": 48,
                "treated_post": 48,
            },
        )

        frame = pd.read_csv(self.csv_path)
        contaminated = (frame["year"] >= 2019) & (frame["treated"] == 1)
        frame.loc[contaminated, "y"] += 1000.0
        frame.to_csv(self.csv_path, index=False)

        second = estimate_policy_core(self.csv_path, self.model)["placebo"]
        self.assertAlmostEqual(
            first["estimate"]["coefficient"],
            second["estimate"]["coefficient"],
            places=12,
        )
        self.assertAlmostEqual(
            first["estimate"]["standard_error"],
            second["estimate"]["standard_error"],
            places=12,
        )

    def test_remote_pre_bin_is_structured_jointly_tested_and_reproduced(self) -> None:
        model = self.model.model_copy(deep=True)
        design = model.parameters["policy_design"]
        design["event_remote_pre_years"] = [2015, 2016]
        design["event_years"] = [2017, 2019, 2020, 2021, 2022]

        primary = estimate_policy_core(self.csv_path, model)["event_study"]
        reproduction = reproduce_policy_model(self.csv_path, model)["event_study"]

        self.assertEqual(primary["requested_remote_pre_years"], [2015, 2016])
        self.assertEqual(primary["generated_remote_pre_years"], [2015, 2016])
        self.assertEqual(primary["unavailable_remote_pre_years"], [])
        self.assertEqual(primary["remote_pre_term"], "event_remote_pre")
        self.assertTrue(primary["remote_pre_requested"])
        self.assertEqual(primary["remote_pre_status"], "complete")
        self.assertTrue(reproduction["remote_pre_requested"])
        self.assertEqual(reproduction["remote_pre_status"], "complete")
        self.assertEqual(
            primary["event_term_scaling"],
            "binary_group_year_contrast",
        )
        self.assertEqual(primary["policy_year_event_term"], "event_2019")
        self.assertEqual(primary["policy_year_event_regressor_weight"], 1.0)
        self.assertEqual(primary["baseline_policy_start_weight"], 0.5)
        self.assertFalse(
            primary["policy_year_event_uses_baseline_policy_start_weight"]
        )
        self.assertFalse(
            primary[
                "policy_year_event_coefficient_directly_comparable_to_baseline"
            ]
        )
        self.assertIn(
            "event_remote_pre",
            primary["joint_pretrend"]["terms"],
        )
        self.assertIn("event_2017", primary["joint_pretrend"]["terms"])
        primary_remote = next(
            item for item in primary["estimates"] if item["term"] == "event_remote_pre"
        )
        replica_remote = next(
            item
            for item in reproduction["estimates"]
            if item["term"] == "event_remote_pre"
        )
        self.assertEqual(primary_remote["event_bin"], "remote_pre")
        self.assertEqual(primary_remote["event_years"], [2015, 2016])
        self.assertIsNone(primary_remote["event_year"])
        self.assertIsNone(primary_remote["relative_year"])
        policy_year = next(
            item for item in primary["estimates"] if item["term"] == "event_2019"
        )
        self.assertEqual(policy_year["regressor_weight"], 1.0)
        self.assertEqual(
            policy_year["event_term_scaling"],
            "binary_group_year_contrast",
        )
        self.assertAlmostEqual(
            primary_remote["coefficient"],
            replica_remote["coefficient"],
            places=8,
        )
        self.assertAlmostEqual(
            primary_remote["standard_error"],
            replica_remote["standard_error"],
            places=8,
        )

    def test_remote_pre_bin_does_not_silently_drop_a_frozen_year(self) -> None:
        model = self.model.model_copy(deep=True)
        design = model.parameters["policy_design"]
        design["event_remote_pre_years"] = [2014, 2015]

        event_study = estimate_policy_core(self.csv_path, model)["event_study"]

        self.assertEqual(event_study["status"], "not_executed")
        self.assertTrue(event_study["remote_pre_requested"])
        self.assertEqual(event_study["remote_pre_status"], "incomplete")
        self.assertFalse(event_study["remote_pre_complete"])
        self.assertEqual(event_study["unavailable_remote_pre_years"], [2014])
        self.assertEqual(event_study["generated_remote_pre_years"], [])
        self.assertEqual(event_study["estimates"], [])

    def test_independent_numpy_reproduction_matches_primary_coefficient(self) -> None:
        primary = estimate_policy_model(self.csv_path, self.model)
        reproduction = reproduce_policy_model(self.csv_path, self.model)

        self.assertEqual(
            reproduction["implementation_id"],
            POLICY_REPRODUCTION_IMPLEMENTATION_ID,
        )
        self.assertNotEqual(
            primary["implementation_id"], reproduction["implementation_id"]
        )
        self.assertAlmostEqual(
            primary["primary_estimate"]["coefficient"],
            reproduction["primary_estimate"]["coefficient"],
            places=8,
        )
        self.assertIn(
            "within_iterations",
            reproduction["diagnostics"]["baseline_fit"],
        )

    def test_time_varying_group_is_reported_instead_of_silently_rewritten(self) -> None:
        frame = pd.read_csv(self.csv_path)
        frame.loc[
            (frame["firm"] == "F01") & (frame["year"] == 2021),
            "treated",
        ] = 0
        frame.to_csv(self.csv_path, index=False)

        result = estimate_policy_model(self.csv_path, self.model)

        self.assertEqual(result["diagnostics"]["group_switcher_entities"], 1)
        self.assertGreater(result["diagnostics"]["entities_spanning_policy"], 0)

    def test_fixed_pre_and_stable_only_group_sensitivities_are_explicit(self) -> None:
        frame = pd.read_csv(self.csv_path)
        frame.loc[
            (frame["firm"] == "F01") & (frame["year"] == 2021),
            "treated",
        ] = 0
        frame.to_csv(self.csv_path, index=False)

        fixed = self.model.model_copy(deep=True)
        fixed.parameters["policy_design"][
            "group_assignment_mode"
        ] = "fixed_last_pre_policy"
        stable = self.model.model_copy(deep=True)
        stable.parameters["policy_design"][
            "group_assignment_mode"
        ] = "stable_entities_only"

        fixed_result = estimate_policy_model(self.csv_path, fixed)
        stable_result = estimate_policy_model(self.csv_path, stable)

        self.assertEqual(
            fixed_result["diagnostics"]["source_group_switcher_entities"], 1
        )
        self.assertEqual(
            fixed_result["diagnostics"]["analysis_group_switcher_entities"], 0
        )
        self.assertEqual(
            fixed_result["diagnostics"]["group_assignment_mode"],
            "fixed_last_pre_policy",
        )
        self.assertEqual(
            stable_result["diagnostics"]["entities_dropped_as_group_switchers"],
            1,
        )
        self.assertLess(
            stable_result["diagnostics"]["rows_used"],
            fixed_result["diagnostics"]["rows_used"],
        )

    def test_permutation_placebo_is_seeded_and_reproducible(self) -> None:
        first = estimate_policy_model(self.csv_path, self.model)[
            "permutation_placebo"
        ]
        second = estimate_policy_model(self.csv_path, self.model)[
            "permutation_placebo"
        ]

        self.assertEqual(first["distribution_sha256"], second["distribution_sha256"])
        self.assertEqual(first["empirical_p_value"], second["empirical_p_value"])
        self.assertEqual(first["scheme"], "rowwise_exposure")
        self.assertGreaterEqual(first["empirical_p_value"], 1 / 26)

    def test_assignment_unit_permutation_preserves_unit_rows_and_treated_count(self) -> None:
        model = self.model.model_copy(deep=True)
        design = model.parameters["policy_design"]
        design["permutation_scheme"] = "assignment_unit_label"
        design["permutation_unit_field"] = "firm"

        result = estimate_policy_model(self.csv_path, model)[
            "permutation_placebo"
        ]

        self.assertEqual(result["scheme"], "assignment_unit_label")
        self.assertEqual(result["permutation_unit_field"], "firm")
        self.assertEqual(result["permutation_unit_count"], 48)
        self.assertEqual(result["treated_permutation_unit_count"], 24)
        self.assertIn("complete unit rows", result["interpretation_boundary"])

    @staticmethod
    def _write_synthetic_panel(path: Path) -> None:
        rng = np.random.default_rng(20260718)
        years = (2015, 2016, 2017, 2018, 2019, 2021, 2022)
        time_effects = {
            year: effect
            for year, effect in zip(years, (-0.4, -0.1, 0.2, 0.5, 0.1, 0.8, 1.0))
        }
        rows: list[dict[str, object]] = []
        for firm_index in range(48):
            firm = f"F{firm_index:02d}"
            treated = firm_index % 2
            firm_effect = rng.normal(scale=1.2)
            city = f"C{firm_index % 6}"
            industry = f"I{firm_index % 4}"
            for year in years:
                policy_weight = 0.0 if year < 2019 else (0.5 if year == 2019 else 1.0)
                exposure = treated * policy_weight
                x = rng.normal() + (year - 2015) * 0.08 + firm_index * 0.01
                noise = rng.normal(scale=0.04)
                y = (
                    firm_effect
                    + time_effects[year]
                    + 2.25 * exposure
                    + 0.4 * x
                    + noise
                )
                rows.append(
                    {
                        "firm": firm,
                        "year": year,
                        "treated": treated,
                        "city": city,
                        "industry": industry,
                        "x": x,
                        "y": y,
                    }
                )
        pd.DataFrame(rows).to_csv(path, index=False)


def _policy_model() -> ModelSpec:
    policy_design = {
        "group_field": "treated",
        "time_field": "year",
        "policy_start_year": 2019,
        "policy_start_month": 7,
        "policy_start_weight": 0.5,
        "post_start_weight": 1.0,
        "exposure_name": "policy_exposure",
        "fixed_effects": ["firm", "year"],
        "cluster_fields": ["city", "industry", "year"],
        "cluster_composition": "interaction",
        "event_reference_year": 2018,
        "event_years": [2016, 2017, 2019, 2020, 2021, 2022],
        "event_term_scaling": "binary_group_year_contrast",
        "placebo_start_year": 2017,
        "placebo_repetitions": 25,
        "permutation_scheme": "rowwise_exposure",
        "random_seed": 20260718,
        "group_assignment_mode": "observed_time_varying",
    }
    return ModelSpec(
        step_id="did_baseline",
        name="frozen policy DID",
        rationale="Estimate a pre-registered policy exposure.",
        estimator="absorbing DID",
        outcome="y",
        treatments_or_exposures=["policy_exposure"],
        controls=["x"],
        fixed_effects=["firm", "year"],
        standard_error_strategy="cluster interaction(city, industry, year)",
        parameters={"policy_design": policy_design},
    )


if __name__ == "__main__":
    unittest.main()
