import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from src.solver.mission_solver import MasterMissionSolver, MissionRequirement, SolverResult
from src.strategy.screen_detector import GameScreen
from src.strategy.smart_engine import SmartEngine
from src.strategy.weekly_missions import WeeklyMissions, WeeklySnapshot
from src.workflows.weekly_runner import WeeklyRunner


def word(text, x, y):
    return ([[x - 35, y - 12], [x + 35, y - 12], [x + 35, y + 12], [x - 35, y + 12]], text, 0.95)


DESCRIPTIONS = ["完成本周所有的御主任务", "击败3个持有【地】之力的敌人", "击败3个持有【天】之力的敌人",
                "击败3个持有【龙】特性的敌人", "击败3个持有【魔性】特性的敌人", "击败3个持有【死灵】特性的敌人", "击败3个【从者】"]


class MissionUI:
    def __init__(self, complete=False, claimed=False):
        self.descriptions = list(DESCRIPTIONS)
        self.complete = complete
        self.claimed = claimed
        self.missing_progress = False
        self.page = 0
        self.blue_tab = True
        self.ctrl = MagicMock()
        self.ctrl.cancel_event = None
        self.ctrl.wait_for_still.side_effect = self.frame
        self.ctrl.swipe.side_effect = self.swipe

    def swipe(self, x1, y1, x2, y2, **kwargs):
        self.page = max(0, self.page - 1) if y2 > y1 else min(2, self.page + 1)

    def frame(self, **kwargs):
        img = Image.new("RGB", (1920, 1080), (60, 60, 60))
        ImageDraw.Draw(img).rectangle((1070, 185, 1250, 245), fill=(40, 170, 240) if self.blue_tab else (220, 220, 220))
        ocr = [word("周常", 1160, 215), word("全部", 1530, 320), word(f"{7 if self.claimed else 0}/7", 1340, 320)]
        for index in range(self.page * 3, min(len(self.descriptions), self.page * 3 + 3)):
            y = 410 + (index % 3) * 235
            ocr.append(word(self.descriptions[index], 1200, y))
            total = 6 if index == 0 else 3
            current = total if self.complete else 0
            if not self.missing_progress:
                ocr.append(word(f"目标进行度 {current}/{total}", 1030, y + 110))
            if self.claimed:
                ocr.append(word("已领取", 1450, y + 50))
        img.info["ocr"] = ocr
        return img


