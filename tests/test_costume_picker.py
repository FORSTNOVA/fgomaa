import unittest
from pathlib import Path

from PIL import Image

from src.strategy.auto_party_assembler import AutoPartyAssembler


class CostumePickerTests(unittest.TestCase):
    def test_cached_public_costume_matches_real_filtered_picker(self):
        image = Image.open(Path(__file__).parent / "fixtures/nitocris_costume_picker.png")
        assembler = AutoPartyAssembler()
        matched, score, point = assembler.match_servant_in_region(
            image, 501200, roi=(140, 275, 1800, 1065),
            scales=[1.65, 1.75, 1.85, 1.95, 2.05],
        )
        self.assertTrue(matched)
        self.assertGreater(score, 0.8)
        self.assertTrue(1270 < point[0] < 1520 and 590 < point[1] < 870)


if __name__ == "__main__":
    unittest.main()
