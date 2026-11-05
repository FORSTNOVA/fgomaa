"""逐步模拟与操作录制；每次重放同一进场快照的动作前缀，撤销不累加状态。"""
from __future__ import annotations
import copy
import json
import re
import secrets
import threading
import uuid
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from src.core.atomic_io import replace_file
from src.core.path_helper import get_root_dir
from src.strategy.laplace import LaplaceService, LaplaceError, _digest
from src.strategy.manual_simulation import StrictModel, ManualPlan, SupportStats, SupportCE, OwnedStats, Level, ManualAction, ManualSimulation, MashForm


def valid_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value):raise ValueError('无效记录ID')
    return value


class DraftSlot(StrictModel):
    source: Literal['owned','support']='owned'
    svt_id: int=Field(default=0,ge=0)
    ce_id: int|None=Field(default=None,ge=1)
    support: SupportStats|None=None
    support_ce: SupportCE|None=None
    owned_stats: OwnedStats|None=None
    owned_ce: SupportCE|None=None
    mash_form: MashForm|None=None


class SetupDraft(StrictModel):
    quest_id: str=Field(default='',max_length=100)
    party: list[DraftSlot]=Field(min_length=6,max_length=6)
    mystic_code_id: int|None=Field(default=None,ge=1)
    mystic_code_lv: Level|None=None

    def ready_plan(self):
        return ManualPlan.model_validate({**self.model_dump(),'turns':[{'actions':[{'kind':'np','actor':1}]}]})


class InteractiveEvent(StrictModel):
    kind: Literal['skill','master','np','enemy','cards','back','attack','facecards','card','unselect','enemy_step','enemy_turn']
    actor: int|None=Field(default=None,ge=1,le=6)
    skill: int|None=Field(default=None,ge=1,le=3)
    target: int|None=Field(default=None,ge=1,le=6)
    enemy: int|None=Field(default=None,ge=1,le=6)
    swap: list[int]|None=Field(default=None,min_length=2,max_length=2)
    card: int|None=Field(default=None,ge=1,le=5)

    @model_validator(mode='after')
    def fields_match(self):
        value=self.model_dump(exclude_none=True)
        if self.kind in ('skill','master'):
            ManualAction.model_validate(value)
        elif self.kind=='np':
            if self.actor is None or set(value)-{'kind','actor'}:raise ValueError('宝具选择需要角色槽位')
        elif self.kind=='enemy':
            if self.enemy is None or set(value)-{'kind','enemy'}:raise ValueError('请选择敌方目标')
        elif self.kind=='card':
            if self.card is None or set(value)-{'kind','card'}:raise ValueError('请选择本回合手牌的第1至5张')
        elif set(value)!={'kind'}:raise ValueError('该操作不接受额外参数')
        return self


def compile_events(events,version=1):
    if version!=1:
        result=compile_card_events(events,strict=version in (2,3))
        if version:result['interactive_version']=version
        return result
    turns=[{'actions':[]}];selected=[];phase='commands';enemy=None
    for raw in events:
        event=raw if isinstance(raw,InteractiveEvent) else InteractiveEvent.model_validate(raw)
        value=event.model_dump(exclude_none=True);kind=event.kind
        if kind in ('card','unselect','enemy_step','enemy_turn'):raise ValueError('旧版进场快照不支持实际手牌与敌方回合，请重新进场')
        if kind in ('skill','master','enemy'):
            if phase!='commands':raise ValueError('选卡阶段请先返回技能界面')
            if kind=='enemy':enemy=event.enemy
            else:turns[-1]['actions'].append({**value,'enemy':event.enemy or enemy})
        elif kind=='cards':
            if phase!='commands':raise ValueError('已经处于选卡阶段')
            phase='cards'
        elif kind=='back':
            if phase!='cards':raise ValueError('当前不在选卡阶段')
            phase='commands';selected=[]
        elif kind=='np':
            if phase!='cards' or event.actor in selected or len(selected)>=3:raise ValueError('请在选卡阶段依次选择最多3个不同角色的宝具')
            selected.append(event.actor)
        else:
            if phase!='cards':raise ValueError('请先点击Attack进入选卡阶段')
            if kind=='attack':
                if not selected:raise ValueError('请先选择要释放的宝具')
                turns[-1]['actions'].extend({'kind':'np','actor':actor,'enemy':enemy} for actor in selected)
            else:
                if selected:raise ValueError('任意三卡普攻不能与宝具混排')
                turns[-1]['actions'].append({'kind':'facecards','enemy':enemy})
            turns.append({'actions':[]});phase='commands';selected=[];enemy=None
        if len(turns)>31:raise ValueError('最多模拟30回合')
    return {'manual_turns':turns,'selected_nps':selected,'interactive_phase':phase,
            'interactive_turn':len(turns),'interactive_enemy':enemy}


