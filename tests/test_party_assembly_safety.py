import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from src.parser.chaldea_parser import ChaldeaParser
from src.strategy.auto_party_assembler import AutoPartyAssembler
from src.strategy.auto_team_builder import AutoTeamBuilder
from src.strategy.screen_detector import GameScreen, ScreenDetector
from src.strategy.smart_engine import SmartEngine


class TestPartyAssemblySafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = ChaldeaParser()
        cls.parser.parse_file(Path(__file__).parent / "sample_chaldea.json")
        cls.recommendation = AutoTeamBuilder(cls.parser).recommend_for_quest("daily_qp_extreme")

    def setUp(self):
        self.rec = self.recommendation.model_copy(deep=True)
        self.before = self.rec.model_dump()
        self.ctrl = MagicMock()
        self.ctrl.cancel_event = None
        self.ctrl.wait_for_still.return_value = Image.new("RGB", (1920, 1080), (100, 100, 100))
        self.logs = []
        self.assembler = AutoPartyAssembler(log_callback=self.logs.append)
        self.assembler.verify_slot_has_servant = MagicMock(return_value=(True, 0.96))
        self.assembler.verify_slot_level = MagicMock(return_value=True)
        self.assembler.select_servant_in_picker = MagicMock(return_value=False)
        self.start_ocr = [([[1650, 970], [1850, 970], [1850, 1040], [1650, 1040]], "开始任务", 0.95)]
        ocr_patch = patch.object(ScreenDetector, "run_ocr", return_value=self.start_ocr)
        self.ocr = ocr_patch.start()
        self.addCleanup(ocr_patch.stop)

    def assemble(self):
        return self.assembler.assemble_party_on_confirm_screen(self.ctrl, self.rec, self.parser)

    def test_unrecognized_dps_is_not_preserved_or_renamed(self):
        self.assembler.verify_slot_has_servant.side_effect = lambda img, slot, sid: (slot != 1, 0.1)
        self.ocr.return_value = []  # 旧代码会将空 OCR 转成“打手从者”并返回成功。
        self.assertFalse(self.assemble())
        self.assertEqual(self.rec.model_dump(), self.before)
        self.assembler.select_servant_in_picker.assert_called_once()
        self.assertEqual(self.assembler.select_servant_in_picker.call_args.kwargs["svt_name"], self.rec.slot1_dps)
        self.assertFalse(any("score=0.85" in text for text in self.logs))

    def test_other_top_dps_is_not_preserved_with_old_strategy(self):
        def verify(img, slot, sid):
            return (slot != 1 or sid == 704300, 0.96)
        self.assembler.verify_slot_has_servant.side_effect = verify
        self.assertFalse(self.assemble())
        self.assertEqual(self.rec.model_dump(), self.before)
        self.assertFalse(any(c.args[2] == 704300 for c in self.assembler.verify_slot_has_servant.call_args_list))

    def test_picker_success_without_actual_replacement_is_failure(self):
        self.assembler.verify_slot_has_servant.side_effect = lambda img, slot, sid: (slot != 1, 0.1)
        self.assembler.select_servant_in_picker.return_value = True
        self.assertFalse(self.assemble())
        self.assertEqual(self.rec.model_dump(), self.before)

    def test_verified_replacement_succeeds_without_changing_strategy(self):
        dps_checks = 0
        def verify(img, slot, sid):
            nonlocal dps_checks
            if slot == 1:
                dps_checks += 1
                return (dps_checks > 1, 0.96)
            return True, 0.96
        self.assembler.verify_slot_has_servant.side_effect = verify
        self.assembler.select_servant_in_picker.return_value = True
        self.assertTrue(self.assemble())
        self.assertEqual(self.rec.model_dump(), self.before)
        self.assembler.select_servant_in_picker.assert_called_once()

    def test_already_correct_party_needs_no_replacement(self):
        self.assertTrue(self.assemble())
        self.ctrl.click.assert_not_called()
        self.assembler.select_servant_in_picker.assert_not_called()
        self.assertEqual(self.rec.model_dump(), self.before)

    def test_each_auxiliary_replacement_failure_stops_assembly(self):
        for bad_slot in (2, 4):
            with self.subTest(slot=bad_slot):
                self.assembler.select_servant_in_picker.reset_mock()
                self.assembler.verify_slot_has_servant.side_effect = lambda img, slot, sid: (slot != bad_slot, 0.1)
                self.assertFalse(self.assemble())
                self.assembler.select_servant_in_picker.assert_called_once()

    def test_auxiliary_picker_success_still_requires_slot_verification(self):
        self.assembler.select_servant_in_picker.return_value = True
        self.assembler.verify_slot_has_servant.side_effect = lambda img, slot, sid: (slot != 2, 0.1)
        self.assertFalse(self.assemble())

    def test_wrong_support_is_not_accepted(self):
        self.assembler.verify_slot_has_servant.side_effect = lambda img, slot, sid: (slot != 3, 0.1)
        self.assertFalse(self.assemble())
        self.ctrl.click.assert_not_called()

    def test_final_verification_catches_displaced_servant(self):
        checks = {}
        def verify(img, slot, sid):
            checks[slot] = checks.get(slot, 0) + 1
            return (not (slot == 2 and checks[slot] > 1), 0.96)
        self.assembler.verify_slot_has_servant.side_effect = verify
        self.assertFalse(self.assemble())

    def test_missing_dps_id_fails_before_clicking(self):
        self.rec.slot1_dps = "打手从者"
        self.assertFalse(self.assemble())
        self.ctrl.click.assert_not_called()
        self.assembler.verify_slot_has_servant.assert_not_called()

    def test_ambiguous_recommendation_is_not_silently_resolved(self):
        self.rec.slot3_friend_support = "助战 阿尔托莉雅·Caster / 光之高扬斯卡娅"
        self.assertFalse(self.assemble())
        self.ctrl.click.assert_not_called()

    def test_candidate_ranking_does_not_override_recommended_name(self):
        self.rec.candidate_dps_ranking.insert(0, {"name": "摩根", "svt_id": 704000, "lv": 90})
        self.assertTrue(self.assemble())
        self.assertEqual(self.assembler.verify_slot_has_servant.call_args_list[0].args[2], 404200)

    def test_not_back_at_party_confirmation_is_failure(self):
        self.ocr.return_value = []
        self.assertFalse(self.assemble())

    def engine(self):
        engine = SmartEngine(self.ctrl, log_callback=self.logs.append)
        engine._ensure_laplace = MagicMock(return_value=True)
        engine.current_recommendation = self.rec
        engine.active_strategy = self.rec.strategy
        engine.navigate_to_free_quest = MagicMock(return_value=True)
        engine.get_current_screen = MagicMock(return_value=(GameScreen.PARTY_CONFIRM, {}))
        engine.party_assembler.assemble_party_on_confirm_screen = MagicMock(return_value=False)
        engine.detector = MagicMock()
        return engine

    def test_daily_does_not_start_battle_after_failed_assembly(self):
        engine = self.engine()
        engine.get_current_screen.side_effect = [(GameScreen.CHALDEA_GATE, {}), (GameScreen.PARTY_CONFIRM, {})]
        engine._select_daily_quest = MagicMock(return_value="daily")
        with patch.object(ScreenDetector, "find_target_quest_coordinate", return_value=(1300, 300)):
            self.assertFalse(engine.run_reactive_daily(options={"enable_story_summon": False,
                "enable_fp": False, "enable_reward": False}, repeat_runs=1))
        self.ctrl.click.assert_not_called()
        self.ctrl.click_and_verify.assert_not_called()
        engine.detector.identify_party_from_screen.assert_not_called()
        self.assertIsNone(engine.active_strategy)

    def test_weekly_does_not_start_battle_after_failed_assembly(self):
        engine = self.engine()
        plan = SimpleNamespace(runs=1, spot="test", war="test", name="test")
        self.assertFalse(engine.run_reactive_weekly_plan(plan))
        self.ctrl.click.assert_not_called()
        self.ctrl.click_and_verify.assert_not_called()
        engine.detector.identify_party_from_screen.assert_not_called()
        self.assertIsNone(engine.active_strategy)


if __name__ == "__main__":
    unittest.main()
