import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from PIL import Image

from src.strategy.screen_detector import GameScreen, ScreenDetector
from src.strategy.smart_engine import SmartEngine
from src.workflows.daily_runner import DailyRunner


def word(text, x=960, y=500):
    return ([[x - 50, y - 15], [x + 50, y - 15], [x + 50, y + 15], [x - 50, y + 15]], text, 0.95)


def details(*words):
    return {"ocr_results": list(words), "all_texts": [w[1] for w in words]}


class TestSummonNavigation(unittest.TestCase):
    def engine(self):
        return SmartEngine(controller=MagicMock(), log_callback=lambda _: None)

    def detect(self, *words):
        with patch.object(ScreenDetector, "run_ocr", return_value=list(words)):
            return ScreenDetector.detect(Image.new("RGB", (1920, 1080), (100, 100, 100)))[0]

    def test_pool_with_ce_advertisement_is_not_detail(self):
        self.assertEqual(self.detect(word("关闭", 163, 63), word("灵基召唤", 1740, 50),
                                     word("概念礼装"), word("最大HP"), word("最大ATK")), GameScreen.SUMMON_SCREEN)

    def test_real_ce_selection_title_is_preserved(self):
        self.assertEqual(self.detect(word("关闭", 163, 63), word("概念礼装选择", 1600, 60)),
                         GameScreen.CRAFT_ESSENCE_SELECT)

    def test_real_details_require_multiple_fields(self):
        self.assertEqual(self.detect(word("关闭", 163, 63), word("最大HP"), word("最大ATK")),
                         GameScreen.CRAFT_ESSENCE_SELECT)
        self.assertNotEqual(self.detect(word("关闭", 163, 63), word("概念礼装")),
                            GameScreen.CRAFT_ESSENCE_SELECT)

    def test_summon_result_precedes_burn_and_ce_rules(self):
        self.assertEqual(self.detect(word("关闭", 163, 63), word("所持概念礼装"),
                                     word("编队"), word("强化"), word("灵基变还"), word("已召唤")),
                         GameScreen.SUMMON_RESULT)

    def test_bottom_tabs_do_not_confirm_active_pool(self):
        self.assertFalse(SmartEngine._is_summon_pool([word("剧情召唤", 710, 986)], "story"))
        self.assertFalse(SmartEngine._is_summon_pool([word("友情点召唤", 1000, 987)], "fp"))
        self.assertFalse(SmartEngine._is_summon_pool([word("消耗友情点", 920, 870)], "fp"))
        self.assertTrue(SmartEngine._is_summon_pool([word("友情点召唤", 960, 595)], "fp"))

    def test_special_fp_pool_is_not_regular_pool(self):
        self.assertFalse(SmartEngine._is_summon_pool([word("特殊友情点召唤"), word("消耗友情点", y=870)], "fp"))
        self.assertFalse(SmartEngine._is_summon_pool([word("友情点召唤"), word("活动友情池开放中")], "fp"))

    def test_time_limited_fp_marker_can_be_split_by_ocr(self):
        self.assertFalse(SmartEngine._is_summon_pool([
            word("期间限定", y=250), word("友情点召唤"), word("消耗友情点", y=870)], "fp"))
        self.assertFalse(SmartEngine._is_summon_pool([word("期間限定友情池"), word("友情点召唤")], "fp"))

    def test_multiple_identical_limited_pools_do_not_stop_story_search(self):
        engine = self.engine()
        limited = details(word("期间限定友情池"), word("友情点召唤"))
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.SUMMON_SCREEN, limited)] * 3 +
            [(GameScreen.SUMMON_SCREEN, details(word("剧情召唤")))])
        self.assertTrue(engine.switch_summon_pool("story", details(word("活动卡池"))))
        self.assertEqual(engine.ctrl.click.call_count, 4)

    def test_regular_fp_search_passes_multiple_limited_pools(self):
        engine = self.engine()
        limited = details(word("期间限定"), word("友情点召唤"))
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.SUMMON_SCREEN, limited)] * 3 +
            [(GameScreen.SUMMON_SCREEN, details(word("友情点召唤")))])
        self.assertTrue(engine.switch_summon_pool("fp", details(word("活动卡池"))))
        self.assertEqual(engine.ctrl.click.call_count, 4)

    def test_identical_limited_pools_still_obey_total_limit(self):
        engine = self.engine()
        limited = details(word("期间限定友情池"), word("友情点召唤"))
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUMMON_SCREEN, limited))
        self.assertFalse(engine.switch_summon_pool("story", limited, max_attempts=3))
        self.assertEqual(engine.ctrl.click.call_count, 3)

    def test_left_switch_skips_special_pool_before_story(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.SUMMON_SCREEN, details(word("友情点召唤"))),
            (GameScreen.SUMMON_SCREEN, details(word("特殊友情点召唤"))),
            (GameScreen.SUMMON_SCREEN, details(word("剧情召唤")))])
        self.assertTrue(engine.switch_summon_pool("story", details(word("活动卡池"))))
        self.assertEqual([c.args for c in engine.ctrl.click.call_args_list], [(70, 540)] * 3)

    def test_special_fp_tab_is_not_selected_as_regular(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUMMON_SCREEN, details(word("友情点召唤"))))
        self.assertTrue(engine.switch_summon_pool("fp", details(word("剧情召唤"), word("特殊友情点召唤", 1000, 986))))
        self.assertEqual(engine.ctrl.click.call_args.args, (70, 540))

    def test_switch_success_requires_fresh_target_body(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUMMON_SCREEN, details(word("剧情召唤", y=580))))
        self.assertTrue(engine.switch_summon_pool("story", details(word("活动卡池"), word("剧情召唤", 710, 986))))
        self.assertEqual(engine.ctrl.click.call_args.args, (710, 986))
        engine.get_current_screen.assert_called_once_with(wait_still=True)

    def test_unchanged_clicks_stop_after_two_attempts(self):
        engine = self.engine()
        view = details(word("活动卡池"))
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUMMON_SCREEN, view))
        self.assertFalse(engine.switch_summon_pool("story", view))
        self.assertEqual(engine.ctrl.click.call_count, 2)

    def test_changing_wrong_pools_still_hit_limit(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.SUMMON_SCREEN, details(word(f"活动卡池{i}"))) for i in range(3)])
        self.assertFalse(engine.switch_summon_pool("story", details(word("起始卡池")), max_attempts=3))
        self.assertEqual(engine.ctrl.click.call_count, 3)

    def test_unexpected_page_stops_without_closing_it(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(return_value=(GameScreen.CRAFT_ESSENCE_SELECT, {}))
        self.assertFalse(engine.switch_summon_pool("story", details(word("活动卡池"))))
        self.assertEqual(engine.ctrl.click.call_count, 1)

    @patch("src.strategy.smart_engine.time.sleep")
    def test_loading_is_polled_before_next_click(self, _sleep):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[(GameScreen.LOADING, {}),
            (GameScreen.SUMMON_SCREEN, details(word("友情点召唤", y=590)))])
        self.assertTrue(engine.switch_summon_pool("fp", details(word("剧情召唤", y=580))))
        self.assertEqual(engine.ctrl.click.call_count, 1)

    @patch("src.strategy.smart_engine.time.sleep")
    def test_daily_story_then_fp_branch_is_reachable(self, _sleep):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.SUMMON_SCREEN, details(word("剧情召唤", y=580))),
            (GameScreen.SUMMON_SCREEN, details(word("友情点召唤", y=590), word("免费召唤10次", y=780))),
            (GameScreen.MAIN_MENU, {})])
        engine.complete_free_summon = MagicMock(return_value=True)
        self.assertTrue(engine.run_reactive_daily(options={"enable_quest": False, "enable_reward": False}))
        engine.complete_free_summon.assert_called_once_with((960, 780))

    @patch("src.strategy.smart_engine.time.sleep")
    def test_free_summon_closes_result_without_clicking_burn(self, _sleep):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.POPUP_DIALOG, details(word("每日可免费召唤1次。"), word("是否开始召唤？"), word("决定", 1260, 845))),
            (GameScreen.LOADING, {}), (GameScreen.UNKNOWN, {}),
            (GameScreen.SUMMON_RESULT, {"close_coord": (163, 63)}),
            (GameScreen.SUMMON_SCREEN, details(word("友情点召唤")))])
        self.assertTrue(engine.complete_free_summon((960, 780)))
        self.assertEqual([c.args for c in engine.ctrl.click.call_args_list],
                         [(960, 780), (1260, 845), (960, 540), (163, 63)])

    def test_paid_confirmation_is_never_submitted(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(return_value=(GameScreen.POPUP_DIALOG,
            details(word("消耗圣晶石3个，是否确认执行召唤？"), word("决定", 1260, 845))))
        self.assertFalse(engine.complete_free_summon((960, 780)))
        self.assertEqual(engine.ctrl.click.call_count, 1)

    def test_paid_confirmation_mentioning_free_is_also_rejected(self):
        engine = self.engine()
        engine.get_current_screen = MagicMock(return_value=(GameScreen.POPUP_DIALOG,
            details(word("免费召唤已使用，消耗圣晶石3个，是否确认执行召唤？"), word("决定", 1260, 845))))
        self.assertFalse(engine.complete_free_summon((960, 780)))
        self.assertEqual(engine.ctrl.click.call_count, 1)

    @patch("src.strategy.smart_engine.time.sleep")
    def test_pool_advertising_enhancement_is_not_closed_as_result(self, _sleep):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.SUMMON_SCREEN, details(word("友情点召唤"), word("免费召唤", y=780),
                                               word("强化成功率提升"), word("关闭", 163, 63))),
            (GameScreen.MAIN_MENU, {})])
        engine.complete_free_summon = MagicMock(return_value=True)
        self.assertTrue(engine.run_reactive_daily(options={"enable_story_summon": False,
                                                          "enable_quest": False, "enable_reward": False}))
        engine.complete_free_summon.assert_called_once_with((960, 780))
        engine.ctrl.click.assert_not_called()

    @patch("src.strategy.smart_engine.time.sleep")
    def test_return_to_pool_without_result_is_not_success(self, _sleep):
        engine = self.engine()
        engine.get_current_screen = MagicMock(side_effect=[
            (GameScreen.POPUP_DIALOG, details(word("免费召唤，是否确认执行召唤？"), word("决定", 1260, 845)))] +
            [(GameScreen.SUMMON_SCREEN, {})] * 59)
        self.assertFalse(engine.complete_free_summon((960, 780)))
        self.assertEqual(engine.ctrl.click.call_count, 2)

    @patch("src.strategy.smart_engine.time.sleep")
    def test_daily_switch_failure_blocks_later_tasks(self, _sleep):
        engine = self.engine()
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUMMON_SCREEN, details(word("活动卡池"))))
        engine.switch_summon_pool = MagicMock(return_value=False)
        self.assertFalse(engine.run_reactive_daily(options={"enable_quest": False, "enable_reward": False}))
        engine.ctrl.click.assert_not_called()

    def test_runner_does_not_log_completion_after_failure(self):
        mgr = MagicMock()
        mgr.get_connected_devices.return_value = [SimpleNamespace(serial="test", model="test")]
        with patch("src.workflows.daily_runner.FGOController"), patch("src.workflows.daily_runner.SmartEngine") as engine:
            engine.return_value.run_reactive_daily.return_value = False
            engine.return_value.active_strategy = None
            report = DailyRunner(device_manager=mgr).run()
        self.assertFalse(report.success)
        self.assertIn("[未完成]", report.logs[-1])


if __name__ == "__main__":
    unittest.main()
