"""
Unit tests for Daily Story Free Summon module and DailyRunner options
"""

import sys
from pathlib import Path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import unittest
from src.workflows.daily_runner import DailyTaskReport, DailyRunner


class TestDailyStorySummon(unittest.TestCase):
    def test_daily_report_fields(self):
        report = DailyTaskReport(
            success=True,
            device_serial="test_device",
            story_summon_completed=True,
            fp_summon_completed=True,
            quest_clears=3,
            master_mission_claimed=True
        )
        self.assertTrue(report.story_summon_completed)
        self.assertTrue(report.fp_summon_completed)
        self.assertEqual(report.quest_clears, 3)
        self.assertFalse(hasattr(report, "giftbox_claimed"))

    def test_daily_runner_dry_run_story_option(self):
        runner = DailyRunner(dry_run=True)
        report = runner.run(
            target_quest="daily_qp_extreme",
            repeat_runs=0,
            options={
                "enable_story_summon": True,
                "enable_fp": True,
                "enable_quest": False,
                "enable_reward": False
            }
        )
        self.assertTrue(report.success)


if __name__ == "__main__":
    unittest.main()
