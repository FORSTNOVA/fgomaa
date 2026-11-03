from pathlib import Path
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from PIL import Image

from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.screen_detector import ScreenDetector


class TestNPReading(unittest.TestCase):
    def test_actual_167_percent_band_is_read_instead_of_fullscreen_seven(self):
        image=Image.new('RGB',(1920,1080))
        image.paste(Image.open(Path(__file__).parent/'fixtures/np_167_read_as_7.png'),(250,975))
        self.assertEqual(ScreenDetector.read_servant_np_percentages(image),{1:167,2:100,3:100})

    def test_decimal_or_overflow_cannot_match_a_safe_looking_suffix(self):
        box=[[360,990],[430,990],[430,1015],[360,1015]]
        for text in ('16.7%','1167%','301%'):
            with self.subTest(text=text):
                self.assertEqual(AnchorVerifier.extract_servant_np_percentages([(box,text,.99)]),{})

    def test_disagreeing_local_readings_do_not_authorize_np(self):
        readings=[(text,.99) for text in ('167%','7%','100%','100%','0%','0%')]
        ocr=MagicMock()
        ocr.text_recognizer.return_value=(readings,0)
        with patch.object(ScreenDetector,'get_ocr',return_value=ocr):
            self.assertEqual(ScreenDetector.read_servant_np_percentages(Image.new('RGB',(1920,1080))),{2:100,3:0})

    def test_glowing_np_gauge_does_not_require_text_detection(self):
        image=Image.new('RGB',(1920,1080))
        image.paste(Image.open(Path(__file__).parent/'fixtures/np_146_glowing_gauge.png'),(250,975))
        self.assertEqual(ScreenDetector.read_servant_np_percentages(image),{1:146,2:20,3:100})

    def test_incomplete_frame_is_reread_without_inventing_missing_np(self):
        from src.strategy.smart_engine import SmartEngine
        engine=object.__new__(SmartEngine)
        engine.ctrl=MagicMock()
        engine.cancel_event=None
        engine.log=MagicMock()
        with patch.object(AnchorVerifier,'detect_ready_skills',return_value={}), \
             patch.object(ScreenDetector,'read_servant_np_percentages',side_effect=[{2:100,3:100},{1:167,2:100,3:100}]), \
             patch('src.strategy.smart_engine.time.sleep'):
            values,_=engine._get_current_np_and_ready_skills()
        self.assertEqual(values[1],167)
        self.assertEqual(engine.ctrl.screencap.call_count,2)
