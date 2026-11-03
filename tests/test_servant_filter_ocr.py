"""Regression for the real daily filter's missing Rider OCR button."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from src.strategy.servant_picker import ServantPicker
from src.strategy.screen_detector import ScreenDetector


class TestServantFilterOCR(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Image.open(Path(__file__).parent/'fixtures/servant_filter_missed_rider.png') as image:
            cls.image = image.convert('RGB')
        cls.full_ocr = ScreenDetector.run_ocr(cls.image)

    @classmethod
    def tearDownClass(cls):
        cls.image.close()

    def setUp(self):
        self.ctrl = MagicMock(cancel_event=None)
        self.picker = ServantPicker(self.ctrl, SimpleNamespace(log=MagicMock()))
        self.picker.frame = MagicMock(return_value=self.image)

    def test_real_rider_button_is_recovered_at_its_actual_position(self):
        self.assertIsNone(self.picker.button(self.full_ocr, '骑兵'))
        ocr = self.picker.filter_ocr(self.image, ['骑兵'])
        point = self.picker.button(ocr, '骑兵')
        self.assertIsNotNone(point)
        self.assertTrue(1130 < point[0] < 1360 and 560 < point[1] < 650)
        self.assertIs(self.picker.button_selected(self.image, point), False)

    def test_recognized_button_does_not_need_fallback(self):
        with patch.object(ScreenDetector, 'get_ocr') as local, patch.object(ScreenDetector, 'run_ocr', return_value=self.full_ocr):
            ocr = self.picker.filter_ocr(self.image, ['魔术师'])
        self.assertIsNotNone(self.picker.button(ocr, '魔术师'))
        local.assert_not_called()

    def test_no_local_read_cannot_guess_the_rider_position(self):
        with patch.object(ScreenDetector, 'get_ocr', return_value=MagicMock(return_value=(None, None))), patch.object(ScreenDetector, 'run_ocr', return_value=self.full_ocr):
            self.assertFalse(self.picker.set_filter_button('骑兵'))
        self.ctrl.click.assert_not_called()

    def test_low_confidence_or_other_label_cannot_select_rider(self):
        box = [[0, 0], [80, 0], [80, 35], [0, 35]]
        for text, confidence in [('骑兵', .54), ('弓兵', .99)]:
            with self.subTest(text=text, confidence=confidence), patch.object(ScreenDetector, 'get_ocr', return_value=MagicMock(return_value=([(box, text, confidence)], None))), patch.object(ScreenDetector, 'run_ocr', return_value=self.full_ocr):
                self.assertFalse(self.picker.set_filter_button('骑兵'))
        self.ctrl.click.assert_not_called()

    def test_no_filter_dialog_does_not_attempt_local_button_reading(self):
        no_dialog = [row for row in self.full_ocr if row[1] != '筛选设定']
        with patch.object(ScreenDetector, 'get_ocr') as local, patch.object(ScreenDetector, 'run_ocr', return_value=no_dialog):
            self.assertEqual(self.picker.filter_ocr(self.image, ['骑兵']), no_dialog)
        local.assert_not_called()


if __name__ == '__main__':
    unittest.main()
