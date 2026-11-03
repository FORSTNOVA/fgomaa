import threading
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from src.strategy.craft_essence_picker import CraftEssencePicker, EquipmentError, EquipmentTarget
from src.strategy.screen_detector import ScreenDetector
from src.strategy.smart_engine import SmartEngine
from tests import test_party_assembly_safety as party_tests
from tests import test_servant_picker as servant_tests


def word(text, x, y):
    return ([[x - 40, y - 15], [x + 40, y - 15], [x + 40, y + 15], [x - 40, y + 15]], text, .95)


def target(slot=1):
    return EquipmentTarget(slot, 9400480, '黑之圣杯', 100, True, 100,
                           'https://static.atlasacademy.io/CN/Faces/f_94004800.png')


class TestEquipmentRecognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Image.open(Path(__file__).parent / 'fixtures/ce_picker_black_grail.png') as image:
            cls.img = image.copy()
        cls.ocr = ScreenDetector.run_ocr(cls.img)

    @classmethod
    def tearDownClass(cls):
        cls.img.close()

    def setUp(self):
        self.ctrl = MagicMock(cancel_event=None)
        self.picker = CraftEssencePicker(self.ctrl)

    def test_real_same_name_copies_have_different_levels_and_limit_breaks(self):
        cards = self.picker.cards(self.ocr)
        copies = [card for card in cards if self.picker.face_score(self.img, card, target()) >= .80]
        self.assertEqual({(c['level'], c['cap']) for c in copies}, {(100, 100), (20, 20)})
        accepted = [c for c in copies if self.picker.card_matches_stats(c, target())]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(self.picker.removed_card(self.ocr, accepted[0]))
        self.assertTrue(self.picker.name_matches(self.ocr, target()))

    def test_same_level_different_max_level_distinguishes_mlb(self):
        for mlb, cap in ((True, 100), (False, 20)):
            t = EquipmentTarget(1, 9400480, '黑之圣杯', 20, mlb, 100, target().face_url)
            self.assertTrue(self.picker.card_matches_stats({'level':20, 'cap':cap}, t))
            self.assertFalse(self.picker.card_matches_stats({'level':20, 'cap':100 if cap == 20 else 20}, t))

    def test_servant_level_and_cost_are_never_ce_candidates(self):
        self.assertEqual(self.picker.cards([word('等级90/90', 270, 290), word('106/113', 500, 195)]), [])

    def test_other_ce_with_same_level_does_not_match_black_grail(self):
        for card in self.picker.cards(self.ocr):
            if 700 < card['point'][0] < 950:  # Lv100 Kaleidoscope.
                self.assertLess(self.picker.face_score(self.img, card, target()), .80)

    def test_equipped_label_does_not_hide_kaleidoscope(self):
        t = replace(target(), ce_id=9400340, name='万华镜', face_url='https://static.atlasacademy.io/CN/Faces/f_94003400.png')
        card = next(card for card in self.picker.cards(self.ocr) if card['point'][0] == 835)
        self.assertGreater(self.picker.face_score(self.img, card, t), .95)

    def test_real_scrollbar_top_requires_three_stationary_full_row_checks(self):
        self.assertLessEqual(self.picker.scroll_position(self.img), .015)
        self.picker.frame = MagicMock(return_value=(self.img, self.ocr))
        self.picker.to_top(float('inf'))
        self.assertEqual(self.ctrl.swipe.call_count,3)

    def test_near_top_clipped_first_row_is_not_accepted(self):
        with Image.open(Path(__file__).parent/'fixtures/ce_near_top_clipped.png') as image:
            clipped=image.copy()
        self.addCleanup(clipped.close)
        clipped_ocr=ScreenDetector.run_ocr(clipped)
        self.assertLessEqual(self.picker.scroll_position(clipped),.015)
        self.assertFalse(any(540<=card['roi'][1]<=600 for card in self.picker.cards(clipped_ocr)))
        self.picker.frame=MagicMock(side_effect=[(clipped,clipped_ocr),(self.img,self.ocr)]+[(self.img,self.ocr)]*6)
        self.picker.to_top(float('inf'))
        self.assertEqual(self.ctrl.swipe.call_count,4)

    def test_single_card_touch_sparkle_does_not_prevent_top_detection(self):
        after=self.img.copy()
        self.addCleanup(after.close)
        ImageDraw.Draw(after).rectangle((500,630,540,650),fill='white')
        self.assertLess(self.picker.static_diff(self.img,after,self.ocr),.004)

    def test_name_must_be_in_selected_ce_header(self):
        self.assertFalse(self.picker.name_matches([word('黑之圣杯', 700, 900)], target()))
        self.assertFalse(self.picker.name_matches([word('圣杯', 700, 310)], target()))

    def test_missing_equipped_marker_cannot_pass_verification(self):
        ocr = [row for row in self.ocr if row[1] != '卸除']
        self.picker.frame = MagicMock(return_value=(self.img, ocr))
        self.picker.clear_filters = MagicMock()
        self.picker.to_top = MagicMock()
        self.picker.static_diff = MagicMock(return_value=0)
        with self.assertRaises(EquipmentError):
            self.picker.find(target(), verify_only=True)
        self.ctrl.click.assert_not_called()


