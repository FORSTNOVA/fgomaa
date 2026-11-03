"""
Unit tests for FGO Master Mission ILP Solver
"""

import sys
from pathlib import Path

# 将项目根目录添加到 sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.solver.mission_solver import MasterMissionSolver, MissionRequirement


def test_basic_solver():
    print("\n--- [Test 1] 结构化周常需求求解 ---")
    solver = MasterMissionSolver()

    # 模拟真实周常的 5 个典型任务要求
    requirements = [
        MissionRequirement(trait="earth", count=15, description="击败15个具有【地】属性的敌人"),
        MissionRequirement(trait="demonic", count=8, description="击败8个具有【魔性】特性的敌人"),
        MissionRequirement(trait="servant", count=3, description="击败3个【从者】"),
        MissionRequirement(trait="undead", count=6, description="击败6个具有【死灵】特性的敌人"),
        MissionRequirement(trait="drops", count=15, description="获得15个战利品"),
    ]

    result = solver.solve(requirements)
    assert result.success, f"Solver failed: {result.message}"
    assert result.total_ap > 0, "Total AP should be positive"
    assert len(result.plans) > 0, "Should generate at least one quest plan"

    print(f"求解状态: {result.message}")
    print(f"总消耗 AP: {result.total_ap}")
    print(f"总战斗场次: {result.total_runs}")
    print("\n规划详细关卡执行顺序:")
    for i, plan in enumerate(result.plans, 1):
        print(f"  {i}. [{plan.war}] {plan.spot} - {plan.name} (单次 {plan.ap_per_run} AP) × {plan.runs} 次 -> 小计 {plan.total_ap} AP")

    print("\n目标达成验证:")
    for req in requirements:
        achieved_count = result.achieved.get(req.trait, 0)
        print(f"  - {req.description} ({req.trait}): 目标 {req.count} | 实际达成 {achieved_count} -> {'[OK]' if achieved_count >= req.count else '[FAIL]'}")
        assert achieved_count >= req.count


def test_chinese_text_parsing():
    print("\n--- [Test 2] 中文自然语言 / OCR 任务解析求解 ---")
    solver = MasterMissionSolver()

    raw_texts = [
        "击败15个具有【地】属性的敌人",
        "击败8个具有【魔性】特性的敌人",
        "击败3个【从者】",
        "获得15个战利品",
        "完成任意关卡3次"
    ]

    parsed_reqs = []
    for text in raw_texts:
        req = solver.parse_chinese_mission(text)
        assert req is not None, f"Failed to parse text: {text}"
        parsed_reqs.append(req)
        print(f"  解析成功: '{text}' -> trait='{req.trait}', count={req.count}")

    result = solver.solve(parsed_reqs)
    assert result.success
    print(f"\n中文解析求解成功！最优总消耗 AP: {result.total_ap}")
    for plan in result.plans:
        print(f"  -> {plan.spot} × {plan.runs} 次 (消耗 {plan.total_ap} AP)")


if __name__ == "__main__":
    test_basic_solver()
    test_chinese_text_parsing()
    print("\nAll tests passed successfully!")
