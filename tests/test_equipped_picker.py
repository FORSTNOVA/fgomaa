from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from src.strategy.auto_party_assembler import AutoPartyAssembler
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.servant_picker import ServantPicker


class EquippedPickerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / "fixtures"
        self.first = Image.open(root / "equipped_short_picker_static_a.png")
        self.second = Image.open(root / "equipped_short_picker_static_b.png")
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)
        self.ocr = [([[1500, 20], [1850, 20], [1850, 110], [1500, 110]], "从者选择", .99)]
        for x in (231, 511, 792, 1074, 1356):
            self.ocr.append(([[x, 320], [x + 142, 320], [x + 142, 356], [x, 356]], "06/06等级", .9))

    def test_equipped_durga_cost_overlay_does_not_hide_the_face(self):
        assembler = AutoPartyAssembler()
        matched, score, point = assembler.match_servant_in_region(
            self.first, 204800, roi=ServantPicker.GRID, scales=[1.65, 1.75, 1.85, 1.95, 2.05])
        self.assertTrue(matched)
        self.assertGreater(score, .95)
        self.assertTrue(140 < point[0] < 390 and 350 < point[1] < 470)
        self.assertFalse(assembler.match_servant_in_region(self.first, 501200, roi=ServantPicker.GRID)[0])

    def test_animated_empty_background_does_not_count_as_card_movement(self):
        self.assertGreater(AnchorVerifier.compute_frame_diff(self.first, self.second, roi=ServantPicker.GRID), .004)
        self.assertLess(ServantPicker.list_frame_diff(self.first, self.second, self.ocr), .001)

    def test_short_list_to_top_finishes_after_three_stationary_card_checks(self):
        ctrl = MagicMock()
        ctrl.cancel_event = None
        picker = ServantPicker(ctrl, SimpleNamespace(log=MagicMock()))
        picker.frame = MagicMock(side_effect=[self.first, self.second, self.first, self.second])
        with patch.object(ServantPicker, "scroll_position", return_value=None), \
             patch("src.strategy.servant_picker.ScreenDetector.run_ocr", return_value=self.ocr):
            self.assertTrue(picker.to_top(float("inf")))
        self.assertEqual(ctrl.swipe.call_count, 3)


if __name__ == "__main__":
    unittest.main()
