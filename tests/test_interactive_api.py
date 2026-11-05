import copy
import io
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

from src.server import web_server,interactive_api
from src.strategy.interactive_simulation import InteractiveSimulation,SetupDraft
from tests.test_manual_simulation import manual_fixture


class TestInteractiveAPI(unittest.TestCase):
    def setUp(self):
        self.parser,plan=manual_fixture()
        self.draft=SetupDraft.model_validate({k:v for k,v in plan.items() if k!='turns'}).model_dump()
        self.state=SimpleNamespace(chaldea_parser=self.parser,device_mgr=MagicMock(),task_lock=threading.Lock(),
            cancel_event=threading.Event(),is_running=False,active_interactive_id='',interactive_report=None,add_log=MagicMock())
        for p in [patch.object(web_server,'state',self.state),
                  patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=False)),
                  patch('src.workflows.battle_runner.simulation_catalog',return_value=[{'quest_id':'fuyuki_x_a','simulation_supported':True}])]:
            p.start();self.addCleanup(p.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)

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

    def test_rejects_live_flags_invalid_events_and_invalid_setup_before_worker(self):
        with patch.object(interactive_api,'InteractiveSimulation') as engine:
            for endpoint,body in [('/enter',[]),('/enter',{}),('/enter',{'draft':self.draft,'auto_battle':True}),
                ('/enter',{'draft':self.draft,'client_revision':True}),
                ('/enter',{'draft':{**self.draft,'party':self.draft['party'][:5]}}),
                ('/replay',{'session_id':'../private','events':[]}),
                ('/replay',{'session_id':'a'*32,'events':[{'kind':'np','actor':1}]})]:
                with self.subTest(body=body):self.assertEqual(self.request('/api/interactive'+endpoint,body).args[1],400)
            engine.assert_not_called();self.assertFalse(self.state.is_running)
        self.state.device_mgr.get_connected_devices.assert_not_called()

    def test_enter_freezes_account_and_echoes_revision_without_device_calls(self):
        entered=threading.Event();release=threading.Event();captured={}
        def run(draft,parser,cancel):
            captured.update(draft=draft,parser=parser);entered.set();release.wait(2)
            return {'scene':{'accepted':True,'players':[]}}
        try:
            with patch.object(interactive_api,'InteractiveSimulation') as engine:
                engine.return_value.enter.side_effect=run
                response=self.request('/api/interactive/enter',{'draft':self.draft,'client_revision':12})
                self.assertTrue(response.args[0]['success']);self.assertTrue(entered.wait(1))
                self.parser.servants[100100].lv=1
                self.assertEqual(captured['parser'].servants[100100].lv,70)
                self.assertEqual(captured['draft'],self.draft)
                release.set();self.wait_idle()
            self.assertEqual(self.state.interactive_report['client_revision'],12)
            self.assertEqual(self.state.interactive_report['status'],'completed')
            self.state.device_mgr.get_connected_devices.assert_not_called()
        finally:release.set()

    def test_replay_does_not_reimport_current_account(self):
        with patch.object(interactive_api,'InteractiveSimulation') as engine:
            engine.return_value.replay.return_value={'scene':{'accepted':False},'failure':{'message':'NP不足'}}
            self.state.chaldea_parser=None
            self.request('/api/interactive/replay',{'session_id':'b'*32,'events':[{'kind':'cards'}],'client_revision':3})
            self.wait_idle();engine.return_value.replay.assert_called_once_with('b'*32,[{'kind':'cards'}],self.state.cancel_event)
        self.assertEqual(self.state.interactive_report['status'],'completed')
        self.assertFalse(self.state.interactive_report['result']['scene']['accepted'])

    def test_cancel_matches_request_keeps_mutex_and_discards_late_result(self):
        entered=threading.Event();release=threading.Event()
        def run(*args):entered.set();release.wait(2);return {'scene':{'accepted':True}}
        try:
            with patch.object(interactive_api,'InteractiveSimulation') as engine:
                engine.return_value.enter.side_effect=run
                response=self.request('/api/interactive/enter',{'draft':self.draft})
                identity=response.args[0]['request_id'];self.assertTrue(entered.wait(1))
                self.assertFalse(self.request('/api/interactive/stop',{'request_id':'old'}).args[0]['cancelled'])
                self.assertFalse(self.state.cancel_event.is_set())
                self.assertTrue(self.request('/api/interactive/stop',{'request_id':identity}).args[0]['cancelled'])
                self.assertTrue(self.state.is_running)
                for endpoint,body in [('/api/interactive/enter',{'draft':self.draft}),
                    ('/api/interactive/replay',{'session_id':'a'*32,'events':[]}),
                    ('/api/manual/start',{'plan':manual_fixture()[1]}),('/api/run_daily',{}),('/api/run_weekly',{})]:
                    with self.subTest(endpoint=endpoint):self.assertEqual(self.request(endpoint,body).args[1],409)
                release.set();self.wait_idle()
            self.assertEqual(self.state.interactive_report['status'],'cancelled')
            self.assertNotIn('result',self.state.interactive_report)
            self.assertFalse(self.request('/api/interactive/stop',{'request_id':identity}).args[0]['cancelled'])
        finally:release.set()

    def test_catalog_sync_blocks_and_errors_release_mutex(self):
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=True)):
            self.assertEqual(self.request('/api/interactive/enter',{'draft':self.draft}).args[1],409)
        with patch.object(interactive_api,'InteractiveSimulation') as engine:
            engine.return_value.enter.side_effect=ValueError('公开数据缺失')
            self.request('/api/interactive/enter',{'draft':self.draft});self.wait_idle()
        self.assertEqual(self.state.interactive_report['status'],'failed')
        self.assertEqual(self.state.interactive_report['error'],'公开数据缺失')
        self.assertEqual(self.state.active_interactive_id,'')

    def test_incomplete_setup_and_redo_branch_save_reload_without_entering(self):
        engine=InteractiveSimulation(root=self.tmp.name)
        empty=copy.deepcopy(self.draft);empty['party'][0]['svt_id']=0
        entries=[{'kind':'setup','label':'选关', 'draft':empty},
            {'kind':'setup','label':'选择角色','draft':self.draft}]
        with patch.object(interactive_api,'InteractiveSimulation',return_value=engine):
            result=self.request('/api/interactive/save',{'recording':{'name':'未完成编队','entries':entries,'cursor':0}}).args[0]
            self.assertTrue(result['success'])
            saved=self.request('/api/interactive/recordings').args[0]['recordings'][0]
        self.assertEqual(saved['cursor'],0);self.assertEqual(len(saved['entries']),2)
        self.assertEqual(saved['entries'][0]['draft']['party'][0]['svt_id'],0)
        self.state.device_mgr.get_connected_devices.assert_not_called()

    def test_status_and_runtime_are_read_only(self):
        self.assertIsNone(self.request('/api/interactive/status').args[0]['report'])
        runtime=self.request('/api/interactive/runtime').args[0]
        self.assertIn('interactive_v1',runtime['features'])
        self.state.device_mgr.get_connected_devices.assert_not_called()

    def test_live_recording_uses_selected_device_and_shared_task_lock(self):
        entered, release = threading.Event(), threading.Event()

        def run(recording_id, device_serial, *, cancel_event):
            entered.set()
            release.wait(2)
            return {'confirmed_clears': 1}

        body = {'recording_id': 'a' * 32, 'device_serial': '127.0.0.1:16417'}
        with patch('src.workflows.recorded_farm.RecordedFarmRunner') as runner:
            runner.return_value.run.side_effect = run
            try:
                self.assertTrue(self.request('/api/interactive/farm', body).args[0]['success'])
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.request('/api/interactive/farm', body).args[1], 409)
                runner.return_value.run.assert_called_once_with(
                    body['recording_id'], body['device_serial'], cancel_event=self.state.cancel_event)
            finally:
                release.set()
                self.wait_idle()
            self.state.add_log.assert_called_with('录制实机刷本完成一场', 'success')


if __name__=='__main__':unittest.main()
