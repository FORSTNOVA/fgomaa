import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

from PIL import Image
from src.strategy.event_navigation import (TITLE,FARM_QUESTS,HALLOWEEN_WAR,HALLOWEEN_EVENT,
    EventNavigator,chapter_id,map_identity,matches_map_image,banner_coordinate,resolve_spot)
from src.strategy.quest_catalog import normalize_export
from src.strategy.screen_detector import ScreenDetector,GameScreen
from src.strategy.smart_engine import SmartEngine
from src.strategy.quest_battle import QuestBattleExecutor
from src.workflows.quest_runner import quest_catalog,resolve_queue
from src.workflows.first_clear_runner import FirstClearOptions,first_clear_catalog
from tests.test_chapter_navigation import word,frame


class TestEventNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).parents[1]
        cls.war=json.loads((root/'data/laplace/public/CN/war/9195.json').read_text(encoding='utf8'))
        cls.entries=normalize_export([cls.war])
        cls.frames={}
        for name in ('halloween_map_view','halloween_map_panned'):
            with Image.open(root/f'tests/fixtures/{name}.jpg') as image:cls.frames[name]=image.copy()
        with Image.open(root/'tests/fixtures/halloween_2026_banner.png') as image:
            cls.banner_ocr=[([[x+900,y+300] for x,y in box],text,score) for box,text,score in ScreenDetector.run_ocr(image)]

    @classmethod
    def tearDownClass(cls):
        for image in cls.frames.values():image.close()

    def setUp(self):
        self.parser=SimpleNamespace(servants={1:object()},quest_records_complete=True,
            quest_records={e['id']:{'clearNum':1,'questPhase':e['phase']} for e in self.entries.values()})
        self.bindings=copy.deepcopy(self.entries)
        self.patches=[patch('src.strategy.quest_catalog.quest_bindings',return_value=self.bindings),
                      patch('src.workflows.first_clear_runner.quest_bindings',return_value=self.bindings),
                      patch('time.time',return_value=1793500000)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.ledger=MagicMock();self.ledger.observation.return_value=None

    def test_real_banner_ocr_locates_this_activity_and_rejects_generic_halloween(self):
        pos=banner_coordinate(self.banner_ocr)
        self.assertIsNotNone(pos);self.assertTrue(900<pos[0]<1850 and 300<pos[1]<600)
        e=SimpleNamespace(ctrl=MagicMock(),log=MagicMock())
        self.assertTrue(EventNavigator(e).enter(self.banner_ocr))
        self.assertEqual(e.ctrl.click.call_args.args,pos)
        self.assertIsNone(chapter_id('万圣节'))
        self.assertIsNone(banner_coordinate([word('万圣节推荐召唤')]))

    def test_map_features_recognize_public_background_pan_and_large_drawer_occlusion(self):
        for name,image in self.frames.items():
            with self.subTest(name=name):
                self.assertTrue(matches_map_image(image))
                self.assertEqual(map_identity([],image),('万圣节星球',TITLE))

    def test_unrelated_real_map_and_blank_image_are_rejected(self):
        with Image.open(Path(__file__).parent/'fixtures/ordeal2_map.png') as image:
            self.assertFalse(matches_map_image(image))
        self.assertFalse(matches_map_image(Image.new('RGB',(1920,1080),'gray')))
        self.assertIsNone(map_identity([word(TITLE)]))  # 正文横幅不是当前地图身份。

    def test_map_background_accepts_game_vertical_scaling(self):
        # PNG高2048而公开mapImageH为1594，游戏可沿Y轴缩放背景。
        image=Image.new('RGB',(1920,1080),(30,35,50))
        image.paste(self.frames['halloween_map_view'].crop((0,90,1920,990)).resize((1920,700)),(0,90))
        self.assertTrue(matches_map_image(image))

    def test_background_proof_detects_map_and_open_drawer_without_header(self):
        for button in ('管理室','关闭'):
            with patch.object(ScreenDetector,'run_ocr',return_value=[word(button,160,60)]):
                screen,_,details=ScreenDetector.detect(self.frames['halloween_map_view'])
            self.assertEqual(screen,GameScreen.REGIONAL_MAP)
            self.assertEqual(details['current_map'],TITLE)

    def test_hall_notice_does_not_become_map_even_with_background_proof(self):
        words=[word('通知',160,60),word('活动报酬',1700,60),word('菜单',1750,980)]
        with patch.object(ScreenDetector,'run_ocr',return_value=words):
            self.assertNotEqual(ScreenDetector.detect(self.frames['halloween_map_view'])[0],GameScreen.REGIONAL_MAP)

    def test_nine_repeatable_ap_quests_are_available_with_clear_evidence(self):
        rows=quest_catalog(self.parser)
        supported=[r for r in rows if r['supported']]
        self.assertEqual({r['game_id'] for r in supported},FARM_QUESTS)
        self.assertTrue(all(r['kind']=='event' and r['navigation_supported'] for r in supported))
        queue=resolve_queue([{'quest_id':r['quest_id'],'runs':1} for r in supported],self.parser)
        self.assertEqual(len(queue),9);self.assertTrue(all(t.kind=='event' for t in queue))

    def test_story_one_time_challenges_and_high_difficulty_remain_disabled(self):
        rows=quest_catalog(self.parser)
        self.assertTrue(all(not r['supported'] for r in rows if r['game_id'] not in FARM_QUESTS))
        high=next(r for r in rows if r['game_id']==94150201)
        self.assertIn('高难',high['reason'])

    def test_unknown_activity_special_resources_and_expired_time_are_blocked(self):
        key='atlas_cn_94150101';base=self.bindings[key]
        for change in ({'event_id':999},{'war_id':9001},{'consume_type':'item'},{'closed_at':1},{'opened_at':2145888000}):
            self.bindings[key]={**base,**change}
            self.assertFalse(next(r for r in quest_catalog(self.parser) if r['quest_id']==key)['supported'])
        self.bindings[key]=base

    def test_unknown_spot_resolves_only_after_exact_rename_prerequisite(self):
        entry=self.bindings['atlas_cn_94150103']
        self.assertEqual(resolve_spot(entry,lambda q,p:False)['spot'],'？？？')
        self.assertEqual(resolve_spot(entry,lambda q,p:q==94150001 and p==1)['spot'],'新亚马逊平原')
        del self.parser.quest_records[94150001]
        row=next(r for r in quest_catalog(self.parser) if r['game_id']==94150103)
        self.assertFalse(row['supported']);self.assertIn('地点',row['reason'])

    def test_executor_preserves_renamed_spot_after_simulator_returns_original_placeholder(self):
        engine=SimpleNamespace(chaldea_parser=self.parser,navigate_to_free_quest=MagicMock(return_value=True))
        plan=SimpleNamespace(kind='event',quest_id='atlas_cn_94150103',first_clear=False)
        self.assertTrue(QuestBattleExecutor(engine).navigate(plan,TITLE,'？？？','Ｍ星之旅 尊享配送很危险！'))
        engine.navigate_to_free_quest.assert_called_once_with(TITLE,'新亚马逊平原','Ｍ星之旅 尊享配送很危险！')

    def test_open_uncleared_event_farms_enter_first_clear_with_date_and_prerequisite_proof(self):
        for qid in FARM_QUESTS:self.parser.quest_records.pop(qid)
        rows=first_clear_catalog(self.parser,FirstClearOptions(war_ids=[9195]),self.ledger)
        self.assertEqual({r['game_id'] for r in rows if r['supported']},FARM_QUESTS)
        with self.assertRaises(ValueError):resolve_queue([{'quest_id':'atlas_cn_94150101','runs':1}],self.parser)
        del self.parser.quest_records[94150002]
        rows=first_clear_catalog(self.parser,FirstClearOptions(war_ids=[9195]),self.ledger)
        blocked=next(r for r in rows if r['game_id']==94150101)
        self.assertFalse(blocked['supported']);self.assertIn('开放条件',blocked['reason'])

    def test_date_condition_cannot_bypass_missing_opened_timestamp(self):
        for qid in FARM_QUESTS:self.parser.quest_records.pop(qid)
        for key,e in self.bindings.items():
            if e['id'] in FARM_QUESTS:e['opened_at']=0
        rows=first_clear_catalog(self.parser,FirstClearOptions(war_ids=[9195]),self.ledger)
        self.assertFalse(any(r['supported'] for r in rows))

    def test_fullwidth_event_title_m_matches_ascii_ocr_and_still_requires_ap(self):
        e=SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        e.current_recommendation=SimpleNamespace(laplace={'quest_ap':30})
        name='Ｍ星之旅 小心宇宙海盗！'
        words=[word('自由关卡',1100,200),word('M星之旅 小心宇宙海盗!',1450,225),word('AP30',1200,305)]
        self.assertEqual(e._named_free_quest_card(words,name),(1450,225))
        e.current_recommendation.laplace['quest_ap']=40
        self.assertIsNone(e._named_free_quest_card(words,name))

    def test_event_map_cannot_match_a_story_map(self):
        self.assertFalse(SmartEngine.is_target_map('万圣节星球',TITLE,'第一特异点 邪龙百年战争 奥尔良'))
        self.assertFalse(SmartEngine.is_target_map('奥尔良','第一特异点 邪龙百年战争 奥尔良',TITLE))
        self.assertFalse(SmartEngine.is_target_map('未知地图','未知章节大地图',TITLE))

    def test_hall_event_map_spot_card_support_route_keeps_activity_reward_button(self):
        words=[word('管理室',160,60),word('活动报酬',1750,70)]
        details={'ocr_results':words,'all_texts':[r[1] for r in words],'map_name':'万圣节星球','current_map':TITLE}
        spot={**details,'ocr_results':words+[word('第一仓储库',600,500)]}
        card={**details,'ocr_results':words+[word('自由关卡',1100,200),word('M星之旅 小心宇宙海盗!',1450,225),word('AP30',1200,305)]}
        e=SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        e.current_recommendation=SimpleNamespace(target_quest_id='atlas_cn_94150101',laplace={'quest_ap':30})
        e.get_current_screen=MagicMock(side_effect=[frame(GameScreen.MAIN_MENU,self.banner_ocr),
            (GameScreen.REGIONAL_MAP,spot),(GameScreen.REGIONAL_MAP,card),frame(GameScreen.SUPPORT_SELECT,[])])
        with patch('src.strategy.smart_engine.time.sleep'):
            self.assertTrue(e.navigate_to_free_quest(TITLE,'第一仓储库','Ｍ星之旅 小心宇宙海盗！'))
        self.assertEqual(e.ctrl.click.call_count,3)
        self.assertTrue(all(c.args[1]<900 for c in e.ctrl.click.call_args_list))


if __name__=='__main__':unittest.main()