class TestWeeklyScanner(unittest.TestCase):
    def setUp(self):
        self.ui = MissionUI()
        self.engine = SimpleNamespace(ctrl=self.ui.ctrl, log=MagicMock(), is_cancelled=lambda: False,
            navigate_to_main_menu=MagicMock(return_value=True),
            get_current_screen=MagicMock(return_value=(GameScreen.MASTER_MISSIONS, {"ocr_results": [word("周常", 1160, 215)]})))
        self.scanner = WeeklyMissions(self.engine)
        p = patch("src.strategy.weekly_missions.ScreenDetector.run_ocr", side_effect=lambda img: img.info["ocr"])
        p.start(); self.addCleanup(p.stop)

    def test_full_scan_preserves_all_six_objectives(self):
        result = self.scanner.scan()
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(result.requirements), 6)
        self.assertFalse(result.objectives_complete)

    def test_live_ocr_prefix_and_bracket_variants_share_a_key(self):
        first = "败5个持有【神性特性的敌人（战斗中被召唤出来的敌人除外）"
        second = "击败5个持有『神性】特性的敌人（战斗中被召唤出来的敌人除外）"
        self.assertEqual(self.scanner.card_key(first), self.scanner.card_key(second))
        self.assertNotEqual(self.scanner.card_key(second), self.scanner.card_key(second.replace("5个", "15个")))
        self.assertNotEqual(self.scanner.card_key(second), self.scanner.card_key(second.replace("神性", "龙")))
        self.assertEqual(self.scanner.card_key(second), self.scanner.card_key(second.replace("的敌人除外", "敌人除外")))
        self.assertNotEqual(self.scanner.card_key("击败3个敌人（必须使用红卡）"), self.scanner.card_key("击败3个敌人"))
        self.assertEqual(self.scanner.card_key('击败15个持有1人」之力的敌人'),
                         self.scanner.card_key('击败15个持有【人】之力的敌人'))
        self.assertEqual(self.scanner.card_key('完成本周所有的御主任务'),
                         self.scanner.card_key('完成本周所有的御王仕务'))

    def test_background_text_below_title_does_not_change_task_identity(self):
        ocr = [word("击败20个敌人（部分首领、战斗中被召唤出来的敌人", 1200, 450),
               word("除外）", 960, 482), word("20/20", 960, 570), word("活动举", 1570, 610)]
        cards = self.scanner.extract_cards(ocr)
        self.assertEqual(cards[0]["progress"], (20, 20))
        self.assertTrue(cards[0]["description"].endswith("除外）"))
        self.assertEqual(self.scanner.signature(ocr), self.scanner.signature(
            [(box, text.replace("击败", "败").replace("）", ")"), score) for box, text, score in ocr]))

    def test_incomplete_list_is_rejected(self):
        self.ui.descriptions.pop()
        result = self.scanner.scan()
        self.assertFalse(result.success)
        self.assertIn("6/7", result.message)

    def test_unknown_mission_is_not_classified_as_meta(self):
        self.ui.descriptions[-1] = "击败3个具有未知XYZ特性的敌人"
        result = self.scanner.scan()
        self.assertFalse(result.success)
        self.assertIn("无法解析", result.message)

    def test_missing_progress_is_not_assumed_zero_or_done(self):
        self.ui.missing_progress = True
        self.assertFalse(self.scanner.scan().success)

    def test_incomplete_event_is_not_all_complete(self):
        self.ui.descriptions[-1] = "在【活动】中获得【活动道具】3个"
        result = self.scanner.scan()
        self.assertTrue(result.success, result.message)
        self.assertTrue(result.requirements[-1].is_event)
        self.assertFalse(result.objectives_complete)

    def test_complete_objectives_are_distinct_from_claimed_rewards(self):
        self.ui.complete = True
        result = self.scanner.scan()
        self.assertTrue(result.success, result.message)
        self.assertTrue(result.objectives_complete)
        self.assertFalse(result.all_claimed)

    def test_explicit_claimed_status_is_verified(self):
        self.ui.complete = self.ui.claimed = True
        self.assertTrue(self.scanner.scan().all_claimed)

    def test_weekly_text_without_active_tab_is_rejected(self):
        self.ui.blue_tab = False
        self.assertFalse(self.scanner.scan().success)

    def test_scan_starts_from_top_even_if_previously_scrolled(self):
        self.ui.page = 2
        result = self.scanner.scan()
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(result.requirements), 6)

    def test_navigation_failure_returns_valid_failure_model(self):
        self.engine.get_current_screen.return_value = (GameScreen.UNKNOWN, {})
        self.engine.navigate_to_main_menu.return_value = False
        result = self.scanner.solve()
        self.assertFalse(result.success)
        self.assertEqual(result.plans, [])

    def test_live_empty_weekly_page_stops_without_scrolling(self):
        frame = self.ui.frame()
        frame.info["ocr"] = [word("周常", 1160, 215), word("全部", 1530, 320),
                             word("0/0", 1340, 320), word("现在没有正在进行中的御主任务", 1360, 746)]
        self.ui.ctrl.wait_for_still.side_effect = lambda **kwargs: frame
        result = self.scanner.solve()
        self.assertFalse(result.success)
        self.assertFalse(result.missions_verified)
        self.assertFalse(result.all_completed)
        self.assertFalse(result.rewards_claimed)
        self.assertIn("0/0", result.message)
        self.ui.ctrl.swipe.assert_not_called()
        self.engine.navigate_to_main_menu.assert_called_once()

    def test_zero_task_counter_alone_is_not_success(self):
        frame = self.ui.frame()
        frame.info["ocr"] = [word("周常", 1160, 215), word("全部", 1530, 320), word("0/0", 1340, 320)]
        self.ui.ctrl.wait_for_still.side_effect = lambda **kwargs: frame
        result = self.scanner.scan(leave=False)
        self.assertFalse(result.success)
        self.assertFalse(result.objectives_complete)
        self.ui.ctrl.swipe.assert_not_called()
        self.engine.navigate_to_main_menu.assert_not_called()

    def test_empty_weekly_return_failure_is_reported(self):
        frame = self.ui.frame()
        frame.info["ocr"] = [word("周常", 1160, 215), word("全部", 1530, 320), word("0/0", 1340, 320)]
        self.ui.ctrl.wait_for_still.side_effect = lambda **kwargs: frame
        self.engine.navigate_to_main_menu.return_value = False
        result = self.scanner.scan()
        self.assertFalse(result.success)
        self.assertIn("未能确认返回大厅", result.message)

    def test_no_claim_button_is_not_claim_success(self):
        self.scanner.scan = MagicMock(return_value=WeeklySnapshot(True, "ok", [], True, False))
        self.assertFalse(self.scanner.claim())

    def test_claim_requires_fresh_evidence_after_click(self):
        self.scanner.scan = MagicMock(return_value=WeeklySnapshot(True, "ok", [], True, False))
        img = self.ui.frame()
        ocr = img.info["ocr"] + [word("统一领取", 1770, 320)]
        self.scanner.read = MagicMock(return_value=(img, ocr))
        self.assertFalse(self.scanner.claim())
        self.assertEqual(self.ui.ctrl.click.call_count, 3)

    def test_claim_succeeds_only_after_verified_claimed_snapshot(self):
        self.scanner.scan = MagicMock(side_effect=[WeeklySnapshot(True, "ok", [], True, False),
            WeeklySnapshot(True, "ok", [], True, True)])
        img = self.ui.frame()
        self.scanner.read = MagicMock(return_value=(img, img.info["ocr"] + [word("统一领取", 1770, 320)]))
        self.assertTrue(self.scanner.claim())


