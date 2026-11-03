import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

from PIL import Image
from src.strategy.chapter_navigation import (CHAPTERS,NAVIGATION_WARS,ChapterNavigator,
    chapter_id,map_identity,banner_coordinate,record_coordinate,supports_navigation)
from src.strategy.screen_detector import ScreenDetector,GameScreen
from src.strategy.smart_engine import SmartEngine
from src.workflows.quest_runner import quest_catalog,resolve_queue
from src.workflows.first_clear_runner import FirstClearOptions,first_clear_catalog


def word(text,x=1400,y=400,score=.99):
    return ([[x-50,y-10],[x+50,y-10],[x+50,y+10],[x-50,y+10]],text,score)


def frame(screen,words):
    return screen,{'ocr_results':words,'all_texts':[r[1] for r in words]}


def binding(wid):
    return {'id':770000+wid,'phase':3,'phases':[1,2,3],'kind':'free','war_id':wid,
            'war':CHAPTERS[wid][1],'spot':'地点甲','name':'普通自由关卡','consume_type':'ap',
            'ap':21,'after_clear':'repeatLast','release_conditions':[]}


class TestChapterNavigation(unittest.TestCase):
    def engine(self,frames=()):
        engine=SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        engine.get_current_screen=MagicMock(side_effect=frames)
        engine.current_recommendation=SimpleNamespace(target_quest_id='target',laplace={'quest_ap':21})
        return engine

    def test_all_shipped_cn_war_names_and_free_map_spots_match_registry(self):
        for wid in CHAPTERS:
            with self.subTest(wid=wid):
                war=json.loads((Path(__file__).parents[1]/f'data/laplace/public/CN/war/{wid}.json').read_text(encoding='utf8'))
                self.assertEqual(chapter_id(war['longName']),wid)
                maps={m['id'] for m in war['maps']}
                free=[s for s in war['spots'] if any(q['type']=='free' for q in s['quests'])]
                self.assertTrue(free)
                self.assertTrue(all(s['mapId'] in maps for s in free))

    def test_header_identity_distinguishes_every_pair_of_new_chapters(self):
        for wid,(_,name,tokens) in CHAPTERS.items():
            identity=ScreenDetector.identify_regional_map([word(tokens[0],1550,60)])
            self.assertEqual(chapter_id(identity[1]),wid)
            self.assertEqual(SmartEngine._get_war_search_tokens(name),list(tokens))
            for other,(_,other_name,_) in CHAPTERS.items():
                self.assertEqual(SmartEngine.is_target_map(*identity,other_name),wid==other)

    def test_common_lostbelt_five_title_or_conflicting_headers_are_not_an_identity(self):
        self.assertIsNone(chapter_id('Lostbelt No.5'))
        self.assertIsNone(map_identity([word('神代巨神海洋 星间都市山脉',1550,60)]))
        self.assertFalse(SmartEngine.is_target_map('未知地图','未知章节大地图',CHAPTERS[305][1]))
        self.assertFalse(SmartEngine.is_target_map('未知地图','未知章节大地图','特异点F 燃烧污染都市 冬木'))

    def test_body_banner_does_not_prove_map_identity_and_low_confidence_is_rejected(self):
        for wid,(_,name,_) in CHAPTERS.items():
            self.assertIsNone(map_identity([word(name)]))
            self.assertIsNone(map_identity([word(name,1550,60,.4)]))
            self.assertIsNotNone(banner_coordinate([word(name)],wid))
        self.assertIsNone(chapter_id('basin'))  # SIN不能匹配英文词中的一部分。

    def test_record_entry_distinguishes_one_one_point_five_and_two(self):
        words=[word('主线记录',1350,250),word('1.5',1700,250),word('主线记录 1',1400,500),word('主线记录 2',1400,750)]
        self.assertEqual(record_coordinate(words,'1.5'),(1400,250))
        self.assertEqual(record_coordinate(words,'2'),(1400,750))
        self.assertIsNone(record_coordinate([word('主线物语 2')],'2'))
        self.assertIsNone(record_coordinate([word('主线记录 2',1400,200),word('主线记录 2',1400,600)],'2'))

    def test_part_two_and_one_point_five_lists_and_new_map_drawers_are_detected(self):
        blank=Image.new('RGB',(1920,1080),'white')
        for part,label in [('1.5','恶性隔绝魔境'),('2','地狱界曼荼罗')]:
            words=[word('关闭',160,60),word('主线记录 '+part,1400,60),word(label)]
            with patch.object(ScreenDetector,'run_ocr',return_value=words):
                self.assertEqual(ScreenDetector.detect(blank)[0],GameScreen.MAIN_RECORD)
        for label in ('神代巨神海洋','星间都市山脉','死想显现界域','黄金树海纪行'):
            words=[word('关闭',160,60),word(label,1550,60)]
            with patch.object(ScreenDetector,'run_ocr',return_value=words):
                self.assertEqual(ScreenDetector.detect(blank)[0],GameScreen.REGIONAL_MAP)

    def test_wrong_record_group_returns_to_terminal_instead_of_scanning_it(self):
        e=self.engine();nav=ChapterNavigator(e,201)
        self.assertTrue(nav.enter([word('关闭',160,60),word('主线记录 2',1400,60),word('永久冻土帝国')],record=True))
        self.assertEqual(e.ctrl.click.call_args.args,(160,60));e.ctrl.swipe.assert_not_called()
        e.ctrl.reset_mock()
        self.assertFalse(nav.enter([word('主线记录 2',1400,60)],record=True))
        e.ctrl.click.assert_not_called()

    def test_list_search_rewinds_then_stops_at_bottom_if_chapter_is_absent(self):
        e=self.engine();e.ctrl.screencap.return_value=Image.new('RGB',(1920,1080))
        nav=ChapterNavigator(e,201)
        with patch('src.strategy.anchor_verifier.AnchorVerifier.compute_frame_diff',return_value=0):
            results=[nav.enter([word('其他章节')],record=True) for _ in range(5)]
        self.assertEqual(results,[True,True,True,True,False]);e.ctrl.click.assert_not_called()
        self.assertEqual(e.ctrl.swipe.call_args_list[0].args,(1400,350,1400,800))
        self.assertEqual(e.ctrl.swipe.call_args_list[-1].args,(1400,800,1400,350))

    def chain(self,wid):
        _,name,tokens=CHAPTERS[wid];part='1.5' if wid<300 else '2'
        header=[word('管理室',160,60),word(tokens[0],1550,60)]
        return [frame(GameScreen.MAIN_MENU,[word('通知',160,60),word('主线记录 '+part)]),
                frame(GameScreen.MAIN_RECORD,[word('关闭',160,60),word('主线记录 '+part,1400,60),word(name)]),
                frame(GameScreen.REGIONAL_MAP,header+[word('地点甲',600,500)]),
                frame(GameScreen.REGIONAL_MAP,header+[word('自由关卡',1100,200),word('普通自由关卡',1450,225),word('AP21',1200,305)]),
                frame(GameScreen.SUPPORT_SELECT,[])]

    def test_all_fourteen_hall_record_map_spot_card_support_routes(self):
        for wid,(_,name,_) in CHAPTERS.items():
            with self.subTest(wid=wid):
                e=self.engine(self.chain(wid))
                with patch('src.strategy.smart_engine.time.sleep'):
                    self.assertTrue(e.navigate_to_free_quest(name,'地点甲','普通自由关卡'))
                self.assertEqual(e.ctrl.click.call_count,4)
                self.assertTrue(all(call.args[1]<900 for call in e.ctrl.click.call_args_list))

    def test_first_clear_inspection_stops_at_card_without_entering_support(self):
        for wid in (201,305,306,307,310,311):
            e=self.engine(self.chain(wid))
            with patch('src.strategy.smart_engine.time.sleep'):
                self.assertTrue(e.navigate_to_free_quest(CHAPTERS[wid][1],'地点甲','普通自由关卡',inspect_only=True))
            self.assertEqual(e.ctrl.click.call_count,3)
            self.assertEqual(e.get_current_screen.call_count,4)

    def test_wrong_lostbelt_five_map_never_clicks_matching_card_title(self):
        wrong=[word('管理室',160,60),word('星间都市山脉',1550,60),word('自由关卡',1100,180),
               word('普通自由关卡',1450,225),word('AP21',1200,305)]
        e=self.engine([frame(GameScreen.REGIONAL_MAP,wrong),frame(GameScreen.SUPPORT_SELECT,[])])
        with patch('src.strategy.smart_engine.time.sleep'):
            self.assertFalse(e.navigate_to_free_quest(CHAPTERS[305][1],'地点甲','普通自由关卡'))
        self.assertEqual(e.ctrl.click.call_count,1)
        self.assertEqual(e.ctrl.click.call_args.args,(160,60))

    def test_cancel_and_unrelated_support_do_not_take_control(self):
        e=self.engine([frame(GameScreen.SUPPORT_SELECT,[])])
        self.assertFalse(e.navigate_to_free_quest(CHAPTERS[311][1],'地点甲','普通自由关卡'))
        e.ctrl.click.assert_not_called()
        e=self.engine();e.is_cancelled=lambda:True
        self.assertFalse(e.navigate_to_free_quest(CHAPTERS[201][1],'地点甲','普通自由关卡'))
        e.get_current_screen.assert_not_called();e.ctrl.click.assert_not_called()

    def test_real_public_spots_guide_pans_only_in_every_new_chapter(self):
        for wid in CHAPTERS:
            war=json.loads((Path(__file__).parents[1]/f'data/laplace/public/CN/war/{wid}.json').read_text(encoding='utf8'))
            target=next(s for s in war['spots'] if any(q['type']=='free' for q in s['quests']))
            anchors=[s for s in war['spots'] if s['mapId']==target['mapId'] and s['name'].strip('？? ')
                     and abs(s['x']-target['x'])+abs(s['y']-target['y'])>25
                     and len({(a['x'],a['y']) for a in war['spots'] if a['mapId']==s['mapId'] and a['name']==s['name']})==1]
            self.assertTrue(anchors);anchor=anchors[0]
            e=self.engine();nav=ChapterNavigator(e,wid);nav.war=war
            with patch('src.strategy.quest_catalog.quest_binding',return_value={'war_id':wid,'spot_id':target['id']}):
                nav.pan([word(anchor['name'],600,400)],'target')
            e.ctrl.click.assert_not_called();e.ctrl.swipe.assert_called_once()
            x,y,ex,ey=e.ctrl.swipe.call_args.args
            if target['x'] != anchor['x']:
                self.assertLess((ex-x)*(target['x']-anchor['x']),0)
            if target['y'] != anchor['y']:
                self.assertLess((ey-y)*(target['y']-anchor['y']),0)

    def test_new_catalog_and_first_clear_scopes_share_navigation_guards(self):
        bindings={str(wid):binding(wid) for wid in CHAPTERS}
        bindings['311']['event_id']=80421
        cleared=SimpleNamespace(servants={1:object()},quest_records_complete=True,
                quest_records={e['id']:{'questPhase':3,'clearNum':1} for e in bindings.values()})
        ledger=MagicMock();ledger.observation.return_value=None
        with patch('src.strategy.quest_catalog.quest_bindings',return_value=bindings), \
             patch('src.workflows.first_clear_runner.quest_bindings',return_value=bindings):
            self.assertTrue(all(r['supported'] and r['navigation_supported'] for r in quest_catalog(cleared)))
            self.assertEqual(len(resolve_queue([{'quest_id':str(wid),'runs':1} for wid in CHAPTERS],cleared)),14)
            cleared.quest_records={}
            self.assertTrue(all(not r['supported'] and r['navigation_supported'] for r in quest_catalog(cleared)))
            rows=first_clear_catalog(cleared,FirstClearOptions(war_ids=list(CHAPTERS)),ledger)
            self.assertEqual(len(rows),14);self.assertTrue(all(r['supported'] for r in rows))

    def test_event_special_resource_high_difficulty_and_unknown_spots_stay_blocked(self):
        parser=SimpleNamespace(servants={1:object()},quest_records={770311:{'questPhase':3,'clearNum':1}})
        base={**binding(311),'event_id':80421}
        self.assertTrue(supports_navigation(base))
        self.assertFalse(supports_navigation({**base,'event_id':123}))
        self.assertFalse(supports_navigation({**base,'war_id':401,'event_id':0}))
        for change in ({'event_id':123},{'consume_type':'item'},{'name':'高难挑战'},{'spot':'？？？'},
                       {'after_clear':'close'},{'closed_at':1}):
            with self.subTest(change=change),patch('src.strategy.quest_catalog.quest_bindings',return_value={'target':{**base,**change}}):
                self.assertFalse(quest_catalog(parser)[0]['supported'])

    def test_scope_accepts_all_registered_wars_and_rejects_special_earth(self):
        self.assertEqual(len(FirstClearOptions(war_ids=sorted(NAVIGATION_WARS)).war_ids),27)
        with self.assertRaises(ValueError):FirstClearOptions(war_ids=[401])
        with self.assertRaises(ValueError):FirstClearOptions(war_ids=[201,201])


if __name__=='__main__':unittest.main()
