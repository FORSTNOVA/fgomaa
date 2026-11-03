import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from PIL import Image

from src.strategy.screen_detector import ScreenDetector, GameScreen
from src.strategy.team_detector import TeamDetector
from src.strategy.smart_engine import SmartEngine


class TestWeeklyFixes(unittest.TestCase):

    def test_septem_catalog_alias_can_locate_rome_chapter(self):
        target = "第二特异点 永续狂气帝国 七丘之城"
        self.assertIn('罗马',SmartEngine._get_war_search_tokens(target))
        self.assertIn('第二特异点',SmartEngine._get_war_search_tokens(target))
        self.assertTrue(SmartEngine.is_target_map('罗马','第二特异点 永恒狂气帝国 罗马',target))

    def test_map_landmarks_do_not_match_generic_unconfirmed_prefix(self):
        target = "未确认坐标Ｘ－Ｅ"
        for text in ("未确认坐标X - E", "末确认坐标XE", "未确认坐标Ｘ－Ｅ"):
            self.assertTrue(SmartEngine._spot_matches(target, text))
        for text in ("未确认坐标X-F", "未确认坐标X-B", "未确认坐标", "X-EVENT"):
            self.assertFalse(SmartEngine._spot_matches(target, text))
        self.assertTrue(SmartEngine._spot_matches("马西利亚", "马西利亚"))
        self.assertFalse(SmartEngine._spot_matches("马西利亚", "亚壁街道"))

    def test_unique_free_card_requires_all_cross_checks(self):
        engine = SmartEngine(MagicMock())
        engine.current_recommendation = SimpleNamespace(target_quest_id='fuyuki_x_e',
            laplace={'passed': True, 'quest_id': 93000005})
        def word(text, x, y):
            return ([[x-25,y-10],[x+25,y-10],[x+25,y+10],[x-25,y+10]], text, .95)
        words = [word('自由关卡', 1220, 265), word('AP 5', 1240, 335), word('已获取', 1720, 320)]
        title = '骸骨彷徨的教会'
        self.assertEqual(engine._single_free_quest_card(words, title), (1450, 325))
        for invalid in (words[:-1], words[1:], [words[0], word('AP4',1240,335), words[2]],
                        words + [word('自由关卡',1220,570)]):
            self.assertIsNone(engine._single_free_quest_card(invalid, title))
        self.assertIsNone(engine._single_free_quest_card(words, '烧尽崩塌的校舍'))
        engine.current_recommendation.laplace['quest_id'] = 93000006
        self.assertIsNone(engine._single_free_quest_card(words, title))

    def test_live_second_card_keeps_its_actual_y_and_ap(self):
        evidence = json.loads((Path(__file__).parent/'fixtures/weekly_multi_card_ocr.json').read_text(encoding='utf-8'))
        engine = SmartEngine(MagicMock())
        engine.current_recommendation = SimpleNamespace(laplace={'quest_ap':9})
        self.assertEqual(engine._named_free_quest_card(evidence['cards'],'远古的港口'),(1450,506))
        self.assertIsNone(engine._named_free_quest_card(evidence['cards'],'被渴望的圣女'))
        self.assertFalse(engine._support_quest_level_matches(evidence['support'],16))
        self.assertTrue(engine._support_quest_level_matches(evidence['support'],80))
        self.assertFalse(engine._support_quest_level_matches([],16))

    def test_regional_map_not_misidentified_as_main_menu(self):
        """测试包含菜单、行动力但属于章节大地图/关卡列表时，不会误判为终端大厅 (MAIN_MENU)"""
        # 模拟特异点大地图/关卡列表 OCR
        mock_ocr = [
            ([[1600, 40], [1800, 40], [1800, 70], [1600, 70]], "燃烧污染都市冬木", 0.95),
            ([[150, 50], [200, 50], [200, 80], [150, 80]], "关闭", 0.95),
            ([[1000, 150], [1200, 150], [1200, 180], [1000, 180]], "未确认坐标X-A", 0.95),
            ([[1200, 320], [1260, 320], [1260, 350], [1200, 350]], "AP 4", 0.95),
            ([[1700, 960], [1850, 960], [1850, 1000], [1700, 1000]], "菜单", 0.95),
            ([[400, 940], [500, 940], [500, 970], [400, 970]], "行动力 158/190", 0.95),
        ]
        dummy_img = Image.new("RGB", (1920, 1080), (100, 100, 100))
        with patch.object(ScreenDetector, "run_ocr", return_value=mock_ocr):
            screen, conf, details = ScreenDetector.detect(dummy_img)
            self.assertNotEqual(screen, GameScreen.MAIN_MENU)
            self.assertEqual(screen, GameScreen.REGIONAL_MAP)

    def test_team_detector_slot_boundaries_and_aliases(self):
        """测试 1920x1080 下 4 个槽位从者名称被精准隔离映射，不再把 1/2 号位混杂在一起"""
        detector = TeamDetector()
        mock_ocr = [
            # Slot 1: 赵云 (cx=200)
            ([[150, 800], [250, 800], [250, 840], [150, 840]], "枪阶 赵云", 0.95),
            # Slot 2: 梣 / 救世主托内莉可 (cx=500)
            ([[450, 800], [550, 800], [550, 840], [450, 840]], "魔术师 梣", 0.95),
            # Slot 3: 阿尔托莉雅·Caster (cx=800)
            ([[750, 800], [850, 800], [850, 840], [750, 840]], "魔术师 阿尔托莉雅·Caster", 0.95),
            # Slot 4: 奥伯龙 (cx=1120)
            ([[1080, 800], [1160, 800], [1160, 840], [1080, 840]], "奥伯龙", 0.95),
        ]
        servants = detector.detect_servants_from_screen(mock_ocr)
        self.assertEqual(servants[0], "赵云")
        self.assertEqual(servants[1], "梣")
        self.assertEqual(servants[2], "阿尔托莉雅·Caster")
        self.assertEqual(servants[3], "奥伯龙")

    def test_navigate_to_main_menu_from_regional_map(self):
        """测试从区域大地图（带关卡抽屉）一步步返回主大厅"""
        ctrl = MagicMock()
        engine = SmartEngine(controller=ctrl)

        # 模拟第1次在关卡抽屉（有关闭），第2次在大地图（有管理室），第3次在主线记录（有关闭），第4次到达大厅
        screen_seq = [
            (GameScreen.REGIONAL_MAP, {"ocr_results": [([[150, 50], [200, 50], [200, 80], [150, 80]], "关闭", 0.95)], "all_texts": ["关闭", "燃烧污染都市"]}),
            (GameScreen.REGIONAL_MAP, {"ocr_results": [([[150, 50], [200, 50], [200, 80], [150, 80]], "管理室", 0.95)], "all_texts": ["管理室", "燃烧污染都市"]}),
            (GameScreen.MAIN_RECORD, {"ocr_results": [([[150, 50], [200, 50], [200, 80], [150, 80]], "关闭", 0.95)], "all_texts": ["关闭", "主线记录"]}),
            (GameScreen.MAIN_MENU, {"ocr_results": [], "all_texts": ["活动报酬", "通知", "菜单"]}),
        ]
        engine.get_current_screen = MagicMock(side_effect=screen_seq)

        ok = engine.navigate_to_main_menu(max_steps=5)
        self.assertTrue(ok)
        self.assertEqual(ctrl.click.call_count, 3)

    def test_claim_weekly_rewards_skips_when_no_button(self):
        """测试周常面板无【统一领取】时，绝不盲点(1710, 301)以避免误触圣晶片弹窗"""
        ctrl = MagicMock()
        engine = SmartEngine(controller=ctrl)
        engine.get_current_screen = MagicMock(return_value=(GameScreen.MASTER_MISSIONS, {"ocr_results": []}))
        ctrl.wait_for_still = MagicMock(return_value=Image.new("RGB", (100, 100), (0, 0, 0)))

        # 模拟当前周常界面没有任何【统一领取】或【全部领取】文本
        with patch.object(ScreenDetector, "run_ocr", return_value=[([[100, 200], [200, 200], [200, 240], [100, 240]], "周常", 0.95)]):
            res = engine.claim_weekly_rewards()
            self.assertFalse(res)
            # 确认没有点击 (1710, 301)
            clicked_coords = [call[0] for call in ctrl.click.call_args_list]
            self.assertNotIn((1710, 301), [(c[0], c[1]) for c in clicked_coords])


if __name__ == "__main__":
    unittest.main()