def compile_card_events(events,strict=True):
    """纯语法检查；波次清场与敌方行动可否执行由原生战场决定。"""
    phase='commands';selected=[];turns=0;values=[]
    for raw in events:
        event=raw if isinstance(raw,InteractiveEvent) else InteractiveEvent.model_validate(raw)
        value=event.model_dump(exclude_none=True);kind=event.kind;values.append(value)
        if kind in ('skill','master','enemy'):
            if kind=='enemy' and phase=='cards':continue
            if phase not in ('commands','post_attack'):raise ValueError('请先返回己方技能阶段')
            phase='commands'
        elif kind=='cards':
            if phase not in ('commands','post_attack'):raise ValueError('已经处于选卡或敌方阶段')
            phase='cards'
        elif kind=='back':
            if phase!='cards':raise ValueError('当前不在选卡阶段')
            selected=[];phase='commands'
        elif kind in ('np','card'):
            if phase!='cards' or len(selected)>=3 or value in selected:raise ValueError('请在选卡阶段选择最多3张不同指令卡或宝具')
            selected.append(value)
        elif kind=='unselect':
            if phase!='cards' or not selected:raise ValueError('没有可撤销的已选指令卡')
            selected.pop()
        elif kind in ('enemy_step','enemy_turn'):
            if phase not in ('post_attack','enemy'):raise ValueError('尚未进入敌方阶段')
            phase='post_attack'
        else:
            if phase!='cards':raise ValueError('请先进入选卡阶段')
            if kind=='facecards':
                if strict:raise ValueError('请从五张实际手牌中选择三张指令卡')
                if selected:raise ValueError('任意三卡证明不能与宝具混排')
            elif (strict and len(selected)!=3) or not selected:
                raise ValueError('请选择恰好3张普通指令卡或宝具')
            turns+=1
            if turns>30:raise ValueError('最多模拟30回合')
            phase='post_attack';selected=[]
    return {'interactive_events':values,'interactive_version':2}


class RecordingEntry(StrictModel):
    kind: Literal['setup','enter','event']
    label: str=Field(min_length=1,max_length=150)
    draft: SetupDraft|None=None
    session_id: str|None=None
    event: InteractiveEvent|None=None
    scene: dict|None=None
    engine_hash: str=''

    @model_validator(mode='after')
    def entry_fields(self):
        if self.kind in ('setup','enter') and self.draft is None:raise ValueError('配置记录缺少编队')
        if self.kind=='event' and self.event is None:raise ValueError('战斗记录缺少操作')
        if self.kind=='enter':valid_id(self.session_id)
        if self.kind!='event' and self.event is not None:raise ValueError('配置记录不能包含战斗动作')
        return self


