import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

from PIL import Image
from src.strategy.screen_detector import ScreenDetector
from src.strategy.first_clear_progress import read_card_progress,inspect_free_quest
from src.strategy.quest_battle import QuestBattleExecutor
from src.strategy.ap_recovery import AppleLimitReached
from src.workflows.first_clear_runner import (FirstClearOptions,FirstClearLedger,FirstClearRunner,
                                               FirstClearTarget,first_clear_catalog)


def observation(phase=1,qid=123,ap=5,available=100):
    return {'quest_id':qid,'next_phase':phase,'completed':phase is None,
            'completed_phases':3 if phase is None else phase-1,'card_ap':ap,'available_ap':available}


def entry(qid=123):
    return {'id':qid,'phase':3,'phases':[1,2,3],'war':'奏章Ⅲ 新灵长后继战 原型肇始','war_id':404,
            'spot':'市中心','name':'月球街道','kind':'free','consume_type':'ap','after_clear':'repeatLast',
            'release_conditions':[{'type':'questClear','targetId':99}], 'ap':5}


def row(qid=123):
    e=entry(qid)
    return {'quest_id':f'q{qid}','game_id':qid,'phase':3,'war':e['war'],'war_id':404,'spot':e['spot'],
            'name':e['name'],'kind':'free','ap_per_run':5,'cleared':False,'supported':False,
            'reason':'账号尚无该阶段已通关记录'}


class TestLiveFreeQuestProgress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames=[]
        for filename,name,qid in [('ordeal4_card','惩戒邪恶之辈',93040501),
                                   ('free_phase2','月球街道',93040402),('ordeal2_card','秘密的碰头',93040302)]:
            image=Image.open(Path(__file__).parent/'fixtures'/f'{filename}.png')
            cls.frames.append((image,ScreenDetector.run_ocr(image),{'id':qid,'name':name,'phases':[1,2,3]}))

    @classmethod
    def tearDownClass(cls):
        for image,_,_ in cls.frames:image.close()

    def test_real_unstarted_partial_and_completed_card_states(self):
        for frame,expected in zip(self.frames,[1,2,None]):
            with self.subTest(expected=expected):
                value=read_card_progress(*frame)
                self.assertEqual(value['next_phase'],expected)
                self.assertEqual(value['completed'],expected is None)
                self.assertIsInstance(value['available_ap'],int)

    def test_wrong_title_missing_progress_or_ap_never_guesses(self):
        image,words,e=self.frames[1]
        for changed in [[],[w for w in words if w[1]!='任务进行度'],
                        [w for w in words if not w[1].replace(' ','').startswith('AP')]]:
            with self.assertRaises(ValueError):read_card_progress(image,changed,e)
        with self.assertRaises(ValueError):read_card_progress(image,words,{**e,'name':'另一关'})
        with self.assertRaises(ValueError):read_card_progress(image,words,{**e,'phases':[1,2,3,4]})

    def test_missing_glyphs_cannot_be_read_as_phase_one(self):
        image,words,e=self.frames[1]
        with self.assertRaises(ValueError):
            read_card_progress(Image.new('RGB',(1920,1080),'white'),words,e)

    def test_two_completed_arrows_with_gray_repeat_marker_means_phase_three(self):
        gray,_,_=self.frames[0];complete,words,e=self.frames[2]
        image=complete.copy()
        image.paste(gray.crop((1449,300,1538,370)),(1449,300))
        value=read_card_progress(image,words,e)
        self.assertEqual(value['next_phase'],3);self.assertFalse(value['completed'])

    def test_blue_repeat_marker_with_incomplete_previous_phase_is_rejected(self):
        partial,words,e=self.frames[1];complete,_,_=self.frames[2]
        # The partial card header is ten pixels higher than the complete card.
        image=partial.copy();image.paste(complete.crop((1449,300,1538,370)),(1450,290))
        with self.assertRaisesRegex(ValueError,'顺序'):read_card_progress(image,words,e)


