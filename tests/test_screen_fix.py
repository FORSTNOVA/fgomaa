"""
Unit tests for screen detector fixes & party confirm recognition
"""

import sys
from pathlib import Path
from PIL import Image

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.strategy.screen_detector import ScreenDetector, GameScreen
from src.strategy.team_detector import TeamDetector
from src.strategy.battle_plans import BattlePlanManager


def test_event_banner_not_combat():
    print("\n--- [Test 1] 活动攻击加成文案绝不误判为战斗状态 ---")
    # 模拟包含“攻击威力提升50%”的每日任务界面 OCR
    mock_ocr = [
        ([[1600, 30], [1800, 30], [1800, 60], [1600, 60]], "每日任务", 0.99),
        ([[100, 40], [200, 40], [200, 80], [100, 80]], "关闭", 0.98),
        ([[1400, 500], [1800, 500], [1800, 550], [1400, 550]], "术之修炼场极级", 0.95),
        ([[1450, 950], [1850, 950], [1850, 1000], [1450, 1000]], "关联从者攻击威力提升50%！", 0.96),
    ]
    # 创建纯黑图片仅供函数形式调用
    dummy_img = Image.new("RGB", (1920, 1080), (50, 50, 50))
    # 猴子补丁临时测试 detect
    orig_run_ocr = ScreenDetector.run_ocr
    ScreenDetector.run_ocr = lambda img: mock_ocr
    try:
        screen, conf, details = ScreenDetector.detect(dummy_img)
        print(f"识别结果: {screen.value} (置信度: {conf})")
        assert screen == GameScreen.CHALDEA_GATE, f"Expected CHALDEA_GATE, got {screen}"
        print("√ 测试通过: 活动文案带有'攻击'绝不会被误判为战斗，正确归属为迦勒底之门关卡列表！")
    finally:
        ScreenDetector.run_ocr = orig_run_ocr


def test_craft_essence_screen_detect():
    print("\n--- [Test 2] 概念礼装选择界面精确识别 ---")
    mock_ocr = [
        ([[100, 40], [200, 40], [200, 80], [100, 80]], "关闭", 0.98),
        ([[1600, 30], [1800, 30], [1800, 60], [1600, 60]], "概念礼装选择", 0.99),
        ([[500, 100], [800, 100], [800, 150], [500, 150]], "总消耗值 105/113", 0.95),
    ]
    dummy_img = Image.new("RGB", (1920, 1080), (50, 50, 50))
    orig_run_ocr = ScreenDetector.run_ocr
    ScreenDetector.run_ocr = lambda img: mock_ocr
    try:
        screen, conf, details = ScreenDetector.detect(dummy_img)
        print(f"识别结果: {screen.value} (置信度: {conf})")
        assert screen == GameScreen.CRAFT_ESSENCE_SELECT, f"Expected CRAFT_ESSENCE_SELECT, got {screen}"
        print("√ 测试通过: 概念礼装选择界面精确识别，能及时触发关闭返回上一层！")
    finally:
        ScreenDetector.run_ocr = orig_run_ocr


def test_party_confirm_servant_recognition():
    print("\n--- [Test 3] 出击编队确认界面从者识别与 3T 绑定 ---")
    plan_mgr = BattlePlanManager()
    detector = TeamDetector(plan_manager=plan_mgr)

    # 模拟编队界面 OCR 文本 (摩根在 Slot 1，杀狐在 Slot 2 与 Slot 3 助战位)
    mock_party_ocr = [
        ([[100, 40], [200, 40], [200, 80], [100, 80]], "返回", 0.98),
        ([[1600, 950], [1850, 950], [1850, 1020], [1600, 1020]], "开始任务", 0.99),
        ([[250, 500], [400, 500], [400, 550], [250, 550]], "摩根", 0.97),
        ([[700, 500], [850, 500], [850, 550], [700, 550]], "光之高扬斯卡娅", 0.97),
        ([[1100, 500], [1250, 500], [1250, 550], [1100, 550]], "杀狐", 0.96),
    ]
    servants = detector.detect_servants_from_screen(mock_party_ocr)
    print(f"识别到的 1-3 号位从者: {servants}")
    assert servants[0] == "摩根"
    assert servants[1] == "光之高扬斯卡娅"
    assert servants[2] == "光之高扬斯卡娅"

    party, strategy, logs = detector.identify_party_and_plan(servants)
    for l in logs:
        print(f"  {l}")
    assert strategy is not None
    assert strategy.id == "buster_morgan_koyan"
    print("√ 测试通过: 出击编队从者与 3T 策略完全识别就绪！")


