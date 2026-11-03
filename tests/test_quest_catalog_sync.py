import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from src.strategy.quest_catalog import QuestCatalogSync, normalize_export, quest_bindings
from src.workflows.quest_runner import quest_catalog, resolve_queue
from src.strategy.quest_navigation import synced_daily_coordinate
from src.strategy.laplace import PublicData, LaplaceError, recommendation_signature
from src.strategy.auto_team_builder import AutoTeamBuilder
from tests.test_laplace_integration import fixture


def exports():
    quest={'id':93000007,'name':'熊熊燃烧的森林','phases':[1,2,3],'type':'free','consumeType':'ap','consume':6,
           'afterClear':'repeatLast','openedAt':1,'closedAt':2145888000}
    story={**quest,'id':100001,'name':'剧情','type':'main'}
    return [{'id':100,'longName':'特异点F\n燃烧污染都市 冬木','eventId':0,
             'spots':[{'id':10007,'name':'未确认坐标X-H','quests':[quest,story]}]}]


class TestQuestCatalogSync(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1000
        self.fetch=MagicMock(side_effect=lambda url,headers: (200,{}, {'CN':{'hash':'one'},'JP':{'hash':'jp'}}) if url.endswith('/info') else (200,{'Etag':'abc','Last-Modified':'date'},exports()))
        self.service=QuestCatalogSync(self.temp.name,self.fetch,lambda:self.now)

    def test_full_export_normalizes_exact_ids_phases_and_source(self):
        self.assertTrue(self.service.refresh())
        d=self.service.snapshot();self.assertEqual(d['stats']['quests'],2)
        self.assertEqual(d['entries']['atlas_cn_93000007']['phase'],3)
        self.assertEqual(d['entries']['atlas_cn_93000007']['war'],'特异点F 燃烧污染都市 冬木')
        self.assertFalse(self.service.due())

    def test_conditional_304_reuses_catalog_without_refetching_detail(self):
        self.service.refresh();self.now+=86401
        self.fetch.side_effect=lambda url,headers:(200,{}, {'CN':{'hash':'one'},'JP':{'hash':'jp'}}) if url.endswith('/info') else (304,{},None)
        self.assertTrue(self.service.refresh())
        self.assertEqual(self.fetch.call_args.args[1]['If-None-Match'],'abc')
        self.assertEqual(self.service.snapshot()['checked_at'],self.now)
        self.assertEqual(self.service.snapshot()['synced_at'],1000)
        self.assertEqual(self.service.snapshot()['enemy_refresh_after'],1000)

    def test_forced_refresh_does_not_send_conditional_headers(self):
        self.service.refresh();self.now+=2
        self.assertTrue(self.service.refresh(force=True))
        self.assertEqual(self.fetch.call_args.args[1],{})
        self.assertEqual(self.service.snapshot()['enemy_refresh_after'],1002)

    def test_version_changed_304_invalidates_enemy_cache(self):
        self.service.refresh();self.now=2000
        self.fetch.side_effect=lambda url,headers:(200,{}, {'CN':{'hash':'two'}}) if url.endswith('/info') else (304,{},None)
        self.assertTrue(self.service.refresh());self.assertEqual(self.service.snapshot()['enemy_refresh_after'],2000)

    def test_network_failure_preserves_published_snapshot_and_backs_off(self):
        self.service.refresh();before=(Path(self.temp.name)/'current.json').read_bytes()
        self.now+=86401;self.fetch.side_effect=TimeoutError('offline')
        self.assertFalse(self.service.refresh());self.assertEqual((Path(self.temp.name)/'current.json').read_bytes(),before)
        self.assertFalse(self.service.due());self.assertIn('offline',self.service.status()['error'])
        self.now+=601;self.assertTrue(self.service.due())

    def test_malformed_or_empty_export_cannot_replace_valid_catalog(self):
        self.service.refresh();before=(Path(self.temp.name)/'current.json').read_bytes()
        for value in [[],{},[{'id':'forged'}]]:
            self.fetch.side_effect=lambda url,headers:(200,{}, {'CN':{'hash':'one'}}) if url.endswith('/info') else (200,{},value)
            self.assertFalse(self.service.refresh(force=True));self.assertEqual((Path(self.temp.name)/'current.json').read_bytes(),before)

    def test_placeholder_without_phases_is_visible_but_not_executable(self):
        wars=exports();wars[0]['spots'][0]['quests'][0]['phases']=[]
        rows=normalize_export(wars);self.assertEqual(rows['atlas_cn_93000007']['phase'],0)

    def test_corrupt_current_recovers_previous_valid_snapshot(self):
        self.service.refresh();self.now=2000;self.service.refresh(force=True)
        (Path(self.temp.name)/'current.json').write_text('broken',encoding='utf-8')
        restored=QuestCatalogSync(self.temp.name,self.fetch,lambda:self.now)
        self.assertEqual(restored.snapshot()['synced_at'],1000)

    def test_conflicting_duplicate_ids_fail_instead_of_silently_rebinding(self):
        wars=exports();wars[0]['spots'][0]['quests'].append({**wars[0]['spots'][0]['quests'][0],'name':'other'})
        with self.assertRaises(ValueError):normalize_export(wars)

    def test_parallel_refresh_is_rejected_while_download_is_active(self):
        started=threading.Event();release=threading.Event()
        def fetch(url,headers):
            started.set();release.wait(3)
            return (200,{}, {'CN':{'hash':'one'}}) if url.endswith('/info') else (200,{},exports())
        self.service.fetch=fetch
        self.assertTrue(self.service.start(force=True));self.assertTrue(started.wait(1))
        self.assertFalse(self.service.start(force=True));self.assertFalse(self.service.refresh(force=True))
        release.set()
        import time
        for _ in range(100):
            if not self.service.running:break
            time.sleep(.01)
        self.assertFalse(self.service.running)

    def test_new_free_quest_requires_clear_evidence_and_story_stays_disabled(self):
        self.service.refresh()
        parser=SimpleNamespace(servants={1:object()},quest_records={93000007:{'clearNum':1,'questPhase':3}})
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=self.service):
            rows={r['quest_id']:r for r in quest_catalog(parser)}
            self.assertTrue(rows['atlas_cn_93000007']['supported'])
            self.assertFalse(rows['atlas_cn_100001']['supported'])
            self.assertEqual(resolve_queue([{'quest_id':'atlas_cn_93000007','runs':1}],parser)[0].game_id,93000007)
            parser.quest_records={}
            with self.assertRaises(ValueError):resolve_queue([{'quest_id':'atlas_cn_93000007','runs':1}],parser)

    def test_unsupported_chapter_and_expired_quest_cannot_enter_queue(self):
        wars=exports();wars[0]['id']=999
        self.fetch.side_effect=lambda url,headers:(200,{}, {'CN':{'hash':'one'}}) if url.endswith('/info') else (200,{},wars)
        self.service.refresh()
        parser=SimpleNamespace(servants={1:object()},quest_records={93000007:{'clearNum':1,'questPhase':3}})
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=self.service):
            row=next(r for r in quest_catalog(parser) if r['quest_id']=='atlas_cn_93000007')
            self.assertFalse(row['supported']);self.assertIn('导航',row['reason'])
        wars[0]['id']=100;wars[0]['spots'][0]['quests'][0]['closedAt']=5
        self.service.refresh(force=True)
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=self.service):
            row=next(r for r in quest_catalog(parser) if r['quest_id']=='atlas_cn_93000007')
            self.assertFalse(row['supported']);self.assertIn('结束',row['reason'])

    def test_builtin_alias_id_and_phase_remain_stable_after_sync(self):
        wars=exports();wars[0]['spots'][0]['quests'][0]['id']=93000001
        self.fetch.side_effect=lambda url,headers:(200,{}, {'CN':{'hash':'one'}}) if url.endswith('/info') else (200,{},wars)
        self.service.refresh()
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=self.service):
            bindings=quest_bindings();self.assertNotIn('atlas_cn_93000001',bindings)
            self.assertEqual((bindings['fuyuki_x_a']['id'],bindings['fuyuki_x_a']['phase']),(93000001,3))

    def test_refresh_invalidates_previously_passed_simulation_signature(self):
        parser,rec=fixture();self.service.refresh()
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=self.service):
            before=recommendation_signature(rec,parser);self.now=2000;self.service.refresh(force=True)
            self.assertNotEqual(before,recommendation_signature(rec,parser))

    def test_stale_enemy_cache_refresh_rejects_wrong_phase_and_preserves_old_file(self):
        self.service.refresh()
        root=Path(self.temp.name)/'public';f=root/'CN/quest/93000007_3.json';f.parent.mkdir(parents=True)
        original=json.dumps({'id':93000007,'phase':3});f.write_text(original,encoding='utf-8');os.utime(f,(100,100))
        response=MagicMock();response.read.return_value=json.dumps({'id':93000007,'phase':1}).encode()
        response.__enter__.return_value=response
        with patch('src.strategy.quest_catalog.catalog_sync',return_value=self.service),patch('urllib.request.urlopen',return_value=response):
            with self.assertRaises(LaplaceError):PublicData(root).get('CN','quest',93000007,3)
        self.assertEqual(f.read_text(encoding='utf-8'),original)

    def test_dynamic_id_does_not_use_keyword_fallback_when_enemy_data_missing(self):
        parser,_=fixture()
        with patch('src.strategy.quest_catalog.quest_profile',side_effect=ValueError('no enemy data')):
            with self.assertRaises(ValueError):AutoTeamBuilder(parser).recommend_for_quest(quest_id='atlas_cn_93000007')


class TestSyncedDailyIdentity(unittest.TestCase):
    def word(self,text,x,y,score=.99):return ([[x-50,y-10],[x+50,y-10],[x+50,y+10],[x-50,y+10]],text,score)

    @patch('src.strategy.quest_catalog.quest_binding',return_value={'kind':'daily','name':'每日替换 弓之修炼场 初级','ap':10})
    def test_exact_name_difficulty_and_visible_ap_are_required(self,binding):
        words=[self.word('弓之修炼场 初级',1300,350),self.word('AP5',1300,430)]
        self.assertEqual(synced_daily_coordinate(words,'atlas_cn_1'),(1300,350))
        self.assertIsNone(synced_daily_coordinate([self.word('弓之修炼场 极级',1300,350),words[1]],'atlas_cn_1'))
        self.assertIsNone(synced_daily_coordinate(words[:1],'atlas_cn_1'))
        self.assertIsNone(synced_daily_coordinate([words[0],self.word('AP40',1300,430)],'atlas_cn_1'))


if __name__=='__main__':unittest.main()
