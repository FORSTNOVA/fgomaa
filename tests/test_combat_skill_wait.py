import unittest
from unittest.mock import MagicMock, patch

from src.core.controller import FGOController
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.screen_detector import ScreenDetector


class TestCombatSkillWait(unittest.TestCase):
    def setUp(self):
        self.ctrl = object.__new__(FGOController)
        self.ctrl.cancel_event = None
        self.ctrl.log = MagicMock()

    def test_confirmation_and_target_selection_do_not_consume_animation_wait(self):
        now = [0.0]
        def advance(seconds):
            now[0] += seconds
        self.ctrl.click = MagicMock(side_effect=lambda *a, **k: advance(k.get("delay_after", 0)))
        self.ctrl._safe_sleep = advance
        self.ctrl.screencap = MagicMock(side_effect=["confirm", "target", "target", "animation", "ready"])
        def ocr(frame):
            advance(1.5)  # Real OCR/ADB can use most of the old six-second budget.
            title = {"confirm": "技能使用", "target": "请选择对象"}.get(frame)
            return [(None, title, 1)] if title else []
        with patch("src.core.controller.time.time", side_effect=lambda: now[0]), \
             patch("src.core.controller.time.sleep", side_effect=advance), \
             patch.object(ScreenDetector, "run_ocr", side_effect=ocr), \
             patch.object(AnchorVerifier, "is_skill_confirm_dialog_open", side_effect=lambda f: f == "confirm"), \
             patch.object(AnchorVerifier, "is_skill_target_dialog_open", side_effect=lambda f: f == "target"), \
             patch.object(AnchorVerifier, "is_attack_button_ready", side_effect=lambda f: f == "ready"):
            self.assertTrue(self.ctrl.wait_for_combat_command_ready(timeout=6, target_idx=1))
        self.assertGreater(now[0], 6)
        self.assertEqual([c.args[:2] for c in self.ctrl.click.call_args_list], [(1260, 615), (500, 560)])

    def test_animation_with_target_like_pixels_does_not_click_a_servant(self):
        self.ctrl.screencap = MagicMock(side_effect=["animation", "ready"])
        self.ctrl.click = MagicMock()
        self.ctrl._safe_sleep = MagicMock()
        with patch.object(ScreenDetector, "run_ocr", return_value=[]), \
             patch.object(AnchorVerifier, "is_skill_confirm_dialog_open", return_value=False), \
             patch.object(AnchorVerifier, "is_skill_target_dialog_open", side_effect=lambda f: f == "animation"), \
             patch.object(AnchorVerifier, "is_attack_button_ready", side_effect=lambda f: f == "ready"):
            self.assertTrue(self.ctrl.wait_for_combat_command_ready())
        self.ctrl.click.assert_not_called()

    def test_ready_attack_button_without_consumed_skill_is_not_success(self):
        self.ctrl.wait_for_combat_command_ready = MagicMock(return_value=True)
        self.ctrl.screencap = MagicMock()
        self.ctrl.click = MagicMock()
        with patch.object(AnchorVerifier, "is_skill_ready", return_value=True), \
             patch.object(AnchorVerifier, "is_attack_button_ready", return_value=False):
            self.assertFalse(self.ctrl.cast_servant_skill(2, 2, 1))

    def test_master_skill_propagates_wait_failure(self):
        self.ctrl.wait_for_combat_command_ready = MagicMock(side_effect=[True, False])
        self.ctrl.screencap = MagicMock()
        self.ctrl.click = MagicMock()
        with patch.object(AnchorVerifier, "is_skill_confirm_dialog_open", return_value=False):
            self.assertFalse(self.ctrl.cast_master_skill(1, 1))

    def test_visible_skill_confirmation_prevents_retry_that_would_dismiss_it(self):
        self.ctrl.wait_for_combat_command_ready=MagicMock(return_value=True)
        self.ctrl.screencap=MagicMock(return_value='oberon_confirmation')
        self.ctrl.click=MagicMock()
        with patch.object(AnchorVerifier,'is_skill_ready',side_effect=[True,True,False]), \
             patch.object(AnchorVerifier,'is_attack_button_ready',return_value=True), \
             patch.object(AnchorVerifier,'is_skill_confirm_dialog_open',return_value=False), \
             patch.object(AnchorVerifier,'is_skill_target_dialog_open',return_value=False), \
             patch.object(ScreenDetector,'run_ocr',return_value=[(None,'技能使用',.99)]):
            self.assertTrue(self.ctrl.cast_servant_skill(2,3,1))
        self.ctrl.click.assert_called_once_with(852,870,delay_after=.62)


if __name__ == "__main__":
    unittest.main()
