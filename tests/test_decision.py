"""Unit tests for the pure optimum-start decision path."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "evohome_optimum_start"
sys.path.insert(0, str(COMPONENT))
spec = importlib.util.spec_from_file_location("decision", COMPONENT / "decision.py")
assert spec and spec.loader
decision = importlib.util.module_from_spec(spec)
sys.modules["decision"] = decision
spec.loader.exec_module(decision)

DecisionInput = decision.DecisionInput
decide = decision.decide


class DecisionTests(unittest.TestCase):
    """Exercise the overnight regression and safety gates."""

    def setUp(self) -> None:
        self.switch = datetime(2026, 11, 24, 7, 30, tzinfo=UTC)

    def inputs(self, **changes: object) -> object:
        values = {
            "now": self.switch - timedelta(hours=2),
            "enabled": True,
            "system_mode": "Auto",
            "zone_mode": "FollowSchedule",
            "current_c": 17.5,
            "scheduled_target_c": 14.0,
            "next_target_c": 20.0,
            "next_switch": self.switch,
            "target_tolerance": 0.2,
            "lead_minutes": 180.0,
            "lead_capped": True,
        }
        values.update(changes)
        return DecisionInput(**values)

    def test_rechecks_room_after_early_already_at_target_state(self) -> None:
        midnight = decide(
            self.inputs(
                now=self.switch - timedelta(hours=7, minutes=26),
                current_c=20.0,
            )
        )
        morning = decide(self.inputs())

        self.assertEqual(midnight.status, "already_at_next_target")
        self.assertFalse(midnight.should_start)
        self.assertEqual(morning.status, "waiting_for_capped_start")
        self.assertTrue(morning.should_start)
        self.assertEqual(morning.predicted_start, self.switch - timedelta(hours=3))

    def test_waits_until_predicted_start(self) -> None:
        result = decide(
            self.inputs(
                now=self.switch - timedelta(hours=2),
                lead_minutes=90,
                lead_capped=False,
            )
        )
        self.assertEqual(result.status, "waiting_for_predicted_start")
        self.assertFalse(result.should_start)

    def test_manual_override_is_protected(self) -> None:
        result = decide(self.inputs(zone_mode="TemporaryOverride"))
        self.assertEqual(result.status, "protected_manual_zone_override")
        self.assertFalse(result.should_start)

    def test_non_auto_system_mode_is_protected(self) -> None:
        result = decide(self.inputs(system_mode="Away"))
        self.assertEqual(result.status, "protected_system_mode_Away")
        self.assertFalse(result.should_start)

    def test_elapsed_switchpoint_waits_for_fresh_schedule(self) -> None:
        result = decide(self.inputs(now=self.switch + timedelta(minutes=1)))
        self.assertEqual(result.status, "waiting_for_next_schedule_update")
        self.assertFalse(result.should_start)


if __name__ == "__main__":
    unittest.main()
