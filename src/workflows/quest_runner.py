"""通用自动作战工作流：明确关卡队列、模拟预览、单关执行与运行记录。"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.path_helper import get_resource_path, get_root_dir
from src.core.atomic_io import replace_file
from src.core.controller import FGOController
from src.core.device_manager import DeviceManager
from src.strategy.smart_engine import SmartEngine
from src.strategy.quest_battle import QuestBattleExecutor


class QuestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    quest_id: str
    runs: int = Field(default=1, ge=1, le=100)


class QuestTarget(BaseModel):
    quest_id: str
    game_id: int
    phase: int
    war: str
    spot: str
    name: str
    kind: Literal['free', 'daily', 'event']
    runs: int
    ap_per_run: int | None = None


class QuestRunReport(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: str = 'pending'
    success: bool = False
    preview_only: bool = False
    device_serial: str = ''
    confirmed_clears: int = 0
    estimated_ap_spent: int = 0
    stations: list[dict] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    error: str = ''
    artifacts_dir: str = ''


def quest_catalog(parser):
    from src.strategy.quest_catalog import quest_bindings
    from src.strategy.chapter_navigation import supports_navigation
    from src.strategy.event_navigation import supports_navigation as supports_event,resolve_spot
    from time import time
    bindings = quest_bindings()
    free = {q['id']:q for q in json.loads(get_resource_path('data/free_quests_traits.json').read_text(encoding='utf-8'))}
    rows = []
    now = time()
    for key, entry in bindings.items():
        entry = resolve_spot(entry)
        kind = entry.get('kind') or ('daily' if key.startswith('daily_') else 'free')
        if 'kind' not in entry and kind == 'free' and key not in free:
            continue
        ap = entry.get('ap',free.get(key, {}).get('ap'))
        if kind == 'daily' and ap is None:
            cache = get_resource_path(f"data/laplace/public/CN/quest/{entry['id']}_{entry['phase']}.json")
            if cache.exists():
                ap = json.loads(cache.read_text(encoding='utf-8')).get('consume')
        reason = ''
        if not parser or not parser.servants:
            reason = '请先导入账号数据'
        elif entry.get('phase',0) < 1 or not entry.get('name'):
            reason = '关卡尚无完整阶段信息'
        elif entry.get('opened_at',0) > now or 0 < entry.get('closed_at',0) < now:
            reason = '关卡未开放或已经结束'
        elif kind not in ('free','daily','event'):
            reason = '该类型关卡尚未适配自动推关'
        elif entry.get('consume_type','ap') != 'ap':
            reason = '关卡需要特殊入场资源'
        elif kind in ('free','event') and any(label in entry.get('name','') for label in ('高难','高難')):
            reason = '高难关卡机制尚未适配'
        elif kind == 'event' and not supports_event(entry):
            reason = '该活动关卡尚未适配普通周回'
        elif kind in ('free','event') and not entry.get('spot','').strip('？? '):
            reason = '地点名称不明确，尚未适配导航'
        elif kind == 'free' and entry.get('war_id') is not None and not supports_navigation(entry):
            reason = '该章节的自动导航尚未适配'
        elif kind in ('free','event') and entry.get('after_clear','repeatLast') != 'repeatLast':
            reason = '该关卡不是普通可重复自由关卡'
        rows.append({'quest_id':key, 'game_id':entry['id'], 'phase':entry['phase'],
                     'war':entry['war'], 'spot':entry['spot'], 'name':entry['name'],
                     'recommend_lv':entry.get('recommend_lv',''),
                     'kind':kind, 'ap_per_run':ap, 'supported':not reason,
                     'reason':reason, 'source':entry.get('source',''), 'war_id':entry.get('war_id'),
                     'navigation_supported':(kind=='free' and supports_navigation(entry)) or (kind=='event' and supports_event(entry))})
    return rows


def resolve_queue(items, parser):
    if not isinstance(items, list) or not 1 <= len(items) <= 30:
        raise ValueError('请添加1至30个关卡')
    catalog = {q['quest_id']:q for q in quest_catalog(parser)}
    result = []
    for item in items:
        request = QuestRequest.model_validate(item)
        entry = catalog.get(request.quest_id)
        if entry is None or not entry['supported']:
            raise ValueError(f'关卡{request.quest_id}尚不支持自动执行，不能使用近似关卡代替')
        result.append(QuestTarget(**{k:v for k,v in entry.items() if k in QuestTarget.model_fields}, runs=request.runs))
    return result


class QuestRunner:
    def __init__(self, chaldea_parser, device_manager=None, log_callback=None, report_root=None):
        self.parser = chaldea_parser
        self.device_mgr = device_manager or DeviceManager()
        self.log_callback = log_callback or (lambda text:None)
        self.report_root = Path(report_root or (get_root_dir()/'.app_profile/quest_runs'))

    def run(self, items, *, device_serial='', preview_only=False, cancel_event=None, on_progress=None):
        report = QuestRunReport(preview_only=preview_only, device_serial=device_serial)
        folder = self.report_root/report.run_id
        folder.mkdir(parents=True, exist_ok=False)
        report.artifacts_dir = str(folder)
        ctrl = None
        engine = None
        active = None
        cancel_event = cancel_event or threading.Event()

        def save():
            content = report.model_dump(mode='json')
            temporary = folder/'report.tmp'
            temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
            replace_file(temporary,folder/'report.json')
            if on_progress:
                on_progress(content)

        def log(text):
            report.logs.append(text)
            self.log_callback(text)

        def collect_progress():
            if active is None or engine is None:
                return
            clears = engine.last_quest_clears
            if type(clears) is not int or not 0 <= clears <= active['target']['runs']:
                raise RuntimeError('执行器返回了无效的通关次数')
            active['clears'] = clears
            rec = engine.current_recommendation
            if rec is not None:
                active['recommendation'] = rec.model_dump(mode='json')
            report.confirmed_clears = sum(row['clears'] for row in report.stations)
            report.estimated_ap_spent = sum(row['clears'] * (row['target']['ap_per_run'] or 0) for row in report.stations)

        def stage(value):
            report.status = value
            if active is not None:
                active['status'] = value
                collect_progress()
            save()

        try:
            targets = resolve_queue(items, self.parser)
            report.stations = [{'target':t.model_dump(), 'status':'pending', 'clears':0} for t in targets]
            if cancel_event.is_set():
                raise RuntimeError('任务已取消')
            if not preview_only:
                devices = self.device_mgr.get_connected_devices(auto_connect=True)
                matches = [d for d in devices if d.serial == device_serial] if device_serial else devices
                if len(matches) != 1:
                    raise ValueError('请选择一个明确的在线设备')
                report.device_serial = matches[0].serial
                ctrl = FGOController(device_serial=matches[0].serial, adb_path=self.device_mgr.adb_path,
                                     cancel_event=cancel_event, log_callback=log)
            engine = SmartEngine(controller=ctrl, chaldea_parser=self.parser, cancel_event=cancel_event, log_callback=log)
            engine.quest_progress_callback = stage
            for index, target in enumerate(targets):
                active = report.stations[index]
                engine.last_quest_clears = 0
                engine.current_recommendation = None
                if cancel_event.is_set():
                    raise RuntimeError('任务已取消')
                log(f'[自动推关 {index+1}/{len(targets)}] {target.war} / {target.spot} / {target.name} × {target.runs}')
                ok = (QuestBattleExecutor(engine).prepare(target) if preview_only
                      else engine.run_quest(target, cancel_event=cancel_event))
                collect_progress()
                clears = engine.last_quest_clears
                if not ok or (not preview_only and clears != target.runs):
                    raise RuntimeError('本站模拟或执行未通过，已停止队列，后续关卡未执行')
                active['status'] = 'preview_passed' if preview_only else 'completed'
                save()
            report.success = not cancel_event.is_set()
            report.status = ('preview_passed' if preview_only else 'completed') if report.success else 'cancelled'
        except Exception as exc:
            try:
                collect_progress()
            except RuntimeError:
                pass
            report.status = 'cancelled' if cancel_event.is_set() else 'failed'
            report.error = str(exc)
            if active is not None:
                active['status'] = report.status
            log('[自动推关停止] ' + str(exc))
            if ctrl is not None:
                try:
                    ctrl.screencap(force_fresh=True).save(folder/'stopped.png')
                except Exception:
                    pass
        finally:
            save()
        return report


if __name__ == '__main__':
    import argparse
    import signal
    from src.parser.chaldea_parser import ChaldeaParser
    cli = argparse.ArgumentParser(description='按明确关卡队列模拟或自动作战')
    cli.add_argument('--account', type=Path, default=get_resource_path('data/user_account.json'))
    cli.add_argument('--queue', type=Path, required=True, help='JSON数组：[{"quest_id":"fuyuki_x_a","runs":1}]')
    cli.add_argument('--device', default='')
    cli.add_argument('--preview', action='store_true', help='仅模拟，不连接设备')
    args = cli.parse_args()
    parser = ChaldeaParser()
    parser.parse_file(args.account)
    cancellation = threading.Event()
    signal.signal(signal.SIGINT, lambda *_:cancellation.set())
    result = QuestRunner(parser, log_callback=print).run(json.loads(args.queue.read_text(encoding='utf-8')),
        device_serial=args.device, preview_only=args.preview, cancel_event=cancellation)
    print(result.status, result.error, result.artifacts_dir)
    raise SystemExit(0 if result.success else 1)
