"""Pure learning and prediction functions for Evohome Optimum Start."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any

try:
    from .const import (
        MAX_OUTDOOR_SLOPE,
        MAX_RATE,
        MAX_SAMPLES,
        MIN_RATE,
        REFERENCE_OUTDOOR_C,
    )
except ImportError:  # Allows this pure module to be tested without Home Assistant.
    from const import (  # type: ignore[no-redef]
        MAX_OUTDOOR_SLOPE,
        MAX_RATE,
        MAX_SAMPLES,
        MIN_RATE,
        REFERENCE_OUTDOOR_C,
    )


def clamp(value: float, low: float, high: float) -> float:
    """Clamp a numeric value."""
    return max(low, min(high, value))


@dataclass(slots=True)
class HeatingSample:
    """A measured zone heating run."""

    timestamp: str
    outdoor_c: float
    start_c: float
    end_c: float
    target_c: float
    minutes: float
    reached_target: bool

    @property
    def rise_c(self) -> float:
        """Return the useful temperature rise."""
        return max(0.0, self.end_c - self.start_c)

    @property
    def rate_c_per_hour(self) -> float:
        """Return measured average heating rate."""
        if self.minutes <= 0:
            return 0.0
        return self.rise_c * 60.0 / self.minutes

    def as_dict(self) -> dict[str, Any]:
        """Serialize the sample."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HeatingSample | None:
        """Deserialize a sample, rejecting corrupt data."""
        try:
            sample = cls(
                timestamp=str(value["timestamp"]),
                outdoor_c=float(value["outdoor_c"]),
                start_c=float(value["start_c"]),
                end_c=float(value["end_c"]),
                target_c=float(value["target_c"]),
                minutes=float(value["minutes"]),
                reached_target=bool(value["reached_target"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        numbers = (
            sample.outdoor_c,
            sample.start_c,
            sample.end_c,
            sample.target_c,
            sample.minutes,
        )
        if not all(isfinite(number) for number in numbers) or sample.minutes <= 0:
            return None
        return sample


@dataclass(slots=True)
class HeatingModel:
    """A small outdoor-compensated regression model."""

    initial_rate: float
    samples: list[HeatingSample]

    def add(self, sample: HeatingSample, minimum_rise: float) -> bool:
        """Add a plausible learning sample."""
        rate = sample.rate_c_per_hour
        if (
            sample.rise_c < minimum_rise
            or sample.minutes < 5
            or not MIN_RATE <= rate <= MAX_RATE
            or not -40 <= sample.outdoor_c <= 50
        ):
            return False
        self.samples.append(sample)
        del self.samples[:-MAX_SAMPLES]
        return True

    def fit(self) -> tuple[float, float]:
        """Return base rate at 10 C and rate change per outdoor degree.

        Recent observations receive more weight. With fewer than three samples,
        use a conservative blend toward the configured initial rate.
        """
        if not self.samples:
            return clamp(self.initial_rate, MIN_RATE, MAX_RATE), 0.0

        usable = [
            sample
            for sample in self.samples
            if MIN_RATE <= sample.rate_c_per_hour <= MAX_RATE
        ]
        if not usable:
            return clamp(self.initial_rate, MIN_RATE, MAX_RATE), 0.0

        sample_count = len(usable)
        confidence = sample_count / (sample_count + 0.5)
        if sample_count < 3:
            measured = sum(sample.rate_c_per_hour for sample in usable) / sample_count
            base = self.initial_rate * (1 - confidence) + measured * confidence
            return clamp(base, MIN_RATE, MAX_RATE), 0.0

        # Weighted least squares for y = intercept + slope * (outdoor - 10).
        # An exponential weight gives the latest sample roughly twice the weight
        # of a sample ten cycles older without making a single run dominant.
        weighted: list[tuple[float, float, float]] = []
        newest = len(usable) - 1
        for index, sample in enumerate(usable):
            age = newest - index
            weight = 0.93**age
            weighted.append(
                (
                    sample.outdoor_c - REFERENCE_OUTDOOR_C,
                    sample.rate_c_per_hour,
                    weight,
                )
            )

        sum_w = sum(weight for _, _, weight in weighted)
        mean_x = sum(x * weight for x, _, weight in weighted) / sum_w
        mean_y = sum(y * weight for _, y, weight in weighted) / sum_w
        variance_x = sum(weight * (x - mean_x) ** 2 for x, _, weight in weighted)
        covariance = sum(
            weight * (x - mean_x) * (y - mean_y) for x, y, weight in weighted
        )
        slope = covariance / variance_x if variance_x >= 1.0 else 0.0
        slope = clamp(slope, -MAX_OUTDOOR_SLOPE, MAX_OUTDOOR_SLOPE)
        intercept = mean_y - slope * mean_x

        # Keep the same monotonic confidence rule when regression becomes possible.
        # The previous split dropped confidence from 2/3 with two samples to 1/4
        # with three, which could abruptly return a successfully learning zone to
        # the maximum lead time.
        base = self.initial_rate * (1 - confidence) + intercept * confidence
        return clamp(base, MIN_RATE, MAX_RATE), slope

    def predict_rate(self, outdoor_c: float) -> float:
        """Predict the current heating rate."""
        base, slope = self.fit()
        predicted = base + slope * (outdoor_c - REFERENCE_OUTDOOR_C)
        return clamp(predicted, MIN_RATE, MAX_RATE)

    def predict_lead_minutes(
        self,
        *,
        current_c: float,
        target_c: float,
        outdoor_c: float,
        warmup_delay: float,
        safety_margin: float,
        maximum_lead: float,
    ) -> tuple[float, float, bool]:
        """Return lead, predicted rate, and whether the lead was capped."""
        rate = self.predict_rate(outdoor_c)
        rise = max(0.0, target_c - current_c)
        # A measured rate is an effective rate from override start to target reach,
        # so it already includes the normal valve/boiler response delay. Retain the
        # configured warm-up delay only for the unlearned initial estimate; adding
        # it again after learning would double-count that delay.
        startup_delay = 0.0 if self.samples else warmup_delay
        raw_lead = startup_delay + safety_margin + rise * 60.0 / rate
        return min(maximum_lead, raw_lead), rate, raw_lead > maximum_lead
