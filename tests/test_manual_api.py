import copy
import io
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

from src.server import web_server
from src.strategy.manual_simulation import ManualPlanStore
from tests.test_manual_simulation import manual_fixture


class TestManualAPI(unittest.TestCase):
    def setUp(self):
        self.parser,self.plan=manual_fixture()
        self.state=SimpleNamespace(chaldea_parser=self.parser,device_mgr=MagicMock(),task_lock=threading.Lock(),
            cancel_event=threading.Event(),is_running=False,active_manual_id='',manual_report=None,add_log=MagicMock())
        for p in [patch.object(web_server,'state',self.state),
                  patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=False)),
                  patch('src.workflows.battle_runner.simulation_catalog',return_value=[{'quest_id':'fuyuki_x_a','simulation_supported':True}])]:
            p.start();self.addCleanup(p.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        from pathlib import Path
        p=patch('src.server.manual_api.get_root_dir',return_value=Path(self.tmp.name));p.start();self.addCleanup(p.stop)

    def request(self,path,body=None):
        h=object.__new__(web_server.MAARequestHandler);h.path=path;h._send_json=MagicMock()
        if body is None:h.do_GET()
        else:
            encoded=json.dumps(body).encode();h.headers={'Content-Length':str(len(encoded))};h.rfile=io.BytesIO(encoded);h.do_POST()
        return h._send_json.call_args

    def wait_idle(self):
        limit=time.monotonic()+2
        while self.state.is_running and time.monotonic()<limit:time.sleep(.01)
        self.assertFalse(self.state.is_running)

    def test_inventory_exposes_only_owned_lists_and_custom_support_choices(self):
        result=self.request('/api/manual/inventory').args[0]
        self.assertTrue(result['success']);self.assertEqual(len(result['servants']),5)
        self.assertTrue(result['support_servants']);self.assertTrue(result['support_ces'])
        saber = next(row for row in result['servants'] if row['svt_id'] == 100100)
        self.assertEqual((saber['class_name'], saber['rarity']), ('saber', 5))
        support = next(row for row in result['support_servants'] if row['id'] == 100100)
        self.assertEqual((support['class_name'], support['rarity']), ('saber', 5))
        self.assertIsNone(self.request('/api/manual/status').args[0]['report'])
        self.state.device_mgr.get_connected_devices.assert_not_called()

    def test_invalid_party_and_auto_battle_fields_never_launch(self):
        with patch('src.server.manual_api.ManualSimulation') as simulation:
            for body in [[],{}, {'plan':self.plan,'auto_battle':True}, {'plan':self.plan,'client_revision':True},
                         {'plan':{**self.plan,'party':self.plan['party'][:5]}}]:
                response=self.request('/api/manual/start',body)
                self.assertEqual(response.args[1],400)
            simulation.assert_not_called();self.assertFalse(self.state.is_running)

    def test_task_freezes_account_and_order_and_has_no_device_calls(self):
        entered=threading.Event();release=threading.Event();captured={}
        def run(plan,parser,cancel):
            captured.update(plan=plan,parser=parser);entered.set();release.wait(2)
            return {'passed':True,'message':'manual','steps':[]}
        try:
            with patch('src.server.manual_api.ManualSimulation') as simulation:
                simulation.return_value.run.side_effect=run
                response=self.request('/api/manual/start',{'plan':self.plan,'client_revision':7})
                self.assertTrue(response.args[0]['success']);self.assertTrue(entered.wait(1))
                self.parser.servants[100100].lv=1
                self.assertEqual(captured['parser'].servants[100100].lv,70)
                self.assertEqual(captured['plan'].turns[1].actions[0].actor,2)
                release.set();self.wait_idle()
            self.assertEqual(self.state.manual_report['client_revision'],7)
            self.assertEqual(self.state.manual_report['status'],'completed')
            self.state.device_mgr.get_connected_devices.assert_not_called()
        finally:release.set()

    def test_old_stop_cannot_cancel_another_task_and_correct_stop_keeps_mutex(self):
        entered=threading.Event();release=threading.Event()
        def run(*args):entered.set();release.wait(2);return {'passed':True}
        try:
            with patch('src.server.manual_api.ManualSimulation') as simulation:
                simulation.return_value.run.side_effect=run
                response=self.request('/api/manual/start',{'plan':self.plan})
                identity=response.args[0]['request_id'];self.assertTrue(entered.wait(1))
                self.assertFalse(self.request('/api/manual/stop',{'request_id':'old'}).args[0]['cancelled'])
                self.assertFalse(self.state.cancel_event.is_set())
                self.assertTrue(self.request('/api/manual/stop',{'request_id':identity}).args[0]['cancelled'])
                self.assertTrue(self.state.is_running);self.assertTrue(self.state.cancel_event.is_set())
                for endpoint,body in [('/api/manual/start',{'plan':self.plan}),('/api/run_daily',{}),('/api/run_weekly',{})]:
                    self.assertEqual(self.request(endpoint,body).args[1],409)
                release.set();self.wait_idle()
            self.assertEqual(self.state.manual_report['status'],'cancelled')
            self.assertNotIn('result',self.state.manual_report)
            self.assertFalse(self.request('/api/manual/stop',{'request_id':identity}).args[0]['cancelled'])
        finally:release.set()

    def test_save_and_reload_order_and_ce_without_requiring_simulation_success(self):
        store=ManualPlanStore(self.tmp.name)
        with patch('src.server.manual_api.ManualPlanStore',return_value=store):
            response=self.request('/api/manual/plans/save',{'name':'我的模拟','plan':self.plan})
            self.assertTrue(response.args[0]['success'])
            result=self.request('/api/manual/plans').args[0]['plans']
        self.assertEqual(result[0]['plan']['turns'][1]['actions'][0]['kind'],'skill')
        self.assertEqual(result[0]['plan']['party'][0]['ce_id'],9400340)

    def test_sync_lock_and_worker_errors_release_state(self):
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=True)):
            self.assertEqual(self.request('/api/manual/start',{'plan':self.plan}).args[1],409)
        with patch('src.server.manual_api.ManualSimulation') as simulation:
            simulation.return_value.run.side_effect=RuntimeError('cannot calculate')
            self.request('/api/manual/start',{'plan':self.plan});self.wait_idle()
        self.assertEqual(self.state.manual_report['status'],'failed')
        self.assertEqual(self.state.manual_report['error'],'cannot calculate')


if __name__=='__main__':unittest.main()
