# tests/test_map_navigation.py
import unittest
from src.strategy.screen_detector import ScreenDetector
from src.strategy.smart_engine import SmartEngine

class TestMapNavigation(unittest.TestCase):
    def test_identify_regional_map_orleans(self):
        ocr_mock = [
            ([[1400, 30], [1600, 30], [1600, 60], [1400, 60]], "邪龙百年战争", 0.95),
            ([[1700, 30], [1850, 30], [1700, 60], [1850, 60]], "奥尔良", 0.95),
            ([[1500, 80], [1700, 80], [1500, 110], [1700, 110]], "第一特异点", 0.95),
            ([[1750, 80], [1850, 80], [1750, 110], [1850, 110]], "A.D.1431", 0.95),
            ([[100, 50], [200, 50], [100, 80], [200, 80]], "管理室", 0.95),
        ]
        short_map, canon_map = ScreenDetector.identify_regional_map(ocr_mock)
        self.assertEqual(short_map, "奥尔良")
        self.assertIn("第一特异点", canon_map)
        self.assertIn("奥尔良", canon_map)

    def test_identify_regional_map_fuyuki(self):
        ocr_mock = [
            ([[1400, 30], [1600, 30], [1600, 60], [1400, 60]], "特异点F", 0.95),
            ([[1700, 30], [1850, 30], [1700, 60], [1850, 60]], "燃烧污染都市", 0.95),
            ([[1500, 80], [1700, 80], [1500, 110], [1700, 110]], "冬木", 0.95),
            ([[1750, 80], [1850, 80], [1750, 110], [1850, 110]], "A.D.2004", 0.95),
        ]
        short_map, canon_map = ScreenDetector.identify_regional_map(ocr_mock)
        self.assertEqual(short_map, "冬木")
        self.assertIn("特异点F", canon_map)
        self.assertIn("冬木", canon_map)

    def test_identify_regional_map_septem(self):
        ocr_mock = [
            ([[1400, 30], [1600, 30], [1600, 60], [1400, 60]], "第二特异点", 0.95),
            ([[1700, 30], [1850, 30], [1700, 60], [1850, 60]], "永恒狂气帝国", 0.95),
            ([[1500, 80], [1700, 80], [1500, 110], [1700, 110]], "七丘之城", 0.95),
        ]
        short_map, canon_map = ScreenDetector.identify_regional_map(ocr_mock)
        self.assertEqual(short_map, "罗马")
        self.assertIn("第二特异点", canon_map)

    def test_is_target_map(self):
        # Same map matches
        self.assertTrue(SmartEngine.is_target_map("奥尔良", "第一特异点 邪龙百年战争 奥尔良", "第一特异点 邪龙百年战争 奥尔良"))
        self.assertTrue(SmartEngine.is_target_map("冬木", "特异点F 燃烧污染都市 冬木", "特异点F 燃烧污染都市 冬木"))
        self.assertTrue(SmartEngine.is_target_map("冬木", "特异点F 燃烧污染都市 冬木", "特异点F"))
        self.assertTrue(SmartEngine.is_target_map("罗马", "第二特异点 永恒狂气帝国 罗马", "第二特异点"))

        # Cross map mismatches
        self.assertFalse(SmartEngine.is_target_map("奥尔良", "第一特异点 邪龙百年战争 奥尔良", "特异点F 燃烧污染都市 冬木"))
        self.assertFalse(SmartEngine.is_target_map("冬木", "特异点F 燃烧污染都市 冬木", "第一特异点 邪龙百年战争 奥尔良"))
        self.assertFalse(SmartEngine.is_target_map("罗马", "第二特异点 永恒狂气帝国 罗马", "第一特异点 邪龙百年战争 奥尔良"))

    def test_war_search_tokens(self):
        tokens_fuyuki = SmartEngine._get_war_search_tokens("特异点F 燃烧污染都市 冬木")
        self.assertIn("冬木", tokens_fuyuki)
        self.assertIn("特异点F", tokens_fuyuki)

        tokens_orleans = SmartEngine._get_war_search_tokens("第一特异点 邪龙百年战争 奥尔良")
        self.assertIn("奥尔良", tokens_orleans)
        self.assertIn("第一特异点", tokens_orleans)

if __name__ == '__main__':
    unittest.main()
