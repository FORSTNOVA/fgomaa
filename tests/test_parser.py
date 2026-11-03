"""
Unit tests for Chaldea & FGO Account Data Parser
"""

import sys
from pathlib import Path

# 确保 Windows 终端 UTF-8 输出正常
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.parser.chaldea_parser import ChaldeaParser


def test_parser_with_sample():
    print("\n--- [Test 1] 迦勒底 JSON 样本解析测试 ---")
    sample_file = root_dir / "tests" / "sample_chaldea.json"
    parser = ChaldeaParser()
    servants = parser.parse_file(sample_file)

    assert len(servants) >= 5, f"Expected at least 5 servants, got {len(servants)}"

    # 1. 测试按关键词检索核心打手与拐
    morgan = parser.get_by_name("摩根")
    assert morgan is not None, "Should find Morgan"
    assert morgan.lv == 100
    assert morgan.np_lv == 5
    assert morgan.is_all_skills_max(), "Morgan skills should be 10/10/10"
    assert morgan.has_max_append_np(), "Morgan should have max append skill 2"
    assert morgan.get_append_np_charge() == 20

    oberon = parser.get_by_name("奥伯龙")
    assert oberon is not None
    assert oberon.has_max_append_np()

    castoria = parser.get_by_name("C呆")
    assert castoria is not None
    assert castoria.is_all_skills_max()

    print("从者练度属性断言测试全部通过！")
    print("\n" + parser.summary())


def test_parser_with_raw_capture_format():
    print("\n--- [Test 2] 官方抓包数据格式兼容性测试 ---")
    parser = ChaldeaParser()
    raw_capture_data = {
        "userServant": [
            {
                "svtId": 501900,  # 诸葛孔明
                "lv": 90,
                "npLv": 1,
                "skillLv1": 10,
                "skillLv2": 10,
                "skillLv3": 10,
                "appendSkillLv2": 10
            }
        ]
    }
    servants = parser.parse_data(raw_capture_data)
    assert 501900 in servants
    zhuge = servants[501900]
    assert "诸葛孔明" in zhuge.name
    assert zhuge.is_all_skills_max()
    assert zhuge.get_append_np_charge() == 20
    print(f"抓包格式解析成功: {zhuge.name} (Lv.{zhuge.lv}, 技能 {zhuge.skills}, 被动充能 {zhuge.get_append_np_charge()}%)")


if __name__ == "__main__":
    test_parser_with_sample()
    test_parser_with_raw_capture_format()
    print("\nAll parser tests passed successfully!")