class InteractiveRecording(StrictModel):
    version: Literal[1,2]=1
    name: str=Field(min_length=1,max_length=80)
    entries: list[RecordingEntry]=Field(min_length=1,max_length=600)
    cursor: int=Field(ge=0,lt=600)
    id: str|None=None

    @model_validator(mode='after')
    def timeline(self):
        if self.cursor>=len(self.entries):raise ValueError('回退位置超出录制范围')
        if self.id is not None:valid_id(self.id)
        if self.version==2 and (self.entries[0].kind!='enter' or any(
                entry.kind!='event' for entry in self.entries[1:])):
            raise ValueError('战斗录制须从一次成功进场开始，后续只接受战斗操作')
        active=False;events=[]
        for entry in self.entries:
            if entry.kind=='setup':active=False;events=[]
            elif entry.kind=='enter':active=True;events=[]
            else:
                if not active:raise ValueError('战斗操作之前必须进入模拟关卡')
                events.append(entry.event)
                compile_events(events,version=0)
        return self


class InteractiveSimulation:
    def __init__(self,service=None,root=None):
        self.service=service or LaplaceService()
        self.root=Path(root or get_root_dir()/'.app_profile/interactive_simulation')

    def write(self,file,value):
        file.parent.mkdir(parents=True,exist_ok=True)
        temporary=file.with_suffix('.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        replace_file(temporary,file)

    def enter(self,draft,parser,cancel_event=None,*,version=3,seed=None):
        if type(version) is not int or version not in (1,2,3):raise ValueError('模拟版本无效')
        draft=SetupDraft.model_validate(draft);plan=draft.ready_plan()
        from src.workflows.battle_runner import simulation_catalog
        row=next((r for r in simulation_catalog(parser) if r['quest_id']==draft.quest_id),None)
        if not row or not row['simulation_supported']:raise ValueError('没有准确可模拟的目标关卡')
        payload,metadata=ManualSimulation(self.service).prepare(plan,parser,cancel_event)
        if version>=2:
            seed=secrets.randbits(31) if seed is None else seed
            if type(seed) is not int or not 0<=seed<2**31:raise ValueError('随机种子无效')
            payload['request'].update(interactive_version=version,rng_seed=seed)
            existing={s['id'] for s in payload['data']['servants']}
            enemies={e['svt']['id'] for stage in payload['data']['quest']['stages'] for e in stage['enemies']}
            for enemy_id in sorted(enemies-existing):
                payload['data']['servants'].append(self.service.data.get(metadata['enemy_region'],'servant',enemy_id))
            metadata['assumptions']=[note for note in metadata['assumptions'] if '仅模拟己方' not in note]
            metadata['assumptions'].extend(['固定进场种子，15张普通卡每回合随机发5张，三回合用尽后重洗；回退和重载保持同一随机结果。',
                '敌方采用基础行动策略：按行动次数限制分配普攻，蓄力满时释放宝具；非完整关卡AI，不自动猜测技能或脚本条件。'])
            if version==3:
                from src.strategy.battle_ai_data import freeze_battle_ai
                metadata['ai_data']=freeze_battle_ai(payload,parser,self.service.data,metadata['enemy_region'])
                metadata['assumptions']=[s for s in metadata['assumptions'] if '最低伤害浮动' not in s and '基础行动策略' not in s]
                metadata['assumptions'].extend(['逐目标按游戏常量范围均匀抽取伤害乱数，随机流固定在进场快照。',
                    '按冻结的公开AI条件、权重、技能和脚本执行；遇到未支持条件/脚本会停止并报告具体节点。'])
        identity=uuid.uuid4().hex
        session={'draft':draft.model_dump(),'payload':payload,'metadata':metadata,'version':version}
        self.write(self.root/'sessions'/(identity+'.json'),session)
        return self.replay(identity,[],cancel_event)

    def replay(self,identity,events,cancel_event=None):
        identity=valid_id(identity)
        if not isinstance(events,list) or len(events)>600:raise ValueError('操作数量无效')
        file=self.root/'sessions'/(identity+'.json')
        if not file.is_file():raise ValueError('进场快照不存在，请重新进入模拟关卡')
        session=json.loads(file.read_text(encoding='utf-8'));payload=copy.deepcopy(session['payload'])
        version=session.get('version',1)
        if version not in (1,2,3):raise ValueError('进场快照版本不受支持')
        compiled=compile_events(events,version=version)
        payload['request'].update(compiled,interactive=True)
        service=self.service;event=cancel_event or threading.Event()
        service.data.cancel_event=event
        manifest=json.loads((service.runtime/'manifest.json').read_text(encoding='utf-8'))
        if f'interactive_v{version}' not in manifest.get('features',[]):raise LaplaceError('请更新逐步模拟运行时')
        while not service._lock.acquire(timeout=.1):
            if event.is_set():raise LaplaceError('计算已取消')
        try:
            if event.is_set():raise LaplaceError('计算已取消')
            for attempt in range(4):
                result=service._run_worker(payload,event)
                missing=result.get('missing_data',[])
                if not missing:break
                if attempt==3:raise LaplaceError('公开技能依赖仍不完整')
                for url in sorted(set(missing)):
                    match=re.fullmatch(r'https://api\.atlasacademy\.io/nice/(JP|CN)/skill/(\d+)',url)
                    if not match:raise LaplaceError('缺少尚未支持的公开数据')
                    payload['data']['base_skills'].append(service.data.get(match[1],'skill',int(match[2])))
            if event.is_set():raise LaplaceError('计算已取消')
            if not result.get('success') or 'scene' not in result.get('manual',{}):raise LaplaceError(result.get('message','没有返回有效战场状态'))
            if version==3 and session['payload']['data']['base_skills']!=payload['data']['base_skills']:
                session['payload']['data']['base_skills']=copy.deepcopy(payload['data']['base_skills'])
                self.write(file,session)
            manual=result['manual']
            return {'session_id':identity,'scene':manual['scene'],'failure':manual.get('failure'),
                    'engine_hash':manifest['kernel_sha256'],'input_digest':_digest(payload),
                    'metadata':session['metadata'],'draft':session['draft']}
        finally:service._lock.release()

    def save_recording(self,value,identity=None):
        value=InteractiveRecording.model_validate(value)
        version=1;events=[];recognition=None
        for entry in value.entries:
            if entry.kind=='setup':events=[]
            elif entry.kind=='enter':
                file=self.root/'sessions'/(valid_id(entry.session_id)+'.json')
                if not file.is_file():raise ValueError('录制引用的进场快照已经不存在')
                session=json.loads(file.read_text(encoding='utf-8'))
                if SetupDraft.model_validate(session['draft'])!=entry.draft:raise ValueError('录制编队与进场快照不一致')
                version=session.get('version',1);events=[]
                if value.version==2:
                    metadata=session['metadata']
                    payload=session['payload']
                    quest=payload['data']['quest']
                    recognition={
                        'quest':{'catalog_key':entry.draft.quest_id,'game_id':metadata['quest_id'],
                                 'phase':metadata['phase'],'name':metadata['quest_name'],
                                 'war_id':quest.get('warId'),'spot_id':quest.get('spotId')},
                        'party':[{'slot':row['party_slot'],'source':'support' if row['is_friend'] else 'owned',
                                  'svt_id':row['svt_id'],'name':row['name'],'lv':row['lv'],
                                  'ascension':row['ascension'],'costume_id':row.get('costume_id'),
                                  'np_id':row['np_id'],'skill_ids':row['skill_ids'],
                                  'battle_chara_id':row.get('battle_chara_id'),'ce':row.get('ce')}
                                 for row in payload['request']['team']],
                        'mystic_code':metadata['mystic_code'],
                    }
            else:
                events.append(entry.event);compile_events(events,version=version)
        identity=valid_id(identity) if identity else value.id or uuid.uuid4().hex
        result={**value.model_dump(),'id':identity}
        if recognition is not None:result['recognition']=recognition
        self.write(self.root/'recordings'/(identity+'.json'),result)
        return result

    def recordings(self):
        folder=self.root/'recordings'
        if not folder.exists():return []
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(folder.glob('*.json')) if re.fullmatch(r'[0-9a-f]{32}',p.stem)]
