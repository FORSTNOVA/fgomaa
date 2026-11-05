import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

from src.parser.chaldea_parser import ChaldeaParser, ServantProfile, CraftEssenceProfile
from src.strategy.laplace import LaplaceService
from src.strategy.manual_simulation import (ManualPlan,ManualPlanStore,ManualSimulation,validate_ownership)


def manual_fixture():
    parser=ChaldeaParser()
    ids=[100100,504500,2800100,404200,200100]
    parser.servants={sid:ServantProfile(svt_id=sid,name=str(sid),lv=70,skill_lv1=10,skill_lv2=10,skill_lv3=10) for sid in ids}
    parser.craft_essences={9400340:CraftEssenceProfile(ce_id=9400340,name='万华镜',lv=100,is_mlb=True)}
    parser.mystic_codes={20:10}
    plan={'quest_id':'fuyuki_x_a','party':[
        {'source':'owned','svt_id':100100,'ce_id':9400340},
        {'source':'owned','svt_id':504500},
        {'source':'support','svt_id':504500,'support':{'lv':90,'skills':[10,10,10]}},
        {'source':'owned','svt_id':2800100},
        {'source':'owned','svt_id':404200},
        {'source':'owned','svt_id':200100}],
        'mystic_code_id':20,'turns':[{'actions':[{'kind':'np','actor':1}]},
        {'actions':[{'kind':'skill','actor':2,'skill':1,'target':1},
                    {'kind':'skill','actor':2,'skill':2,'target':1},
                    {'kind':'skill','actor':3,'skill':1,'target':1},
                    {'kind':'skill','actor':3,'skill':2,'target':1},{'kind':'np','actor':1}]}]}
    return parser,plan


