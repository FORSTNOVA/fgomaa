import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.workflows.quest_runner import QuestRunner, quest_catalog, resolve_queue
from src.strategy.quest_navigation import navigate_daily_quest
from src.strategy.screen_detector import GameScreen
from src.strategy.smart_engine import SmartEngine
from src.strategy.quest_battle import QuestBattleExecutor


class TestQuestRunner(unittest.TestCase):
    def setUp(self):
        self.parser = SimpleNamespace(servants={1:object()},quest_records={93000001:{'clearNum':1,'questPhase':3},93000005:{'clearNum':1,'questPhase':3}})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.devices = MagicMock(adb_path='adb')
        self.devices.get_connected_devices.return_value=[SimpleNamespace(serial='target')]
        self.runner = QuestRunner(self.parser,self.devices,report_root=self.tmp.name)
        self.engine = MagicMock(last_quest_clears=0,current_recommendation=None)
        self.queue=[{'quest_id':'fuyuki_x_a','runs':1},{'quest_id':'fuyuki_x_e','runs':2},{'quest_id':'fuyuki_x_a','runs':1}]
        def execute(target,**kwargs):
            self.engine.last_quest_clears=target.runs
            return True
        self.engine.run_quest.side_effect=execute
        self.fake_engine=patch('src.workflows.quest_runner.SmartEngine',return_value=self.engine)
        self.fake_ctrl=patch('src.workflows.quest_runner.FGOController')
        self.fake_engine.start();self.fake_ctrl.start()
        self.addCleanup(self.fake_engine.stop);self.addCleanup(self.fake_ctrl.stop)

    def test_unknown_quest_never_falls_back_to_similar_name(self):
        with self.assertRaises(ValueError):resolve_queue([{'quest_id':'qp_fake','runs':1}],self.parser)

    def test_invalid_queue_or_runs_rejected(self):
        for data in [[],{},[{'quest_id':'fuyuki_x_a','runs':0}],[{'quest_id':'fuyuki_x_a','runs':True}],[{'quest_id':'fuyuki_x_a','runs':'2'}],[{'quest_id':'fuyuki_x_a','runs':1,'war':'forged'}]]:
            with self.subTest(data=data),self.assertRaises(ValueError):resolve_queue(data,self.parser)

    def test_uncleared_free_quest_is_not_executable(self):
        with self.assertRaises(ValueError):resolve_queue([{'quest_id':'fuyuki_x_b','runs':1}],self.parser)
        self.assertTrue(resolve_queue([{'quest_id':'daily_qp_extreme','runs':1}],self.parser))

    def test_partial_clear_does_not_prove_final_phase(self):
        self.parser.quest_records[93000001]['questPhase']=1
        with self.assertRaises(ValueError):resolve_queue(self.queue[:1],self.parser)

    def test_preview_has_no_device_or_battle_calls_and_no_clears(self):
        with patch.object(QuestBattleExecutor,'prepare',return_value=True):
            result=self.runner.run(self.queue,preview_only=True)
        self.assertTrue(result.success);self.assertEqual(result.status,'preview_passed')
        self.assertEqual(result.confirmed_clears,0)
        self.devices.get_connected_devices.assert_not_called();self.engine.run_quest.assert_not_called()

    def test_complete_queue_counts_actual_clears_and_writes_report(self):
        result=self.runner.run(self.queue,device_serial='target')
        self.assertTrue(result.success);self.assertEqual(result.confirmed_clears,4)
        self.assertEqual(result.estimated_ap_spent,16)
        record=json.loads((Path(result.artifacts_dir)/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(record['success']);self.assertEqual(self.engine.run_quest.call_count,3)

    def test_failure_preserves_partial_clears_and_never_executes_next(self):
        def execute(target,**kwargs):
            self.engine.last_quest_clears=1
            return target.quest_id=='fuyuki_x_a'
        self.engine.run_quest.side_effect=execute
        result=self.runner.run(self.queue,device_serial='target')
        self.assertFalse(result.success);self.assertEqual(result.confirmed_clears,2)
        self.assertEqual(result.stations[2]['status'],'pending');self.assertEqual(self.engine.run_quest.call_count,2)

    def test_exception_after_victory_preserves_confirmed_count(self):
        def execute(*args,**kwargs):
            self.engine.last_quest_clears=1
            raise RuntimeError('settlement failed')
        self.engine.run_quest.side_effect=execute
        result=self.runner.run(self.queue,device_serial='target')
        self.assertFalse(result.success);self.assertEqual(result.confirmed_clears,1)
        self.assertEqual(self.engine.run_quest.call_count,1)

    def test_cancellation_between_stations_does_not_enter_next(self):
        event=threading.Event()
        def execute(target,**kwargs):
            self.engine.last_quest_clears=target.runs;event.set();return True
        self.engine.run_quest.side_effect=execute
        result=self.runner.run(self.queue,device_serial='target',cancel_event=event)
        self.assertFalse(result.success);self.assertEqual(result.status,'cancelled')
        self.assertEqual(result.confirmed_clears,1);self.assertEqual(self.engine.run_quest.call_count,1)

    def test_ambiguous_device_is_rejected_before_controller(self):
        self.devices.get_connected_devices.return_value=[SimpleNamespace(serial='a'),SimpleNamespace(serial='b')]
        result=self.runner.run(self.queue)
        self.assertFalse(result.success);self.engine.run_quest.assert_not_called()

    def test_invalid_clear_count_cannot_claim_success(self):
        def execute(*args,**kwargs):self.engine.last_quest_clears=999;return True
        self.engine.run_quest.side_effect=execute
        result=self.runner.run(self.queue,device_serial='target')
        self.assertFalse(result.success);self.assertEqual(result.confirmed_clears,0)

    def test_legacy_entry_delegates_to_shared_executor(self):
        engine=SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        engine.run_quest=MagicMock(return_value=False)
        self.assertFalse(engine.run_reactive_weekly_plan('plan'))
        engine.run_quest.assert_called_once_with('plan',None)


class TestDailyQuestNavigation(unittest.TestCase):
    def engine(self,frames):
        return SimpleNamespace(is_cancelled=lambda:False,get_current_screen=MagicMock(side_effect=frames),ctrl=MagicMock(),log=MagicMock())

    def test_unrelated_party_page_cannot_prove_daily_identity(self):
        e=self.engine([(GameScreen.PARTY_CONFIRM,{})])
        self.assertFalse(navigate_daily_quest(e,'daily_qp_extreme'));e.ctrl.click.assert_not_called()

    @patch('src.strategy.quest_navigation.AnchorVerifier.compute_frame_diff',return_value=0)
    @patch('src.strategy.quest_navigation.ScreenDetector.find_target_quest_coordinate',return_value=(1300,400))
    def test_exact_card_click_then_support_proves_navigation(self,find,diff):
        e=self.engine([(GameScreen.CHALDEA_GATE,{'ocr_results':[]})]*2+[(GameScreen.SUPPORT_SELECT,{})])
        self.assertTrue(navigate_daily_quest(e,'daily_qp_extreme'))
        e.ctrl.click.assert_called_once_with(1300,400,delay_after=1.5)

    @patch('src.strategy.quest_navigation.AnchorVerifier.compute_frame_diff',return_value=0)
    @patch('src.strategy.quest_navigation.ScreenDetector.find_target_quest_coordinate',return_value=None)
    def test_unopened_daily_quest_stops_without_substitution(self,find,diff):
        e=self.engine([(GameScreen.CHALDEA_GATE,{'ocr_results':[]})]*10)
        self.assertFalse(navigate_daily_quest(e,'daily_training_saber'))
        e.ctrl.click.assert_not_called()


if __name__=='__main__':unittest.main()
