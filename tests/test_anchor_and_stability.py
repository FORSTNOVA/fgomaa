"""
Unit Tests for MAA/FGA Stability, Anchor Verification, and Closed-Loop Actions
"""

import sys
import time
import unittest
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import numpy as np
from PIL import Image
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.screen_detector import ScreenDetector, GameScreen


class TestAnchorAndStability(unittest.TestCase):
    """测试 MAA 画面消抖与 FGA 颜色锚点系统"""

    def test_frame_stability_detection(self):
        """测试连续相同帧判定为绝对静止，动态帧判定为动画过渡"""
        # 两张相同帧
        img1 = Image.new("RGB", (1920, 1080), (128, 128, 128))
        img2 = Image.new("RGB", (1920, 1080), (128, 128, 128))

        is_stable, diff = AnchorVerifier.is_frame_stable(img1, img2, threshold=0.012)
        self.assertTrue(is_stable)
        self.assertEqual(diff, 0.0)

        # 两张剧烈变动帧 (模拟转场/淡入淡出动画)
        img3 = Image.new("RGB", (1920, 1080), (240, 240, 240))
        is_stable_moving, diff_moving = AnchorVerifier.is_frame_stable(img1, img3, threshold=0.012)
        self.assertFalse(is_stable_moving)
        self.assertGreater(diff_moving, 0.3)

    def test_card_select_back_button_anchor(self):
        """测试 FGA 标准选卡界面右下角蓝色 Back 按钮 HSV 颜色检测"""
        # 1. 构造一个包含右下角蓝色 Back 按钮特征的画面
        img_blue = Image.new("RGB", (1920, 1080), (30, 30, 30))
        # 在 [930:1050, 1580:1890] 区域填入 FGA 典型的蓝色按键 RGB (35, 110, 225)
        arr = np.array(img_blue)
        arr[950:1030, 1620:1850] = [35, 110, 225]
        img_with_back = Image.fromarray(arr)

        has_back = AnchorVerifier.is_card_select_back_button_present(img_with_back)
        self.assertTrue(has_back, "应当正确检测到右下角蓝色 Back 按钮锚点！")

        # 2. 构造一个无蓝色按键的常规战斗画面
        img_no_blue = Image.new("RGB", (1920, 1080), (40, 40, 40))
        has_back_neg = AnchorVerifier.is_card_select_back_button_present(img_no_blue)
        self.assertFalse(has_back_neg, "无蓝色按键时应返回 False！")

    def test_start_quest_gold_button_anchor(self):
        """测试出击编队右下角金色开始任务按钮 HSV 检测"""
        img_gold = Image.new("RGB", (1920, 1080), (30, 30, 30))
        # 在 [940:1060, 1550:1890] 区域填入金色 RGB (225, 175, 40)
        arr = np.array(img_gold)
        arr[960:1040, 1600:1850] = [225, 175, 40]
        img_with_start = Image.fromarray(arr)

        has_start = AnchorVerifier.is_start_quest_button_present(img_with_start)
        self.assertTrue(has_start, "应当正确检测到右下角金色出击按钮锚点！")

    def test_wave_extraction(self):
        """测试从 OCR 文本中提取当前战斗 Wave 轮数"""
        samples = [
            ([[[50, 30], [200, 30], [200, 70], [50, 70]], "BATTLE 1/3", 0.99], 1),
            ([[[60, 35], [220, 35], [220, 75], [60, 75]], "BATTLE 2/3", 0.98], 2),
            ([[[50, 30], [200, 30], [200, 70], [50, 70]], "BATTLE3/3", 0.99], 3),
            ([[[80, 40], [150, 40], [150, 80], [80, 80]], "Wave 2/3", 0.97], 2),
            ([[[70, 30], [130, 30], [130, 70], [70, 70]], "3/3", 0.95], 3),
        ]
        for dummy_box, expected_wave in samples:
            wave = AnchorVerifier.extract_battle_wave([dummy_box])
            self.assertEqual(wave, expected_wave, f"应当提取到 Wave {expected_wave}，实际: {wave}")

    def test_card_select_precedence_over_combat(self):
        """测试选卡界面具有最高优先级，绝不因顶部残存 BATTLE 1/3 文本而误判为常规战斗"""
        # 模拟选卡界面：顶部有 BATTLE 1/3，右下角有蓝色 Back 按钮与指令卡
        img_card_select = Image.new("RGB", (1920, 1080), (40, 40, 40))
        arr = np.array(img_card_select)
        arr[950:1030, 1620:1850] = [35, 110, 225]
        test_img = Image.fromarray(arr)

        # 模拟 OCR 结果同时包含 BATTLE 1/3 与 Back
        dummy_ocr = [
            ([[50, 30], [200, 30], [200, 70], [50, 70]], "BATTLE 1/3", 0.99),
            ([[1650, 960], [1800, 960], [1800, 1020], [1650, 1020]], "Back", 0.98),
            ([[300, 700], [500, 700], [500, 800], [300, 800]], "Buster", 0.95),
            ([[700, 700], [900, 700], [900, 800], [700, 800]], "Arts", 0.95),
        ]

        import unittest.mock as mock
        with mock.patch.object(ScreenDetector, "run_ocr", return_value=dummy_ocr):
            screen, conf, details = ScreenDetector.detect(test_img)
            self.assertEqual(screen, GameScreen.BATTLE_CARD_SELECT)
            self.assertEqual(details.get("wave"), 1)
            print(f"√ 选卡界面优先级测试通过: 正确识别为 {screen.value}，Wave 轮数: {details.get('wave')}")

    def test_controller_wait_for_still(self):
        """测试控制器 wait_for_still 消抖在画面稳定时提前退出"""
        from unittest.mock import MagicMock, patch
        from src.core.controller import FGOController

        # 构造 mock 控制器
        with patch.object(FGOController, "__init__", lambda self: None):
            ctrl = FGOController()
            ctrl.cancel_event = None
            ctrl.log_cb = lambda msg: None
            ctrl._safe_sleep = lambda s: None

            stable_img = Image.new("RGB", (1920, 1080), (100, 100, 100))
            ctrl.screencap = MagicMock(return_value=stable_img)

            t0 = time.time()
            result_img = ctrl.wait_for_still(timeout=1.0, min_still_duration=0.1, check_interval=0.02)
            self.assertEqual(result_img, stable_img)
            print("√ 控制器 wait_for_still 消抖测试通过: 画面静止时快速返回！")

    def test_controller_click_and_verify(self):
        """测试控制器 click_and_verify 闭环动作在条件满足时成功，否则重试"""
        from unittest.mock import MagicMock, patch
        from src.core.controller import FGOController

        with patch.object(FGOController, "__init__", lambda self: None):
            ctrl = FGOController()
            ctrl.cancel_event = None
            ctrl.click = MagicMock()
            ctrl._safe_sleep = lambda s: None

            # 模拟第 2 次尝试时校验成功
            call_count = 0
            def mock_verify():
                nonlocal call_count
                call_count += 1
                return call_count >= 2

            success = ctrl.click_and_verify(960, 540, verify_fn=mock_verify, timeout=1.0, interval=0.1, max_retries=2)
            self.assertTrue(success)
            self.assertGreaterEqual(ctrl.click.call_count, 1)
            print("√ 控制器 click_and_verify 闭环动作测试通过！")


if __name__ == "__main__":
    unittest.main()
