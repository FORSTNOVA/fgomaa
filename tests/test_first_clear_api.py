import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

from src.server import web_server


class TestFirstClearAPI(unittest.TestCase):
    def setUp(self):
        self.state=SimpleNamespace(chaldea_parser=SimpleNamespace(servants={1:object()},quest_records_complete=True),
              device_mgr=MagicMock(),task_lock=threading.Lock(),cancel_event=threading.Event(),
              is_running=False,quest_report=None,add_log=MagicMock())
        state_patch=patch.object(web_server,'state',self.state);state_patch.start();self.addCleanup(state_patch.stop)
        sync_patch=patch('src.strategy.quest_catalog.catalog_sync',return_value=SimpleNamespace(running=False))
        sync_patch.start();self.addCleanup(sync_patch.stop)

    def request(self,path,body=None):
        h=object.__new__(web_server.MAARequestHandler);h.path=path;h._send_json=MagicMock()
        if body is None:h.do_GET()
        else:
            encoded=json.dumps(body).encode();h.headers={'Content-Length':str(len(encoded))};h.rfile=io.BytesIO(encoded)
            h.do_POST()
        return h._send_json.call_args

    def test_catalog_preserves_scope_and_returns_recorded_progress(self):
        rows=[{'supported':True,'progress':{'next_phase':2}},{'supported':False,'reason':'closed'}]
        with patch('src.workflows.first_clear_runner.FirstClearLedger') as ledger, \
             patch('src.workflows.first_clear_runner.first_clear_catalog',return_value=rows) as catalog:
            response=self.request('/api/quests/first-clear/catalog?device_serial=target&war_id=404')
        self.assertEqual(response.args[0]['candidate_count'],1)
        self.assertEqual(catalog.call_args.args[1].war_ids,[404])
        self.assertEqual(ledger.call_args.args[1],'target')

    def test_invalid_scope_and_refill_options_are_rejected(self):
        response=self.request('/api/quests/first-clear/catalog?war_id=401')
        self.assertEqual(response.args[1],400)
        for body in [{'device_serial':'target','options':{'auto_refill':True}},
                     {'device_serial':'target','options':{'refill_apple':'stone','max_apples':1}},
                     {'device_serial':'target','options':{'refill_apple':'gold','max_apples':0}},
                     {'device_serial':'target','options':{'war_ids':[401]}},
                     {'device_serial':'','options':{}}, {'device_serial':'target','queue':[]}]:
            response=self.request('/api/quests/first-clear/run',body)
            self.assertEqual(response.args[1],400);self.assertFalse(self.state.is_running)

    def test_new_story_chapters_are_available_as_first_clear_catalog_scopes(self):
        from src.strategy.chapter_navigation import CHAPTERS
        for wid in CHAPTERS:
            with self.subTest(wid=wid),patch('src.workflows.first_clear_runner.FirstClearLedger'), \
                 patch('src.workflows.first_clear_runner.first_clear_catalog',return_value=[]) as catalog:
                response=self.request(f'/api/quests/first-clear/catalog?device_serial=target&war_id={wid}')
                self.assertTrue(response.args[0]['success'])
                self.assertEqual(catalog.call_args.args[1].war_ids,[wid])

    def test_old_manual_queue_still_rejects_unverified_uncleared_quests(self):
        with patch('src.workflows.quest_runner.resolve_queue',side_effect=ValueError('uncleared')):
            response=self.request('/api/quests/run',{'queue':[{'quest_id':'unknown','runs':1}]})
        self.assertEqual(response.args[1],400)

    def test_disabled_limits_are_passed_to_worker_as_null(self):
        captured={};finished=threading.Event()
        def run(options,**kwargs):
            captured['options']=options;finished.set()
            return SimpleNamespace(success=True,error='')
        with patch('src.workflows.first_clear_runner.FirstClearRunner') as runner:
            runner.return_value.run.side_effect=run
            response=self.request('/api/quests/first-clear/run',{'device_serial':'target','options':{
                'max_quests':None,'ap_budget':None,'refill_apple':'silver','max_apples':None}})
            self.assertTrue(response.args[0]['success']);self.assertTrue(finished.wait(1))
            deadline=time.monotonic()+1
            while self.state.is_running and time.monotonic()<deadline:time.sleep(.01)
        options=captured['options']
        self.assertIsNone(options.max_quests);self.assertIsNone(options.ap_budget);self.assertIsNone(options.max_apples)
        self.assertEqual(options.refill_apple,'silver')

    def test_start_worker_and_stop_keep_global_exclusion_until_worker_exits(self):
        entered=threading.Event();release=threading.Event();captured={}
        def run(options,**kwargs):
            captured.update(options=options,**kwargs);entered.set();release.wait(2)
            kwargs['on_progress']({'mode':'first_clear','status':'cancelled'})
            return SimpleNamespace(success=False,error='cancelled')
        with patch('src.workflows.first_clear_runner.FirstClearRunner') as runner:
            runner.return_value.run.side_effect=run
            body={'device_serial':'target','options':{'war_ids':[404],'max_quests':1,'ap_budget':50,
                                                    'refill_apple':'silver','max_apples':2}}
            response=self.request('/api/quests/first-clear/run',body)
            self.assertTrue(response.args[0]['success']);self.assertTrue(entered.wait(1))
            self.assertTrue(self.state.is_running)
            response=self.request('/api/stop',{})
            self.assertTrue(self.state.cancel_event.is_set());self.assertTrue(self.state.is_running)
            response=self.request('/api/quests/first-clear/run',body)
            self.assertEqual(response.args[1],409)
            release.set()
            deadline=time.monotonic()+2
            while self.state.is_running and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.state.is_running)
        self.assertEqual(captured['options'].war_ids,[404])
        self.assertEqual((captured['options'].refill_apple,captured['options'].max_apples),('silver',2))
        self.assertIs(captured['cancel_event'],self.state.cancel_event)
        self.assertEqual(self.state.quest_report['mode'],'first_clear')


if __name__=='__main__':unittest.main()
