"""
Unit tests for 3T Battle Strategy & Party Detector
"""

import sys
from pathlib import Path

# 确保 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.parser.chaldea_parser import ChaldeaParser
from src.strategy.battle_plans import BattlePlanManager
from src.strategy.team_detector import TeamDetector


def test_strategy_matching_and_execution():
    print("\n--- [Test 1] 编队检测与 3T 策略自动匹配 ---")
    plan_mgr = BattlePlanManager()
    assert len(plan_mgr.teams) >= 3, "Should load at least 3 default team strategies"

    # 加载前面测试准备的 Chaldea 档案
    sample_chaldea = root_dir / "tests" / "sample_chaldea.json"
    parser = ChaldeaParser()
    parser.parse_file(sample_chaldea)

    detector = TeamDetector(plan_manager=plan_mgr, chaldea_parser=parser)

    # 模拟在出击界面识别到了 [摩根, 杀狐, 杀狐(助战)]
    detected_slots = ["摩根", "光之高扬斯卡娅", "光之高扬斯卡娅"]
    party, strategy, logs = detector.identify_party_and_plan(detected_slots)

    for line in logs:
        print(f"  {line}")

    assert strategy is not None, "Should match Morgan 3T Strategy"
    assert strategy.id == "buster_morgan_koyan"

    print("\n--- [Test 2] 3T 战斗回合指令流输出验证 ---")
    for turn in [1, 2, 3]:
        steps = plan_mgr.format_turn_instructions(strategy, turn)
        print(f"\n[回合 {turn} 执行步骤清单]")
        for s_idx, step in enumerate(steps, 1):
            print(f"   Step {s_idx}: {step}")

    assert len(strategy.turns[3].master_skills) > 0, "Turn 3 should have Master Order Change"
    assert strategy.turns[3].master_skills[0].swap == [2, 4], "Should swap 2 with 4 (Oberon)"


def test_castoria_arts_matching():
    print("\n--- [Test 3] 蓝卡 C 呆队伍匹配测试 ---")
    plan_mgr = BattlePlanManager()
    detector = TeamDetector(plan_manager=plan_mgr)

    detected_slots = ["太空伊什塔尔", "阿尔托莉雅·Caster", "阿尔托莉雅·Caster"]
    party, strategy, logs = detector.identify_party_and_plan(detected_slots)

    for line in logs:
        print(f"  {line}")

    assert strategy is not None
    assert strategy.id == "arts_castoria_system"
    print("蓝卡队匹配成功！")


if __name__ == "__main__":
    test_strategy_matching_and_execution()
    test_castoria_arts_matching()
    print("\nAll strategy and battle plan tests passed successfully!")
