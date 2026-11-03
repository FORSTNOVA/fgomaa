"""
Weekly Workflow Runner - 周常自动化智能规划与执行流 (真机实机感知闭环版)
实现：读取当周任务 -> ILP 求解最低 AP 路线 -> 实机响应式视觉感知刷本 -> 实机一键领取全部周常圣晶石碎片。
杜绝任何屏幕盲点或动作与代码不匹配的问题！
"""

from __future__ import annotations
import time
import threading
from typing import Optional, List, Dict, Any
from pydantic import BaseModel

from src.core.device_manager import DeviceManager
from src.core.controller import FGOController
from src.strategy.screen_detector import ScreenDetector, GameScreen
from src.strategy.smart_engine import SmartEngine
from src.solver.mission_solver import MasterMissionSolver, MissionRequirement, SolverResult, QuestPlan
from src.strategy.team_detector import TeamDetector
from src.strategy.battle_plans import BattlePlanManager
from src.parser.chaldea_parser import ChaldeaParser


class WeeklyTaskReport(BaseModel):
    success: bool
    device_serial: str
    total_ap_spent: Optional[int] = None
    estimated_ap_spent: int = 0
    planned_ap: int = 0
    planned_battles: int = 0
    dry_run: bool = False
    progress_verified: bool = False
    all_missions_complete: bool = False
    pending_missions: List[str] = []
    total_battles: int = 0
    plan_summary: List[str] = []
    rewards_claimed: bool = False
    logs: List[str] = []