class TestWeeklyPlanningSafety(unittest.TestCase):
    def setUp(self):
        self.solver = MasterMissionSolver()

    def test_empty_requirements_fail(self):
        self.assertFalse(self.solver.solve([]).success)

    def test_live_generic_enemy_mission_uses_remaining_enemy_count(self):
        req = self.solver.parse_chinese_mission(
            "击败40个敌人（部分首领、战斗中被召唤出来的敌人除外）", progress_cur=28, progress_total=40)
        self.assertEqual((req.trait, req.count), ("enemies", 12))
        result = self.solver.solve([req], allowed_wars=["冬木"])
        self.assertTrue(result.success, result.message)
        self.assertGreaterEqual(result.achieved["enemies"], 12)
        self.assertTrue(WeeklyRunner().validate_plans(result.plans, [req]))
        with self.assertRaises(ValueError):
            WeeklyRunner().validate_plans([dict(quest_id="fuyuki_x_a", runs=1)], [req])

    def test_generic_enemy_rule_does_not_ignore_unknown_conditions(self):
        self.assertIsNone(self.solver.parse_chinese_mission("击败40个具有未知XYZ特性的敌人"))
        self.assertIsNone(self.solver.parse_chinese_mission("击败40个敌人（必须使用红卡）"))

    def test_current_week_route_covers_neutral_good_divine_and_human(self):
        requirements = [MissionRequirement(trait=trait, count=count) for trait, count in
                        [('human', 15), ('neutral', 3), ('good', 3), ('divine', 1), ('enemies', 12)]]
        result = self.solver.solve(requirements)
        self.assertTrue(result.success, result.message)
        self.assertEqual((result.total_ap, result.total_runs), (23, 3))
        self.assertEqual({p.quest_id: p.runs for p in result.plans}, {'fuyuki_x_e': 1, 'septem_massilia': 2})
        self.assertTrue(WeeklyRunner().validate_plans(result.plans, requirements))

    def test_one_unsupported_requirement_blocks_whole_plan(self):
        result = self.solver.solve([MissionRequirement(trait="earth", count=1), MissionRequirement(trait="unsupported", count=1)])
        self.assertFalse(result.success)

    def test_pending_event_cannot_be_reported_as_complete(self):
        result = self.solver.solve([MissionRequirement(trait="event_item", count=3, is_event=True)])
        self.assertFalse(result.success)
        self.assertFalse(result.all_completed)
        self.assertEqual(len(result.event_missions), 1)

    def test_finished_event_does_not_block_completion(self):
        result = self.solver.solve([MissionRequirement(trait="event_item", count=0, is_event=True)])
        self.assertTrue(result.success)
        self.assertTrue(result.all_completed)

    def test_unknown_count_is_not_guessed_as_fifteen(self):
        self.assertIsNone(self.solver.parse_chinese_mission("击败持有【龙】特性的敌人"))

    def test_live_missing_attribute_bracket_preserves_condition(self):
        for text in ('击败15个持有人】之力的敌人', '击败15个持有【人之力的敌人', '击败15个持有1人」之力的敌人'):
            self.assertEqual(self.solver.parse_chinese_mission(text).trait, 'human')
        self.assertIsNone(self.solver.parse_chinese_mission('击败15个持有人】之力且【神性】特性的敌人'))

    def test_invalid_progress_is_rejected(self):
        self.assertIsNone(self.solver.parse_chinese_mission("击败3个【龙】敌人", progress_cur=8, progress_total=3))

    def test_composite_conditions_are_not_downgraded_to_single_trait(self):
        self.assertIsNone(self.solver.parse_chinese_mission("击败3个持有【神性】的从者"))
        self.assertIsNone(self.solver.parse_chinese_mission("击败3个持有【龙】且【魔性】的敌人"))

    def test_enemy_word_is_not_misread_as_human_attribute(self):
        self.assertIsNone(self.solver.parse_chinese_mission("击败3个持有未知XYZ特性的敌人"))

    def test_manual_task_batch_cannot_drop_unknown_entries(self):
        self.assertRaises(ValueError, self.solver.parse_missions, ["击败3个【地】敌人", "无法解析的任务"])


