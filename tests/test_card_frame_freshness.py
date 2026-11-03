import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from PIL import Image
from src.core.controller import FGOController


class TestCardFrameFreshness(unittest.TestCase):
    def setUp(self):
        self.ctrl=object.__new__(FGOController)
        self.ctrl.cancel_event=None
        self.ctrl.log=MagicMock()
        self.ctrl.click=MagicMock()
        self.ctrl._safe_sleep=MagicMock()
        self.opening=Image.new('RGB',(1920,1080),'red')
        self.addCleanup(self.opening.close)
        self.settled=Image.open(Path(__file__).parent/'fixtures/command_cards_percival.png')
        self.addCleanup(self.settled.close)
        self.ctrl.screencap=MagicMock(side_effect=[self.opening,self.settled])

    def test_normal_attack_uses_current_card_faces_after_tray_opening(self):
        with patch('src.core.controller.AnchorVerifier.is_card_select_back_button_present',return_value=True):
            self.assertTrue(self.ctrl.select_attack_and_np(None,preferred_color='Arts'))
        self.assertEqual([c.args[:2] for c in self.ctrl.click.call_args_list],
                         [(1725,900),(225,750),(562,750),(1350,750)])

    def test_np_followup_uses_current_card_faces_after_np_selected(self):
        with patch('src.core.controller.AnchorVerifier.is_card_select_back_button_present',return_value=True):
            self.assertTrue(self.ctrl.select_attack_and_np(1,preferred_color='Quick'))
        self.assertEqual([c.args[:2] for c in self.ctrl.click.call_args_list],
                         [(1725,900),(700,320),(1762,750),(225,750)])


if __name__=='__main__':unittest.main()