class WeeklyRunner:
    """周常智能规划与执行器 (真机闭环驱动版)"""

    def __init__(
        self,
        device_manager: Optional[DeviceManager] = None,
        chaldea_parser: Optional[ChaldeaParser] = None,
        dry_run: bool = False,
    ):
        self.device_mgr = device_manager or DeviceManager()
        self.chaldea_parser = chaldea_parser
        self.dry_run = dry_run
        self.solver = MasterMissionSolver()
        self.plan_mgr = BattlePlanManager()

    def log(self, report: WeeklyTaskReport, message: str) -> None:
        report.logs.append(message)
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    def run(
        self,
        custom_mission_texts: Optional[List[str]] = None,
        plans: Optional[List[Any]] = None,
        cancel_event: Optional[threading.Event] = None
    ) -> WeeklyTaskReport:
        """执行完整周常闭环 (真机驱动，完全消除界面盲点)"""
        report = WeeklyTaskReport(success=False, device_serial="None", dry_run=self.dry_run)

        def stop(message):
            self.log(report, "[周常停止] " + message)
            return report

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        if cancelled():
            return stop("任务已取消")
        try:
            requirements = self.solver.parse_missions(custom_mission_texts or [])
        except (ValueError, TypeError) as exc:
            return stop(str(exc))

        if self.dry_run:
            report.device_serial = "DryRun"
            if not requirements:
                return stop("离线预览需要明确的任务文本，不连接设备，也不模拟已完成或已领奖。")
            result = self.solver.solve(requirements)
            report.planned_ap = result.total_ap
            report.planned_battles = result.total_runs
            report.plan_summary = [f"[{p.war}] {p.spot} - {p.name} × {p.runs}" for p in result.plans]
            report.pending_missions = result.event_missions
            report.success = result.success
            self.log(report, "[离线规划] " + result.message)
            return report

        devices = self.device_mgr.get_connected_devices(auto_connect=True)
        if not devices:
            return stop("未检测到在线设备")
        device = devices[0]
        report.device_serial = device.serial
        ctrl = FGOController(device_serial=device.serial, adb_path=self.device_mgr.adb_path,
                             log_callback=lambda msg: self.log(report, msg), cancel_event=cancel_event)
        engine = SmartEngine(controller=ctrl, log_callback=lambda msg: self.log(report, msg),
                             cancel_event=cancel_event, chaldea_parser=self.chaldea_parser)
        # 无论是否有缓存或外部规划，都先重新核对本周任务及实际剩余进度。
        fresh = engine.auto_scan_and_solve_weekly()
        if not fresh.missions_verified or not fresh.success:
            report.pending_missions = fresh.event_missions
            return stop(fresh.message)
        if cancelled():
            return stop("任务已取消")
        if requirements and any(r.trait not in {f.trait for f in fresh.requirements} for r in requirements):
            return stop("指定任务与实机本周目标不一致，请刷新任务。")
        quest_plans = fresh.plans
        if plans and not fresh.all_completed:
            try:
                quest_plans = self.validate_plans(plans, fresh.requirements)
            except (ValueError, TypeError) as exc:
                return stop(str(exc))
        report.planned_ap = sum(p.total_ap for p in quest_plans)
        report.planned_battles = sum(p.runs for p in quest_plans)
        report.plan_summary = [f"[{p.war}] {p.spot} - {p.name} × {p.runs}" for p in quest_plans]
        for plan in quest_plans:
            if cancelled():
                return stop("任务已取消")
            station_ok = engine.run_reactive_weekly_plan(plan, cancel_event=cancel_event)
            clears = engine.last_weekly_clears
            if type(clears) is not int or not 0 <= clears <= plan.runs:
                return stop("执行场次报告无效")
            report.total_battles += clears
            report.estimated_ap_spent += clears * plan.ap_per_run
            if not station_ok or clears != plan.runs:
                return stop(f"[{plan.spot}] 未完成或未能确认退出结算，不进入下一站。")
        if cancelled():
            return stop("任务已取消")
        verified = engine.auto_scan_and_solve_weekly()
        report.progress_verified = verified.missions_verified
        report.all_missions_complete = verified.success and verified.missions_verified and verified.all_completed
        report.pending_missions = verified.event_missions
        if not report.all_missions_complete:
            return stop("刷本后重新核验未通过，不能报告周常完成：" + verified.message)
        if cancelled():
            return stop("任务已取消")
        report.rewards_claimed = bool(engine.claim_weekly_rewards())
        if not report.rewards_claimed or cancelled():
            report.rewards_claimed = False
            return stop("任务进度已核验，但尚未确认全部奖励领取成功。")
        report.success = True
        self.log(report, f"[周常完成] 已复核任务和领奖；本次确认通关 {report.total_battles} 场，"
                         f"对应标称 AP 估算 {report.estimated_ap_spent}，实际 AP 消耗未测量。")
        return report

    def validate_plans(self, plans, requirements):
        """将外部规划匹配到本地关卡，拒绝未知关卡、伪造AP或漏覆盖需求。"""
        canonical = []
        for plan in plans:
            data = plan.model_dump() if isinstance(plan, QuestPlan) else (plan if isinstance(plan, dict) else vars(plan))
            runs = data.get("runs", 0)
            if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= 50:
                raise ValueError("规划场次必须为 1 至 50 的整数")
            matches = [q for q in self.solver.quests if
                       (data.get("quest_id") and q.id == data["quest_id"]) or
                       (not data.get("quest_id") and q.war == data.get("war") and q.spot == data.get("spot") and q.name == data.get("name"))]
            if len(matches) != 1:
                raise ValueError("规划包含未知或不明确的关卡，请刷新规划。")
            q = matches[0]
            if any(data.get(key, value) != value for key, value in
                   (("war", q.war), ("spot", q.spot), ("name", q.name), ("ap_per_run", q.ap), ("total_ap", q.ap * runs))):
                raise ValueError("规划关卡身份或 AP 数据与本地记录不符，请刷新规划。")
            canonical.append(QuestPlan(quest_id=q.id, war=q.war, spot=q.spot, name=q.name,
                                       ap_per_run=q.ap, runs=runs, total_ap=q.ap * runs))
        for req in requirements:
            if req.count <= 0:
                continue
            achieved = sum(next(q for q in self.solver.quests if q.id == p.quest_id).objective_count(req.trait) * p.runs for p in canonical)
            if req.is_event or achieved < req.count:
                raise ValueError("规划没有覆盖全部剩余任务需求，请重新求解。")
        return canonical
