"""Unit tests for the pure learning model."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "evohome_optimum_start"
sys.path.insert(0, str(COMPONENT))
spec = importlib.util.spec_from_file_location("learning", COMPONENT / "learning.py")
assert spec and spec.loader
learning = importlib.util.module_from_spec(spec)
sys.modules["learning"] = learning
spec.loader.exec_module(learning)

HeatingModel = learning.HeatingModel
HeatingSample = learning.HeatingSample


def sample(outdoor: float, rate: float, index: int) -> object:
    minutes = 60.0 / rate
    return HeatingSample(
        timestamp=f"2026-01-{index + 1:02d}T07:00:00+00:00",
        outdoor_c=outdoor,
        start_c=18.0,
        end_c=19.0,
        target_c=19.0,
        minutes=minutes,
        reached_target=True,
    )


class HeatingModelTests(unittest.TestCase):
    def test_initial_prediction_and_cap(self) -> None:
        model = HeatingModel(0.7, [])
        lead, rate, capped = model.predict_lead_minutes(
            current_c=16,
            target_c=21,
            outdoor_c=0,
            warmup_delay=10,
            safety_margin=10,
            maximum_lead=180,
        )
        self.assertEqual(rate, 0.7)
        self.assertEqual(lead, 180)
        self.assertTrue(capped)

    def test_rejects_tiny_or_implausible_samples(self) -> None:
        model = HeatingModel(0.7, [])
        tiny = HeatingSample(
            timestamp="2026-01-01T07:00:00+00:00",
            outdoor_c=5,
            start_c=18.0,
            end_c=18.1,
            target_c=20,
            minutes=30,
            reached_target=False,
        )
        self.assertFalse(model.add(tiny, 0.3))
        self.assertEqual(model.samples, [])

    def test_learns_outdoor_compensation(self) -> None:
        model = HeatingModel(0.7, [])
        observations = [
            (-5, 0.45),
            (0, 0.55),
            (5, 0.65),
            (10, 0.75),
            (15, 0.85),
            (20, 0.95),
        ]
        for index, (outdoor, rate) in enumerate(observations):
            self.assertTrue(model.add(sample(outdoor, rate, index), 0.3))
        cold = model.predict_rate(-5)
        mild = model.predict_rate(15)
        base, slope = model.fit()
        self.assertGreater(mild, cold)
        self.assertGreater(slope, 0)
        self.assertGreater(base, 0.6)

    def test_early_samples_blend_with_initial_rate(self) -> None:
        model = HeatingModel(0.7, [])
        self.assertTrue(model.add(sample(5, 1.0, 0), 0.3))
        predicted = model.predict_rate(5)
        self.assertGreater(predicted, 0.7)
        self.assertLess(predicted, 1.0)

    def test_accepts_plausible_fast_room_sample(self) -> None:
        model = HeatingModel(0.7, [])
        fast = HeatingSample(
            timestamp="2026-11-26T05:45:49+00:00",
            outdoor_c=3.9,
            start_c=15.1,
            end_c=19.8,
            target_c=20.0,
            minutes=75.3,
            reached_target=True,
        )

        self.assertTrue(model.add(fast, 0.3))
        self.assertAlmostEqual(model.samples[0].rate_c_per_hour, 3.745, places=3)

    def test_confidence_increases_when_third_sample_enables_regression(self) -> None:
        model = HeatingModel(0.7, [])
        predicted_rates: list[float] = []
        for index in range(3):
            self.assertTrue(model.add(sample(5, 2.0, index), 0.3))
            predicted_rates.append(model.predict_rate(5))

        self.assertGreater(predicted_rates[1], predicted_rates[0])
        self.assertGreaterEqual(predicted_rates[2], predicted_rates[1])

    def test_learned_effective_rate_does_not_double_count_warmup_delay(self) -> None:
        model = HeatingModel(1.0, [sample(5, 1.0, 0)])

        lead, _rate, _capped = model.predict_lead_minutes(
            current_c=19,
            target_c=20,
            outdoor_c=5,
            warmup_delay=10,
            safety_margin=10,
            maximum_lead=180,
        )

        self.assertEqual(lead, 70.0)


if __name__ == "__main__":
    unittest.main()
