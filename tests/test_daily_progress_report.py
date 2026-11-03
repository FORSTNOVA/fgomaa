from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.workflows.daily_runner import DailyRunner
from src.strategy.smart_engine import SmartEngine
from src.strategy.screen_detector import GameScreen


class TestDailyProgressReport(unittest.TestCase):
    def test_partial_failure_preserves_actual_progress_and_strategy(self):
        manager = MagicMock()
        manager.get_connected_devices.return_value = [SimpleNamespace(serial="test", model="test")]
        engine = SimpleNamespace(run_reactive_daily=lambda **_: False, active_strategy=None,
            last_daily_strategy="verified", daily_progress={"story_summon_completed": True,
                "fp_summon_completed": False, "quest_clears": 1, "master_mission_claimed": False})
        with patch("src.workflows.daily_runner.FGOController"), \
             patch("src.workflows.daily_runner.SmartEngine", return_value=engine):
            report = DailyRunner(device_manager=manager).run()
        self.assertFalse(report.success)
        self.assertTrue(report.story_summon_completed)
        self.assertEqual(report.quest_clears, 1)
        self.assertFalse(report.master_mission_claimed)
        self.assertEqual(report.matched_team_strategy, "verified")

    def test_unknown_screen_limit_is_failure_even_when_tasks_are_disabled(self):
        ctrl = MagicMock(cancel_event=None)
        engine = SmartEngine(ctrl, log_callback=lambda _: None)
        engine.get_current_screen = MagicMock(return_value=(GameScreen.UNKNOWN, {}))
        with patch("src.strategy.smart_engine.time.sleep"):
            self.assertFalse(engine.run_reactive_daily(repeat_runs=0, options={
                "enable_story_summon": False, "enable_fp": False,
                "enable_quest": False, "enable_reward": False}))
        self.assertEqual(engine.get_current_screen.call_count, 500)
        ctrl.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