class TestWeeklyStationSafety(unittest.TestCase):
    def setUp(self):
        self.ctrl = MagicMock(); self.ctrl.cancel_event = None
        self.ctrl.wait_for_still.return_value = Image.new("RGB", (1920, 1080), (100, 100, 100))
        self.engine = SmartEngine(self.ctrl, log_callback=lambda _: None)
        # Isolate station accounting from the separate Laplace preflight tests.
        self.engine._ensure_laplace = MagicMock(return_value=True)
        self.engine._ensure_craft_essences = MagicMock(return_value=True)
        self.engine.current_recommendation = SimpleNamespace(laplace={}, slot1_dps="test", direction="Arts",
            slot1_class="Rider", slot1_affinity=2, slot2_support="test", slot3_friend_support="test",
            slot4_backline_support="test", direction_name="test",
            strategy=SimpleNamespace(name="test", turns={}))
        self.engine.party_assembler.assemble_party_on_confirm_screen = MagicMock(return_value=True)
        self.engine.navigate_to_free_quest = MagicMock(return_value=True)
        self.engine.detector = MagicMock()
        self.engine.detector.identify_party_from_screen.return_value = (SimpleNamespace(
            slot1_servant="test", slot2_servant="test", slot3_servant="test"), None, [])
        self.plan = SimpleNamespace(runs=1, war="test", spot="test", name="test")
        p = patch("src.strategy.smart_engine.ScreenDetector.run_ocr", return_value=[word("开始任务", 1750, 1000)])
        p.start(); self.addCleanup(p.stop)
        p = patch("src.strategy.smart_engine.time.sleep")
        p.start(); self.addCleanup(p.stop)

    def run_screens(self, screens):
        self.engine.get_current_screen = MagicMock(side_effect=screens)
        return self.engine.run_reactive_weekly_plan(self.plan)

    def test_ap_dialog_before_battle_counts_zero(self):
        self.assertFalse(self.run_screens([(GameScreen.CONTINUE_QUEST, {"all_texts": ["补充AP"]})]))
        self.assertEqual(self.engine.last_weekly_clears, 0)
        self.ctrl.click.assert_not_called()

    def test_story_scene_stops_without_skipping_or_counting_a_clear(self):
        self.assertFalse(self.run_screens([(GameScreen.UNKNOWN, {'all_texts':['跳过','玛尔达','记录','自动']})]))
        self.assertEqual(self.engine.last_weekly_clears,0)
        self.ctrl.click.assert_not_called()

    def test_wrong_recommended_level_blocks_support_selection(self):
        self.engine.current_recommendation.laplace = {'recommended_level':16}
        self.engine.party_assembler.select_support_on_support_screen = MagicMock()
        self.assertFalse(self.run_screens([(GameScreen.SUPPORT_SELECT, {'ocr_results':[word('等级80',330,88)]})]))
        self.engine.party_assembler.select_support_on_support_screen.assert_not_called()
        self.ctrl.click.assert_not_called()

    def test_continue_panel_without_battle_counts_zero(self):
        self.assertFalse(self.run_screens([(GameScreen.CONTINUE_QUEST, {"all_texts": ["连续出击"]})]))
        self.assertEqual(self.engine.last_weekly_clears, 0)

    def test_old_result_page_counts_zero(self):
        self.assertFalse(self.run_screens([(GameScreen.BATTLE_RESULT, {})]))
        self.assertEqual(self.engine.last_weekly_clears, 0)

    def test_multiple_result_pages_count_once(self):
        panel = {"all_texts": ["连续出击"], "ocr_results": [word("关闭", 640, 830)]}
        self.assertTrue(self.run_screens([(GameScreen.PARTY_CONFIRM, {}), (GameScreen.BATTLE_COMBAT, {"wave":1}),
            (GameScreen.BATTLE_RESULT, {}), (GameScreen.BATTLE_RESULT, {}),
            (GameScreen.CONTINUE_QUEST, panel), (GameScreen.REGIONAL_MAP, {})]))
        self.assertEqual(self.engine.last_weekly_clears, 1)

    def test_failed_continue_does_not_count_another_battle(self):
        self.plan.runs = 2
        panel = {"all_texts": ["连续出击"], "ocr_results": [word("连续出击", 1280, 830)]}
        self.assertFalse(self.run_screens([(GameScreen.PARTY_CONFIRM, {}), (GameScreen.BATTLE_COMBAT, {"wave":1}),
            (GameScreen.CONTINUE_QUEST, panel), (GameScreen.CONTINUE_QUEST, panel)]))
        self.assertEqual(self.engine.last_weekly_clears, 1)

    def test_unconfirmed_exit_is_failure_even_after_clear(self):
        panel = {"all_texts": ["连续出击"], "ocr_results": [word("关闭", 640, 830)]}
        self.assertFalse(self.run_screens([(GameScreen.PARTY_CONFIRM, {}), (GameScreen.BATTLE_COMBAT, {"wave":1}),
            (GameScreen.CONTINUE_QUEST, panel)] + [(GameScreen.UNKNOWN, {})] * 6))
        self.assertEqual(self.engine.last_weekly_clears, 1)

    def test_navigation_failure_does_not_start_battle(self):
        self.engine.navigate_to_free_quest.return_value = False
        self.assertFalse(self.engine.run_reactive_weekly_plan(self.plan))
        self.ctrl.click.assert_not_called()

    def test_unknown_support_page_does_not_prove_target_quest(self):
        engine = SmartEngine(self.ctrl, log_callback=lambda _: None)
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUPPORT_SELECT, {}))
        self.assertFalse(engine.navigate_to_free_quest("冬木", "未确认坐标X-A", "未确认坐标X-A"))
        self.ctrl.click.assert_not_called()

    def test_exact_header_can_prove_target_quest(self):
        engine = SmartEngine(self.ctrl, log_callback=lambda _: None)
        engine.get_current_screen = MagicMock(return_value=(GameScreen.SUPPORT_SELECT,
            {"ocr_results": [word("未确认坐标 X-A", 1000, 100)]}))
        self.assertTrue(engine.navigate_to_free_quest("冬木", "未确认坐标X-A", "未确认坐标X-A"))

    def test_strategy_generation_failure_cannot_reuse_previous_station(self):
        self.engine.team_builder = MagicMock()
        self.engine.team_builder.recommend_for_quest.side_effect = RuntimeError("bad recommendation")
        self.engine.active_strategy = MagicMock()
        self.assertFalse(self.engine.run_reactive_weekly_plan(self.plan))
        self.assertIsNone(self.engine.active_strategy)
        self.engine.navigate_to_free_quest.assert_not_called()


