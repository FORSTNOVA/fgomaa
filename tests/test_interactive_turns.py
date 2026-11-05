import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src.strategy.interactive_simulation import InteractiveSimulation,compile_events
from tests.test_manual_simulation import manual_fixture


class TestCardEventValidation(unittest.TestCase):
    def test_mixed_chain_and_removing_last_selection_keep_order(self):
        events=[{'kind':'cards'},{'kind':'card','card':2},{'kind':'np','actor':1},
            {'kind':'card','card':3},{'kind':'unselect'},{'kind':'card','card':1},{'kind':'attack'},
            {'kind':'enemy_step'},{'kind':'enemy_turn'},{'kind':'skill','actor':2,'skill':1}]
        value=compile_events(events,version=2)
        self.assertEqual(value['interactive_events'],events)
        self.assertEqual(value['interactive_version'],2)

    def test_three_different_cards_required_and_legacy_stays_legacy(self):
        cases=[[],[{'kind':'card','card':1}],
            [{'kind':'card','card':1},{'kind':'card','card':1},{'kind':'card','card':2}],
            [{'kind':'np','actor':1},{'kind':'np','actor':1},{'kind':'card','card':2}]]
        for choices in cases:
            with self.subTest(choices=choices),self.assertRaises(ValueError):
                compile_events([{'kind':'cards'},*choices,{'kind':'attack'}],version=2)
        for events in [[{'kind':'card','card':1}],[{'kind':'enemy_turn'}],
            [{'kind':'cards'},{'kind':'card','card':6}],[{'kind':'cards'},{'kind':'facecards'}]]:
            with self.assertRaises(ValueError):compile_events(events,version=2)
        with self.assertRaises(ValueError):compile_events([{'kind':'cards'},{'kind':'card','card':1}])
        self.assertIn('manual_turns',compile_events([{'kind':'cards'},{'kind':'np','actor':1},{'kind':'attack'}]))


class TestInteractiveTurnsNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.parser,plan=manual_fixture()
        cls.draft={k:v for k,v in plan.items() if k!='turns'}
        cls.engine=InteractiveSimulation(root=cls.tmp.name)
        cls.entry,cls.quest,cls.region=cls.engine.service._load_quest(cls.draft['quest_id'],None)
        cls.start=cls.new_session()

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    @classmethod
    def new_session(cls,seed=123,atk=3000,max_acts=1,enemy_np=False,empty_np=False):
        quest=copy.deepcopy(cls.quest)
        if enemy_np:
            servant=cls.engine.service.data.get('CN','servant',100100)
            np=copy.deepcopy(servant['noblePhantasms'][0])
            # A damage-only charge attack; Saber's real NP also restores her NP.
            np['functions']=[f for f in np['functions'] if f.get('funcType','').startswith('damageNp')]
            if empty_np:np['functions']=[]
        for stage in quest['stages']:
            for e in stage['enemies']:
                e['hp']=500000;e['atk']=atk;e['ai']['maxActNum']=max_acts
                if enemy_np:
                    e['chargeTurn']=1;e['noblePhantasm']={'noblePhantasmId':np['id'],'noblePhantasm':np,
                        'noblePhantasmLv':1,'noblePhantasmLv1':1}
        # Explicit synthetic durability/attack fixture, never a live battle payload.
        with patch.object(cls.engine.service,'_load_quest',return_value=(cls.entry,quest,cls.region)):
            return cls.engine.enter(cls.draft,cls.parser,seed=seed,version=2)

    def replay(self,events,start=None):return self.engine.replay((start or self.start)['session_id'],events)
    def card_turn(self,cards=(1,2,3)):
        return [{'kind':'cards'},*[{'kind':'card','card':i} for i in cards],{'kind':'attack'}]
    def player(self,result,slot):return next(p for p in result['scene']['players'] if p['slot']==slot)

    def test_seeded_five_card_hand_and_reopening_do_not_reroll(self):
        scene=self.start['scene'];self.assertEqual(scene['version'],2)
        self.assertEqual(len(scene['hand']),5);self.assertEqual(len({c['deck_position'] for c in scene['hand']}),5)
        self.assertEqual(scene['deck_remaining'],10)
        result=self.replay([{'kind':'cards'},{'kind':'card','card':1},{'kind':'back'},{'kind':'cards'}])
        self.assertEqual(result['scene']['hand'],scene['hand'])
        self.assertNotEqual(self.new_session(seed=124)['scene']['hand'],scene['hand'])
        self.assertEqual(self.replay([])['scene'],scene)

    def test_same_owned_essence_uses_each_slots_level_and_limit_break(self):
        draft=copy.deepcopy(self.draft)
        draft['party'][1].update(ce_id=9400340,owned_ce={'lv':80,'is_mlb':False})
        start=self.engine.enter(draft,self.parser,seed=123)
        self.assertEqual(self.player(start,1)['np'],100)
        self.assertEqual(self.player(start,2)['np'],80)
        session=json.loads((self.engine.root/'sessions'/(start['session_id']+'.json')).read_bytes())
        team=session['payload']['request']['team']
        self.assertEqual([(row['ce']['lv'],row['ce']['is_mlb']) for row in team[:2]],[(100,True),(80,False)])
        self.assertEqual(self.replay([],start)['scene'],start['scene'])

    def test_mixed_normal_np_order_damage_refund_and_undo(self):
        events=[{'kind':'cards'},{'kind':'card','card':1},{'kind':'np','actor':1},
            {'kind':'card','card':2},{'kind':'attack'}]
        after=self.replay(events);self.assertTrue(after['scene']['accepted'])
        self.assertEqual(after['scene']['phase'],'enemy')
        rows=after['scene']['last_attack']['damage']
        self.assertEqual([r['actor_slot'] for r in rows],[2,1,1])
        self.assertEqual([r['is_np'] for r in rows],[False,True,False])
        self.assertGreater(rows[-1]['np_gain'],0)
        before=self.replay(events[:-1]);self.assertEqual(before['scene']['phase'],'cards')
        self.assertEqual(len(before['scene']['selected_cards']),3)
        self.assertEqual(self.player(before,1)['np'],100)
        self.assertEqual(after['scene'],self.replay(events)['scene'])

    def test_star_allocation_and_quick_first_card_bonus_are_reversible(self):
        events=[{'kind':'skill','actor':1,'skill':3}]
        star=self.replay(events);self.assertTrue(star['scene']['accepted'])
        stars=int(star['scene']['stars']);self.assertGreater(stars,0)
        chances=[c['critical_chance'] for c in star['scene']['hand']]
        self.assertEqual(sum(chances),min(50,stars)*10)
        quick=next(c for c in star['scene']['hand'] if c['type']=='quick')
        events += [{'kind':'cards'},{'kind':'card','card':quick['index']}]
        selected=self.replay(events)
        self.assertEqual([c['critical_chance'] for c in selected['scene']['hand']],[min(100,chance+20) for chance in chances])
        back=self.replay(events+[{'kind':'back'}])
        self.assertEqual(back['scene']['hand'],star['scene']['hand'])
        self.assertEqual(star['scene'],self.replay(events[:1])['scene'])

    def test_same_actor_mixed_brave_chain_adds_real_extra_card(self):
        events=[{'kind':'cards'},{'kind':'card','card':2},{'kind':'np','actor':1},
            {'kind':'card','card':3},{'kind':'attack'}]
        result=self.replay(events);self.assertTrue(result['scene']['accepted'])
        attack=result['scene']['last_attack'];self.assertEqual(attack['chain_type'],'brave')
        self.assertEqual(len(attack['chain']),4);self.assertEqual(len(attack['damage']),4)
        self.assertEqual(attack['chain'][-1]['card_type'],4)

    def test_unplayed_cards_are_discarded_three_turns_exhaust_15_then_reshuffle(self):
        seen=set();events=[];current=self.start
        for turn in range(3):
            hand=current['scene']['hand'];identities={c['deck_position'] for c in hand}
            self.assertFalse(seen&identities);seen|=identities
            self.assertEqual(current['scene']['deck_cycle'],1)
            choices=(1,2,3)
            if turn==2:
                arts=[c['index'] for c in hand if c['type']=='arts'];self.assertGreaterEqual(len(arts),3);choices=arts[:3]
            events+=self.card_turn(choices);attack=self.replay(events)
            self.assertTrue(attack['scene']['accepted'])
            if turn==2:self.assertIn(attack['scene']['last_attack']['chain_type'],('arts','braveAndArts'))
            events.append({'kind':'enemy_turn'});current=self.replay(events)
            self.assertTrue(current['scene']['accepted']);self.assertEqual(current['scene']['phase'],'commands')
        self.assertEqual(len(seen),15);self.assertEqual(current['scene']['deck_cycle'],2)
        self.assertEqual(current['scene']['deck_remaining'],10)
        reloaded=InteractiveSimulation(root=self.tmp.name).replay(self.start['session_id'],events)
        self.assertEqual(reloaded['scene'],current['scene'])

    def test_enemy_damage_received_np_undo_and_replay_are_exact(self):
        events=self.card_turn();before=self.replay(events)
        after=self.replay(events+[{'kind':'enemy_step'}]);self.assertTrue(after['scene']['accepted'])
        attack=after['scene']['last_attack'];self.assertEqual(attack['kind'],'enemy')
        damage=attack['damage'][0];slot=damage['target_slot']
        self.assertGreater(damage['damage'],0);self.assertGreater(damage['def_np_gain'],0)
        self.assertLess(self.player(after,slot)['hp'],self.player(before,slot)['hp'])
        self.assertGreater(self.player(after,slot)['np'],self.player(before,slot)['np'])
        self.assertEqual(before['scene'],self.replay(events)['scene'])
        self.assertEqual(after['scene'],self.replay(events+[{'kind':'enemy_step'}])['scene'])

    def test_enemy_full_charge_uses_np_and_resets_charge(self):
        start=self.new_session(enemy_np=True)
        events=self.card_turn()+[{'kind':'enemy_turn'}]
        turn2=self.replay(events,start)
        self.assertEqual(turn2['scene']['enemies'][0]['charge'],1)
        events+=self.card_turn()+[{'kind':'enemy_step'}]
        after=self.replay(events,start);self.assertTrue(after['scene']['accepted'])
        self.assertTrue(after['scene']['last_attack']['is_np'])
        self.assertTrue(after['scene']['last_attack']['damage'])
        self.assertTrue(all(row['is_np'] for row in after['scene']['last_attack']['damage']))
        self.assertEqual(after['scene']['enemies'][0]['charge'],0)
        incomplete=self.new_session(enemy_np=True,empty_np=True)
        failed=self.replay(events,incomplete)
        self.assertFalse(failed['scene']['accepted'])
        self.assertIn('完整宝具数据',failed['failure']['message'])

    def test_retirement_backup_replacement_and_party_defeat_can_be_undone(self):
        start=self.new_session(atk=500000,max_acts=3)
        events=self.card_turn();before=self.replay(events,start)
        first=self.replay(events+[{'kind':'enemy_step'}],start)
        self.assertTrue(first['scene']['accepted'])
        self.assertTrue(any(not p['alive'] for p in first['scene']['players']))
        events.append({'kind':'enemy_turn'});replacement=self.replay(events,start)
        self.assertTrue(replacement['scene']['accepted'])
        self.assertTrue(any(p['slot']>=4 and 0<p['position']<=3 for p in replacement['scene']['players']))
        self.assertEqual(replacement['scene']['deck_cycle'],2)
        alive={p['slot'] for p in replacement['scene']['players'] if p['alive']}
        self.assertTrue(all(c['slot'] in alive for c in replacement['scene']['hand']))
        events+=self.card_turn()+[{'kind':'enemy_turn'}];lost=self.replay(events,start)
        self.assertTrue(lost['scene']['accepted']);self.assertTrue(lost['scene']['lost'])
        self.assertEqual(lost['scene']['phase'],'lost')
        self.assertEqual(self.replay(self.card_turn(),start)['scene'],before['scene'])
        self.assertFalse(self.replay(events+[{'kind':'cards'}],start)['scene']['accepted'])

    def test_new_session_saved_mid_selection_keeps_seed_and_redo_branch(self):
        prefix=[{'kind':'cards'},{'kind':'card','card':2},{'kind':'np','actor':1}]
        entries=[{'kind':'enter','label':'进场','draft':self.start['draft'],'session_id':self.start['session_id'],
            'scene':self.start['scene']}]
        for i,event in enumerate(prefix):
            entries.append({'kind':'event','label':str(i),'event':event,'scene':self.replay(prefix[:i+1])['scene']})
        value=self.engine.save_recording({'name':'随机手牌录制','entries':entries,'cursor':2})
        loaded=next(r for r in self.engine.recordings() if r['id']==value['id'])
        self.assertEqual(loaded['cursor'],2);self.assertEqual(len(loaded['entries']),4)
        scene=InteractiveSimulation(root=self.tmp.name).replay(self.start['session_id'],prefix[:2])['scene']
        self.assertEqual(scene,loaded['entries'][2]['scene']);self.assertEqual(scene['rng_seed'],123)

    def test_self_sacrifice_during_player_turn_reshuffles_after_enemy_turn(self):
        from src.parser.chaldea_parser import ServantProfile
        parser=copy.deepcopy(self.parser);draft=copy.deepcopy(self.draft)
        parser.servants[201300]=ServantProfile(svt_id=201300,name='阿拉什',lv=70,skill_lv1=10,skill_lv2=10,skill_lv3=10)
        draft['party'][0]['svt_id']=201300;draft['party'][5]['svt_id']=100100
        quest=copy.deepcopy(self.quest)
        for stage in quest['stages']:
            for e in stage['enemies']:e['hp']=500000;e['atk']=3000
        with patch.object(self.engine.service,'_load_quest',return_value=(self.entry,quest,self.region)):
            start=self.engine.enter(draft,parser,seed=123,version=2)
        events=[{'kind':'cards'},{'kind':'np','actor':1},{'kind':'card','card':1},
            {'kind':'card','card':4},{'kind':'attack'}]
        own=self.replay(events,start);self.assertTrue(own['scene']['accepted'])
        self.assertFalse(self.player(own,1)['alive'])
        after=self.replay(events+[{'kind':'enemy_turn'}],start)
        self.assertTrue(after['scene']['accepted']);self.assertEqual(after['scene']['deck_cycle'],2)
        self.assertTrue(all(c['slot']!=1 for c in after['scene']['hand']))
        self.assertEqual(after['scene'],self.replay(events+[{'kind':'enemy_turn'}],start)['scene'])

    def test_bad_phase_unready_np_and_cooldown_do_not_accept_actions(self):
        for events in [self.card_turn()+[{'kind':'skill','actor':2,'skill':1}],
            [{'kind':'cards'},{'kind':'np','actor':2}],
            [{'kind':'skill','actor':2,'skill':1,'enemy':6}],
            [{'kind':'skill','actor':2,'skill':2,'target':1},{'kind':'skill','actor':2,'skill':2,'target':1}]]:
            with self.subTest(events=events):self.assertFalse(self.replay(events)['scene']['accepted'])


if __name__=='__main__':unittest.main()
