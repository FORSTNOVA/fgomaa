"""
FGO MAA Assistant - 主控制台入口
集设备自发现、Chaldea 档案透视、智能周常 ILP 规划求解、3T 战斗与日常周常闭环于一体。
"""

import sys
import time
from pathlib import Path

# 确保 Windows 终端 UTF-8 输出正常
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir))

from src.core.device_manager import DeviceManager
from src.parser.chaldea_parser import ChaldeaParser
from src.solver.mission_solver import MasterMissionSolver
from src.strategy.battle_plans import BattlePlanManager
from src.workflows.daily_runner import DailyRunner
from src.workflows.weekly_runner import WeeklyRunner


def print_banner():
    banner = r"""
========================================================================
       ______ _____  ____     __  __             
      |  ____/ ____|/ __ \   |  \/  |   /\     /\ 
      | |__ | |  __| |  | |  | \  / |  /  \   /  \ 
      |  __|| | |_ | |  | |  | |\/| | / /\ \ / /\ \ 
      | |   | |__| | |__| |  | |  | |/ ____ / ____ \ 
      |_|    \_____|\____/   |_|  |_/_/    /_/    \_\
           FGO 自动化助手 (基于 MaaFramework 架构 & 运筹学 ILP 求解器)
========================================================================
"""
    print(banner)


def show_menu():
    print("""
  [1] 扫描并连接运行中的设备 / 模拟器 (MuMu 12 / 雷电 / 夜神 / 真机)
  [2] 导入并透视 Chaldea (迦勒底) 账户档案与从者练度
  [3] 智能周常最少 AP 关卡规划器 (输入中文任务或使用经典预设)
  [4] 运行日常自动化全流程 (签到 + 免费十连 + 刷本3次 + 领绿方块)
  [5] 运行周常自动化全流程 (智能路线计算 + 自动导航打本 + 领石头)
  [6] 查看当前配置的 3T 战斗队伍预设 (红卡摩根/狂娜/蓝卡C呆)
  [0] 退出程序
""")


def main():
    print_banner()

    device_mgr = DeviceManager()
    chaldea_parser = ChaldeaParser()
    sample_file = root_dir / "tests" / "sample_chaldea.json"
    if sample_file.exists():
        chaldea_parser.parse_file(sample_file)

    while True:
        show_menu()
        choice = input("请选择功能编号 [0-6]: ").strip()

        if choice == "1":
            print("\n正在快速扫描在线设备与端口...")
            devs = device_mgr.get_connected_devices(auto_connect=True)
            if not devs:
                print("× 未检测到任何在线设备，请先启动模拟器（如 MuMu 12）！")
            else:
                print(f"√ 成功识别到 {len(devs)} 个设备连接:")
                for i, d in enumerate(devs, 1):
                    print(f"   [{i}] 地址: {d.serial} | 型号: {d.model} | 分辨率: {d.resolution} | 状态: {d.state}")

        elif choice == "2":
            print("\n" + chaldea_parser.summary())

        elif choice == "3":
            solver = MasterMissionSolver()
            print("\n【智能周常求解器】")
            print("请选择:")
            print("  A. 使用本周经典 6 项御主任务预设 (地/魔性/从者/死灵/战利品/关卡数)")
            print("  B. 手动输入当前任务文字")
            sub_choice = input("选择 [A/B, 默认A]: ").strip().upper() or "A"

            texts = []
            if sub_choice == "B":
                print("请输入任务文本(每行一条，输入空行结束):")
                while True:
                    line = input("> ").strip()
                    if not line:
                        break
                    texts.append(line)
            else:
                texts = [
                    "击败15个具有【地】属性的敌人",
                    "击败8个具有【魔性】特性的敌人",
                    "击败3个【从者】",
                    "击败6个具有【死灵】特性的敌人",
                    "获得15个战利品",
                    "完成任意关卡3次"
                ]

            try:
                reqs = solver.parse_missions(texts)
            except ValueError as exc:
                print(f"× 规划失败: {exc}")
                continue
            res = solver.solve(reqs)
            if res.success:
                print(f"\n√ 规划完成！总耗费 AP: {res.total_ap}，总战斗场次: {res.total_runs} 场")
                print("最优执行路线:")
                for idx, plan in enumerate(res.plans, 1):
                    print(f"  {idx}. [{plan.war}] {plan.spot} - {plan.name} (单次 {plan.ap_per_run} AP) × {plan.runs} 次 -> 小计 {plan.total_ap} AP")
            else:
                print(f"× 规划失败: {res.message}")

        elif choice == "4":
            print("\n>>> 开始执行日常自动化流程...")
            runner = DailyRunner(device_manager=device_mgr, chaldea_parser=chaldea_parser, dry_run=False)
            report = runner.run(target_quest="daily_qp_extreme", repeat_runs=3)
            print(f"\n日常任务执行结果: {'成功' if report.success else '失败'}")

        elif choice == "5":
            print("\n>>> 开始执行智能周常自动化流程...")
            runner = WeeklyRunner(device_manager=device_mgr, chaldea_parser=chaldea_parser, dry_run=False)
            report = runner.run()
            print(f"\n周常任务执行结果: {'成功' if report.success else '失败'}；"
                  f"计划 {report.planned_battles} 场 / {report.planned_ap} AP，"
                  f"确认通关 {report.total_battles} 场，标称 AP 估算 {report.estimated_ap_spent}（实际消耗未测量）")

        elif choice == "6":
            plan_mgr = BattlePlanManager()
            print(f"\n当前已加载 {len(plan_mgr.teams)} 个 3T 战斗方案预设:")
            for t_id, team in plan_mgr.teams.items():
                print(f"\n  * 【{team.name}】(ID: {team.id})")
                print(f"    - 匹配从者: {', '.join(team.match_servants)}")
                print(f"    - 最低被动充能要求: {team.required_min_np_charge}%")
                for turn in [1, 2, 3]:
                    steps = plan_mgr.format_turn_instructions(team, turn)
                    print(f"    - 回合 {turn}: {steps[0] if steps else '普通攻击'}")

        elif choice == "0":
            print("\n感谢使用 FGO MAA 自动化助手，再见！")
            break
        else:
            print("无效输入，请重新选择。")

        input("\n按回车键返回主菜单...")


if __name__ == "__main__":
    main()