class TestEquipmentWorkflow(unittest.TestCase):
    def setUp(self):
        self.ctrl = MagicMock(cancel_event=None)
        self.logs = []
        self.picker = CraftEssencePicker(self.ctrl, self.logs.append)
        self.picker.targets = MagicMock(return_value=[target()])
        self.picker.equip_and_verify = MagicMock()

    def test_third_complete_attempt_may_succeed(self):
        self.picker.equip_and_verify.side_effect = [EquipmentError('wrong level'), EquipmentError('not equipped'), None]
        self.assertTrue(self.picker.ensure(None, None))
        self.assertEqual(self.picker.targets.call_count, 3)
        self.assertEqual(self.picker.equip_and_verify.call_count, 3)

    def test_three_failed_attempts_report_error_and_stop(self):
        self.picker.equip_and_verify.side_effect = EquipmentError('missing CE')
        self.assertFalse(self.picker.ensure(None, None))
        self.assertEqual(self.picker.equip_and_verify.call_count, 3)
        self.assertIn('中断任务', self.logs[-1])
        self.ctrl.click.assert_not_called()

    def test_success_does_not_spend_remaining_attempts(self):
        self.assertTrue(self.picker.ensure(None, None))
        self.picker.equip_and_verify.assert_called_once()

    def test_cancellation_does_not_retry(self):
        self.ctrl.cancel_event = threading.Event()
        self.ctrl.cancel_event.set()
        self.assertFalse(self.picker.ensure(None, None))
        self.picker.equip_and_verify.assert_not_called()

    def test_complete_retry_restarts_equipping_all_slots_after_failed_final_audit(self):
        picker = CraftEssencePicker(self.ctrl)
        picker.targets = MagicMock(return_value=[target(1), target(2)])
        picker.open_slot = MagicMock()
        picker.back_to_party = MagicMock()
        picker.frame = MagicMock(return_value=(None, [word('开始任务', 1750, 1000)]))
        calls = []
        failed = False
        def find(t, verify_only=False):
            nonlocal failed
            calls.append((t.slot, verify_only))
            if verify_only and not failed:
                failed = True
                raise EquipmentError('later slot displaced earlier equipment')
        picker.find = MagicMock(side_effect=find)
        self.assertTrue(picker.ensure(None, None))
        self.assertEqual(calls, [(1,False),(2,False),(1,True), (1,False),(2,False),(1,True),(2,True)])

    def test_recovery_on_unknown_screen_never_clicks(self):
        self.picker.frame = MagicMock(return_value=(None, [word('加载中', 960, 900)]))
        with self.assertRaises(EquipmentError):
            self.picker.back_to_party()
        self.ctrl.click.assert_not_called()

    def test_recovery_from_party_does_not_click_start(self):
        self.picker.frame = MagicMock(return_value=(None, [word('开始任务', 1750, 1000)]))
        self.picker.back_to_party()
        self.ctrl.click.assert_not_called()

    def test_engine_discards_strategy_after_equipment_failure(self):
        engine = SmartEngine(self.ctrl)
        engine.active_strategy = object()
        with patch.object(CraftEssencePicker, 'ensure', return_value=False):
            self.assertFalse(engine._ensure_craft_essences())
        self.assertIsNone(engine.active_strategy)
        self.ctrl.click.assert_not_called()

    def test_target_account_changes_are_not_accepted(self):
        picker = CraftEssencePicker(self.ctrl)
        owned = SimpleNamespace(lv=20, is_mlb=False)
        rec = SimpleNamespace(laplace={'passed':True}, battle_mode='loop',
                              slot1_ce_detail={'ce_id':9400480, 'lv':100, 'is_mlb':True, 'is_owned':True})
        parser = SimpleNamespace(craft_essences={9400480:owned})
        with self.assertRaisesRegex(EquipmentError, '已变化'):
            picker.targets(rec, parser)
        self.ctrl.click.assert_not_called()


