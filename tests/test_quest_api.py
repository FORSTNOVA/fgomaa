import json
import threading
import time
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from src.server import web_server


class TestQuestAPI(unittest.TestCase):
    def setUp(self):
        self.state=SimpleNamespace(chaldea_parser=SimpleNamespace(servants={1:object()},quest_records={93000001:{'clearNum':1,'questPhase':3}}),
            device_mgr=MagicMock(),task_lock=threading.Lock(),is_running=False,cancel_event=threading.Event(),quest_report=None,add_log=MagicMock())
        patcher=patch.object(web_server,'state',self.state);patcher.start();self.addCleanup(patcher.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),web_server.MAARequestHandler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def request(self,path,body=None):
        req=Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        try:
            with urlopen(req,timeout=5) as res:return res.status,json.load(res)
        except HTTPError as exc:return exc.code,json.load(exc)

    def test_catalog_and_status_are_available_without_starting(self):
        status,data=self.request('/api/quests/catalog');self.assertEqual(status,200)
        self.assertTrue(any(q['quest_id']=='fuyuki_x_a' and q['supported'] for q in data['quests']))
        self.assertIsNone(self.request('/api/quests/status')[1]['report'])
        self.assertFalse(self.state.is_running)

    def test_invalid_queue_never_launches_runner(self):
        with patch('src.workflows.quest_runner.QuestRunner') as runner:
            status,_=self.request('/api/quests/run',{'queue':[{'quest_id':'made_up','runs':1}]})
        self.assertEqual(status,400);runner.assert_not_called();self.assertFalse(self.state.is_running)

    def test_sync_status_and_manual_force_use_the_shared_service(self):
        sync=MagicMock(running=False)
        sync.status.return_value={'running':False,'stats':{'quests':100}}
        sync.start.return_value=True
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=sync):
            self.assertEqual(self.request('/api/quests/sync/status')[1]['stats']['quests'],100)
            self.assertEqual(self.request('/api/quests/sync',{'force':True})[0],200)
        self.assertTrue(sync.start.call_args.kwargs['force'])

    def test_sync_is_rejected_during_battle_and_invalid_force_is_rejected(self):
        self.state.is_running=True
        with patch('src.strategy.quest_catalog.catalog_sync') as sync:
            self.assertEqual(self.request('/api/quests/sync',{'force':True})[0],409)
            self.assertEqual(self.request('/api/quests/sync',{'force':'yes'})[0],400)
        sync.return_value.start.assert_not_called()

    def test_battle_cannot_start_while_catalog_is_being_replaced(self):
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=True,snapshot=lambda:{})):
            body={'queue':[{'quest_id':'fuyuki_x_a','runs':1}]}
            for endpoint in ['/api/quests/run','/api/run_daily','/api/run_weekly']:
                self.assertEqual(self.request(endpoint,body)[0],409)
        self.assertFalse(self.state.is_running)

    def test_stop_does_not_unlock_or_clear_cancellation_before_worker_exits(self):
        started=threading.Event();release=threading.Event()
        def execute(*args,**kwargs):
            started.set();release.wait(5)
            return SimpleNamespace(success=False,error='cancelled')
        try:
            with patch('src.workflows.quest_runner.QuestRunner') as runner:
                runner.return_value.run.side_effect=execute
                body={'queue':[{'quest_id':'fuyuki_x_a','runs':1}],'device_serial':'target'}
                self.assertEqual(self.request('/api/quests/preview',body)[0],200)
                self.assertTrue(started.wait(2));self.assertTrue(self.state.is_running)
                self.assertEqual(self.request('/api/stop',{})[0],200)
                self.assertTrue(self.state.cancel_event.is_set());self.assertTrue(self.state.is_running)
                for endpoint in ['/api/quests/run','/api/run_daily','/api/run_weekly']:
                    self.assertEqual(self.request(endpoint,body)[0],409)
                self.assertTrue(self.state.cancel_event.is_set())
                release.set()
                for _ in range(100):
                    if not self.state.is_running:break
                    time.sleep(.01)
                self.assertFalse(self.state.is_running)
        finally:release.set()


if __name__=='__main__':unittest.main()
