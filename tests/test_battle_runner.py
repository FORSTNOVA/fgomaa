import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.strategy.ap_recovery import AppleLimitReached
from src.strategy.quest_resources import daily_card_ap, inspect_battle_quest
from src.strategy.quest_repeat import repeat_ap, repeat_buttons
from src.strategy.screen_detector import GameScreen
from src.strategy.quest_battle import QuestBattleExecutor
from src.workflows.battle_runner import BattleRequest, BattleRunner, resolve_battle_request, simulation_catalog
from src.workflows.resource_options import ResourceOptions
from src.workflows.first_clear_runner import FirstClearOptions


def catalog_row(**changes):
    return dict(quest_id='test_quest', game_id=123, phase=3, war='冬木', spot='X-A', name='训练场',
                kind='free', ap_per_run=5, cleared=True, supported=True, reason='',
                simulation_supported=True, simulation_reason='', **changes)


def observation(cost=5, available=100):
    return {'quest_id':123, 'card_ap':cost, 'available_ap':available, 'completed':True}


class TestBattleRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parser = SimpleNamespace(servants={1:object()})
        self.devices = MagicMock(adb_path='adb')
        self.devices.get_connected_devices.return_value = [SimpleNamespace(serial='target')]
        self.engine = MagicMock(last_quest_clears=0, current_recommendation=None)
        self.recommendation = MagicMock(laplace={'passed':True, 'quest_ap':5}, target_quest_id='test_quest')
        self.recommendation.model_dump.return_value = {'slot1_dps':'owned', 'laplace':{'passed':True}}
        self.event = threading.Event()
        self.engine.is_cancelled.side_effect = self.event.is_set
        self.runner = BattleRunner(self.parser, self.devices, report_root=self.tmp.name)
        for name, value in [('simulation_catalog', [catalog_row()]), ('SmartEngine', self.engine)]:
            p = patch('src.workflows.battle_runner.'+name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch('src.workflows.battle_runner.FGOController')
        self.controller = p.start(); self.addCleanup(p.stop)
        p = patch('src.workflows.battle_runner.inspect_battle_quest', return_value=observation())
        self.inspect = p.start(); self.addCleanup(p.stop)
        p = patch('src.workflows.battle_runner.QuestBattleExecutor.prepare', side_effect=self.prepare)
        self.prepare_mock = p.start(); self.addCleanup(p.stop)
        p = patch('src.workflows.battle_runner.QuestBattleExecutor.run', side_effect=self.battle)
        self.battle_mock = p.start(); self.addCleanup(p.stop)

    def prepare(self, plan):
        self.engine.current_recommendation = self.recommendation
        self.engine.quest_progress_callback('simulating')
        return True

    def battle(self, plan, **kwargs):
        self.assertTrue(kwargs['prepared'])
        self.engine.quest_resource_check(plan)
        self.engine.quest_start_callback(plan)
        self.engine.last_quest_clears = 1
        self.engine.last_quest_started = True
        return True

    def run_battle(self, options=None, auto=True):
        body = {'quest_id':'test_quest', 'auto_battle':auto, 'device_serial':'target'}
        if options is not None:
            body['options'] = options
        return self.runner.run(body, cancel_event=self.event)

    def test_default_preview_never_connects_or_spends_even_with_refill_options(self):
        self.assertFalse(BattleRequest(quest_id='test_quest').auto_battle)
        result = self.run_battle({'refill_apple':'gold', 'max_apples':1}, auto=False)
        self.assertTrue(result.success); self.assertEqual(result.status, 'preview_passed')
        self.assertEqual((result.confirmed_clears, result.estimated_ap_spent, result.apples_committed), (0,0,0))
        self.devices.get_connected_devices.assert_not_called(); self.controller.assert_not_called()
        self.inspect.assert_not_called(); self.battle_mock.assert_not_called()
        saved = json.loads((Path(result.artifacts_dir)/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['stations'][0]['recommendation']['laplace']['passed'])

    def test_simulation_failure_does_not_create_controller(self):
        self.prepare_mock.side_effect = lambda _:False
        result = self.run_battle()
        self.assertEqual(result.status, 'failed'); self.controller.assert_not_called()
        self.devices.get_connected_devices.assert_not_called(); self.battle_mock.assert_not_called()

    def test_actual_discount_and_battle_limit_count_confirmed_results(self):
        self.inspect.return_value = observation(cost=2)
        result = self.run_battle({'max_quests':3, 'ap_budget':6})
        self.assertTrue(result.success); self.assertEqual(result.confirmed_clears,3)
        self.assertEqual(result.estimated_ap_spent,6); self.assertEqual(self.battle_mock.call_count,3)
        self.prepare_mock.assert_called_once()

    def test_repeat_panel_ap_is_reserved_in_the_same_budget_as_first_entry(self):
        def battle(plan, *, repeat_next, repeat_resources, **kwargs):
            if self.battle_mock.call_count == 1:
                self.assertTrue(repeat_next)
                return self.battle(plan, **kwargs)
            self.assertFalse(repeat_next)
            self.assertFalse(repeat_resources(plan, 5, 4))
            self.assertFalse(repeat_resources(plan, 4, 100))
            self.assertTrue(repeat_resources(plan, 5, 5))
            self.engine.quest_start_callback(plan)
            self.engine.last_quest_clears = 1
            return True
        self.battle_mock.side_effect = battle
        result = self.run_battle({'max_quests':2, 'ap_budget':10})
        self.assertTrue(result.success)
        self.assertEqual((result.confirmed_clears, result.estimated_ap_spent), (2, 10))
        self.assertEqual(result.stations[1]['observation']['source'], 'continue_panel')
        self.inspect.assert_called_once()

    def test_repeat_panel_budget_rejection_never_reserves_next_entry(self):
        def battle(plan, *, repeat_resources, **kwargs):
            if self.battle_mock.call_count == 1:
                return self.battle(plan, **kwargs)
            self.assertFalse(repeat_resources(plan, 5, 100))
            self.engine.quest_resource_check(plan)
        self.battle_mock.side_effect = battle
        result = self.run_battle({'max_quests':2, 'ap_budget':9})
        self.assertEqual(result.status, 'ap_limit_reached')
        self.assertEqual((result.confirmed_clears, result.estimated_ap_spent), (1, 5))

    def test_budget_stops_before_refill_or_submission(self):
        self.inspect.return_value = observation(cost=5, available=0)
        with patch('src.workflows.battle_runner.AppleRecovery') as recovery:
            result = self.run_battle({'ap_budget':4, 'refill_apple':'gold', 'max_apples':1})
        self.assertEqual(result.status,'ap_limit_reached'); self.assertEqual(result.estimated_ap_spent,0)
        recovery.assert_not_called(); self.assertEqual(result.confirmed_clears,0)

    def test_insufficient_ap_with_default_no_refill_stops(self):
        self.inspect.return_value = observation(available=4)
        result = self.run_battle()
        self.assertEqual(result.status,'ap_limit_reached'); self.assertEqual(result.apples_committed,0)
        self.assertEqual(result.estimated_ap_spent,0)

    def test_bronze_batch_reserves_each_fruit_and_rereads_card(self):
        self.recommendation.laplace['quest_ap'] = 21
        self.inspect.side_effect = [observation(cost=21,available=4), observation(cost=21,available=24)]
        def restore(apple, cost, submit, **kwargs):
            self.assertEqual((apple,cost,kwargs['max_count']), ('bronze',21,2))
            submit({'count':2,'status':'pending'})
            return {'count':2,'status':'verified','after_ap':24}
        with patch('src.workflows.battle_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect = restore
            result = self.run_battle({'max_quests':1,'refill_apple':'bronze','max_apples':2})
        self.assertTrue(result.success)
        self.assertEqual((result.apples_used,result.apples_committed,result.apple_attempts),(2,2,1))
        self.assertEqual(result.estimated_ap_spent,21); self.assertEqual(self.inspect.call_count,2)

    def test_unknown_refill_result_keeps_reservation_and_never_retries(self):
        self.inspect.return_value = observation(available=0)
        def restore(apple, cost, submit, **kwargs):
            submit({'count':1,'status':'pending'})
            raise RuntimeError('unknown confirmation')
        with patch('src.workflows.battle_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect = restore
            result = self.run_battle({'refill_apple':'silver','max_apples':1})
            self.assertEqual(recovery.return_value.restore_once.call_count,1)
        self.assertEqual(result.status,'failed'); self.assertEqual(result.apples_committed,1)
        self.assertEqual(result.apples_used,0); self.assertEqual(result.apple_refills[0]['status'],'unverified')
        self.assertEqual(result.estimated_ap_spent,0)

    def test_refill_limit_failure_stops_without_submission(self):
        self.inspect.return_value = observation(available=0)
        with patch('src.workflows.battle_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect = AppleLimitReached('insufficient cap')
            result = self.run_battle({'refill_apple':'bronze','max_apples':1})
        self.assertEqual(result.status,'ap_limit_reached'); self.assertEqual(result.apples_committed,0)

    def test_refill_followed_by_cost_change_never_starts_battle(self):
        self.inspect.side_effect = [observation(available=0), observation(cost=4,available=100)]
        def restore(apple, cost, submit, **kwargs):
            submit({'count':1,'status':'pending'})
            return {'count':1,'status':'verified','after_ap':100}
        with patch('src.workflows.battle_runner.AppleRecovery') as recovery:
            recovery.return_value.restore_once.side_effect = restore
            result = self.run_battle({'refill_apple':'gold','max_apples':1})
        self.assertEqual(result.status,'failed'); self.assertEqual(result.estimated_ap_spent,0)

    def test_partial_clear_and_unknown_click_preserve_consumption_and_stop(self):
        def failed(plan, **kwargs):
            self.engine.quest_resource_check(plan)
            self.engine.quest_start_callback(plan)
            raise RuntimeError('click outcome unknown')
        self.battle_mock.side_effect = failed
        result = self.run_battle()
        self.assertFalse(result.success); self.assertEqual(result.estimated_ap_spent,5)
        self.assertEqual(result.confirmed_clears,0); self.assertEqual(self.battle_mock.call_count,1)

    def test_confirmed_clear_but_failed_settlement_does_not_repeat(self):
        def failed(plan, **kwargs):
            self.battle(plan,**kwargs)
            return False
        self.battle_mock.side_effect = failed
        result = self.run_battle()
        self.assertFalse(result.success); self.assertEqual(result.confirmed_clears,1)
        self.assertEqual(self.battle_mock.call_count,1)

    def test_second_submission_of_same_battle_is_blocked(self):
        def duplicate(plan, **kwargs):
            self.engine.quest_resource_check(plan)
            self.engine.quest_start_callback(plan)
            self.engine.quest_start_callback(plan)
        self.battle_mock.side_effect = duplicate
        result = self.run_battle()
        self.assertEqual(result.status,'failed'); self.assertEqual(result.estimated_ap_spent,5)
        self.assertEqual(self.battle_mock.call_count,1)

    def test_missing_or_wrong_ap_identity_never_submits(self):
        for value in [observation(available=None), {**observation(),'quest_id':999}, observation(cost=6)]:
            self.inspect.return_value = value
            result = self.run_battle()
            self.assertEqual(result.status,'failed'); self.assertEqual(result.estimated_ap_spent,0)

    def test_cancelled_after_simulation_never_connects(self):
        def cancel(plan):
            self.prepare(plan); self.event.set(); return True
        self.prepare_mock.side_effect = cancel
        result = self.run_battle()
        self.assertEqual(result.status,'cancelled'); self.controller.assert_not_called()

    def test_ambiguous_device_stops_before_controller(self):
        self.devices.get_connected_devices.return_value = [SimpleNamespace(serial='other')]
        result = self.run_battle()
        self.assertEqual(result.status,'failed'); self.controller.assert_not_called()

    def test_all_disabled_limits_use_none_and_cancellation_stops_loop(self):
        def finish(plan, **kwargs):
            self.battle(plan,**kwargs); self.event.set(); return True
        self.battle_mock.side_effect = finish
        result = self.run_battle({'max_quests':None, 'ap_budget':None,'refill_apple':'gold','max_apples':None})
        self.assertEqual(result.status,'cancelled'); self.assertEqual(result.confirmed_clears,1)
        self.assertEqual(self.battle_mock.call_count,1)


class TestBattleCatalogAndOptions(unittest.TestCase):
    def test_shared_resource_contract_matches_first_clear(self):
        self.assertEqual(BattleRequest(quest_id='test').options.max_quests,1)
        self.assertEqual(BattleRequest(quest_id='test',options={}).options.max_quests,1)
        settings={'max_quests':None, 'ap_budget':None, 'refill_apple':'silver', 'max_apples':None}
        self.assertEqual(ResourceOptions(**settings).model_dump(),FirstClearOptions(**settings).model_dump(exclude={'war_ids'}))
        for changes in [{'auto_battle':'true'}, {'auto_battle':1}, {'options':{'refill_apple':'stone'}},
                        {'options':{'refill_apple':'gold','max_apples':0}}, {'options':{'ap_budget':True}},
                        {'runs':5}, {'war':'forged'}]:
            with self.assertRaises(ValueError):BattleRequest.model_validate({'quest_id':'test',**changes})

    def test_free_quest_auto_battle_request_needs_no_clear_record(self):
        parser=SimpleNamespace(servants={1:object()},quest_records_complete=False)
        request,target=resolve_battle_request({'quest_id':'fuyuki_x_a','auto_battle':True,'device_serial':'target'},parser)
        self.assertEqual(target.phase,3); self.assertTrue(request.auto_battle)

    def test_catalog_does_not_enable_execution_by_enabling_simulation(self):
        row={k:v for k,v in catalog_row().items() if not k.startswith('simulation')}
        row.update(supported=False,reason='该章节的自动导航尚未适配')
        with patch('src.workflows.battle_runner.quest_catalog',return_value=[row]), \
             patch('src.workflows.battle_runner.quest_bindings',return_value={'test_quest':{'id':123}}):
            result=simulation_catalog(SimpleNamespace(servants={1:object()}))[0]
        self.assertTrue(result['simulation_supported']); self.assertFalse(result['supported'])


def word(text,x,y):
    return ([[x-30,y-10],[x+30,y-10],[x+30,y+10],[x-30,y+10]],text,.99)


class TestBattleResources(unittest.TestCase):
    def test_daily_discount_requires_unique_cost_within_target_card(self):
        self.assertEqual(daily_card_ap([word('AP20',1250,450)],(1300,400),40),20)
        for words in [[],[word('AP20',1250,450),word('AP40',1250,460)], [word('AP41',1250,450)]]:
            with self.assertRaises(ValueError):daily_card_ap(words,(1300,400),40)

    def test_daily_observation_requires_gate_identity_and_two_equal_costs(self):
        plan=SimpleNamespace(quest_id='daily_qp_extreme',game_id=123,phase=1,kind='daily',ap_per_run=40)
        engine=MagicMock(); engine.is_cancelled.return_value=False
        words=[word('AP20',1250,450),word('行动力25/144',350,1000)]
        with patch('src.strategy.quest_resources.quest_binding',return_value={'id':123,'phase':1}), \
             patch('src.strategy.quest_resources.navigate_daily_quest',return_value=True) as navigate, \
             patch('src.strategy.quest_resources.ScreenDetector.detect',return_value=(GameScreen.CHALDEA_GATE,.99,{})), \
             patch('src.strategy.quest_resources.ScreenDetector.run_ocr',return_value=words), \
             patch('src.strategy.quest_resources.ScreenDetector.find_target_quest_coordinate',return_value=(1300,400)):
            result=inspect_battle_quest(engine,plan)
        self.assertEqual((result['card_ap'],result['available_ap']),(20,25))
        self.assertTrue(navigate.call_args.kwargs['inspect_only']); engine.ctrl.click.assert_not_called()

    def test_repeat_entry_checks_current_phase_instead_of_requiring_completion(self):
        for kind, phase, next_phase in [('free',3,1),('free',3,3),('free',3,None),('event',1,1)]:
            plan=SimpleNamespace(quest_id='test',game_id=123,phase=phase,kind=kind)
            state={**observation(),'completed':next_phase is None,'next_phase':next_phase}
            with self.subTest(kind=kind,phase=phase,next_phase=next_phase), \
                 patch('src.strategy.quest_resources.quest_binding',return_value={'id':123,'phase':phase}), \
                 patch('src.strategy.quest_resources.inspect_free_quest',return_value=state):
                if next_phase not in (None,phase):
                    with self.assertRaisesRegex(ValueError,'自动首通'):inspect_battle_quest(MagicMock(),plan)
                else:
                    self.assertEqual(inspect_battle_quest(MagicMock(),plan),state)


class TestPreparedBattleEntry(unittest.TestCase):
    def test_wrong_preview_target_cannot_navigate(self):
        engine=MagicMock(); engine.is_cancelled.return_value=False
        engine.current_recommendation=SimpleNamespace(target_quest_id='other')
        plan=SimpleNamespace(quest_id='selected',runs=1,name='name',war='war',spot='spot')
        with patch.object(QuestBattleExecutor,'navigate') as navigate:
            self.assertFalse(QuestBattleExecutor(engine).run(plan,prepared=True))
        navigate.assert_not_called(); engine.ctrl.click.assert_not_called()

    def test_resource_rejection_prevents_daily_navigation(self):
        engine=SimpleNamespace(quest_resource_check=MagicMock(return_value=False))
        with patch('src.strategy.quest_navigation.navigate_daily_quest') as navigate:
            self.assertFalse(QuestBattleExecutor(engine).navigate(SimpleNamespace(kind='daily'),'war','spot','name'))
        navigate.assert_not_called()

    def test_manual_queue_stops_before_entry_when_live_phase_does_not_match(self):
        engine=SimpleNamespace(navigate_to_free_quest=MagicMock(),log=MagicMock())
        plan=SimpleNamespace(quest_id='test',game_id=123,phase=3,kind='free')
        with patch('src.strategy.quest_resources.inspect_battle_quest',side_effect=ValueError('阶段不符')):
            self.assertFalse(QuestBattleExecutor(engine).navigate(plan,'war','spot','name'))
        engine.navigate_to_free_quest.assert_not_called()


class TestRepeatBattleEntry(unittest.TestCase):
    panel_ocr = [word('是否连续进行同一关卡？', 950, 169), word('关闭', 665, 874), word('连续出击', 1258, 875)]
    panel = (GameScreen.CONTINUE_QUEST, {'all_texts':['连续出击'], 'ocr_results':panel_ocr})
    support = (GameScreen.SUPPORT_SELECT, {'ocr_results':[word('等级10', 320, 88)]})
    loading = (GameScreen.LOADING, {})
    combat = (GameScreen.BATTLE_COMBAT, {'wave':1, 'turn':1})
    result = (GameScreen.BATTLE_RESULT, {'ocr_results':[word('下一步', 1700, 1004)]})
    map_screen = (GameScreen.REGIONAL_MAP, {})

    def setUp(self):
        self.engine = MagicMock()
        self.engine.is_cancelled.return_value = False
        self.engine._combat_np_after_actions.return_value = 100
        self.engine.current_recommendation = SimpleNamespace(
            target_quest_id='selected', laplace={'passed':True, 'recommended_level':10},
            slot1_dps='test', slot1_class='Caster', slot1_affinity=2, slot2_support='test',
            slot4_backline_support='test', direction='Arts', strategy=SimpleNamespace(name='test', turns={}))
        self.engine.detector.identify_party_from_screen.return_value = (SimpleNamespace(slot1_servant='test'), None, [])
        self.engine.party_assembler.select_support_on_support_screen.return_value = (700, 460)
        self.plan = SimpleNamespace(quest_id='selected', phase=1, runs=1, single_submission=True,
                                    name='test', war='test', spot='test')
        self.executor = QuestBattleExecutor(self.engine)
        self.verify_resources = MagicMock(return_value=True)
        for target, options in [
            ('src.strategy.quest_battle.time.sleep', {}),
            ('src.strategy.quest_battle.repeat_ap', {'return_value':(5, 10)}),
            ('src.strategy.quest_battle.ScreenDetector.run_ocr',
             {'side_effect':[[word('开始任务', 1750, 1000)], self.panel_ocr, [word('开始任务', 1750, 1000)]]}),
        ]:
            context = patch(target, **options)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(self.executor, 'navigate', return_value=True)
        self.navigate = context.start()
        self.addCleanup(context.stop)

    def first_battle(self):
        screens = [(GameScreen.PARTY_CONFIRM, {}), self.loading, self.combat, self.result,
                   (GameScreen.POPUP_DIALOG, {'all_texts':['发送好友申请？'],
                    'ocr_results':[word('结束', 495, 924), word('申请好友', 1420, 925)]}), self.panel]
        self.engine.get_current_screen.side_effect = screens
        self.assertTrue(self.executor.run(self.plan, prepared=True, repeat_next=True,
                                          repeat_resources=self.verify_resources))
        self.assertEqual(self.engine.last_quest_clears, 1)

    def test_repeat_reuses_party_reselects_support_and_closes_at_limit(self):
        self.first_battle()
        self.engine.get_current_screen.side_effect = [
            self.support, self.loading, self.combat, self.result, self.panel, self.map_screen]
        self.assertTrue(self.executor.run(self.plan, prepared=True, repeat_resources=self.verify_resources))
        self.assertEqual(self.engine.last_quest_clears, 1)
        self.navigate.assert_called_once()
        self.engine.party_assembler.assemble_party_on_confirm_screen.assert_called_once()
        self.engine.party_assembler.select_support_on_support_screen.assert_called_once()
        self.assertEqual(self.engine.quest_start_callback.call_count, 2)
        self.verify_resources.assert_called_once_with(self.plan, 5, 10)
        clicks = [call.args for call in self.engine.ctrl.click.call_args_list]
        self.assertEqual(clicks, [(1750, 1000), (1700, 1004), (495, 924),
                                  (1258, 875), (700, 460), (1700, 1004), (665, 874)])

    def test_rejected_repeat_returns_to_normal_navigation_without_clicking_continue(self):
        self.first_battle()
        self.verify_resources.return_value = False
        self.engine.get_current_screen.side_effect = [
            self.map_screen, (GameScreen.PARTY_CONFIRM, {}), self.loading, self.combat,
            self.result, self.panel, self.map_screen]
        self.assertTrue(self.executor.run(self.plan, prepared=True, repeat_resources=self.verify_resources))
        self.assertEqual(self.navigate.call_count, 2)
        self.assertEqual(self.engine.quest_start_callback.call_count, 2)
        self.assertNotIn((1258, 875), [call.args for call in self.engine.ctrl.click.call_args_list])

    def test_uncertain_continuation_click_is_never_retried(self):
        self.first_battle()
        self.engine.get_current_screen.side_effect = [self.panel] * 6
        self.assertFalse(self.executor.run(self.plan, prepared=True, repeat_resources=self.verify_resources))
        self.assertEqual(self.engine.last_quest_clears, 0)
        self.navigate.assert_called_once()
        self.assertEqual([call.args for call in self.engine.ctrl.click.call_args_list].count((1258, 875)), 1)
        self.engine.party_assembler.select_support_on_support_screen.assert_not_called()

    def test_panel_requires_real_confirmation_and_agreeing_ap(self):
        self.assertEqual(repeat_buttons(self.panel_ocr), ((665, 874), (1258, 875)))
        self.assertIsNone(repeat_buttons(self.panel_ocr[1:]))
        from PIL import Image
        image = Image.new('RGB', (1920, 1080))
        with patch('src.strategy.quest_repeat.ScreenDetector.get_ocr') as ocr:
            lines = [('行动力 消耗：５', .9), ('行动力消耗:5', .9),
                     ('行动力 现在：112/144', .9), ('行动力现在:112/144', .9)]
            ocr.return_value.text_recognizer.return_value = (lines, None)
            self.assertEqual(repeat_ap(image), (5, 112))
            lines[1] = ('行动力消耗:6', .9)
            self.assertIsNone(repeat_ap(image))


if __name__=='__main__':unittest.main()
