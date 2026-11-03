"""
Unit tests for Weekly Mission Solver & Weekly Runner
测试：周常 OCR 解析、折行文本、进行度进度扣减、活动限定道具识别与 ILP 运筹学求解
"""

import unittest
from src.solver.mission_solver import MasterMissionSolver, MissionRequirement
from src.workflows.weekly_runner import WeeklyRunner, WeeklyTaskReport


class TestWeeklySolverAndRunner(unittest.TestCase):

    def setUp(self):
        self.solver = MasterMissionSolver()

    def test_parse_trait_missions_with_progress(self):
        # 1. 地之力，进度 3/15，剩余应为 12
        r1 = self.solver.parse_chinese_mission(
            "击败15个持有『地】之力的敌人（战斗中被召唤出来的敌人除外）",
            progress_cur=3,
            progress_total=15
        )
        self.assertIsNotNone(r1)
        self.assertEqual(r1.trait, "earth")
        self.assertEqual(r1.count, 12)
        self.assertFalse(r1.is_event)

        # 2. 龙特性，缺少"击"字 ("败15个")，进度 0/15
        r2 = self.solver.parse_chinese_mission(
            "败15个持有【龙】特性的敌人（战斗中被召唤出来的敌人除外）",
            progress_cur=0,
            progress_total=15
        )
        self.assertIsNotNone(r2)
        self.assertEqual(r2.trait, "dragon")
        self.assertEqual(r2.count, 15)
        self.assertFalse(r2.is_event)

        # 3. 超巨大特性，进度 0/5
        r3 = self.solver.parse_chinese_mission(
            "击败5个持有『超巨大】特性的敌人（战斗中被召唤出来的敌人除外）",
            progress_cur=0,
            progress_total=5
        )
        self.assertIsNotNone(r3)
        self.assertEqual(r3.trait, "super_giant")
        self.assertEqual(r3.count, 5)

    def test_parse_event_currency_missions(self):
        # 万圣节专属战利品 (死亡拨片)
        r_event = self.solver.parse_chinese_mission(
            "在『终结·万圣节2026』中重新累计获得战利品【死亡拨片】50个",
            progress_cur=10,
            progress_total=50
        )
        self.assertIsNotNone(r_event)
        self.assertTrue(r_event.is_event)
        self.assertEqual(r_event.count, 40)
        self.assertEqual(r_event.trait, "event_item")

    def test_meta_mission_skip(self):
        # 完成本周所有御主任务 -> 汇总元任务，应返回 None
        r_meta = self.solver.parse_chinese_mission(
            "完成本周所有的御主任务",
            progress_cur=0,
            progress_total=6
        )
        self.assertIsNone(r_meta)

    def test_solve_optimal_route(self):
        # 地之力:15, 龙:15, 超巨大:5
        reqs = [
            MissionRequirement(trait="earth", count=15),
            MissionRequirement(trait="dragon", count=15),
            MissionRequirement(trait="super_giant", count=5),
            MissionRequirement(trait="event_item", count=50, is_event=True, description="活动道具")
        ]
        res = self.solver.solve(reqs)
        self.assertFalse(res.success)
        self.assertEqual(len(res.event_missions), 1)
        # 现有关卡库无超巨大产出；原23AP结果实际上忽略了这项需求。
        res = self.solver.solve(reqs[:-1])
        self.assertFalse(res.success)
        self.assertIn("super_giant", res.message)
        res = self.solver.solve(reqs[:2])
        self.assertTrue(res.success)
        # 公开战斗样本校正后，拉沙里泰每场有 6 地之力、6 龙。
        self.assertEqual(res.total_ap, 21)
        self.assertEqual(res.total_runs, 3)
        self.assertEqual(len(res.plans), 1)
        self.assertEqual(res.plans[0].quest_id, "orleans_la_charite")
        self.assertEqual(len(res.event_missions), 0)

    def test_weekly_runner_dry_run(self):
        runner = WeeklyRunner(dry_run=True)
        report = runner.run(
            custom_mission_texts=[
                "击败15个持有『地】之力的敌人",
                "击败15个持有【龙】特性的敌人"
            ]
        )
        self.assertTrue(report.success)
        self.assertEqual(report.planned_ap, 21)
        self.assertEqual(report.planned_battles, 3)
        self.assertIsNone(report.total_ap_spent)
        self.assertEqual(report.total_battles, 0)
        self.assertFalse(report.rewards_claimed)

    def test_screen_detector_new_screens(self):
        from src.strategy.screen_detector import GameScreen
        self.assertEqual(GameScreen.MAIN_RECORD.value, "main_record")
        self.assertEqual(GameScreen.REGIONAL_MAP.value, "regional_map")


if __name__ == "__main__":
    unittest.main()