class TestManualPlan(unittest.TestCase):
    def setUp(self):self.parser,self.plan=manual_fixture()

    def test_six_slots_and_one_support_are_required(self):
        for plan in [{**self.plan,'party':self.plan['party'][:5]},
                     {**self.plan,'party':self.plan['party']+[self.plan['party'][0]]}]:
            with self.assertRaises(ValueError):ManualPlan.model_validate(plan)
        bad=copy.deepcopy(self.plan);bad['party'][2]={'source':'owned','svt_id':503900}
        with self.assertRaises(ValueError):ManualPlan.model_validate(bad)

    def test_owned_overrides_validate_fields_and_cannot_replace_ownership(self):
        for changes in [{'support':{'lv':120}},{'lv':120},
                        {'owned_stats':{'lv':True}},{'owned_stats':{'skill_lv1':11}},
                        {'owned_stats':{'skill_ids':[123]}}]:
            bad=copy.deepcopy(self.plan);bad['party'][0].update(changes)
            with self.assertRaises(ValueError):ManualPlan.model_validate(bad)
        bad=copy.deepcopy(self.plan);bad['party'][5]['svt_id']=bad['party'][0]['svt_id']
        with self.assertRaises(ValueError):ManualPlan.model_validate(bad)

        self.plan['party'][0]['owned_stats'] = {'lv':90,'np_lv':2,'skill_ids':[None,123,None]}
        validate_ownership(ManualPlan.model_validate(self.plan),self.parser)
        self.plan['party'][0]['svt_id'] = 999
        with self.assertRaises(ValueError):validate_ownership(ManualPlan.model_validate(self.plan),self.parser)

    def test_skill_master_interleaving_and_np_chain_preserved(self):
        actions=[{'kind':'skill','actor':2,'skill':1,'target':1},
                 {'kind':'master','skill':3,'swap':[2,4]},
                 {'kind':'skill','actor':4,'skill':2,'target':1},
                 {'kind':'np','actor':3},{'kind':'np','actor':1}]
        self.plan['turns']=[{'actions':actions}]
        result=ManualPlan.model_validate(self.plan).model_dump()['turns'][0]['actions']
        self.assertEqual([a['kind'] for a in result],['skill','master','skill','np','np'])
        self.assertEqual(result[1]['swap'],[2,4])

    def test_invalid_game_order_and_unconfigured_outfit_rejected(self):
        cases=[[{'kind':'np','actor':1},{'kind':'skill','actor':2,'skill':1}],
               [{'kind':'np','actor':1},{'kind':'np','actor':1}],
               [{'kind':'facecards'},{'kind':'np','actor':1}],
               [{'kind':'skill','actor':2,'skill':1}],
               [{'kind':'np','actor':1,'enemy':1},{'kind':'np','actor':2,'enemy':2}]]
        for actions in cases:
            with self.subTest(actions=actions),self.assertRaises(ValueError):
                ManualPlan.model_validate({**self.plan,'turns':[{'actions':actions}]})
        with self.assertRaises(ValueError):ManualPlan.model_validate({**self.plan,'mystic_code_id':None,
            'turns':[{'actions':[{'kind':'master','skill':1},{'kind':'np','actor':1}]}]})

    def test_strict_levels_and_indices(self):
        for stats in [{'lv':True},{'skills':[10,10]},{'appends':[0]*6},{'np_lv':6},{'hp_fou':2001}]:
            bad=copy.deepcopy(self.plan);bad['party'][2]['support']=stats
            with self.assertRaises(ValueError):ManualPlan.model_validate(bad)
        for action in [{'kind':'skill','actor':7,'skill':1},{'kind':'master','skill':3,'swap':[2,2]},
                       {'kind':'np','actor':1,'target':2}]:
            with self.assertRaises(ValueError):ManualPlan.model_validate({**self.plan,'turns':[{'actions':[action]}]})

    def test_unowned_items_are_rejected(self):
        validate_ownership(ManualPlan.model_validate(self.plan),self.parser)
        for key,value in [('svt_id',999),('ce_id',999)]:
            bad=copy.deepcopy(self.plan);bad['party'][0][key]=value
            with self.assertRaises(ValueError):validate_ownership(ManualPlan.model_validate(bad),self.parser)
        bad=copy.deepcopy(self.plan);bad['mystic_code_id']=30
        with self.assertRaises(ValueError):validate_ownership(ManualPlan.model_validate(bad),self.parser)

    def test_friend_can_borrow_same_ce_as_owned_without_inventory_deduction(self):
        self.plan['party'][2].update(ce_id=9400340,support_ce={'lv':100,'is_mlb':True})
        validate_ownership(ManualPlan.model_validate(self.plan),self.parser)

    def test_saved_plan_round_trip_retains_all_order_and_stats(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ManualPlanStore(folder)
            first=store.save('手动测试',self.plan)
            self.assertEqual(store.list()[0],first)
            changed=copy.deepcopy(self.plan);changed['party'][2]['support']['np_lv']=5
            value=store.save('更新',changed,first['id'])
            self.assertEqual(len(store.list()),1)
            self.assertEqual(store.list()[0]['plan']['party'][2]['support']['np_lv'],5)
            self.assertEqual(value['plan']['turns'],ManualPlan.model_validate(changed).model_dump()['turns'])
            for identity in ['../escape','C:/escape','abc']:
                with self.assertRaises(ValueError):store.save('invalid',self.plan,identity)


class TestManualPreparation(unittest.TestCase):
    def setUp(self):
        self.parser,self.plan=manual_fixture();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=MagicMock();self.service.data.root=Path(self.tmp.name)
        self.service._load_quest.return_value=({'id':93000001,'phase':3,'name':'宅邸残迹'}, {'id':93000001},'JP')
        def get(region,kind,identity,*args,lore=False):
            if kind=='servant':
                if identity==800100:
                    return {'id':identity,'name':'玛修','atkGrowth':[100]*80,'hpGrowth':[200]*80,
                            'profile':{'costume':{'800150':{'id':13,'battleCharaId':800150,'name':'奥特瑙斯'},
                                                  '800190':{'id':17,'battleCharaId':800190,'name':'圣骑士'}}},
                            'ascensionAdd':{'overwriteExpType':{'costume':{'17':5}}}}
                return {'id':identity,'name':str(identity),'atkGrowth':[100]*120,'hpGrowth':[200]*120}
            if kind=='equip':return {'id':identity,'name':'万华镜','lvMax':100,'rarity':5}
            return {'id':identity,'name':'战斗服','maxLv':10}
        self.service.data.get.side_effect=get
        p=patch('src.strategy.manual_simulation.resolve_versions',return_value=({'skill_ids':[1,2,3],'np_id':4,'ascension':4},[]))
        p.start();self.addCleanup(p.stop)

    def test_mash_paladin_uses_form_growth_and_battle_identity(self):
        self.plan['party'][5]['svt_id']=800100
        mash=ServantProfile(svt_id=800100,name='玛修',lv=102,ascension=4,costume_id=17)
        self.parser.servants[800100]=mash
        self.service.data.constants.return_value={'svtExp':{'5':{'lv':list(range(121)),'curve':[0]*121}}}
        plan=ManualPlan.model_validate(self.plan)
        payload,_=ManualSimulation(self.service).prepare(plan,self.parser)
        team=payload['request']['team']
        self.assertEqual((team[5]['lv'],team[5]['ascension'],team[5]['battle_chara_id']),(102,4,800190))
        self.service.data.get.assert_any_call('CN','servant',800100,lore=True)
        mash.costume_id=None
        with self.assertRaisesRegex(ValueError,'玛修.*Lv.102'):
            ManualSimulation(self.service).prepare(plan,self.parser)
        mash.lv=80
        payload,_=ManualSimulation(self.service).prepare(plan,self.parser)
        self.assertNotIn('battle_chara_id',payload['request']['team'][5])

    def test_explicit_mash_form_uses_matching_versions_without_mutating_the_account(self):
        from src.strategy.interactive_simulation import SetupDraft
        from src.strategy.manual_simulation import resolve_versions
        mash=ServantProfile(svt_id=800100,name='玛修',lv=102,ascension=4,costume_id=17,skill_ids=[9,8,7],np_id=6)
        self.parser.servants[800100]=mash
        self.service.data.constants.return_value={'svtExp':{'5':{'lv':list(range(121)),'curve':[0]*121}}}
        for form,lv,chara,skills,np in (
            ('ortinax',80,800150,[744450,460250,2162350],800106),
            ('paladin',102,800190,[2476450,2550,133550],800107),
        ):
            with self.subTest(form=form):
                self.plan['party'][5]={'svt_id':800100,'mash_form':form,'owned_stats':{'skill_ids':[1,2,3],'np_id':4}}
                draft=SetupDraft.model_validate({k:v for k,v in self.plan.items() if k!='turns'})
                reloaded=SetupDraft.model_validate_json(draft.model_dump_json())
                self.assertEqual(reloaded.party[5].mash_form,form)
                payload,_=ManualSimulation(self.service).prepare(reloaded.ready_plan(),self.parser)
                row=payload['request']['team'][5]
                self.assertEqual((row['lv'],row['battle_chara_id']),(lv,chara))
                resolved_profile=resolve_versions.call_args.args[1]
                self.assertEqual((resolved_profile.skill_ids,resolved_profile.np_id),(skills,np))
        self.assertEqual((mash.lv,mash.costume_id,mash.skill_ids,mash.np_id),(102,17,[9,8,7],6))
        self.plan['party'][5]['svt_id']=200100
        with self.assertRaisesRegex(ValueError,'仅自有玛修'):
            ManualPlan.model_validate(self.plan)

    def test_exact_six_owned_levels_custom_support_and_backline_equipment(self):
        self.plan['party'][2].update(support={'lv':120,'np_lv':5,'skills':[9,8,7],'appends':[1,2,3,4,5]},ce_id=9400340,support_ce={'lv':80,'is_mlb':False})
        payload,meta=ManualSimulation(self.service).prepare(ManualPlan.model_validate(self.plan),self.parser)
        team=payload['request']['team']
        self.assertEqual(len(team),6);self.assertEqual([s['party_slot'] for s in team],[1,2,3,4,5,6])
        self.assertEqual(team[0]['lv'],70);self.assertEqual((team[2]['lv'],team[2]['np_lv']),(120,5))
        self.assertEqual(team[2]['append_levels'],[1,2,3,4,5]);self.assertTrue(team[2]['is_friend'])
        self.assertEqual(team[2]['ce']['lv'],80);self.assertFalse(team[2]['ce']['is_mlb'])
        self.assertEqual(payload['request']['manual_turns'],ManualPlan.model_validate(self.plan).model_dump()['turns'])
        self.assertEqual(payload['request']['mystic_code_lv'],10)
        self.assertEqual(meta['enemy_region'],'JP')

    def test_cancel_before_data_does_not_build_team(self):
        event=threading.Event();event.set()
        with self.assertRaisesRegex(Exception,'取消'):
            ManualSimulation(self.service).prepare(ManualPlan.model_validate(self.plan),self.parser,event)
        self.service.data.get.assert_not_called()

    def test_owned_overrides_enter_payload_without_mutating_account_or_other_slots(self):
        self.parser.servants[100100].skill_ids=[1,2,3]
        self.plan['party'][0].update(owned_stats={'lv':90,'np_lv':2,'skill_lv2':7,'append_skill_lv2':10,
            'atk_fou':1000,'skill_ids':[None,99,None]},owned_ce={'lv':80,'is_mlb':False})
        self.plan['mystic_code_lv']=8
        payload,_=ManualSimulation(self.service).prepare(ManualPlan.model_validate(self.plan),self.parser)
        request=payload['request']; row=request['team'][0]
        self.assertEqual((row['lv'],row['np_lv'],row['skill_lv2'],row['atk_fou']),(90,2,7,1000))
        self.assertEqual((row['append_levels'][1],row['ce']['lv'],row['ce']['is_mlb']),(10,80,False))
        self.assertEqual(request['mystic_code_lv'],8)
        self.assertEqual(request['team'][1]['lv'],70)
        self.assertEqual(self.parser.servants[100100].lv,70)
        self.assertEqual(self.parser.craft_essences[9400340].lv,100)
        from src.strategy.manual_simulation import resolve_versions
        self.assertEqual(resolve_versions.call_args_list[0].args[1].skill_ids,[1,99,3])


class TestManualNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser,cls.plan=manual_fixture()
        cls.service=LaplaceService(jobs=Path('scratch/manual_simulation_20261103/test_jobs'))

    def test_native_exact_order_swap_and_six_people(self):
        plan=copy.deepcopy(self.plan)
        plan['turns'][0]['actions']=[{'kind':'skill','actor':2,'skill':1},
            {'kind':'master','skill':3,'swap':[2,4]}, {'kind':'skill','actor':4,'skill':1}, {'kind':'np','actor':1}]
        # Second turn uses the remaining front-line support and Oberon.
        plan['turns'][1]['actions']=[{'kind':'skill','actor':3,'skill':1,'target':1},
            {'kind':'skill','actor':3,'skill':2,'target':1},
            {'kind':'skill','actor':4,'skill':2,'target':1},{'kind':'np','actor':1}]
        result=ManualSimulation(self.service).run(plan,self.parser)
        self.assertTrue(result['passed'],result['message'])
        self.assertEqual(len(result['team']),6);self.assertFalse(result['auto_battle_eligible'])
        first=[a for a in result['actions'] if a['turn']==1]
        self.assertEqual([a['kind'] for a in first],['skill','master','skill','np'])
        self.assertEqual([a['actor'] for a in first if a['kind']=='skill'],[2,4])

    def test_native_owned_level_and_np_override_changes_real_damage(self):
        plan=copy.deepcopy(self.plan)
        plan['turns']=[{'actions':[{'kind':'np','actor':1}]}]
        service=ManualSimulation(self.service)
        before=service.run(plan,self.parser)
        plan['party'][0]['owned_stats']={'lv':90,'np_lv':2,'atk_fou':1000}
        after=service.run(plan,self.parser)
        self.assertEqual(after['team'][0]['lv'],90)
        self.assertGreater(after['steps'][0]['damage'][0]['damage'],before['steps'][0]['damage'][0]['damage'])

    def test_native_cooldown_and_missing_np_never_get_auto_repaired(self):
        for actions,reason in [([{'kind':'skill','actor':2,'skill':1},{'kind':'skill','actor':2,'skill':1},{'kind':'np','actor':1}],'冷却'),
                               ([{'kind':'np','actor':2}],'NP不足'),
                               ([{'kind':'skill','actor':4,'skill':1},{'kind':'np','actor':1}],'不在前排')]:
            with self.subTest(reason=reason):
                result=ManualSimulation(self.service).run({**self.plan,'turns':[{'actions':actions}]},self.parser)
                self.assertFalse(result['passed']);self.assertIn(reason,result['message'])

    def test_native_support_np_chain_keeps_selected_order(self):
        plan=copy.deepcopy(self.plan)
        plan['party'][2].update(ce_id=9400340,support_ce={'lv':100,'is_mlb':True})
        plan['turns'][0]['actions']=[{'kind':'np','actor':3},{'kind':'np','actor':1}]
        result=ManualSimulation(self.service).run(plan,self.parser)
        self.assertTrue(result['passed'],result['message'])
        self.assertEqual([s['slot'] for s in result['steps'][0]['np_before']],[3,1])

    def test_ce_change_changes_bound_digest_and_result(self):
        first=ManualSimulation(self.service).run(self.plan,self.parser)
        plan=copy.deepcopy(self.plan);plan['party'][0]['ce_id']=None
        changed=ManualSimulation(self.service).run(plan,self.parser)
        self.assertTrue(first['passed'],first['message']);self.assertFalse(changed['passed'])
        self.assertNotEqual(first['input_digest'],changed['input_digest'])

    def test_sleeping_actor_cannot_cast_skill_or_select_np(self):
        plan=copy.deepcopy(self.plan)
        plan['turns'][0]['actions']=[{'kind':'master','skill':3,'swap':[2,4]},
            {'kind':'skill','actor':4,'skill':3,'target':1},{'kind':'np','actor':1}]
        for kind in ['skill','np']:
            with self.subTest(kind=kind):
                if kind=='skill':
                    actions=[{'kind':'skill','actor':1,'skill':1},{'kind':'np','actor':1}]
                else:
                    actions=[{'kind':'skill','actor':3,'skill':1},
                        {'kind':'skill','actor':3,'skill':2,'target':1},
                        {'kind':'skill','actor':4,'skill':2,'target':1},{'kind':'np','actor':1}]
                plan['turns'][1]['actions']=actions
                result=ManualSimulation(self.service).run(plan,self.parser)
                self.assertFalse(result['passed'])
                self.assertIn('无法行动' if kind=='skill' else '不可用',result['message'])


if __name__=='__main__':unittest.main()
