import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from src.strategy.screen_detector import ScreenDetector, GameScreen
from src.strategy.anchor_verifier import AnchorVerifier


class TestBattleWaveRecovery(unittest.TestCase):
    def test_live_hud_recovers_second_wave_when_fullscreen_ocr_misses_it(self):
        image = Image.new('RGB',(1920,1080),(100,100,100))
        image.paste(Image.open(Path(__file__).parent/'fixtures/battle_wave_two.png'),(1150,0))
        real_ocr = ScreenDetector.run_ocr
        def recognize(frame):
            return [] if frame.size == (1920,1080) else real_ocr(frame)
        with patch.object(ScreenDetector,'run_ocr',side_effect=recognize), \
             patch('src.strategy.screen_detector.AnchorVerifier.is_attack_button_ready',return_value=True), \
             patch('src.strategy.screen_detector.AnchorVerifier.is_card_select_back_button_present',return_value=False):
            screen, _, details = ScreenDetector.detect(image)
        self.assertEqual(screen,GameScreen.BATTLE_COMBAT)
        self.assertEqual(details['wave'],2)
        self.assertEqual(details['turn'],2)

    def test_missing_wave_never_returns_actionable_combat(self):
        with patch.object(ScreenDetector,'run_ocr',return_value=[]), \
             patch('src.strategy.screen_detector.AnchorVerifier.is_attack_button_ready',return_value=True), \
             patch('src.strategy.screen_detector.AnchorVerifier.is_card_select_back_button_present',return_value=False):
            screen, _, details = ScreenDetector.detect(Image.new('RGB',(1920,1080),(100,100,100)))
        self.assertEqual(screen,GameScreen.UNKNOWN)
        self.assertNotIn('wave',details)

    def test_joined_digits_cannot_match_a_suffix_as_wave_one(self):
        box=[[1320,20],[1410,20],[1410,60],[1320,60]]
        for text in ('21/3','321/3','2/31','BATTLE 21/3'):
            with self.subTest(text=text):
                self.assertIsNone(AnchorVerifier.extract_battle_wave([(box,text,.99)]))

    def test_actual_joined_digit_hud_is_recovered_by_crop(self):
        image=Image.new('RGB',(1920,1080),(100,100,100))
        image.paste(Image.open(Path(__file__).parent/'fixtures/battle_wave_two_joined_digits.png'),(1150,0))
        box=[[1320,20],[1410,20],[1410,60],[1320,60]]
        real_ocr=ScreenDetector.run_ocr
        def recognize(frame):
            return [(box,'21/3',.99)] if frame.size==(1920,1080) else real_ocr(frame)
        with patch.object(ScreenDetector,'run_ocr',side_effect=recognize), \
             patch('src.strategy.screen_detector.AnchorVerifier.is_attack_button_ready',return_value=True), \
             patch('src.strategy.screen_detector.AnchorVerifier.is_card_select_back_button_present',return_value=False):
            screen,_,details=ScreenDetector.detect(image)
        self.assertEqual(screen,GameScreen.BATTLE_COMBAT)
        self.assertEqual(details['wave'],2)
        self.assertIn('battle_counter_ocr',details)


if __name__ == '__main__':
    unittest.main()
