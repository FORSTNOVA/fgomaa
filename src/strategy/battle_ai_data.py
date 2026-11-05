"""Freeze the public AI graph and its script/skill dependencies for one battle."""
from __future__ import annotations
import copy,json,uuid
from src.core.atomic_io import replace_file
from src.strategy.laplace import LaplaceError


class BattleAIData:
    KINDS={'ai/svt','ai/field','ai-act','battle-script','common-release'}

    def __init__(self,data,region):
        if region not in ('JP','CN'):raise ValueError('AI数据区服无效')
        self.data=data;self.region=region

    def get(self,kind,identity):
        if kind not in self.KINDS or type(identity) is not int or identity<=0:raise ValueError('AI数据ID无效')
        self.data.check_cancelled()
        path=self.data.root/self.region/kind/f'{identity}.json'
        from src.strategy.quest_catalog import catalog_sync
        stale_after=catalog_sync().snapshot().get('enemy_refresh_after',0)
        if path.exists() and path.stat().st_mtime>=stale_after:
            wrapped=json.loads(path.read_text(encoding='utf-8'))
            if wrapped.get('id')!=identity or wrapped.get('kind')!=kind:raise LaplaceError('AI缓存身份不一致')
            value=wrapped['data']
        else:
            raw=self.data.download(f'{self.region}/{kind}/{identity}')
            value=json.loads(raw)
            self.validate(kind,identity,value)
            self.data.check_cancelled();path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            temporary.write_text(json.dumps({'id':identity,'kind':kind,'data':value},ensure_ascii=False),encoding='utf-8')
            replace_file(temporary,path)
        self.validate(kind,identity,value);self.data.check_cancelled()
        return copy.deepcopy(value)

    @staticmethod
    def validate(kind,identity,value):
        if kind.startswith('ai/'):
            if not isinstance(value,dict) or not isinstance(value.get('mainAis'),list) or not isinstance(value.get('relatedAis',[]),list):
                raise LaplaceError('AI集合格式不完整')
            if any(not isinstance(a,dict) or a.get('id')!=identity for a in value['mainAis']):raise LaplaceError('AI集合ID不一致')
        elif kind=='ai-act':
            if not isinstance(value,dict) or value.get('id')!=identity:raise LaplaceError('AI行动ID不一致')
        elif not isinstance(value,list) or any(not isinstance(r,dict) for r in value):raise LaplaceError('脚本/解锁数据格式错误')
        elif kind in ('battle-script','common-release') and any(r.get('id')!=identity for r in value):
            raise LaplaceError('脚本或解锁数据ID不一致')


def freeze_battle_ai(payload,parser,data,region):
    store=BattleAIData(data,region)
    frozen={'collections':{},'scripts':{},'acts':{},'releases':{},'quest_records':copy.deepcopy(parser.quest_records),
            'quest_records_complete':parser.quest_records_complete,'region':region}
    skills={s['id']:s for s in payload['data']['base_skills']}
    pending=[];seen=set();nodes=0

    def enqueue(kind,identity):
        if identity and (kind,identity) not in seen:pending.append((kind,int(identity)))

    def skill(identity,embedded=None):
        if not identity:return
        value=embedded or skills.get(identity) or data.get(region,'skill',identity)
        if value.get('id')!=identity:raise LaplaceError('AI引用技能ID错误')
        skills[identity]=value

    def action(act):
        if not isinstance(act,dict):raise LaplaceError('AI缺少行动定义')
        if act.get('type') in ('skillId','skillIdCheckbuff','gimmickSkill') and not act.get('skillId'):raise LaplaceError('AI技能行动缺少技能ID')
        if act.get('skillId'):skill(act['skillId'],act.get('skill'))
        script=act.get('script',{})
        if not isinstance(script,dict):raise LaplaceError('AI行动脚本格式错误')
        enqueue('battle-script',script.get('battleScriptId',0))

    for stage in payload['data']['quest']['stages']:
        for field in stage.get('fieldAis',[]):enqueue('ai/field',field['id'])
        for enemy in stage['enemies']:
            ai=enemy.get('ai') or {}
            if not 0<=ai.get('maxActNum',1)<=3:raise LaplaceError('特殊敌方行动次数超过当前支持范围')
            enqueue('ai/svt',(enemy.get('ai') or {}).get('aiId',0))
            for num in range(1,4):
                values=enemy.get('skills',{});identity=values.get(f'skillId{num}',0)
                if identity:
                    skill(identity,values.get(f'skill{num}'))
                    values[f'skill{num}']=copy.deepcopy(skills[identity])
    while pending:
        kind,identity=pending.pop(0)
        if (kind,identity) in seen:continue
        if len(seen)>=256:raise LaplaceError('AI依赖图超过256项，不能完整冻结')
        seen.add((kind,identity));value=store.get(kind,identity)
        if kind.startswith('ai/'):
            frozen['collections'][f'{kind[3:]}:{identity}']=value
            rows=value['mainAis']+value.get('relatedAis',[]);nodes+=len(rows)
            if nodes>10000:raise LaplaceError('AI依赖节点过多')
            local_ids={a['id'] for a in rows}
            for row in rows:
                action(row['aiAct'])
                dest=(row.get('avals') or [0])[0]
                if dest and dest not in local_ids:enqueue(kind,dest)
                if row.get('cond')=='commonReleaseId':
                    vals=row.get('vals',[])
                    if vals:enqueue('common-release',vals[0])
        elif kind=='battle-script':
            frozen['scripts'][str(identity)]=value
            for row in value:
                detail=row.get('script') or {}
                enqueue('ai-act',detail.get('aiActId',0))
        elif kind=='ai-act':
            frozen['acts'][str(identity)]=value;action(value)
        else:frozen['releases'][str(identity)]=value
    payload['data']['base_skills']=list(skills.values())
    payload['data']['ai_data']=frozen
    return {'collections':len(frozen['collections']),'nodes':nodes,'scripts':len(frozen['scripts']),'acts':len(frozen['acts'])}
