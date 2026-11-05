"""独立的选关模拟与作战工作流；默认仅模拟，资源限制与自动首通共用。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.core.controller import FGOController
from src.core.device_manager import DeviceManager
from src.core.path_helper import get_root_dir
from src.core.atomic_io import replace_file
from src.strategy.ap_recovery import AppleRecovery, AppleLimitReached
from src.strategy.quest_battle import QuestBattleExecutor
from src.strategy.quest_catalog import quest_bindings
from src.strategy.quest_resources import inspect_battle_quest
from src.strategy.smart_engine import SmartEngine
from src.workflows.quest_runner import QuestRunReport, QuestTarget, quest_catalog
from src.workflows.resource_options import ResourceOptions


class BattleOptions(ResourceOptions):
    max_quests: int | None = Field(default=1, ge=1, le=30)


class BattleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    quest_id: str
    auto_battle: bool = False
    device_serial: str = ''
    options: BattleOptions = Field(default_factory=BattleOptions)


class BattleTarget(QuestTarget):
    single_submission: bool = True


class BattleReport(QuestRunReport):
    mode: str = 'battle_lab'
    options: dict = Field(default_factory=dict)
    apples_used: int = 0
    apple_attempts: int = 0
    apples_committed: int = 0
    apple_refills: list[dict] = Field(default_factory=list)


def simulation_catalog(parser):
    """模拟无需实机导航和通关记录；执行仍保留原目录的全部限制。"""
    entries = quest_bindings()
    rows = []
    for row in quest_catalog(parser):
        entry = entries[row['quest_id']]
        reason = ''
        if not parser or not parser.servants:
            reason = '请先导入账号数据'
        elif row['kind'] not in ('free', 'daily', 'event'):
            reason = '此类型的战斗流程尚未适配'
        elif entry.get('consume_type', 'ap') != 'ap' or not row['name'] or row['phase'] < 1:
            reason = '缺少完整的普通关卡阶段信息'
        elif entry.get('after_clear', 'repeatLast') != 'repeatLast' or any(t in row['name'] for t in ('高难', '高難')):
            reason = '一次性或高难关卡机制尚未适配'
        elif row['kind'] == 'event':
            from src.strategy.event_navigation import supports_navigation
            if not supports_navigation(entry):
                reason = '该活动战斗尚未适配'
        rows.append({**row, 'simulation_supported':not reason, 'simulation_reason':reason})
    return rows


def resolve_battle_request(request, parser):
    request = request if isinstance(request, BattleRequest) else BattleRequest.model_validate(request)
    row = next((row for row in simulation_catalog(parser) if row['quest_id'] == request.quest_id), None)
    if row is None or not row['simulation_supported']:
        raise ValueError(row['simulation_reason'] if row else '没有准确的关卡 ID 绑定')
    if request.auto_battle:
        if not request.device_serial:
            raise ValueError('自动跳转战斗需要明确选择在线设备')
        if not row['supported']:
            raise ValueError('此关卡仅可模拟：' + row['reason'])
    target = BattleTarget(**{key:value for key,value in row.items() if key in QuestTarget.model_fields}, runs=1)
    return request, target


class ResourceLimitReached(RuntimeError):
    pass


class BattleRunner:
    def __init__(self, parser, device_manager=None, log_callback=None, report_root=None):
        self.parser = parser
        self.device_mgr = device_manager or DeviceManager()
        self.log_callback = log_callback or (lambda text:None)
        self.report_root = Path(report_root or get_root_dir()/'.app_profile/quest_runs')

    def run(self, request, *, cancel_event=None, on_progress=None):
        request, target = resolve_battle_request(request, self.parser)
        options = request.options
        report = BattleReport(preview_only=not request.auto_battle, device_serial=request.device_serial,
                              options=options.model_dump())
        folder = self.report_root/report.run_id
        folder.mkdir(parents=True, exist_ok=False)
        report.artifacts_dir = str(folder)
        cancel_event = cancel_event or threading.Event()
        ctrl = engine = active = None

        def cancelled():
            if cancel_event.is_set():
                raise RuntimeError('任务已取消')

        def save():
            if active is not None and engine is not None and engine.current_recommendation is not None:
                active['recommendation'] = engine.current_recommendation.model_dump(mode='json')
            data = report.model_dump(mode='json')
            temporary = folder/'report.tmp'
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            replace_file(temporary,folder/'report.json')
            if on_progress:
                on_progress(data)

        def log(text):
            report.logs.append(text)
            self.log_callback(text)

        def stage(value):
            report.status = value
            if active is not None:
                active['status'] = value
            save()

        def observe(plan):
            cancelled()
            value = inspect_battle_quest(engine, plan)
            cost, available = value['card_ap'], value['available_ap']
            if value['quest_id'] != plan.game_id or type(cost) is not int or not 0 <= cost <= nominal_ap:
                raise RuntimeError('目标关卡或实际 AP 消耗校验失败')
            if type(available) is not int or available < 0:
                raise RuntimeError('未确认当前实际体力，停止出击')
            if options.ap_budget is not None and report.estimated_ap_spent+cost > options.ap_budget:
                raise ResourceLimitReached('达到 AP 预算，停止出击')
            active['observation'] = value
            return value

        def resources(plan):
            if active.get('ap_committed'):
                raise RuntimeError('本场已提交出击，未确认结果前不能再次入场')
            stage('observing')
            before = observe(plan)
            cost = before['card_ap']
            while before['available_ap'] < cost:
                cancelled()
                if options.refill_apple == 'none' or (options.max_apples is not None and report.apples_committed >= options.max_apples):
                    raise ResourceLimitReached('当前体力不足，果实恢复未启用或已达到使用上限')
                # 模拟已通过；沿用首通的恢复器、数量预约和恢复后实机复核。
                stage('refilling')
                def submit(event):
                    cancelled()
                    count = event.get('count')
                    if type(count) is not int or count < 1 or (options.max_apples is not None and report.apples_committed+count > options.max_apples):
                        raise RuntimeError('果实使用数量超过本次上限')
                    report.apple_attempts += 1
                    report.apples_committed += count
                    report.apple_refills.append({**event, 'quest_id':plan.game_id, 'phase':plan.phase})
                    save()
                recovery = AppleRecovery(ctrl, log_callback=log, cancel_event=cancel_event,
                                         report_dir=folder/'apple_recovery')
                restored = recovery.restore_once(options.refill_apple, cost, submit,
                    max_count=options.max_apples-report.apples_committed if options.max_apples is not None else 999)
                if restored is not None:
                    report.apple_refills[-1].update(restored)
                    report.apples_used += restored['count']
                    save()
                before = observe(plan)
                if before['card_ap'] != cost or (restored is None and before['available_ap'] < cost):
                    raise RuntimeError('恢复体力后关卡消耗或实际体力复核失败')
                if restored is not None and before['available_ap'] < restored['after_ap']:
                    raise RuntimeError('恢复体力后未确认预期的实际 AP')
            active['actual_ap'] = cost
            engine.current_recommendation.laplace['quest_ap'] = cost
            stage('navigating')
            return True

        def repeat_resources(plan, cost, available):
            cancelled()
            previous = report.stations[-2]
            if (previous['clears'] != 1 or cost != previous.get('actual_ap')
                    or available < cost or active.get('ap_committed')):
                return False
            if options.ap_budget is not None and report.estimated_ap_spent + cost > options.ap_budget:
                return False
            active['observation'] = {'quest_id': plan.game_id, 'phase': plan.phase,
                                     'card_ap': cost, 'available_ap': available, 'source': 'continue_panel'}
            active['actual_ap'] = cost
            stage('observing')
            return True

        def before_start(plan):
            cancelled()
            if active.get('ap_committed') or 'actual_ap' not in active:
                raise RuntimeError('未通过资源核对或本场已经提交过出击')
            # 在点击前预约消耗；未知点击结果也不释放预算，禁止重复花费。
            active['ap_committed'] = True
            report.estimated_ap_spent += active['actual_ap']
            save()

        def collect():
            if engine is None or active is None:
                return
            count = engine.last_quest_clears
            if type(count) is not int or count not in (0, 1):
                raise RuntimeError('执行器返回无效的通关场次')
            active['clears'] = count
            report.confirmed_clears = sum(row['clears'] for row in report.stations)

        try:
            cancelled()
            engine = SmartEngine(None, chaldea_parser=self.parser, cancel_event=cancel_event, log_callback=log)
            engine.quest_progress_callback = stage
            engine.first_clear_observation_dir = folder/'observations'
            executor = QuestBattleExecutor(engine)
            active = {'target':target.model_dump(), 'status':'pending', 'clears':0}
            report.stations.append(active)
            if not executor.prepare(target):
                raise RuntimeError('关卡模拟未通过，停止任务')
            cancelled()
            if not request.auto_battle:
                report.success = True
                stage('preview_passed')
                return report
            nominal_ap = engine.current_recommendation.laplace.get('quest_ap')
            if type(nominal_ap) is not int or nominal_ap < 0:
                raise RuntimeError('模拟缺少明确的关卡 AP 数据')
            devices = self.device_mgr.get_connected_devices(auto_connect=True)
            if len([d for d in devices if d.serial == request.device_serial]) != 1:
                raise ValueError('所选设备不在线或不唯一')
            ctrl = FGOController(device_serial=request.device_serial, adb_path=self.device_mgr.adb_path,
                                 cancel_event=cancel_event, log_callback=log)
            engine.ctrl = ctrl
            engine.quest_resource_check = resources
            engine.quest_start_callback = before_start
            while options.max_quests is None or report.confirmed_clears < options.max_quests:
                cancelled()
                engine.last_quest_clears = 0
                engine.last_quest_started = False
                repeat_next = options.max_quests is None or report.confirmed_clears + 1 < options.max_quests
                ok = executor.run(target, cancel_event=cancel_event, prepared=True,
                                  repeat_next=repeat_next, repeat_resources=repeat_resources)
                collect()
                if not ok or active['clears'] != 1:
                    raise RuntimeError('编队、礼装、战斗或结算未通过，停止后续出击')
                active['status'] = 'completed'
                save()
                cancelled()
                if options.max_quests is not None and report.confirmed_clears >= options.max_quests:
                    break
                active = {'target':target.model_dump(), 'status':'pending', 'clears':0}
                report.stations.append(active)
                engine.last_quest_clears = 0
            report.success = True
            stage('completed')
        except Exception as exc:
            if report.apple_refills and report.apple_refills[-1]['status'] == 'pending':
                report.apple_refills[-1].update(status='unverified', error=str(exc))
            try:
                collect()
            except RuntimeError:
                pass
            status = 'cancelled' if cancel_event.is_set() else 'ap_limit_reached' if isinstance(exc, (ResourceLimitReached, AppleLimitReached)) else 'failed'
            report.error = str(exc)
            stage(status)
            log('[关卡模拟与作战停止] ' + str(exc))
            if ctrl is not None:
                try:
                    ctrl.screencap(force_fresh=True).save(folder/'stopped.png')
                except Exception:
                    pass
        finally:
            save()
        return report
