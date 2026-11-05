"""自由关卡自动首通：自动选关、实机确认阶段、逐阶段模拟与战斗、持久化进度。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from pydantic import Field,field_validator

from src.core.path_helper import get_root_dir
from src.core.atomic_io import replace_file
from src.core.controller import FGOController
from src.core.device_manager import DeviceManager
from src.strategy.smart_engine import SmartEngine
from src.strategy.ap_recovery import AppleRecovery,AppleLimitReached
from src.strategy.quest_battle import QuestBattleExecutor
from src.strategy.first_clear_progress import inspect_free_quest
from src.strategy.first_clear_navigation import FirstClearNavigator
from src.strategy.quest_catalog import quest_bindings,quest_binding
from src.strategy.chapter_navigation import NAVIGATION_WARS
from src.strategy.event_navigation import EVENT_WARS,supports_navigation as supports_event
from src.workflows.resource_options import ResourceOptions
from src.workflows.quest_runner import QuestRunReport,QuestTarget,quest_catalog


FIRST_CLEAR_WARS=NAVIGATION_WARS | EVENT_WARS


class FirstClearOptions(ResourceOptions):
    max_quests:int | None=Field(default=3,ge=1)
    war_ids:list[int]=Field(default_factory=list,max_length=len(FIRST_CLEAR_WARS))

    @field_validator('war_ids')
    @classmethod
    def supported_wars(cls,value):
        if len(set(value))!=len(value) or not set(value)<=FIRST_CLEAR_WARS:
            raise ValueError('仅支持已适配的普通自由关卡章节')
        return value


class FirstClearTarget(QuestTarget):
    first_clear:bool=True


class FirstClearReport(QuestRunReport):
    mode:str='first_clear'
    confirmed_phases:int=0
    confirmed_quests:int=0
    options:dict=Field(default_factory=dict)
    apples_used:int=0
    apple_attempts:int=0
    apples_committed:int=0
    apple_refills:list[dict]=Field(default_factory=list)


class FirstClearLedger:
    def __init__(self,parser,device_serial,root=None):
        identity={'device':device_serial,'quests':getattr(parser,'quest_records',{}),
                  'complete':getattr(parser,'quest_records_complete',False),
                  'servants':[(k,getattr(s,'lv',None),getattr(s,'np_lv',None))
                              for k,s in sorted(parser.servants.items())]}
        fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        self.file=Path(root or get_root_dir()/'.app_profile/first_clear_progress')/(fingerprint+'.json')
        self.data={'version':1,'fingerprint':fingerprint,'observations':{},'pending':{}}
        if self.file.exists():
            data=json.loads(self.file.read_text(encoding='utf-8'))
            if data.get('version')!=1 or data.get('fingerprint')!=fingerprint:
                raise ValueError('首通进度记录与当前账号或设备不一致')
            if not isinstance(data.get('observations'),dict) or not isinstance(data.get('pending'),dict):
                raise ValueError('首通进度记录格式损坏')
            self.data=data

    def save(self):
        self.file.parent.mkdir(parents=True,exist_ok=True)
        temp=self.file.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        temp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf-8')
        replace_file(temp,self.file)

    def observation(self,qid):return self.data['observations'].get(str(qid))

    def observe(self,value):
        if type(value.get('quest_id')) is not int or type(value.get('completed')) is not bool:
            raise ValueError('无效的实机阶段记录')
        value={**value,'observed_at':time.time()}
        self.data['observations'][str(value['quest_id'])]=value
        self.data['pending'].pop(str(value['quest_id']),None)
        self.save()

    def pending(self,qid,phase):
        self.data['pending'][str(qid)]={'phase':phase,'started_at':time.time()}
        self.save()


def quest_cleared(parser, quest_id, phase):
    record = getattr(parser, 'quest_records', {}).get(quest_id, {})
    try:
        return int(record.get('clearNum', 0)) > 0 and int(record.get('questPhase', 0)) >= phase
    except (TypeError, ValueError):
        return False


def first_clear_catalog(parser,options,ledger):
    entries=quest_bindings();by_id={e['id']:e for e in entries.values()}

    def cleared(qid):
        observation=ledger.observation(qid)
        if observation and observation.get('completed'):return True
        entry=by_id.get(qid)
        return bool(entry and quest_cleared(parser,qid,entry['phase']))

    rows=[]
    for row in quest_catalog(parser):
        if row['kind'] not in ('free','event') or row.get('war_id') not in FIRST_CLEAR_WARS:continue
        if options.war_ids and row['war_id'] not in options.war_ids:continue
        entry=entries[row['quest_id']];observation=ledger.observation(entry['id'])
        account_cleared=quest_cleared(parser,entry['id'],entry['phase'])
        reason=row['reason']
        if not reason and not getattr(parser,'quest_records_complete',False):reason='请先导入完整的账号任务记录'
        if not reason and (account_cleared or (observation and observation.get('completed'))):reason='已完成首次攻略'
        if not reason and entry.get('phases') not in ([1],[1,2,3]):reason='首次攻略阶段布局尚未适配'
        if not reason and entry.get('after_clear')!='repeatLast':reason='该自由关卡不是普通可重复关卡'
        if not reason:
            for condition in entry.get('release_conditions',[]):
                if (condition.get('type')=='date' and supports_event(entry)
                        and 0<entry.get('opened_at',0)<=time.time()):
                    continue
                if condition.get('type')!='questClear' or not cleared(condition.get('targetId')):
                    reason='开放条件尚无完整通关证据';break
        rows.append({**row,'cleared':account_cleared,'supported':not reason,'reason':reason,'progress':observation,
                     'phase_verification_required':True})
    rows.sort(key=lambda r:(not r['supported'],r['ap_per_run'] if r['ap_per_run'] is not None else 999,
                            r['war_id'],r['game_id']))
    return rows


class FirstClearRunner:
    def __init__(self,parser,device_manager=None,log_callback=None,report_root=None,progress_root=None):
        self.parser=parser
        self.device_mgr=device_manager or DeviceManager()
        self.log_callback=log_callback or (lambda text:None)
        self.report_root=Path(report_root or get_root_dir()/'.app_profile/quest_runs')
        self.progress_root=progress_root

    def run(self,options,*,device_serial='',cancel_event=None,on_progress=None):
        options=options if isinstance(options,FirstClearOptions) else FirstClearOptions.model_validate(options)
        report=FirstClearReport(device_serial=device_serial,options=options.model_dump())
        folder=self.report_root/report.run_id;folder.mkdir(parents=True,exist_ok=False)
        report.artifacts_dir=str(folder)
        cancel_event=cancel_event or threading.Event()
        ctrl=engine=active=phase_row=None

        def save():
            data=report.model_dump(mode='json');temp=folder/'report.tmp'
            temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');replace_file(temp,folder/'report.json')
            if on_progress:on_progress(data)

        def log(text):report.logs.append(text);self.log_callback(text)

        def stage(value):
            report.status=value
            if active is not None:
                active['status']=value
                if engine is not None and engine.current_recommendation is not None:
                    active['recommendation']=engine.current_recommendation.model_dump(mode='json')
            if phase_row is not None:phase_row['status']=value
            save()

        def cancelled():
            if cancel_event.is_set():raise RuntimeError('任务已取消')

        try:
            cancelled()
            if not self.parser.servants or not getattr(self.parser,'quest_records_complete',False):
                raise ValueError('请先导入完整的账号与任务记录')
            if not device_serial:raise ValueError('请选择一个明确的在线设备')
            devices=self.device_mgr.get_connected_devices(auto_connect=True)
            if len([d for d in devices if d.serial==device_serial])!=1:raise ValueError('所选设备不在线或不唯一')
            ledger=FirstClearLedger(self.parser,device_serial,self.progress_root)
            ctrl=FGOController(device_serial=device_serial,adb_path=self.device_mgr.adb_path,
                               cancel_event=cancel_event,log_callback=log)
            engine=SmartEngine(ctrl,chaldea_parser=self.parser,cancel_event=cancel_event,log_callback=log)
            engine.quest_progress_callback=stage;engine.first_clear_observation_dir=folder/'observations'
            navigator=FirstClearNavigator(engine)
            while options.max_quests is None or report.confirmed_quests<options.max_quests:
                cancelled();active=phase_row=None;stage('selecting')
                candidates=[r for r in first_clear_catalog(self.parser,options,ledger) if r['supported']]
                if not candidates:
                    report.status='completed';report.success=True
                    log('[首通完成] 当前范围没有符合条件的未通关普通自由关卡。');break
                candidate=navigator.select(candidates) if not options.war_ids else candidates[0]
                key=candidate['quest_id'];entry=quest_binding(key)
                entry={**entry,'spot':candidate['spot']}
                active={'target':{**candidate,'runs':len(entry['phases'])},'status':'observing','clears':0,'phases':[]}
                report.stations.append(active);phase_row=None
                log(f"[自动首通选关] {entry['war']} / {entry['spot']} / {entry['name']}")
                stage('observing');engine.current_recommendation=None
                before=inspect_free_quest(engine,key,entry)
                ledger.observe(before);active['observation']=before
                if before['completed']:
                    active['status']='already_completed';save();continue
                while not before['completed']:
                    cancelled();phase_row=None;phase=before['next_phase'];active['phase']=phase
                    if phase not in entry['phases']:raise RuntimeError('实机阶段不属于目标关卡')
                    cost=before['card_ap']
                    if options.ap_budget is not None and report.estimated_ap_spent+cost>options.ap_budget:
                        report.status=active['status']='ap_limit_reached';report.error='达到AP预算，已保留阶段进度';save();return report
                    if before['available_ap'] is None:raise RuntimeError('未确认当前体力，停止出击')
                    phase_key=key+f'@phase={phase}'
                    plan=FirstClearTarget(quest_id=phase_key,game_id=entry['id'],phase=phase,war=entry['war'],
                                          spot=entry['spot'],name=entry['name'],kind=candidate['kind'],runs=1,ap_per_run=cost)
                    if before['available_ap']<cost:
                        if options.refill_apple=='none' or (options.max_apples is not None and report.apples_committed>=options.max_apples):
                            report.status=active['status']='ap_limit_reached';report.error='当前体力不足，苹果恢复未启用或已达到使用上限';save();return report
                        # 先证明当前阶段可执行，避免为无法模拟的关卡消耗苹果。
                        engine.current_recommendation=None;engine.last_quest_clears=0;engine.last_quest_started=False
                        if not QuestBattleExecutor(engine).prepare(plan):
                            raise RuntimeError('恢复体力前的战斗模拟未通过，不使用苹果')
                        recovery=AppleRecovery(ctrl,log_callback=log,cancel_event=cancel_event,report_dir=folder/'apple_recovery')
                        while before['available_ap']<cost:
                            cancelled()
                            if options.max_apples is not None and report.apples_committed>=options.max_apples:
                                report.status=active['status']='ap_limit_reached';report.error='已达到本次苹果使用上限，当前体力仍不足';save();return report
                            stage('refilling')
                            def submit(event):
                                cancelled()
                                count=event.get('count')
                                if (type(count) is not int or count<1 or
                                        (options.max_apples is not None and count>options.max_apples-report.apples_committed)):
                                    raise RuntimeError('果实数量无效或使用上限已达到')
                                report.apple_attempts+=1
                                report.apples_committed+=count
                                report.apple_refills.append({**event,'quest_id':entry['id'],'phase':phase})
                                save()
                            try:
                                restored=recovery.restore_once(options.refill_apple,cost,submit,
                                                               max_count=options.max_apples-report.apples_committed
                                                               if options.max_apples is not None else 999)
                            except AppleLimitReached as exc:
                                report.status=active['status']='ap_limit_reached';report.error=str(exc);save();return report
                            if restored is not None:
                                report.apple_refills[-1].update(restored);report.apples_used+=restored['count'];save()
                            observed=inspect_free_quest(engine,key,entry)
                            if observed['quest_id']!=entry['id'] or observed['next_phase']!=phase or observed['completed']:
                                raise RuntimeError('苹果恢复后关卡或阶段发生变化，停止出击')
                            if observed['available_ap'] is None or (restored is None and observed['available_ap']<cost):
                                raise RuntimeError('苹果恢复后未确认有效体力，停止出击')
                            if restored is not None and observed['available_ap']<restored['after_ap']:
                                raise RuntimeError('苹果恢复后卡片体力复核失败，停止出击')
                            ledger.observe(observed);active['observation']=observed;before=observed
                    phase_row={'phase':phase,'status':'pending','battle_clears':0,'progress_verified':False,'ap':cost,'ap_accounted':False}
                    active['phases'].append(phase_row);active['phase']=phase
                    active.pop('recommendation',None)
                    engine.current_recommendation=None;engine.last_quest_clears=0;engine.last_quest_started=False
                    ledger.pending(entry['id'],phase);save()
                    log(f'[首通阶段] 第{phase}阶段：重新配队模拟、装备核验及单场战斗。')
                    ok=engine.run_quest(plan,cancel_event=cancel_event)
                    count=engine.last_quest_clears
                    if type(count) is not int or count not in (0,1):raise RuntimeError('战斗执行器返回的场次无效')
                    phase_row['battle_clears']=count;active['clears']+=count
                    report.confirmed_clears+=count
                    if count==1 or engine.last_quest_started is True:
                        report.estimated_ap_spent+=cost;phase_row['ap_accounted']=True
                    if engine.current_recommendation is not None:
                        active['recommendation']=engine.current_recommendation.model_dump(mode='json')
                    if not ok or count!=1:raise RuntimeError('本阶段模拟或战斗未通过，保留进度并停止后续关卡')
                    cancelled();stage('verifying_progress')
                    after=inspect_free_quest(engine,key,entry)
                    index=entry['phases'].index(phase)
                    expected=entry['phases'][index+1] if index+1<len(entry['phases']) else None
                    if after['quest_id']!=entry['id'] or after['next_phase']!=expected or after['completed']!=(expected is None):
                        raise RuntimeError('战后未确认准确的阶段前进，禁止继续下一阶段或关卡')
                    ledger.observe(after);phase_row['progress_verified']=True;phase_row['status']='completed'
                    report.confirmed_phases+=1;active['observation']=after;before=after
                    if not after['completed']:
                        log(f"[首通进度] 【{entry['name']}】第 {phase} 阶段完成。本次已通关 "
                            f"{report.confirmed_quests} 关，已确认 {report.confirmed_phases} 个阶段。")
                    save()
                active['status']='completed';report.confirmed_quests+=1;phase_row=None
                log(f"[首通整关完成] 【{entry['name']}】全部阶段完成。本次已通关 "
                    f"{report.confirmed_quests} 关，已确认 {report.confirmed_phases} 个阶段。")
                save()
            else:
                report.status='completed';report.success=True;log('[首通完成] 已达到本次整关数量上限。')
        except Exception as exc:
            if report.apple_refills and report.apple_refills[-1]['status']=='pending':
                report.apple_refills[-1].update(status='unverified',error=str(exc))
            if phase_row is not None and engine is not None and phase_row['battle_clears']==0:
                count=engine.last_quest_clears
                if type(count) is int and count==1:
                    phase_row['battle_clears']=1;active['clears']+=1
                    report.confirmed_clears+=1
            if phase_row is not None and not phase_row['ap_accounted'] and engine is not None and engine.last_quest_started is True:
                report.estimated_ap_spent+=phase_row['ap'];phase_row['ap_accounted']=True
            report.status='cancelled' if cancel_event.is_set() else 'failed';report.error=str(exc)
            if active is not None:active['status']=report.status
            log('[自动首通停止] '+str(exc))
            if ctrl is not None:
                try:ctrl.screencap(force_fresh=True).save(folder/'stopped.png')
                except Exception:pass
        finally:
            log(f"[首通统计] 本次已通关 {report.confirmed_quests} 关，"
                f"已确认 {report.confirmed_phases} 个阶段，战斗胜利 {report.confirmed_clears} 场。")
            save()
        return report


if __name__=='__main__':
    import argparse
    import signal
    import sys
    from src.parser.chaldea_parser import ChaldeaParser
    from src.core.path_helper import get_resource_path
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    cli=argparse.ArgumentParser(description='按真实阶段自动完成未通关普通自由关卡')
    cli.add_argument('--account',type=Path,default=get_resource_path('data/user_account.json'))
    cli.add_argument('--device',required=True)
    cli.add_argument('--war',type=int,action='append',default=[])
    cli.add_argument('--max-quests',type=int,default=3)
    cli.add_argument('--ap-budget',type=int,default=100)
    cli.add_argument('--no-quest-limit',action='store_true',help='不限制整关完成数量')
    cli.add_argument('--no-ap-limit',action='store_true',help='不限制本次战斗AP预算')
    cli.add_argument('--apple',choices=['none','bronze','blue','silver','gold'],default='none')
    cli.add_argument('--max-apples',type=int,default=0)
    cli.add_argument('--no-apple-limit',action='store_true',help='所选果实不限制本次使用个数')
    cli.add_argument('--report-root',type=Path)
    args=cli.parse_args();parser=ChaldeaParser();parser.parse_file(args.account)
    cancel=threading.Event();signal.signal(signal.SIGINT,lambda *_:cancel.set())
    options=FirstClearOptions(war_ids=args.war,max_quests=None if args.no_quest_limit else args.max_quests,
                               ap_budget=None if args.no_ap_limit else args.ap_budget,refill_apple=args.apple,
                               max_apples=(None if args.no_apple_limit else args.max_apples) if args.apple!='none' else 0)
    result=FirstClearRunner(parser,log_callback=lambda t:print(t,flush=True),report_root=args.report_root).run(
        options,device_serial=args.device,cancel_event=cancel)
    print(result.status,result.error,result.artifacts_dir,flush=True)
    raise SystemExit(0 if result.success else 1)
