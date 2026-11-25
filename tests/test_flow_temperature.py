"""Tests for terminal approach analysis and manual flow advice."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "evohome_optimum_start"
spec = importlib.util.spec_from_file_location(
    "flow_temperature", COMPONENT / "flow_temperature.py"
)
assert spec and spec.loader
flow_temperature = importlib.util.module_from_spec(spec)
sys.modules["flow_temperature"] = flow_temperature
spec.loader.exec_module(flow_temperature)

analyse_terminal_approach = flow_temperature.analyse_terminal_approach
recommend_flow_temperature = flow_temperature.recommend_flow_temperature


def trace(*temperatures: float, spacing: int = 10) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
    return [
        {
            "at": (start + timedelta(minutes=index * spacing)).isoformat(),
            "temperature_c": temperature,
        }
        for index, temperature in enumerate(temperatures)
    ]


class TerminalApproachTests(unittest.TestCase):
    def test_detects_long_unfinished_terminal_approach(self) -> None:
        result = analyse_terminal_approach(
            trace(18.0, 19.0, 19.5, 19.6, 19.65, 19.7, spacing=10),
            target_c=20.0,
            tolerance_c=0.2,
        )

        self.assertEqual(result["terminal_assessment"], "possible_capacity_limit")
        self.assertEqual(result["minutes_within_0_5_c_below_target"], 30.0)
        self.assertEqual(result["overshoot_c"], 0.0)

    def test_records_normal_arrival_and_overshoot(self) -> None:
        result = analyse_terminal_approach(
            trace(18.0, 19.0, 19.5, 19.8, 20.0, 20.3),
            target_c=20.0,
            tolerance_c=0.2,
        )

        self.assertEqual(result["terminal_assessment"], "normal_or_controlled_approach")
        self.assertEqual(result["overshoot_c"], 0.3)


class FlowAdviceTests(unittest.TestCase):
    def test_weather_curve_is_anchored_and_bounded(self) -> None:
        mild = recommend_flow_temperature(
            outdoor_c=12,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=45,
            maximum_flow_c=65,
            outcomes=[],
        )
        cold = recommend_flow_temperature(
            outdoor_c=-10,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=45,
            maximum_flow_c=65,
            outcomes=[],
        )

        self.assertEqual(mild["suggested_flow_temperature_c"], 50)
        self.assertEqual(cold["suggested_flow_temperature_c"], 65)
        self.assertEqual(mild["confidence"], "low")

    def test_two_independent_limited_zones_raise_curve_cautiously(self) -> None:
        outcomes = [
            {
                "zone_id": zone,
                "observation_status": "complete",
                "temperature_error_c": -0.5,
                "terminal_assessment": "possible_capacity_limit",
                "outdoor_temperature_c": 7.5,
            }
            for zone in ("living", "kitchen")
        ]
        result = recommend_flow_temperature(
            outdoor_c=7,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=45,
            maximum_flow_c=65,
            outcomes=outcomes,
        )

        self.assertEqual(result["weather_curve_temperature_c"], 55.0)
        self.assertEqual(result["evidence_adjustment_c"], 2.0)
        self.assertEqual(result["suggested_flow_temperature_c"], 57)
        self.assertEqual(result["confidence"], "medium")

    def test_forecast_anticipates_cold_but_ignores_small_change(self) -> None:
        result = recommend_flow_temperature(
            outdoor_c=10,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=50,
            maximum_flow_c=65,
            outcomes=[],
            forecast_outdoor_temperatures=[6, 4, 2, 2, 2],
            current_flow_c=55,
            minimum_change_c=3,
        )

        self.assertEqual(result["design_outdoor_temperature_c"], 2.0)
        self.assertEqual(result["calculated_flow_temperature_c"], 60)
        self.assertEqual(result["suggested_flow_temperature_c"], 60)
        self.assertTrue(result["change_recommended"])
        self.assertEqual(result["recommendation"], "increase")

    def test_stable_mild_forecast_can_recommend_reduction(self) -> None:
        result = recommend_flow_temperature(
            outdoor_c=12,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=50,
            maximum_flow_c=65,
            outcomes=[],
            forecast_outdoor_temperatures=[12, 13, 14, 12, 13],
            current_flow_c=55,
            minimum_change_c=3,
        )

        self.assertEqual(result["calculated_flow_temperature_c"], 50)
        self.assertEqual(result["suggested_flow_temperature_c"], 50)
        self.assertEqual(result["recommendation"], "decrease")

    def test_no_forecast_prevents_a_short_lived_reduction(self) -> None:
        result = recommend_flow_temperature(
            outdoor_c=12,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=50,
            maximum_flow_c=65,
            outcomes=[],
            current_flow_c=55,
            minimum_change_c=3,
        )

        self.assertEqual(result["calculated_flow_temperature_c"], 50)
        self.assertEqual(result["suggested_flow_temperature_c"], 55)
        self.assertFalse(result["change_recommended"])
        self.assertEqual(result["recommendation"], "keep_current_forecast_unavailable")

    def test_forecast_hysteresis_keeps_current_setting(self) -> None:
        result = recommend_flow_temperature(
            outdoor_c=7,
            reference_flow_c=55,
            reference_outdoor_c=7,
            curve_slope=1,
            minimum_flow_c=50,
            maximum_flow_c=65,
            outcomes=[],
            forecast_outdoor_temperatures=[6, 6, 6],
            current_flow_c=55,
            minimum_change_c=3,
        )

        self.assertEqual(result["calculated_flow_temperature_c"], 56)
        self.assertEqual(result["suggested_flow_temperature_c"], 55)
        self.assertFalse(result["change_recommended"])
        self.assertEqual(result["recommendation"], "keep_current")


if __name__ == "__main__":
    unittest.main()
