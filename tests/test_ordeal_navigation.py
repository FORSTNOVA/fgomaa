import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from PIL import Image
from src.strategy.ordeal_navigation import CHAPTERS,chapter_id,map_identity,is_selector,banner_coordinate,OrdealNavigator
from src.strategy.screen_detector import ScreenDetector,GameScreen
from src.strategy.smart_engine import SmartEngine
from src.workflows.quest_runner import quest_catalog,resolve_queue


def word(t,x,y):return ([[x-50,y-10],[x+50,y-10],[x+50,y+10],[x-50,y+10]],t,.95)


class TestOrdealNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames={};cls.ocr={}
        for name in ['ordeal_terminal','ordeal_globe','ordeal_globe_management','ordeal2_map','ordeal2_card']:
            with Image.open(Path(__file__).parent/'fixtures'/f'{name}.png') as img:cls.frames[name]=img.copy()
            cls.ocr[name]=ScreenDetector.run_ocr(cls.frames[name])

    @classmethod
    def tearDownClass(cls):
        for i in cls.frames.values():i.close()

    def test_all_four_chapters_have_distinct_identity_and_no_generic_fallback(self):
        for wid,(name,tokens) in CHAPTERS.items():
            self.assertEqual(chapter_id(name),wid)
            self.assertEqual(chapter_id(name.split()[0]),wid)
            self.assertEqual(SmartEngine._get_war_search_tokens(name),list(tokens))
        self.assertIsNone(chapter_id('Ordeal Call 奏章'))
        self.assertIsNone(chapter_id('不可逆废弃孔 原型肇始'))

    def test_map_matching_rejects_other_chapter_and_unknown_map(self):
        for wid,(name,_) in CHAPTERS.items():
            for other,(other_name,_) in CHAPTERS.items():
                self.assertEqual(SmartEngine.is_target_map(name.split()[0],name,other_name),wid==other)
            self.assertFalse(SmartEngine.is_target_map('未知地图','未知章节大地图',name))
            self.assertFalse(SmartEngine.is_target_map('奏章','Ordeal Call 奏章',name))

    def test_real_terminal_is_not_a_map_and_selects_correct_banner(self):
        ocr=self.ocr['ordeal_terminal']
        self.assertIsNone(map_identity(ocr))
        self.assertIsNotNone(banner_coordinate(ocr,403))
        self.assertIsNotNone(banner_coordinate(ocr,404))
        self.assertIsNotNone(banner_coordinate(ocr,405))
        with patch.object(ScreenDetector,'run_ocr',return_value=ocr):
            self.assertEqual(ScreenDetector.detect(self.frames['ordeal_terminal'])[0],GameScreen.MAIN_MENU)

    def test_real_globe_and_region_are_separate_states(self):
        self.assertTrue(is_selector(self.ocr['ordeal_globe']))
        self.assertFalse(is_selector(self.ocr['ordeal2_map']))
        for name,expected in [('ordeal_globe',GameScreen.ORDEAL_SELECT),('ordeal2_map',GameScreen.REGIONAL_MAP),('ordeal2_card',GameScreen.REGIONAL_MAP)]:
            with patch.object(ScreenDetector,'run_ocr',return_value=self.ocr[name]):
                self.assertEqual(ScreenDetector.detect(self.frames[name])[0],expected)
        self.assertEqual(map_identity(self.ocr['ordeal2_map'])[0],'奏章Ⅱ')

    def test_each_header_is_identified_without_collapsing_into_other_chapters(self):
        for wid,(name,tokens) in CHAPTERS.items():
            result=map_identity([word(tokens[0],1550,50),word('返回',160,60)])
            self.assertEqual(chapter_id(result[1]),wid)

    def test_returned_globe_with_management_button_is_still_a_selector(self):
        words=self.ocr['ordeal_globe_management']
        self.assertTrue(is_selector(words))
        with patch.object(ScreenDetector,'run_ocr',return_value=words):
            self.assertEqual(ScreenDetector.detect(self.frames['ordeal_globe_management'])[0],GameScreen.ORDEAL_SELECT)
        e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock())
        self.assertTrue(OrdealNavigator(e,405).enter(words,selector=True))
        x,y=e.ctrl.click.call_args.args
        self.assertTrue(1550 <= x <= 1850)
        self.assertTrue(150 <= y <= 220)

    def test_missing_chapter_on_returned_globe_can_return_to_management(self):
        words=[word('Ordeal Call',1700,50),word('管理室',164,64),word('Rank',136,306)]
        self.assertTrue(is_selector(words))
        e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock())
        self.assertTrue(OrdealNavigator(e,405).enter(words,selector=True))
        self.assertEqual(e.ctrl.click.call_args.args,(164,64))
        self.assertFalse(is_selector([word('管理室',164,64),word('Rank',136,306)]))

    def test_free_quest_english_card_requires_exact_title_and_ap(self):
        e=SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        e.current_recommendation=SimpleNamespace(laplace={'quest_ap':21})
        words=self.ocr['ordeal2_card']
        self.assertEqual(e._named_free_quest_card(words,'秘密的碰头'),(1450,225))
        e.current_recommendation.laplace['quest_ap']=22
        self.assertIsNone(e._named_free_quest_card(words,'秘密的碰头'))
        e.current_recommendation.laplace['quest_ap']=21
        self.assertIsNone(e._named_free_quest_card(words,'习以为常的道路'))
        no_quest=[r for r in words if r[1].replace(' ','').lower()!='quest']
        self.assertIsNone(e._named_free_quest_card(no_quest,'秘密的碰头'))

    def test_unrelated_globe_does_not_click_a_different_chapter(self):
        e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock())
        nav=OrdealNavigator(e,402)
        self.assertTrue(nav.enter(self.ocr['ordeal_globe'],selector=True))
        point=e.ctrl.click.call_args.args
        self.assertTrue(point[0]<350 and point[1]<150)

    def test_terminal_search_has_a_finite_end_when_entry_is_unavailable(self):
        e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock())
        e.ctrl.screencap.return_value=self.frames['ordeal_terminal']
        nav=OrdealNavigator(e,402);words=[word('旧主线入口',1400,500)]
        results=[nav.enter(words) for _ in range(5)]
        self.assertIn(False,results)
        e.ctrl.click.assert_not_called()

    def test_real_background_and_noisy_ocr_still_detect_terminal_top(self):
        folder=Path(__file__).parent/'fixtures'
        with Image.open(folder/'ordeal_terminal_top_a.png') as a, Image.open(folder/'ordeal_terminal_top_b.png') as b:
            e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock())
            e.ctrl.screencap.side_effect=[a,b,a,b,a]
            nav=OrdealNavigator(e,402)
            results=[nav.enter([word('轮播文字'+str(i),1400,500)]) for i in range(5)]
        self.assertEqual(nav.phase,'scan')
        self.assertIn(False,results)
        e.ctrl.click.assert_not_called()

    def test_expanding_search_does_not_repeat_old_four_direction_closed_loop(self):
        e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock());nav=OrdealNavigator(e,403)
        for _ in range(4):nav.pan([],None)
        actions=[c.args for c in e.ctrl.swipe.call_args_list]
        self.assertEqual(actions[2],actions[3])
        self.assertNotEqual(sum(a[2]-a[0] for a in actions),0)

    def test_public_spot_coordinates_only_pan_and_duplicate_anchors_are_deduplicated(self):
        e=SimpleNamespace(log=MagicMock(),ctrl=MagicMock());nav=OrdealNavigator(e,403)
        source={'name':'东京站','id':5,'mapId':40301,'x':1000,'y':500}
        nav.war={'spots':[source,dict(source),{'name':'自家','id':2,'mapId':40301,'x':500,'y':800}]}
        with patch('src.strategy.quest_catalog.quest_binding',return_value={'war_id':403,'spot_id':2}):
            nav.pan([word('东京站',300,450)],'target')
        e.ctrl.click.assert_not_called()
        x,y,end_x,end_y=e.ctrl.swipe.call_args.args
        self.assertGreater(end_x,x);self.assertLess(end_y,y)

    def test_ordeal_queue_keeps_resource_and_progress_guards(self):
        base={'id':93040201,'phase':3,'name':'隔离设施','spot':'修理部郊外','war':CHAPTERS[402][0],
              'kind':'free','war_id':402,'event_id':0,'consume_type':'ap','ap':21}
        parser=SimpleNamespace(servants={1:object()},quest_records={93040201:{'clearNum':1,'questPhase':3}})
        for change,supported in [({},True),({'consume_type':'item'},False),({'name':'【高难度】挑战'},False),({'spot':'？？？'},False),({'war_id':401},False)]:
            with self.subTest(change=change),patch('src.strategy.quest_catalog.quest_bindings',return_value={'target':{**base,**change}}):
                self.assertEqual(quest_catalog(parser)[0]['supported'],supported)
        parser.quest_records={}
        with patch('src.strategy.quest_catalog.quest_bindings',return_value={'target':base}):
            with self.assertRaises(ValueError):resolve_queue([{'quest_id':'target','runs':1}],parser)

    def test_hall_globe_map_card_support_chain_never_clicks_support_or_start(self):
        e=SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        e.current_recommendation=SimpleNamespace(target_quest_id='atlas_cn_93040302',laplace={'quest_ap':21})
        hall=self.ocr['ordeal_terminal'];globe=self.ocr['ordeal_globe'];card=self.ocr['ordeal2_card']
        frames=[(GameScreen.MAIN_MENU,{'ocr_results':hall}), (GameScreen.ORDEAL_SELECT,{'ocr_results':globe}),
                (GameScreen.REGIONAL_MAP,{'ocr_results':card}), (GameScreen.SUPPORT_SELECT,{'ocr_results':[]})]
        e.get_current_screen=MagicMock(side_effect=frames)
        with patch('src.strategy.smart_engine.time.sleep'):
            self.assertTrue(e.navigate_to_free_quest(CHAPTERS[403][0],'自家','秘密的碰头'))
        self.assertEqual(e.ctrl.click.call_count,3)
        self.assertTrue(all(c.args[1]<900 for c in e.ctrl.click.call_args_list))


if __name__=='__main__':unittest.main()
