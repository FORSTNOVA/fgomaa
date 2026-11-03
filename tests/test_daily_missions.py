import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from src.strategy.daily_missions import DailyMissions


def word(text, x, y):
    return ([[x-50, y-15], [x+50, y-15], [x+50, y+15], [x-50, y+15]], text, 0.98)


def page(titles, claimed=True, summary="4/4"):
    rows = [word("日常", 965, 214), word("全部", 1760, 319), word(summary, 1550, 320)]
    for i, title in enumerate(titles):
        y = 450 + i*230
        rows.append(word(title, 1150, y))
        if claimed:
            rows.append(word("已自动领取", 1750, y+50))
    return rows


class TestDailyMissions(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.engine.is_cancelled.return_value = False
        self.helper = DailyMissions(self.engine)
        self.img = Image.new("RGB", (1920, 1080), (50, 50, 50))
        self.img.paste((40, 150, 245), (890, 192, 1040, 236))
        self.top = page(["通关1次关卡", "通关2次关卡", "通关3次关卡"])
        self.bottom = page(["通关3次关卡", "完成所有的每日御主任务"])

    def test_selected_daily_tab_and_four_card_scan(self):
        self.assertTrue(self.helper.active(self.img, self.top))
        self.helper.read = MagicMock(side_effect=[(self.img, self.top), (self.img, self.bottom)])
        self.assertTrue(self.helper.scan())
        self.assertEqual(self.engine.ctrl.swipe.call_count, 1)

    def test_completed_summary_alone_does_not_prove_claim(self):
        self.helper.read = MagicMock(return_value=(self.img, page(["通关1次关卡"], claimed=False)))
        self.assertFalse(self.helper.scan())

    def test_incomplete_tasks_stop_without_claim_success(self):
        self.helper.read = MagicMock(return_value=(self.img, page(["通关1次关卡"], summary="2/4")))
        self.assertFalse(self.helper.scan())
        self.engine.ctrl.click.assert_not_called()

    def test_previous_tab_claim_button_is_not_reused(self):
        old = [word("日常", 965, 214), word("统一领取", 1775, 319)]
        self.helper.read = MagicMock(return_value=(self.img, self.top))
        with patch.object(self.helper, "scan", return_value=True):
            self.assertTrue(self.helper.claim(old))
        self.assertEqual([c.args[:2] for c in self.engine.ctrl.click.call_args_list], [(965, 214)])


if __name__ == "__main__":
    unittest.main()
