import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from PIL import Image
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.screen_detector import ScreenDetector,GameScreen


class TestBlueBackgroundCards(unittest.TestCase):
    def frame(self,name):
        image=Image.open(Path(__file__).parent/'fixtures'/f'{name}.png')
        self.addCleanup(image.close);return image

    def test_real_blue_background_card_tray_is_detected(self):
        image=self.frame('command_cards_blue_background')
        self.assertFalse(AnchorVerifier.is_attack_button_ready(image))
        self.assertTrue(AnchorVerifier.is_card_select_back_button_present(image))
        screen,_,details=ScreenDetector.detect(image)
        self.assertEqual(screen,GameScreen.BATTLE_CARD_SELECT)

    def test_existing_cards_and_combat_stay_distinct(self):
        for name,expected in [('command_cards_percival',True),('battle_wave_two',False)]:
            with self.subTest(name=name):
                self.assertEqual(AnchorVerifier.is_card_select_back_button_present(self.frame(name)),expected)

    def test_blue_background_without_matching_button_text_is_rejected(self):
        image=self.frame('command_cards_blue_background');ocr=MagicMock()
        for readings in [[('攻击',.95)],[('返回',.4)],[('返回',.9),('取消',.9)]]:
            ocr.text_recognizer.return_value=(readings,None)
            with patch.object(ScreenDetector,'get_ocr',return_value=ocr):
                self.assertFalse(AnchorVerifier.is_card_select_back_button_present(image))


if __name__=='__main__':unittest.main()
