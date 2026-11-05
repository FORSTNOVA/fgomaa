import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.server import web_server
from tests.test_battle_runner import catalog_row


class TestBattleAPI(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(chaldea_parser=SimpleNamespace(servants={1:{'lv':80}}),
            device_mgr=MagicMock(), task_lock=threading.Lock(), cancel_event=threading.Event(),
            is_running=False, battle_report=None, add_log=MagicMock())
        for p in [patch.object(web_server,'state',self.state),
                  patch('src.workflows.battle_runner.simulation_catalog', return_value=[catalog_row()]),
                  patch('src.strategy.quest_catalog.catalog_sync', return_value=SimpleNamespace(running=False))]:
            p.start(); self.addCleanup(p.stop)

    def request(self,path,body=None):
        h=object.__new__(web_server.MAARequestHandler); h.path=path; h._send_json=MagicMock()
        if body is None:
            h.do_GET()
        else:
            encoded=json.dumps(body).encode(); h.headers={'Content-Length':str(len(encoded))}; h.rfile=io.BytesIO(encoded)
            h.do_POST()
        return h._send_json.call_args

    def wait_idle(self):
        deadline=time.monotonic()+2
        while self.state.is_running and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.state.is_running)

    def test_catalog_and_status_do_not_launch_or_touch_device(self):
        self.assertEqual(self.request('/api/battle/catalog').args[0]['quests'][0]['game_id'],123)
        self.assertIsNone(self.request('/api/battle/status').args[0]['report'])
        self.state.device_mgr.get_connected_devices.assert_not_called(); self.assertFalse(self.state.is_running)

    def test_missing_auto_flag_defaults_to_preview_and_freezes_account(self):
        entered=threading.Event(); release=threading.Event(); captured={}
        def run(request,**kwargs):
            captured['request']=request
            entered.set(); release.wait(2)
            kwargs['on_progress']({'mode':'battle_lab','status':'preview_passed','preview_only':True})
            return SimpleNamespace(success=True,status='preview_passed',error='')
        try:
            with patch('src.workflows.battle_runner.BattleRunner') as runner:
                runner.return_value.run.side_effect=run
                response=self.request('/api/battle/start',{'quest_id':'test_quest'})
                self.assertTrue(response.args[0]['success']); self.assertTrue(entered.wait(1))
                self.assertFalse(captured['request'].auto_battle)
                account=runner.call_args.args[0]
                self.state.chaldea_parser.servants[1]['lv']=1
                self.assertEqual(account.servants[1]['lv'],80)
                release.set(); self.wait_idle()
            self.assertEqual(self.state.battle_report['status'],'preview_passed')
        finally:release.set()

    def test_invalid_or_forged_requests_never_launch(self):
        with patch('src.workflows.battle_runner.BattleRunner') as runner:
            for body in [{},[],{'quest_id':'unknown'}, {'quest_id':'test_quest','auto_battle':'yes'},
                         {'quest_id':'test_quest','auto_battle':True}, {'quest_id':'test_quest','options':{'ap_budget':0}},
                         {'quest_id':'test_quest','recommendation':{}}, {'quest_id':'test_quest','options':{'refill_apple':'stone'}}]:
                response=self.request('/api/battle/start',body)
                self.assertEqual(response.args[1],400); self.assertFalse(self.state.is_running)
            runner.assert_not_called()

    def test_explicit_battle_and_disabled_limits_reach_worker(self):
        captured={}
        def run(request,**kwargs):
            captured['request']=request
            return SimpleNamespace(success=True,status='completed',error='')
        with patch('src.workflows.battle_runner.BattleRunner') as runner:
            runner.return_value.run.side_effect=run
            response=self.request('/api/battle/start',{'quest_id':'test_quest','auto_battle':True,'device_serial':'target',
                'options':{'max_quests':None,'ap_budget':None,'refill_apple':'bronze','max_apples':None}})
            self.assertTrue(response.args[0]['success']); self.wait_idle()
        request=captured['request']
        self.assertTrue(request.auto_battle); self.assertEqual(request.device_serial,'target')
        self.assertIsNone(request.options.ap_budget); self.assertIsNone(request.options.max_apples)

    def test_stop_keeps_all_workflows_excluded_until_worker_exits(self):
        entered=threading.Event(); release=threading.Event()
        def run(*args,**kwargs):
            entered.set(); release.wait(2)
            return SimpleNamespace(success=False,status='cancelled',error='cancelled')
        try:
            with patch('src.workflows.battle_runner.BattleRunner') as runner:
                runner.return_value.run.side_effect=run
                self.request('/api/battle/start',{'quest_id':'test_quest'})
                self.assertTrue(entered.wait(1)); self.assertTrue(self.state.is_running)
                self.request('/api/stop',{})
                self.assertTrue(self.state.cancel_event.is_set()); self.assertTrue(self.state.is_running)
                for endpoint in ['/api/battle/start','/api/run_daily','/api/run_weekly','/api/quests/sync']:
                    response=self.request(endpoint,{'quest_id':'test_quest'})
                    self.assertEqual(response.args[1],409)
                release.set(); self.wait_idle()
        finally:release.set()

    def test_catalog_replacement_prevents_start_and_preserves_stop_signal(self):
        self.state.cancel_event.set()
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=True)), \
             patch('src.workflows.battle_runner.BattleRunner') as runner:
            response=self.request('/api/battle/start',{'quest_id':'test_quest'})
        self.assertEqual(response.args[1],409); runner.assert_not_called()
        self.assertTrue(self.state.cancel_event.is_set())


if __name__=='__main__':unittest.main()