class TestFirstClearMode(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parser=SimpleNamespace(servants={1:object()},quest_records_complete=True,
                                    quest_records={99:{'questPhase':3,'clearNum':1}})
        self.ledger=FirstClearLedger(self.parser,'target',Path(self.tmp.name)/'progress')

    def catalog(self,e=None,r=None):
        bindings={'q123':e or entry(), 'prereq':{'id':99,'phase':3}}
        with patch('src.workflows.first_clear_runner.quest_bindings',return_value=bindings), \
             patch('src.workflows.first_clear_runner.quest_catalog',return_value=[r or row()]):
            return first_clear_catalog(self.parser,FirstClearOptions(),self.ledger)[0]

    def test_uncleared_unlocked_ordinary_quest_can_be_selected(self):
        self.assertTrue(self.catalog()['supported'])

    def test_resource_navigation_difficulty_and_phase_guards_remain(self):
        for reason in ['关卡需要特殊入场资源','高难关卡机制尚未适配','该章节的自动导航尚未适配']:
            self.assertFalse(self.catalog(r={**row(),'reason':reason})['supported'])
        self.assertFalse(self.catalog(e={**entry(),'phases':[1,3]})['supported'])
        self.assertFalse(self.catalog(e={**entry(),'after_clear':'close'})['supported'])

    def test_prerequisite_needs_final_phase_and_unknown_conditions_are_blocked(self):
        self.parser.quest_records[99]['questPhase']=1
        self.assertFalse(self.catalog()['supported'])
        self.assertFalse(self.catalog(e={**entry(),'release_conditions':[{'type':'commonRelease','targetId':1}]})['supported'])
        self.parser.quest_records_complete=False
        self.assertFalse(self.catalog()['supported'])

    def test_persistent_progress_does_not_forge_account_records(self):
        before=dict(self.parser.quest_records)
        self.ledger.observe(observation(2));self.ledger.pending(123,2)
        loaded=FirstClearLedger(self.parser,'target',Path(self.tmp.name)/'progress')
        self.assertEqual(loaded.observation(123)['next_phase'],2)
        self.assertEqual(loaded.data['pending']['123']['phase'],2)
        self.assertEqual(self.parser.quest_records,before)
        other=FirstClearLedger(self.parser,'another-device',Path(self.tmp.name)/'progress')
        self.assertIsNone(other.observation(123))
        self.ledger.observe(observation(None));self.assertFalse(self.catalog()['supported'])

    def test_invalid_options_are_rejected(self):
        for value in [{'war_ids':[401]},{'war_ids':[404,404]},{'max_quests':True},
                      {'ap_budget':0},{'max_quests':31},{'auto_refill':True}]:
            with self.subTest(value=value),self.assertRaises(ValueError):FirstClearOptions.model_validate(value)

    def run_fake(self,observations,*,limit=1,budget=100,execute=None,cancel_event=None,on_progress=None,options=None):
        devices=MagicMock(adb_path='adb');devices.get_connected_devices.return_value=[SimpleNamespace(serial='target')]
        engine=MagicMock(last_quest_clears=0,current_recommendation=None)
        plans=[]
        def battle(plan,**kwargs):
            plans.append(plan);engine.last_quest_clears=1;engine.last_quest_started=True
            return execute(engine,plan) if execute else True
        engine.run_quest.side_effect=battle
        runner=FirstClearRunner(self.parser,devices,report_root=Path(self.tmp.name)/'runs',
                                 progress_root=Path(self.tmp.name)/'progress')
        def candidates(parser,options,ledger):
            return [{**r,'supported':True} for r in [row(123),row(124)] if not (ledger.observation(r['game_id']) or {}).get('completed')]
        with patch('src.workflows.first_clear_runner.FGOController'), \
             patch('src.workflows.first_clear_runner.SmartEngine',return_value=engine), \
             patch('src.workflows.first_clear_runner.first_clear_catalog',side_effect=candidates), \
             patch('src.workflows.first_clear_runner.quest_binding',side_effect=lambda key:entry(int(key[1:]))), \
             patch('src.workflows.first_clear_runner.inspect_free_quest',side_effect=observations):
            result=runner.run(options or FirstClearOptions(max_quests=limit,ap_budget=budget),device_serial='target',cancel_event=cancel_event,on_progress=on_progress)
        return result,plans

    def test_apple_options_are_off_by_default_and_bounded_when_enabled(self):
        self.assertEqual((FirstClearOptions().refill_apple,FirstClearOptions().max_apples),('none',0))
        for apple in ['bronze','blue','silver','gold']:
            self.assertEqual(FirstClearOptions(refill_apple=apple,max_apples=1).max_apples,1)
        for values in [{'refill_apple':'stone','max_apples':1},{'refill_apple':'gold'},
                       {'max_apples':1},{'refill_apple':'silver','max_apples':1000},
                       {'refill_apple':'blue','max_apples':True},{'refill_apple':'bronze','max_apples':-1}]:
            with self.subTest(values=values),self.assertRaises(ValueError):FirstClearOptions(**values)

    def test_limits_can_be_independently_disabled_with_null(self):
        options=FirstClearOptions(max_quests=None,ap_budget=None,refill_apple='bronze',max_apples=None)
        self.assertIsNone(options.max_quests);self.assertIsNone(options.ap_budget);self.assertIsNone(options.max_apples)
        self.assertEqual(FirstClearOptions(max_quests=None).ap_budget,100)
        self.assertEqual(FirstClearOptions(ap_budget=None).max_quests,3)
        with self.assertRaises(ValueError):FirstClearOptions(refill_apple='none',max_apples=None)

    def test_disabled_quest_limit_continues_until_candidates_are_finished(self):
        observations=[observation(p,qid) for qid in [123,124] for p in [1,2,3,None]]
        result,plans=self.run_fake(observations,options=FirstClearOptions(max_quests=None))
        self.assertTrue(result.success,result.error);self.assertEqual(result.confirmed_quests,2)
        self.assertEqual(len(plans),6)

    def test_disabled_ap_budget_allows_expenditure_over_default_budget(self):
        result,plans=self.run_fake([observation(p,ap=50) for p in [1,2,3,None]],
                                   options=FirstClearOptions(max_quests=1,ap_budget=None))
        self.assertTrue(result.success,result.error);self.assertEqual(result.estimated_ap_spent,150)
        self.assertEqual(len(plans),3)

    def test_finite_ap_budget_still_stops_when_quest_limit_is_disabled(self):
        result,plans=self.run_fake([observation(p) for p in [1,2,3]],
                                   options=FirstClearOptions(max_quests=None,ap_budget=10))
        self.assertEqual(result.status,'ap_limit_reached');self.assertEqual(len(plans),2)

    def test_disabling_limits_does_not_enable_recovery_or_bypass_current_ap(self):
        with patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            result,plans=self.run_fake([observation(available=0)],
                                       options=FirstClearOptions(max_quests=None,ap_budget=None))
        self.assertEqual(result.status,'ap_limit_reached');self.assertEqual(plans,[])
        recovery.assert_not_called();self.assertEqual(result.apples_committed,0)

    def test_disabled_apple_limit_allows_multiple_batches_and_preserves_counting(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            def restore(apple,cost,submit,*,max_count):
                self.assertEqual(max_count,999)
                return self.apple_result(submit,after=30,count=3)
            recovery.return_value.restore_once.side_effect=restore
            observations=[observation(p,ap=21,available=a) for p in [1,2,3] for a in [0,30]]+[observation(None)]
            result,plans=self.run_fake(observations,options=FirstClearOptions(
                max_quests=1,ap_budget=None,refill_apple='bronze',max_apples=None))
        self.assertTrue(result.success,result.error);self.assertEqual(len(plans),3)
        self.assertEqual((result.apples_used,result.apples_committed,result.apple_attempts),(9,9,3))

    def test_disabled_apple_limit_never_retries_an_unverified_submission(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            def restore(apple,cost,submit,**kwargs):
                self.apple_result(submit,after=30,count=3);raise ValueError('AP未核对')
            recovery.return_value.restore_once.side_effect=restore
            result,plans=self.run_fake([observation(available=0)],options=FirstClearOptions(
                max_quests=None,ap_budget=None,refill_apple='bronze',max_apples=None))
        self.assertEqual(result.status,'failed');self.assertEqual(plans,[])
        self.assertEqual((result.apples_used,result.apples_committed,result.apple_attempts),(0,3,1))
        self.assertEqual(recovery.return_value.restore_once.call_count,1)

    def apple_options(self,**values):
        return FirstClearOptions(max_quests=1,refill_apple='bronze',max_apples=3,**values)

    def apple_result(self,submit,before=0,after=10,count=1):
        event={'apple':'bronze','name':'赤铜果实','count':count,'inventory_before':10,
               'before_ap':before,'expected_after_ap':after,'status':'pending'}
        submit(event)
        return {**event,'status':'verified','after_ap':after}

    def test_apple_refill_is_followed_by_exact_phase_reinspection_then_battle(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True) as prepare, \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect=lambda apple,cost,submit,**kwargs:self.apple_result(submit)
            result,plans=self.run_fake([observation(3,available=0),observation(3,available=10),observation(None)],options=self.apple_options())
        self.assertTrue(result.success,result.error);self.assertEqual([p.phase for p in plans],[3])
        self.assertEqual((result.apples_used,result.apple_attempts),(1,1));prepare.assert_called_once()
        self.assertEqual(result.apple_refills[0]['status'],'verified')

    def test_small_apples_are_selected_as_one_batch_and_counted_as_items(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            def restore(apple,cost,submit,*,max_count):
                self.assertEqual(max_count,3)
                return self.apple_result(submit,0,30,count=3)
            recovery.return_value.restore_once.side_effect=restore
            result,plans=self.run_fake([observation(3,ap=21,available=a) for a in [0,30]]+[observation(None)],options=self.apple_options())
        self.assertTrue(result.success,result.error)
        self.assertEqual((result.apples_used,result.apples_committed,result.apple_attempts,len(plans)),(3,3,1,1))

    def test_budget_or_failed_presimulation_never_consumes_apples(self):
        for budget,prepare_ok in [(4,True),(100,False)]:
            with self.subTest(budget=budget), \
                 patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=prepare_ok), \
                 patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
                result,plans=self.run_fake([observation(3,available=0)],options=self.apple_options(ap_budget=budget))
            self.assertFalse(result.success);self.assertEqual(plans,[])
            self.assertEqual(result.apple_attempts,0);recovery.assert_not_called()

    def test_copper_limit_shortfall_is_a_no_use_limit_stop(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect=AppleLimitReached('需3个，剩余上限2个，未使用')
            options=FirstClearOptions(max_quests=1,refill_apple='bronze',max_apples=2)
            result,plans=self.run_fake([observation(3,ap=21,available=0)],options=options)
        self.assertEqual(result.status,'ap_limit_reached');self.assertEqual(plans,[])
        self.assertEqual((result.apples_used,result.apples_committed,result.apple_attempts),(0,0,0))

    def test_full_batch_reserves_all_items_against_following_phases(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect=lambda apple,cost,submit,**kwargs:self.apple_result(submit,after=30,count=3)
            result,plans=self.run_fake([observation(2,ap=21,available=0),observation(2,ap=21,available=30),
                                        observation(3,ap=21,available=0)],options=self.apple_options())
        self.assertEqual(result.status,'ap_limit_reached');self.assertEqual(len(plans),1)
        self.assertEqual((result.apples_used,result.apples_committed,result.apple_attempts),(3,3,1))
        self.assertEqual(recovery.return_value.restore_once.call_count,1)

    def test_apple_cap_stops_before_any_extra_item_or_battle(self):
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect=lambda apple,cost,submit,**kwargs:self.apple_result(submit)
            options=FirstClearOptions(max_quests=1,refill_apple='bronze',max_apples=1)
            result,plans=self.run_fake([observation(3,ap=21,available=0),observation(3,ap=21,available=10)],options=options)
        self.assertEqual(result.status,'ap_limit_reached');self.assertEqual(plans,[])
        self.assertEqual(result.apple_attempts,1);self.assertEqual(recovery.return_value.restore_once.call_count,1)

    def test_unverified_use_reserves_the_limit_and_stops_without_retry(self):
        def restore(apple,cost,submit,**kwargs):
            self.apple_result(submit,after=30,count=3);raise ValueError('提交后AP未确认')
        with patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
             patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect=restore
            result,plans=self.run_fake([observation(3,available=0)],options=self.apple_options())
        self.assertFalse(result.success);self.assertEqual(plans,[])
        self.assertEqual((result.apples_used,result.apple_attempts),(0,1))
        self.assertEqual(result.apples_committed,3)
        self.assertEqual(result.apple_refills[0]['status'],'unverified')
        self.assertEqual(recovery.return_value.restore_once.call_count,1)

    def test_changed_phase_or_insufficient_natural_recovery_stops_before_battle(self):
        for restored,after in [(True,observation(2,available=10)),(True,observation(3,qid=456,available=10)),
                               (True,observation(3,available=4)),(False,observation(3,available=0))]:
            with self.subTest(after=after), \
                 patch('src.workflows.first_clear_runner.QuestBattleExecutor.prepare',return_value=True), \
                 patch('src.workflows.first_clear_runner.AppleRecovery') as recovery:
                recovery.return_value.restore_once.side_effect=lambda apple,cost,submit,**kwargs:self.apple_result(submit) if restored else None
                result,plans=self.run_fake([observation(3,available=0),after],options=self.apple_options())
            self.assertFalse(result.success);self.assertEqual(plans,[])

    def test_every_phase_replans_then_automatically_selects_next_quest(self):
        observations=[observation(p,qid) for qid in [123,124] for p in [1,2,3,None]]
        result,plans=self.run_fake(observations,limit=2)
        self.assertTrue(result.success,result.error)
        self.assertEqual((result.confirmed_quests,result.confirmed_phases,result.confirmed_clears),(2,6,6))
        self.assertEqual([p.quest_id for p in plans],[f'q{q}@phase={p}' for q in [123,124] for p in [1,2,3]])
        self.assertTrue(all(p.first_clear and p.runs==1 for p in plans))
        self.assertTrue(all(s['status']=='completed' for s in result.stations))

    def test_resume_uses_live_phase_two_instead_of_old_account_snapshot(self):
        self.ledger.pending(123,1)
        result,plans=self.run_fake([observation(2),observation(3),observation(None)])
        self.assertTrue(result.success,result.error);self.assertEqual([p.phase for p in plans],[2,3])

    def test_live_status_replaces_previous_phase_recommendation(self):
        captured=[]
        def execute(engine,plan):
            engine.quest_progress_callback('simulating')
            engine.current_recommendation=SimpleNamespace(model_dump=lambda **kwargs:{'phase':plan.phase})
            engine.quest_progress_callback('navigating')
            return True
        def update(report):
            if report['status'] in ('simulating','navigating'):
                captured.append((report['status'],report['stations'][-1].get('recommendation')))
        result,_=self.run_fake([observation(p) for p in [1,2,3,None]],execute=execute,on_progress=update)
        self.assertTrue(result.success,result.error)
        self.assertEqual(captured,[item for phase in [1,2,3] for item in [('simulating',None),('navigating',{'phase':phase})]])

    def test_unchanged_or_jumped_postbattle_progress_stops_all_following_work(self):
        for after in [observation(1),observation(3),observation(None),observation(2,qid=456)]:
            with self.subTest(after=after):
                result,plans=self.run_fake([observation(1),after],limit=2)
                self.assertFalse(result.success);self.assertEqual(len(plans),1)
                self.assertEqual(result.confirmed_phases,0);self.assertEqual(result.confirmed_clears,1)

    def test_failed_battle_or_exception_does_not_continue_next_phase(self):
        def failure(engine,plan):raise RuntimeError('failure after result')
        for execute in [lambda engine,plan:False,failure]:
            result,plans=self.run_fake([observation(1)],execute=execute)
            self.assertFalse(result.success);self.assertEqual(len(plans),1)
            self.assertEqual(result.confirmed_clears,1)

    def test_budget_ap_and_unreadable_ap_stop_before_start(self):
        for value,budget in [(observation(ap=5),4),(observation(available=4),100),(observation(available=None),100)]:
            result,plans=self.run_fake([value],budget=budget)
            self.assertFalse(result.success);self.assertEqual(plans,[])

    def test_failed_battle_still_accounts_for_ap_without_counting_a_clear(self):
        def execute(engine,plan):engine.last_quest_clears=0;return False
        result,plans=self.run_fake([observation(1)],execute=execute)
        self.assertFalse(result.success);self.assertEqual(result.confirmed_clears,0)
        self.assertEqual(result.estimated_ap_spent,5);self.assertEqual(len(plans),1)

    def test_already_completed_live_card_is_skipped_without_fighting_it(self):
        result,plans=self.run_fake([observation(None),observation(2,qid=124),
                                    observation(3,qid=124),observation(None,qid=124)])
        self.assertTrue(result.success,result.error);self.assertEqual([p.game_id for p in plans],[124,124])

    def test_cancel_after_battle_does_not_record_unverified_phase(self):
        event=threading.Event()
        def execute(engine,plan):event.set();return True
        result,plans=self.run_fake([observation(1)],execute=execute,cancel_event=event)
        self.assertEqual(result.status,'cancelled');self.assertEqual(result.confirmed_phases,0)
        self.assertEqual(len(plans),1);self.assertEqual(result.confirmed_clears,1)


class TestFirstClearPreflight(unittest.TestCase):
    def test_observation_disagreement_between_independent_frames_stops(self):
        engine=MagicMock();engine.is_cancelled.return_value=False
        engine.first_clear_observation_dir=None
        with patch('src.strategy.first_clear_progress.ScreenDetector') as detector, \
             patch('src.strategy.first_clear_progress.read_card_progress',side_effect=[observation(1),observation(2)]):
            detector.identify_regional_map.return_value=('map','canonical')
            with self.assertRaisesRegex(ValueError,'两帧'):inspect_free_quest(engine,'q',entry())
        self.assertEqual(engine.ctrl.screencap.call_count,2)
        engine.ctrl.click.assert_not_called()

    def test_inspection_navigation_failure_never_enters_support_or_guesses_stage(self):
        engine=MagicMock();engine.is_cancelled.return_value=False
        engine.navigate_to_free_quest.return_value=False
        with self.assertRaises(ValueError):inspect_free_quest(engine,'q',entry())
        engine.ctrl.screencap.assert_not_called()
        self.assertTrue(engine.navigate_to_free_quest.call_args.kwargs['inspect_only'])

    def test_stage_or_ap_change_before_start_blocks_navigation_into_support(self):
        engine=MagicMock();engine.current_recommendation.laplace={'quest_ap':5}
        plan=FirstClearTarget(quest_id='q@phase=2',game_id=123,phase=2,war='war',spot='spot',name='quest',kind='free',runs=1,ap_per_run=5)
        for state in [observation(1),observation(2,ap=6),observation(2,available=0)]:
            with patch('src.strategy.first_clear_progress.inspect_free_quest',return_value=state), \
                 patch('src.strategy.quest_catalog.quest_binding',return_value=entry()):
                self.assertFalse(QuestBattleExecutor(engine).navigate(plan,'war','spot','quest'))
        engine.navigate_to_free_quest.assert_not_called()


if __name__=='__main__':unittest.main()
