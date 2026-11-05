import copy,importlib.util,json,ssl,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
from urllib.error import URLError
from src.strategy.battle_ai_data import BattleAIData,freeze_battle_ai
from src.strategy.interactive_simulation import InteractiveSimulation
from src.strategy.laplace import LaplaceError,PublicData
from tests.test_manual_simulation import manual_fixture

ROOT_AI=90000001;FIELD_AI=80000001;SCRIPT=70000001;ACT=60000001


def node(idx=1,kind='attack',*,group=ROOT_AI,condition='none',vals=None,priority=1,weight=100,timing=-1,next_id=0,act=None):
    return {'id':group,'idx':idx,'actNumInt':timing,'actNum':'anytime','priority':priority,'probability':weight,
        'cond':condition,'condNegative':False,'vals':vals or [],'avals':[next_id,0],
        'aiAct':act or {'id':1002,'type':kind,'target':'random','targetIndividuality':[],'script':{}}}


class TestBattleAIData(unittest.TestCase):
    def test_public_cache_identity_and_schema_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            data=PublicData(root=directory);store=BattleAIData(data,'JP')
            response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'mainAis':[node(group=123)],'relatedAis':[]}).encode()
            eof=URLError(ssl.SSLEOFError(8,'UNEXPECTED_EOF_WHILE_READING'))
            with (patch('urllib.request.urlopen',side_effect=[eof,response]) as request,
                 patch('src.strategy.laplace.time.sleep'),
                 patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(snapshot=lambda:{}))):
                first=store.get('ai/svt',123);second=store.get('ai/svt',123)
                self.assertEqual(first,second);self.assertEqual(request.call_count,2)
            file=Path(directory)/'JP/ai/svt/123.json';saved=json.loads(file.read_text(encoding='utf-8'));saved['id']=124
            file.write_text(json.dumps(saved),encoding='utf-8')
            with self.assertRaisesRegex(LaplaceError,'身份'):store.get('ai/svt',123)
            with self.assertRaises(ValueError):store.get('../secrets',123)
        for value in [{}, {'mainAis':[node(group=124)]}]:
            with self.assertRaises(LaplaceError):BattleAIData.validate('ai/svt',123,value)

    def test_cancelled_fetch_and_script_graph_cycle_are_bounded(self):
        data=MagicMock();data.check_cancelled.side_effect=LaplaceError('cancelled')
        with self.assertRaisesRegex(LaplaceError,'cancelled'):BattleAIData(data,'JP').get('ai/svt',123)
        payload={'data':{'base_skills':[],'quest':{'stages':[{'fieldAis':[{'id':FIELD_AI}],'enemies':[]}]}}}
        collection={'mainAis':[node(group=FIELD_AI,act={'id':1,'type':'battleScript','script':{'battleScriptId':SCRIPT}})],'relatedAis':[]}
        definitions={('ai/field',FIELD_AI):collection,('battle-script',SCRIPT):[{'id':SCRIPT,'idx':1,'battleScriptAction':'aiAct','script':{'aiActId':ACT}}],
            ('ai-act',ACT):{'id':ACT,'type':'battleScript','script':{'battleScriptId':SCRIPT}}}
        with patch.object(BattleAIData,'get',side_effect=lambda kind,identity:copy.deepcopy(definitions[kind,identity])):
            summary=freeze_battle_ai(payload,SimpleNamespace(quest_records={},quest_records_complete=False),MagicMock(),'JP')
        self.assertEqual(summary['scripts'],1);self.assertEqual(summary['acts'],1)

    def test_engine_hooks_apply_to_clean_upstream_and_are_idempotent(self):
        source=Path('scratch/laplace_trial/chaldea')
        if not (source/'.git').exists():self.skipTest('prepared source checkout unavailable')
        spec=importlib.util.spec_from_file_location('engine_patches',Path('integrations/laplace/engine_patches.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        names=['battle/functions/damage.dart','battle/models/battle.dart','battle/models/ai.dart',
               'battle/models/svt_entity.dart','battle/functions/function_executor.dart','api/atlas.dart']
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            for name in names:
                raw=subprocess.check_output(['git','show','HEAD:lib/app/'+name],cwd=source)
                path=target/'lib/app'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            module.apply_engine_patches(target)
            once={name:(target/'lib/app'/name).read_bytes() for name in names}
            module.apply_engine_patches(target)
            self.assertEqual(once,{name:(target/'lib/app'/name).read_bytes() for name in names})
            self.assertIn(b'damageRoll',once['battle/functions/damage.dart'])


class TestBattleAINative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.parser,plan=manual_fixture();cls.draft={k:v for k,v in plan.items() if k!='turns'}
        cls.engine=InteractiveSimulation(root=cls.tmp.name)
        cls.entry,cls.quest,cls.region=cls.engine.service._load_quest('fuyuki_x_a',None)
        servant=cls.engine.service.data.get('CN','servant',100100)
        cls.skill=next(s for s in servant['skills'] if s.get('num')==1)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def make(self,rows=None,related=None,*,field=None,scripts=None,acts=None,seed=123,mutate=None,real=False):
        quest=copy.deepcopy(self.quest)
        for stage in quest['stages']:
            for enemy in stage['enemies']:
                enemy['hp']=500000;enemy['atk']=3000
                if not real:enemy['ai']['aiId']=ROOT_AI
                enemy['skills'].update(skillId1=self.skill['id'],skill1=copy.deepcopy(self.skill),skillLv1=10)
        definitions={('ai/svt',ROOT_AI):{'mainAis':rows or [node()],'relatedAis':related or []}}
        if field:
            quest['stages'][0]['fieldAis']=[{'id':FIELD_AI}]
            definitions['ai/field',FIELD_AI]={'mainAis':field,'relatedAis':[]}
        for identity,value in (scripts or {}).items():definitions['battle-script',identity]=value
        for identity,value in (acts or {}).items():definitions['ai-act',identity]=value
        if mutate:mutate(quest)
        with patch.object(self.engine.service,'_load_quest',return_value=(self.entry,quest,self.region)):
            if real:return self.engine.enter(self.draft,self.parser,seed=seed)
            with patch.object(BattleAIData,'get',side_effect=lambda kind,identity:copy.deepcopy(definitions[kind,identity])):
                return self.engine.enter(self.draft,self.parser,seed=seed)

    def replay(self,start,events):return self.engine.replay(start['session_id'],events)
    def turn(self):return [{'kind':'cards'},{'kind':'card','card':1},{'kind':'np','actor':1},{'kind':'card','card':2},{'kind':'attack'}]
    def normal_turn(self):return [{'kind':'cards'},*[{'kind':'card','card':i} for i in [1,2,3]],{'kind':'attack'}]
    def skill_act(self):return {'id':self.skill['id'],'type':'skillId','target':'random','targetIndividuality':[],
        'skillId':self.skill['id'],'skillLv':10,'skill':copy.deepcopy(self.skill),'script':{}}

    def test_random_damage_per_target_replays_and_does_not_change_v2(self):
        start=self.make();events=self.turn()+[{'kind':'enemy_turn'}]
        one=self.replay(start,events);two=self.replay(start,events)
        self.assertTrue(one['scene']['accepted']);self.assertEqual(one['scene'],two['scene'])
        rolls=one['scene']['damage_rolls'];self.assertGreaterEqual(len(rolls),4)
        self.assertTrue(all(900<=r['value']<=1099 for r in rolls));self.assertGreater(len({r['value'] for r in rolls}),1)
        self.assertTrue(all(900<=d['random_milli']<=1099 for a in one['scene']['recent_attacks'] for d in a['damage']))
        another=self.make(seed=124);other=self.replay(another,self.normal_turn()+[{'kind':'enemy_turn'}])
        self.assertNotEqual(rolls,other['scene']['damage_rolls'])

    def test_aoe_rolls_independently_for_each_target(self):
        def mutate(q):
            enemy=q['stages'][0]['enemies'][0]
            for i in [2,3]:
                clone=copy.deepcopy(enemy);clone['npcId']=i;clone['deckId']=i;q['stages'][0]['enemies'].append(clone)
        start=self.make(mutate=mutate);result=self.replay(start,self.turn())
        self.assertTrue(result['scene']['accepted'])
        np=[r for r in result['scene']['last_attack']['damage'] if r['is_np']]
        self.assertEqual(len(np),3);self.assertGreater(len({r['random_milli'] for r in np}),1)

    def test_enemy_skills_thinking_links_and_counter_conditions(self):
        rows=[node(),node(2,'changeThinking',condition='actcountThisturn',vals=[0],priority=100,next_id=ROOT_AI+1)]
        related=[node(group=ROOT_AI+1,kind='skill1',next_id=ROOT_AI+2),node(group=ROOT_AI+2,next_id=ROOT_AI)]
        def mutate(q):q['stages'][0]['enemies'][0]['ai']['maxActNum']=3
        start=self.make(rows,related,mutate=mutate);events=self.turn()+[{'kind':'enemy_step'}]
        skill=self.replay(start,events);self.assertTrue(skill['scene']['accepted'])
        self.assertTrue(any(r['type']=='thinking' for r in skill['scene']['ai_trace']))
        self.assertTrue(any(r['type']=='skill' and r['activated'] for r in skill['scene']['ai_trace']))
        self.assertTrue(any(b['type']=='upAtk' for b in skill['scene']['enemies'][0]['buffs']))
        next_step=self.replay(start,events+[{'kind':'enemy_step'}]);self.assertEqual(next_step['scene']['last_attack']['action'],'attack')
        self.assertEqual(skill['scene'],self.replay(start,events)['scene'])

    def test_hp_branch_negation_and_unknown_condition_fail_closed(self):
        rows=[node(),node(2,kind='skill1',condition='hpLower',vals=[999],priority=100)]
        start=self.make(rows);result=self.replay(start,self.turn()+[{'kind':'enemy_step'}])
        self.assertEqual(result['scene']['last_attack']['action'],'skill1')
        bad=node(condition='raidHpHigher',vals=[100]);bad['condNegative']=True
        unsupported=self.make([bad]);failure=self.replay(unsupported,self.turn()+[{'kind':'enemy_step'}])
        self.assertFalse(failure['scene']['accepted']);self.assertIn('raidHpHigher',failure['failure']['message'])

    def test_field_turn_start_runs_after_first_turn_and_player_skill_reaction(self):
        field=[node(group=FIELD_AI,act=self.skill_act(),condition='turnHigher',vals=[2])];field[0]['timing']=2
        rows=[node(),node(2,act=self.skill_act(),timing=-3,priority=100)]
        start=self.make(rows,field=field)
        self.assertFalse(start['scene']['ai_trace'])
        reacted=self.replay(start,[{'kind':'skill','actor':2,'skill':1,'target':1}])
        self.assertTrue(any(r['timing']=='player_skill' and r['type']=='skill' for r in reacted['scene']['ai_trace']))
        turn2=self.replay(start,self.turn()+[{'kind':'enemy_turn'}])
        self.assertTrue(any(r['timing']=='field:turnStart' and r['type']=='skill' for r in turn2['scene']['ai_trace']))

    def test_battle_script_sequence_skill_and_freeze_survive_reload_offline(self):
        field=[node(group=FIELD_AI,act={'id':1,'type':'battleScript','script':{'battleScriptId':SCRIPT}})];field[0]['timing']=1
        scripts={SCRIPT:[{'id':SCRIPT,'idx':2,'playOrder':2,'battleScriptAction':'message','script':{'messageText':'test'}},
            {'id':SCRIPT,'idx':1,'playOrder':1,'battleScriptAction':'aiAct','script':{'aiActId':ACT,'type':2,'individuality':5000}}]}
        start=self.make(field=field,scripts=scripts,acts={ACT:self.skill_act()})
        trace=start['scene']['ai_trace'];self.assertEqual([r['idx'] for r in trace if r['type']=='script'],[1,2])
        self.assertTrue(any(r['type']=='skill' for r in trace))
        with (patch.object(BattleAIData,'get',side_effect=AssertionError('AI must be frozen')),
             patch.object(self.engine.service.data,'get',side_effect=AssertionError('public dependency must be frozen'))):
            self.assertEqual(self.replay(start,[])['scene'],start['scene'])

    def test_battle_values_drive_ai_condition(self):
        value_skill=copy.deepcopy(self.skill);value_skill['id']=7770001;value_skill['name']='设置战斗变量'
        value_skill['functions']=[{'funcId':7770001,'funcType':'setBattleValue','funcTargetType':'ptAll','funcTargetTeam':'playerAndEnemy',
            'traitVals':[7],'svals':[{'Rate':1000,'Value':3} for _ in range(10)]}]
        act={'id':ACT,'type':'skillId','target':'random','skillId':value_skill['id'],'skillLv':1,'skill':value_skill,'script':{}}
        field=[node(group=FIELD_AI,act=act)];field[0]['timing']=1
        start=self.make([node(),node(2,'skill1',condition='checkBattleValueEqual',vals=[7,3],priority=100)],field=field)
        self.assertEqual(start['scene']['battle_values'].get('7'),3)
        result=self.replay(start,self.turn()+[{'kind':'enemy_step'}]);self.assertEqual(result['scene']['last_attack']['action'],'skill1')

    def test_break_bar_reaction_uses_next_enemy_ai(self):
        rows=[node(),node(2,act=self.skill_act(),timing=-17,priority=100)]
        def mutate(q):
            enemy=q['stages'][0]['enemies'][0];enemy['hp']=100;enemy['enemyScript']={'shift':[2]};enemy['originalEnemyScript']={'shift':[2]}
            shift=copy.deepcopy(enemy);shift['npcId']=2;shift['deck']='shift';shift['hp']=500000;shift['enemyScript']={}
            shift['originalEnemyScript']={};q['stages'][0]['enemies'].append(shift)
        start=self.make(rows,mutate=mutate);result=self.replay(start,self.turn())
        self.assertTrue(result['scene']['accepted'],result.get('failure'))
        self.assertEqual(result['scene']['phase'],'enemy')
        self.assertTrue(any(r['timing']=='shift_after' and r['type']=='skill' for r in result['scene']['ai_trace']))
        self.assertGreater(result['scene']['enemies'][0]['hp'],100)

    def test_script_cycle_and_unknown_command_stop_instead_of_skipping(self):
        field=[node(group=FIELD_AI,act={'id':1,'type':'battleScript','script':{'battleScriptId':SCRIPT}})];field[0]['timing']=1
        scripts={SCRIPT:[{'id':SCRIPT,'idx':1,'battleScriptAction':'aiAct','script':{'aiActId':ACT,'type':1}}]}
        with self.assertRaisesRegex(LaplaceError,'循环'):
            self.make(field=field,scripts=scripts,acts={ACT:{'id':ACT,'type':'battleScript','script':{'battleScriptId':SCRIPT}}})
        with self.assertRaisesRegex(LaplaceError,'脚本命令'):
            self.make(field=field,scripts={SCRIPT:[{'id':SCRIPT,'idx':1,'battleScriptAction':'unknown','script':{}}]})

    def test_save_cursor_and_redo_keep_random_and_ai_history(self):
        start=self.make();events=self.turn()+[{'kind':'enemy_step'}]
        entries=[{'kind':'enter','label':'进场','draft':start['draft'],'session_id':start['session_id'],'scene':start['scene']}]
        for i,e in enumerate(events):entries.append({'kind':'event','label':e['kind'],'event':e,'scene':self.replay(start,events[:i+1])['scene']})
        saved=self.engine.save_recording({'name':'AI随机录制','entries':entries,'cursor':len(entries)-2})
        loaded=next(r for r in self.engine.recordings() if r['id']==saved['id'])
        reloaded=InteractiveSimulation(root=self.tmp.name).replay(start['session_id'],events)
        self.assertEqual(reloaded['scene'],loaded['entries'][-1]['scene'])
        self.assertEqual(loaded['cursor'],len(entries)-2)

    def test_script_forced_win_or_loss_is_not_confused_with_empty_field(self):
        for kind,phase in [('battleEnd','won'),('loseEnd','lost')]:
            with self.subTest(kind=kind):
                start=self.make([node(),node(2,kind=kind,timing=-3,priority=100)])
                result=self.replay(start,[{'kind':'skill','actor':2,'skill':1,'target':1}])
                self.assertTrue(result['scene']['accepted'],result.get('failure'))
                self.assertEqual(result['scene']['phase'],phase)
                self.assertEqual(result['scene']['won'],phase=='won')
                self.assertEqual(result['scene']['lost'],phase=='lost')

    def test_unknown_timing_stops_and_total_skill_count_survives_turn_change(self):
        with self.assertRaisesRegex(LaplaceError,'触发时机'):
            self.make([node(timing=-999)])
        rows=[node(),node(2,act=self.skill_act(),condition='countPlayerSkillEqual',vals=[1],timing=-12,priority=100)]
        start=self.make(rows)
        events=[{'kind':'skill','actor':2,'skill':1,'target':1}]+self.turn()+[{'kind':'enemy_turn'}]
        result=self.replay(start,events)
        self.assertTrue(result['scene']['accepted'],result.get('failure'))
        self.assertTrue(any(r['type']=='skill' and r['timing']=='turn_start' and r['turn']==2 for r in result['scene']['ai_trace']))

    def test_actual_public_ai_executes_and_is_pinned(self):
        start=self.make(real=True);result=self.replay(start,self.turn()+[{'kind':'enemy_step'}])
        self.assertTrue(result['scene']['accepted'])
        self.assertTrue(any(r.get('node','').startswith('1000000:') for r in result['scene']['ai_trace']))
        self.assertEqual(self.replay(start,self.turn()+[{'kind':'enemy_step'}])['scene'],result['scene'])

    def test_actual_public_thinking_graph_traverses_related_nodes(self):
        def mutate(q):
            for stage in q['stages']:
                for e in stage['enemies']:e['ai']['aiId']=105370
        start=self.make(real=True,mutate=mutate)
        result=self.replay(start,self.turn()+[{'kind':'enemy_step'}])
        self.assertTrue(result['scene']['accepted'])
        trace=result['scene']['ai_trace']
        self.assertTrue(any(r['type']=='thinking' and r.get('node')=='105370:2' for r in trace))
        self.assertTrue(any(r.get('node','').startswith('105371:') for r in trace))


if __name__=='__main__':unittest.main()