class TestEquipmentTransfer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Image.open(Path(__file__).parent / 'fixtures/ce_transfer_prompt.png') as image:
            cls.img = image.copy()
        cls.ocr = ScreenDetector.run_ocr(cls.img)

    @classmethod
    def tearDownClass(cls):
        cls.img.close()

    def setUp(self):
        self.ctrl = MagicMock(cancel_event=None)
        self.picker = CraftEssencePicker(self.ctrl)

    def test_real_popup_buttons_require_both_glyphs(self):
        self.assertTrue(self.picker.transfer_prompt(self.ocr))
        positions = self.picker.transfer_buttons(self.img)
        self.assertTrue(630 <= positions[False][0] <= 690)
        self.assertTrue(1220 <= positions[True][0] <= 1300)
        with Image.new('RGB', (1920,1080),'white') as blank:
            with self.assertRaises(EquipmentError):
                self.picker.transfer_buttons(blank)

    def test_transfer_only_to_named_target_servant(self):
        self.picker.accept_transfer(self.img,self.ocr,replace(target(),servant_name='尼托克丽丝〔Alter〕'))
        self.assertEqual(self.ctrl.click.call_args.args,(1262,844))
        self.ctrl.click.reset_mock()
        with self.assertRaises(EquipmentError):
            self.picker.accept_transfer(self.img,self.ocr,replace(target(),servant_name='杜尔伽'))
        self.ctrl.click.assert_not_called()

    def test_retry_recovery_declines_pending_transfer(self):
        self.picker.frame = MagicMock(side_effect=[(self.img,self.ocr),
            (None,[word('开始任务',1750,1000)])])
        self.picker.back_to_party()
        self.ctrl.click.assert_called_once_with(660,844,delay_after=.7)


class TestEquipmentEntryGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        party_tests.TestPartyAssemblySafety.setUpClass()

    def setUp(self):
        self.fixture = party_tests.TestPartyAssemblySafety()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ctrl = self.fixture.ctrl

    def engine(self):
        engine = self.fixture.engine()
        engine.party_assembler.assemble_party_on_confirm_screen.return_value = True
        guard = patch.object(CraftEssencePicker, 'ensure', return_value=False)
        guard.start()
        self.addCleanup(guard.stop)
        return engine

    # Reuse both real daily/weekly control paths with servant assembly succeeding.
    def test_daily_does_not_start_battle_after_failed_assembly(self):
        party_tests.TestPartyAssemblySafety.test_daily_does_not_start_battle_after_failed_assembly(self)

    def test_weekly_does_not_start_battle_after_failed_assembly(self):
        party_tests.TestPartyAssemblySafety.test_weekly_does_not_start_battle_after_failed_assembly(self)


class TestEquipmentRarityFilter(unittest.TestCase):
    def setUp(self):
        class EquipmentUI(servant_tests.FilterUI):
            def frame(self, **kwargs):
                img = super().frame(**kwargs)
                if self.stage == 'picker':
                    img.info['ocr'] = [word('概念礼装选择',1720,50),word('筛选',1460,195)]
                return img
        self.ui = EquipmentUI()
        self.ui.ctrl.screencap.side_effect = self.ui.frame
        self.picker = CraftEssencePicker(self.ui.ctrl)
        patcher = patch.object(ScreenDetector,'run_ocr',side_effect=lambda image:image.info['ocr'])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_target_rarity_clears_old_conditions_and_selects_only_five_star(self):
        self.picker.clear_filters(replace(target(),rarity=5))
        self.assertEqual(self.ui.stage,'picker')
        self.assertFalse(self.ui.hidden_effect)
        self.assertTrue(self.ui.selected['★5'])
        self.assertFalse(any(self.ui.selected[f'★{n}'] for n in range(1,5)))

    def test_unknown_rarity_does_not_hide_target(self):
        self.picker.clear_filters(target())
        self.assertEqual(self.ui.stage,'picker')
        self.assertFalse(self.ui.hidden_effect)
        self.assertFalse(any(self.ui.selected.values()))

    def test_unconfirmed_filter_state_cannot_continue(self):
        self.ui.toggle_enabled = False
        with self.assertRaises(EquipmentError):
            self.picker.clear_filters(replace(target(),rarity=5))


if __name__ == '__main__':
    unittest.main()
