"""手动六人编队、严格行动顺序和可保存的本地模拟方案。"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.path_helper import get_resource_path, get_root_dir
from src.core.atomic_io import replace_file
from src.parser.chaldea_parser import ServantProfile
from src.strategy.account_versions import MELUSINE_NP_FORMS, resolve_versions
from src.strategy.laplace import LaplaceService, LaplaceError, _digest

Level = Annotated[int, Field(ge=1, le=10)]
AppendLevel = Annotated[int, Field(ge=0, le=10)]
SlotNumber = Annotated[int, Field(ge=1, le=6)]
MashForm = Literal['ortinax','paladin']

# Standard combat forms from the CN Mash record, with their unlocked skill/NP sets.
MASH_FORMS = {
    'ortinax': (13, (744450, 460250, 2162350), 800106),
    'paladin': (17, (2476450, 2550, 133550), 800107),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SupportStats(StrictModel):
    lv: int = Field(default=90, ge=1, le=120)
    np_lv: int = Field(default=1, ge=1, le=5)
    skills: list[Level] = Field(default_factory=lambda:[10,10,10], min_length=3, max_length=3)
    appends: list[AppendLevel] = Field(default_factory=lambda:[0]*5, min_length=5, max_length=5)
    atk_fou: int = Field(default=0, ge=0, le=2000)
    hp_fou: int = Field(default=0, ge=0, le=2000)
    ascension: int = Field(default=4, ge=0, le=4)
    skill_ids: list[int] = Field(default_factory=list, max_length=3)
    np_id: int | None = Field(default=None, ge=1)


class SupportCE(StrictModel):
    lv: int = Field(default=1, ge=1, le=100)
    is_mlb: bool = False


class OwnedStats(StrictModel):
    lv: int | None = Field(default=None, ge=1, le=120)
    np_lv: int | None = Field(default=None, ge=1, le=5)
    ascension: int | None = Field(default=None, ge=0, le=4)
    atk_fou: int | None = Field(default=None, ge=0, le=2000)
    hp_fou: int | None = Field(default=None, ge=0, le=2000)
    skill_lv1: Level | None = None
    skill_lv2: Level | None = None
    skill_lv3: Level | None = None
    append_skill_lv1: AppendLevel | None = None
    append_skill_lv2: AppendLevel | None = None
    append_skill_lv3: AppendLevel | None = None
    append_skill_lv4: AppendLevel | None = None
    append_skill_lv5: AppendLevel | None = None
    skill_ids: list[Annotated[int, Field(ge=1)] | None] | None = Field(default=None, min_length=3, max_length=3)
    np_id: int | None = Field(default=None, ge=1)

    def apply(self, profile):
        values = self.model_dump(exclude_none=True, exclude={'skill_ids'})
        if self.skill_ids is not None:
            imported = [*profile.skill_ids, 0, 0, 0]
            values['skill_ids'] = [version if version is not None else imported[i]
                                   for i, version in enumerate(self.skill_ids)]
        return profile.model_copy(update=values, deep=True)


class ManualSlot(StrictModel):
    source: Literal['owned','support'] = 'owned'
    svt_id: int = Field(ge=1)
    ce_id: int | None = Field(default=None, ge=1)
    support: SupportStats | None = None
    support_ce: SupportCE | None = None
    owned_stats: OwnedStats | None = None
    owned_ce: SupportCE | None = None
    mash_form: MashForm | None = None

    @model_validator(mode='after')
    def custom_stats_match_source(self):
        if self.mash_form is not None and (self.svt_id != 800100 or self.source != 'owned'):
            raise ValueError('仅自有玛修可以选择灵基')
        if self.source == 'owned' and (self.support is not None or self.support_ce is not None):
            raise ValueError('自有槽位须使用自有从者和礼装的模拟覆盖字段')
        if self.source == 'support' and (self.owned_stats is not None or self.owned_ce is not None):
            raise ValueError('助战槽位须填写助战练度')
        if self.source == 'support' and self.support is None:
            raise ValueError('请填写自定义助战练度')
        if self.source == 'support' and bool(self.ce_id) != bool(self.support_ce):
            raise ValueError('助战礼装和等级／满破须同时设置')
        if self.owned_ce is not None and self.ce_id is None:
            raise ValueError('自定义礼装练度前请先装备礼装')
        return self


class ManualAction(StrictModel):
    kind: Literal['skill','master','np','facecards']
    actor: SlotNumber | None = None
    skill: int | None = Field(default=None, ge=1, le=3)
    target: SlotNumber | None = None
    enemy: int = Field(default=1, ge=1, le=6)
    swap: list[SlotNumber] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode='after')
    def action_fields(self):
        if self.kind == 'skill' and (self.actor is None or self.skill is None or self.swap is not None):
            raise ValueError('从者技能需要角色槽位和技能编号')
        if self.kind == 'master' and (self.skill is None or self.actor is not None):
            raise ValueError('御主技能需要技能编号，不能设置从者施放者')
        if self.kind == 'np' and (self.actor is None or self.skill is not None or self.swap is not None or self.target is not None):
            raise ValueError('宝具需要角色槽位，可设置敌方目标')
        if self.kind == 'facecards' and any(v is not None for v in (self.actor,self.skill,self.target,self.swap)):
            raise ValueError('任意三卡普攻无需额外角色或技能字段')
        if self.swap is not None and (self.kind != 'master' or self.swap[0] == self.swap[1]):
            raise ValueError('换人必须设置两个不同槽位')
        return self


class ManualTurn(StrictModel):
    actions: list[ManualAction] = Field(min_length=1, max_length=40)

    @model_validator(mode='after')
    def game_order(self):
        attacking = False
        attacks = []
        for action in self.actions:
            if action.kind in ('np','facecards'):
                attacking = True
                attacks.append(action)
            elif attacking:
                raise ValueError('同回合技能／御主技能必须排在攻击前，不能在宝具后释放技能')
        if not attacks or len(attacks) > 3:
            raise ValueError('每回合须安排1至3个宝具，或一次任意三卡普攻')
        if any(a.kind == 'facecards' for a in attacks) and len(attacks) != 1:
            raise ValueError('任意三卡普攻不能与宝具混排')
        actors = [a.actor for a in attacks if a.kind == 'np']
        if len(actors) != len(set(actors)):
            raise ValueError('同一角色每回合只能选择一次宝具')
        if len({a.enemy for a in attacks}) != 1:
            raise ValueError('同回合宝具链使用同一个初始敌方目标')
        return self


class ManualPlan(StrictModel):
    quest_id: str = Field(min_length=1, max_length=100)
    party: list[ManualSlot] = Field(min_length=6, max_length=6)
    mystic_code_id: int | None = Field(default=None, ge=1)
    mystic_code_lv: Level | None = None
    turns: list[ManualTurn] = Field(min_length=1, max_length=30)

    @model_validator(mode='after')
    def party_and_outfit(self):
        if sum(slot.source == 'support' for slot in self.party) != 1:
            raise ValueError('六人编队须包含5名自有角色和1名自定义助战')
        owned = [s.svt_id for s in self.party if s.source == 'owned']
        if len(set(owned)) != len(owned):
            raise ValueError('不能重复编入同一名自有角色')
        if self.mystic_code_id is None and any(a.kind == 'master' for t in self.turns for a in t.actions):
            raise ValueError('有御主技能时必须选择明确的御主礼装')
        if self.mystic_code_lv is not None and self.mystic_code_id is None:
            raise ValueError('自定义御主礼装等级前请先选择御主礼装')
        return self


def inventory(parser):
    names = json.loads(get_resource_path('data/servant_names.json').read_text(encoding='utf-8'))
    filters = json.loads(get_resource_path('data/servant_filter_metadata.json').read_text(encoding='utf-8'))

    def filter_fields(identity):
        metadata = filters.get(str(identity), {})
        rarity = metadata.get('rarity')
        return {'class_name': metadata.get('className'), 'rarity': rarity,
                'rarities': [3, 4, 5] if int(identity) == 800100 else ([rarity] if rarity is not None else [])}

    ces = json.loads(get_resource_path('data/craft_essences.json').read_text(encoding='utf-8'))
    codes = []
    for identity, lv in parser.mystic_codes.items():
        file = get_resource_path(f'data/laplace/public/CN/MC/{identity}.json')
        name = json.loads(file.read_text(encoding='utf-8'))['name'] if file.exists() else f'御主礼装 ID {identity}'
        codes.append({'id':identity,'lv':lv,'name':name})
    return {'servants':[{**s.model_dump(), **filter_fields(s.svt_id)} for s in parser.servants.values() if not s.in_storage],
            'craft_essences':[c.model_dump() for c in parser.craft_essences.values()],
            'support_servants':[{'id':int(sid),'name':name, **filter_fields(sid)} for sid,name in names.items()],
            'support_ces':[{'id':int(cid),'name':value['name']} for cid,value in ces.items()],
            'mystic_codes':codes, 'mystic_codes_complete':parser.mystic_codes_complete}


def support_metadata(identity, data=None):
    data = data or LaplaceService().data
    servant = data.get('CN','servant',identity)
    def versions(kind,num):
        unique = {}
        for row in sorted(servant.get(kind,[]),key=lambda r:r.get('priority',0)):
            if row.get('num') == num:
                name = row['name']
                form = MELUSINE_NP_FORMS.get(row['id']) if kind == 'noblePhantasms' and identity == 304800 else None
                if form:
                    name = f'{form[1]} · {name}'
                unique[row['id']] = {'id':row['id'],'name':name,'priority':row.get('priority',0)}
                if kind == 'noblePhantasms':
                    flags = row.get('effectFlags', [])
                    unique[row['id']].update(
                        card_type={'1':'Arts','2':'Buster','3':'Quick'}.get(str(row.get('card')), ''),
                        target='单体' if 'attackEnemyOne' in flags else '全体' if 'attackEnemyAll' in flags else '辅助')
        return list(unique.values())
    return {'id':identity,'name':servant['name'],'lv_max':servant['lvMax'],
            'skills':[versions('skills',i) for i in range(1,4)],'nps':versions('noblePhantasms',1)}


def validate_ownership(plan, parser):
    for slot in plan.party:
        if slot.source == 'support':
            continue
        owned = parser.servants.get(slot.svt_id)
        if owned is None or owned.in_storage:
            raise ValueError(f'账号未持有可出场的从者 {slot.svt_id}')
        if slot.ce_id is not None and slot.ce_id not in parser.craft_essences:
            raise ValueError(f'账号未持有礼装 {slot.ce_id}')
    if plan.mystic_code_id is not None and plan.mystic_code_id not in parser.mystic_codes:
        raise ValueError('账号没有该御主礼装的已导入等级记录')


class ManualSimulation:
    def __init__(self, service=None):
        self.service = service or LaplaceService()

    def prepare(self, plan, parser, cancel_event=None):
        validate_ownership(plan, parser)
        service = self.service
        entry, quest, region = service._load_quest(plan.quest_id,cancel_event)
        const_data = service.data.constants()
        notes = ['按最低伤害浮动0.9严格执行手动顺序，不补放技能、不自动换人或改礼装。',
                 '仅模拟己方行动；不计敌方攻击及未建模的额外加成，不作为自动出击校验。',
                 '自有练度以账号为默认值，按本次编队的自定义数值覆盖。助战使用所填写的配置。',
                 '模拟允许同名礼装同时装备，等级与满破状态按各槽位计算。']
        if region == 'JP':
            notes.append('国服缺少敌人数据，使用同ID、同阶段日服样本。')
        servants, ces, team = {}, {}, []
        for index, slot in enumerate(plan.party,1):
            if cancel_event is not None and cancel_event.is_set():
                raise LaplaceError('计算已取消')
            imported = parser.servants[slot.svt_id] if slot.source == 'owned' else None
            # Mash's combat form is keyed by costume, separately from her ascension.
            costume_id = imported.costume_id if imported and slot.svt_id == 800100 else None
            form = MASH_FORMS.get(slot.mash_form)
            if form:
                costume_id, skill_ids, np_id = form
            use_form = costume_id is not None and costume_id > 4
            if use_form or slot.svt_id not in servants:
                servants[slot.svt_id] = service.data.get('CN','servant',slot.svt_id,lore=True) if use_form else service.data.get('CN','servant',slot.svt_id)
            servant = servants[slot.svt_id]
            if slot.source == 'owned':
                profile = slot.owned_stats.apply(imported) if slot.owned_stats is not None else imported
                if form:
                    profile = profile.model_copy(update={'costume_id':costume_id,
                                                         'skill_ids':list(skill_ids), 'np_id':np_id})
                records = parser.quest_records
            else:
                custom = slot.support
                profile = ServantProfile(svt_id=slot.svt_id,name=servant['name'],lv=custom.lv,np_lv=custom.np_lv,
                    ascension=custom.ascension,atk_fou=custom.atk_fou,hp_fou=custom.hp_fou,
                    skill_ids=custom.skill_ids,np_id=custom.np_id,
                    **{f'skill_lv{i}':lv for i,lv in enumerate(custom.skills,1)},
                    **{f'append_skill_lv{i}':lv for i,lv in enumerate(custom.appends,1)})
                records = {}
            battle_chara_id = None
            max_lv = min(len(servant['atkGrowth']), len(servant['hpGrowth']))
            if use_form:
                costume = next((c for c in servant.get('profile', {}).get('costume', {}).values()
                                if c['id'] == costume_id), None)
                if costume is None:
                    raise LaplaceError(f'{profile.name} 缺少当前战斗形态的数据（灵衣 ID {costume_id}）')
                battle_chara_id = costume['battleCharaId']
                growth = servant['ascensionAdd']['overwriteExpType']['costume'].get(str(costume_id))
                if growth is not None:
                    curve = const_data['svtExp'][str(growth)]
                    max_lv = min(120, len(curve['curve']) - (curve['lv'][:1] == [0]))
            if form and profile.lv > max_lv:
                notes.append(f'{profile.name} 的模拟等级由 Lv.{profile.lv} 调整为 {costume["name"]}的 Lv.{max_lv}。')
                profile = profile.model_copy(update={'lv':max_lv})
            if not 1 <= profile.lv <= max_lv:
                raise ValueError(f'{index} 号位 {profile.name} Lv.{profile.lv} 超出当前形态的成长数据范围（1–{max_lv}）')
            if not 1 <= profile.np_lv <= 5 or any(not 1 <= v <= 10 for v in profile.skills):
                raise ValueError(f'{index} 号位 {profile.name} 的宝具或技能等级无效')
            resolved, warnings = resolve_versions(servant,profile,records)
            notes.extend(warnings)
            row = {**profile.model_dump(),**resolved,'party_slot':index,'is_friend':slot.source == 'support',
                   'append_levels':profile.append_skills,
                   'atk_fou':profile.atk_fou if profile.atk_fou is not None else profile.fou,
                   'hp_fou':profile.hp_fou if profile.hp_fou is not None else profile.fou}
            if battle_chara_id is not None:
                row['battle_chara_id'] = battle_chara_id
            if slot.ce_id is not None:
                if slot.ce_id not in ces:
                    ces[slot.ce_id] = service.data.get('CN','equip',slot.ce_id)
                equip = ces[slot.ce_id]
                stats = (slot.support_ce if slot.source == 'support'
                         else slot.owned_ce or parser.craft_essences[slot.ce_id])
                if not 1 <= stats.lv <= equip['lvMax']:
                    raise ValueError('礼装等级超过实际等级上限')
                last_limit_gain = {1:10,2:10,3:10,4:15,5:20}.get(equip.get('rarity'),20)
                if not stats.is_mlb and stats.lv > equip['lvMax']-last_limit_gain:
                    raise ValueError('未满破礼装等级超过实际可达上限，请核对满破／等级')
                row['ce'] = {'ce_id':slot.ce_id,'name':equip['name'],'lv':stats.lv,'is_mlb':stats.is_mlb}
            team.append(row)
        mc_id = plan.mystic_code_id
        mc = service.data.get('CN','MC',mc_id) if mc_id else None
        mc_lv = (plan.mystic_code_lv or parser.mystic_codes[mc_id]) if mc_id else 0
        if mc and not 1 <= mc_lv <= mc['maxLv']:
            raise ValueError('御主礼装等级无效')
        data = {'servants':list(servants.values()),'craft_essences':list(ces.values()),'quest':quest,
                'mystic_codes':[mc] if mc else [],'const_data':const_data,
                'base_skills':[json.loads(p.read_text(encoding='utf-8')) for p in (service.data.root/'JP/skill').glob('*.json')]}
        request = {'protocol':4,'mode':'manual','quest_id':entry['id'],'phase':entry['phase'],'quest_data':'quest',
                   'team':team,'mystic_code_id':mc_id,'mystic_code_lv':mc_lv,
                   'manual_turns':[turn.model_dump() for turn in plan.turns]}
        meta = {'quest_id':entry['id'],'phase':entry['phase'],'quest_name':entry['name'],'enemy_region':region,
                'team':team,'mystic_code':{'id':mc_id,'name':mc['name'],'lv':mc_lv} if mc else None,'assumptions':notes}
        return {'request':request,'data':data}, meta

    def run(self, plan, parser, cancel_event=None):
        plan = plan if isinstance(plan,ManualPlan) else ManualPlan.model_validate(plan)
        service = self.service
        cancel_event = cancel_event or threading.Event()
        if 'manual_plan_v1' not in json.loads((service.runtime/'manifest.json').read_text(encoding='utf-8')).get('features',[]):
            raise LaplaceError('请更新支持手动顺序的Laplace运行时')
        while not service._lock.acquire(timeout=.1):
            if cancel_event.is_set():raise LaplaceError('计算已取消')
        try:
            if cancel_event.is_set():raise LaplaceError('计算已取消')
            payload,metadata = self.prepare(plan,parser,cancel_event)
            for attempt in range(4):
                result = service._run_worker(payload,cancel_event)
                missing = result.get('missing_data',[])
                if not missing:break
                if attempt == 3:raise LaplaceError('公开技能依赖仍不完整')
                for url in sorted(set(missing)):
                    match = re.fullmatch(r'https://api\.atlasacademy\.io/nice/(JP|CN)/skill/(\d+)',url)
                    if not match:raise LaplaceError('缺少尚未支持的公开数据')
                    payload['data']['base_skills'].append(service.data.get(match[1],'skill',int(match[2])))
            if cancel_event.is_set():raise LaplaceError('计算已取消')
            if not result.get('success') or result.get('mode') != 'manual':
                raise LaplaceError(result.get('message','手动模拟引擎没有返回有效结果'))
            simulation = result['manual']
            return {**metadata,**simulation,'mode':'manual','engine':'Chaldea Laplace',
                    'input_digest':_digest(payload),'plan':plan.model_dump(),'auto_battle_eligible':False}
        finally:
            service._lock.release()


class ManualPlanStore:
    def __init__(self, root=None):
        self.root = Path(root or get_root_dir()/'.app_profile/manual_plans')

    def list(self):
        if not self.root.exists():return []
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.root.glob('*.json'))
                if re.fullmatch(r'[0-9a-f]{32}',p.stem)]

    def save(self, name, plan, identity=None):
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('方案名称须为1至80个字符')
        plan = ManualPlan.model_validate(plan)
        if identity is not None and (not isinstance(identity,str) or not re.fullmatch(r'[0-9a-f]{32}',identity)):
            raise ValueError('无效方案ID')
        identity = identity or uuid.uuid4().hex
        self.root.mkdir(parents=True,exist_ok=True)
        value = {'id':identity,'name':name.strip(),'version':1,'plan':plan.model_dump()}
        tmp = self.root/(identity+'.tmp')
        tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        replace_file(tmp,self.root/(identity+'.json'))
        return copy.deepcopy(value)