def test_highest_diff_fallback():
    print("\n--- [Test 4] 特殊日常关卡最高难度智能兜底测试 ---")
    mock_ocr = [
        ([[1200, 200], [1600, 200], [1600, 250], [1200, 250]], "【终章前夕！】每日替换术之修炼场超级", 0.95),
        ([[1200, 500], [1600, 500], [1600, 550], [1200, 550]], "【终章前夕！】每日替换术之修炼场极级", 0.95),
        ([[1200, 800], [1600, 800], [1600, 850], [1200, 850]], "【终章前夕！】每日替换暗之修炼场初级", 0.95),
    ]
    res = ScreenDetector.find_highest_difficulty_daily_quest(mock_ocr)
    print("最高难度匹配结果:", res)
    assert res is not None
    assert res[0] == 1400 and res[1] == 525
    assert "极级" in res[2]
    print("√ 测试通过: 极级副本成功被优先锁定！")


def test_battle_result_and_item_popup():
    print("\n--- [Test 5] 战利品结算界面与展开的物品信息界面防误判测试 ---")
    dummy_img = Image.new("RGB", (1920, 1080), (50, 50, 50))
    orig_run_ocr = ScreenDetector.run_ocr
    try:
        # 5.1 正常战利品结算页（带有“长按战利品可查看详细信息”提示，但无关闭按钮）
        mock_result_ocr = [
            ([[200, 100], [500, 100], [500, 150], [200, 150]], "获得了战利品", 0.99),
            ([[300, 940], [800, 940], [800, 980], [300, 980]], "长按战利品可查看详细信息", 0.96),
            ([[1580, 980], [1780, 980], [1780, 1030], [1580, 1030]], "下一步", 0.99),
        ]
        ScreenDetector.run_ocr = lambda img: mock_result_ocr
        screen, conf, details = ScreenDetector.detect(dummy_img)
        assert screen == GameScreen.BATTLE_RESULT, f"Expected BATTLE_RESULT, got {screen}"

        # 5.2 战利品结算页上误触展开了物品信息弹窗（背景仍有“战利品”，居中出现“道具详情 / 持有数 / 关 闭”）
        mock_item_popup_ocr = [
            ([[200, 100], [500, 100], [500, 150], [200, 150]], "获得了战利品", 0.95),
            ([[850, 200], [1070, 200], [1070, 250], [850, 250]], "道具详情", 0.99),
            ([[1350, 300], [1550, 300], [1550, 340], [1350, 340]], "持有数 128", 0.98),
            ([[860, 810], [1060, 810], [1060, 860], [860, 860]], "关 闭", 0.97),
        ]
        ScreenDetector.run_ocr = lambda img: mock_item_popup_ocr
        screen, conf, details = ScreenDetector.detect(dummy_img)
        assert screen == GameScreen.POPUP_DIALOG, f"Expected POPUP_DIALOG, got {screen}"
        assert details.get("close_coord") == (960, 835), f"Unexpected close_coord: {details.get('close_coord')}"

        # 5.3 连续出击弹窗在战利品背景上方弹出
        mock_continue_ocr = [
            ([[200, 100], [500, 100], [500, 150], [200, 150]], "获得了战利品", 0.92),
            ([[540, 800], [740, 800], [740, 860], [540, 860]], "关闭", 0.99),
            ([[1180, 800], [1380, 800], [1380, 860], [1180, 860]], "连续出击", 0.99),
        ]
        ScreenDetector.run_ocr = lambda img: mock_continue_ocr
        screen, conf, details = ScreenDetector.detect(dummy_img)
        assert screen == GameScreen.CONTINUE_QUEST, f"Expected CONTINUE_QUEST, got {screen}"
        print("√ 测试通过: 结算界面、展开的物品信息弹窗与连续出击弹窗完全精准区分！")
    finally:
        ScreenDetector.run_ocr = orig_run_ocr


if __name__ == "__main__":
    test_event_banner_not_combat()
    test_craft_essence_screen_detect()
    test_party_confirm_servant_recognition()
    test_highest_diff_fallback()
    test_battle_result_and_item_popup()
    print("\nAll new screen detector & party recognition tests PASSED successfully!")
