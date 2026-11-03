"""
Daily Workflow Runner - 日常自动化任务执行流 (完全接入闭环感知状态机)
核心理念：Sense -> Think -> Act (先识别当前界面状态 -> 智能研判任务是否需执行 -> 闭环执行)
"""

from __future__ import annotations
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from pydantic import BaseModel

from src.core.device_manager import DeviceManager
from src.core.controller import FGOController
from src.strategy.screen_detector import ScreenDetector, GameScreen
from src.strategy.smart_engine import SmartEngine
from src.strategy.battle_plans import BattlePlanManager
from src.parser.chaldea_parser import ChaldeaParser


class DailyTaskReport(BaseModel):
    success: bool
    device_serial: str
    story_summon_completed: bool = False
    fp_summon_completed: bool = False
    quest_clears: int = 0
    matched_team_strategy: Optional[str] = None
    master_mission_claimed: bool = False
    logs: List[str] = []


class DailyRunner:
    """日常自动化工作流执行器 (智能感知驱动版)"""

    def __init__(
        self,
        device_manager: Optional[DeviceManager] = None,
        chaldea_parser: Optional[ChaldeaParser] = None,
        dry_run: bool = False,
    ):
        self.device_mgr = device_manager or DeviceManager()
        self.chaldea_parser = chaldea_parser
        self.dry_run = dry_run
        self.plan_mgr = BattlePlanManager()

    def log(self, report: DailyTaskReport, message: str) -> None:
        report.logs.append(message)
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)
        except Exception:
            try:
                safe_msg = message.encode("gbk", "ignore").decode("gbk")
                print(f"[{time.strftime('%H:%M:%S')}] {safe_msg}", flush=True)
            except Exception:
                pass

    def run(
        self,
        target_quest: str = "daily_qp_extreme",
        repeat_runs: int = 3,
        options: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[Any] = None
    ) -> DailyTaskReport:
        """执行完整日常任务闭环 (感知决策驱动，支持勾选过滤与实时中断)"""
        opts = options or {}
        enable_story_summon = opts.get("enable_story_summon", True)
        enable_fp = opts.get("enable_fp", True)
        enable_quest = opts.get("enable_quest", True)
        enable_reward = opts.get("enable_reward", True)

        def check_cancelled() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                self.log(report, "[任务终止] 检测到用户取消或停止运行信号，立即中断流程！")
                report.success = False
                return True
            return False

        # 1. 确保设备连接
        devices = self.device_mgr.get_connected_devices(auto_connect=True)
        if not devices:
            return DailyTaskReport(
                success=False,
                device_serial="None",
                logs=["[错误] 未检测到任何在线设备，请先启动模拟器！"]
            )

        active_device = devices[0]
        report = DailyTaskReport(success=True, device_serial=active_device.serial)
        self.log(report, f"已连接目标设备: {active_device.serial} ({active_device.model})")

        # 初始化控制器与智能感知引擎
        ctrl = FGOController(
            device_serial=active_device.serial,
            adb_path=self.device_mgr.adb_path,
            log_callback=lambda msg: self.log(report, msg),
            cancel_event=cancel_event
        )
        engine = SmartEngine(
            controller=ctrl,
            log_callback=lambda msg: self.log(report, msg),
            cancel_event=cancel_event,
            chaldea_parser=self.chaldea_parser
        )

        if check_cancelled():
            return report

        # 启动每步即时感知的响应式日常总调度器
        self.log(report, ">>> [响应式状态机] 启动逐步视觉闭环驱动...")
        if not self.dry_run:
            success = engine.run_reactive_daily(
                options=options,
                target_quest=target_quest,
                repeat_runs=repeat_runs
            )
            report.success = success
            progress = getattr(engine, "daily_progress", {})
            if isinstance(progress, dict):
                for field in ("story_summon_completed", "fp_summon_completed", "quest_clears", "master_mission_claimed"):
                    if field in progress:
                        setattr(report, field, progress[field])
            if engine.active_strategy:
                report.matched_team_strategy = engine.active_strategy.name
            elif isinstance(getattr(engine, "last_daily_strategy", None), str):
                report.matched_team_strategy = engine.last_daily_strategy
        else:
            report.success = True

        if check_cancelled():
            return report

        if report.success:
            self.log(report, ">>> [完成] 全部日常任务闭环执行完毕，状态感知与操作流完全闭合！")
        else:
            self.log(report, ">>> [未完成] 日常流程已停止，请检查最后的识别或操作失败日志。")
        return report