class TestWeeklyRunnerSafety(unittest.TestCase):
    def setUp(self):
        self.mgr = MagicMock()
        self.mgr.get_connected_devices.return_value = [SimpleNamespace(serial="test", model="test")]
        self.runner = WeeklyRunner(device_manager=self.mgr)
        self.runner.log = lambda rep, text: rep.logs.append(text)
        self.fresh = self.runner.solver.solve([MissionRequirement(trait="earth", count=1)])
        self.fresh.missions_verified = True
        self.done = SolverResult(success=True, missions_verified=True, all_completed=True)
        p = patch("src.workflows.weekly_runner.FGOController"); p.start(); self.addCleanup(p.stop)
        p = patch("src.workflows.weekly_runner.SmartEngine")
        self.engine = p.start().return_value; self.addCleanup(p.stop)
        self.engine.auto_scan_and_solve_weekly.side_effect = [self.fresh, self.done]
        self.engine.claim_weekly_rewards.return_value = True
        def station(plan, **kwargs):
            self.engine.last_weekly_clears = plan.runs
            return True
        self.engine.run_reactive_weekly_plan.side_effect = station

    def test_claim_failure_is_reported_as_failure(self):
        self.engine.claim_weekly_rewards.return_value = False
        report = self.runner.run()
        self.assertFalse(report.success)
        self.assertFalse(report.rewards_claimed)

    def test_post_battle_progress_must_actually_be_complete(self):
        self.engine.auto_scan_and_solve_weekly.side_effect = [self.fresh, self.fresh]
        self.assertFalse(self.runner.run().success)
        self.engine.claim_weekly_rewards.assert_not_called()

    def test_failed_station_does_not_report_planned_battles_as_actual(self):
        self.engine.run_reactive_weekly_plan.side_effect = None
        self.engine.run_reactive_weekly_plan.return_value = False
        self.engine.last_weekly_clears = 0
        report = self.runner.run()
        self.assertFalse(report.success)
        self.assertEqual(report.total_battles, 0)
        self.assertGreater(report.planned_battles, 0)
        self.assertIsNone(report.total_ap_spent)
        self.engine.claim_weekly_rewards.assert_not_called()

    def test_success_requires_scan_and_claim_and_confirmed_clears(self):
        report = self.runner.run()
        self.assertTrue(report.success)
        self.assertTrue(report.progress_verified)
        self.assertTrue(report.rewards_claimed)
        self.assertEqual(report.total_battles, self.fresh.total_runs)
        self.assertIsNone(report.total_ap_spent)

    def test_external_unknown_route_cannot_bypass_fresh_scan(self):
        report = self.runner.run(plans=[{"quest_id": "unknown", "runs": 1}])
        self.assertFalse(report.success)
        self.engine.run_reactive_weekly_plan.assert_not_called()

    def test_incomplete_external_plan_is_rejected(self):
        self.assertRaises(ValueError, self.runner.validate_plans, [], [MissionRequirement(trait="earth", count=1)])

    def test_external_ap_cannot_be_forged(self):
        data = self.fresh.plans[0].model_dump()
        data["ap_per_run"] = 0
        self.assertFalse(self.runner.run(plans=[data]).success)
        self.engine.run_reactive_weekly_plan.assert_not_called()

    def test_failed_station_preserves_only_confirmed_partial_clears(self):
        self.engine.run_reactive_weekly_plan.side_effect = None
        self.engine.run_reactive_weekly_plan.return_value = False
        self.engine.last_weekly_clears = 1
        report = self.runner.run()
        self.assertFalse(report.success)
        self.assertEqual(report.total_battles, 1)
        self.engine.claim_weekly_rewards.assert_not_called()

    def test_pending_event_blocks_execution_before_battle(self):
        self.engine.auto_scan_and_solve_weekly.side_effect = [SolverResult(success=False,
            missions_verified=True, event_missions=["活动道具0/50"], message="活动任务未完成")]
        report = self.runner.run()
        self.assertFalse(report.success)
        self.assertEqual(report.pending_missions, ["活动道具0/50"])
        self.engine.run_reactive_weekly_plan.assert_not_called()

    def test_unknown_custom_text_is_not_silently_ignored(self):
        report = self.runner.run(custom_mission_texts=["击败3个【地】敌人", "任务文字无法解析"])
        self.assertFalse(report.success)
        self.mgr.get_connected_devices.assert_not_called()

    def test_already_complete_weekly_still_requires_claim_verification(self):
        self.engine.auto_scan_and_solve_weekly.side_effect = [self.done, self.done]
        self.engine.claim_weekly_rewards.return_value = False
        self.assertFalse(self.runner.run().success)
        self.engine.run_reactive_weekly_plan.assert_not_called()

    def test_unverified_scan_blocks_execution(self):
        self.fresh.missions_verified = False
        self.assertFalse(self.runner.run().success)
        self.engine.run_reactive_weekly_plan.assert_not_called()

    def test_dry_run_does_not_connect_or_claim(self):
        self.runner.dry_run = True
        report = self.runner.run(custom_mission_texts=["击败3个【地】敌人"])
        self.assertTrue(report.success)
        self.assertGreater(report.planned_battles, 0)
        self.assertEqual(report.total_battles, 0)
        self.assertFalse(report.rewards_claimed)
        self.mgr.get_connected_devices.assert_not_called()


if __name__ == "__main__":
    unittest.main()
