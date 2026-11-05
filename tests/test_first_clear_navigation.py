import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

from PIL import Image,ImageDraw

from src.strategy.first_clear_navigation import FirstClearNavigator,number_badges
from src.strategy.screen_detector import ScreenDetector,GameScreen
from src.strategy.smart_engine import SmartEngine
from src.workflows.first_clear_runner import FirstClearOptions


def word(text,x,y):
    return ([[x-20,y-10],[x+20,y-10],[x+20,y+10],[x-20,y+10]],text,.99)


class TestFirstClearNavigation(unittest.TestCase):
    def engine(self):
        e=MagicMock();e.is_cancelled.return_value=False;e.first_clear_observation_dir=None
        e.is_target_map.side_effect=SmartEngine.is_target_map
        e._get_war_search_tokens.side_effect=SmartEngine._get_war_search_tokens
        e._named_free_quest_card.side_effect=lambda ocr,name,**kw: (1450,220) if any(t==name for _,t,_ in ocr) else None
        return e

    def test_first_clear_accepts_more_than_thirty_and_unlimited(self):
        self.assertEqual(FirstClearOptions(max_quests=500).max_quests,500)
        self.assertIsNone(FirstClearOptions(max_quests=None).max_quests)
        for value in [0,-1,True,1.5]:
            with self.subTest(value=value),self.assertRaises(ValueError):FirstClearOptions(max_quests=value)

    def test_real_single_digit_badge_excludes_bottom_gift(self):
        with Image.open(Path(__file__).parent/'fixtures/first_clear_record_badge.png') as im:
            self.assertEqual(number_badges(im,ScreenDetector.run_ocr(im)),[{'x':1808,'y':780,'count':1}])

    def test_real_chapter_counts_and_map_counts(self):
        for name,counts in [('ordeal_terminal.png',[12,15,9]),('ordeal2_map.png',[1,1])]:
            with self.subTest(name=name),Image.open(Path(__file__).parent/'fixtures'/name) as im:
                self.assertEqual([b['count'] for b in number_badges(im,ScreenDetector.run_ocr(im))],counts)

    def test_plain_digits_and_orange_rectangle_are_not_badges(self):
        im=Image.new('RGB',(1920,1080),'black');d=ImageDraw.Draw(im)
        d.rectangle((1200,300,1250,350),fill='orange')
        self.assertEqual(number_badges(im,[word('2',1225,325),word('50',1700,250)]),[])

    def test_badged_chapter_then_badged_spot_then_exact_card(self):
        e=self.engine();nav=FirstClearNavigator(e)
        row={'quest_id':'q','name':'未通关目标','war':'第六特异点 神圣圆桌领域 卡美洛','war_id':106}
        states=[(GameScreen.MAIN_RECORD,1,{'ocr_results':[word('卡美洛',1450,500)]}),
                (GameScreen.REGIONAL_MAP,1,{'map_name':'卡美洛','current_map':row['war'],'ocr_results':[]}),
                (GameScreen.REGIONAL_MAP,1,{'map_name':'卡美洛','current_map':row['war'],'ocr_results':[word('未通关目标',1450,220)]})]
        with patch.object(ScreenDetector,'detect',side_effect=states), \
             patch('src.strategy.first_clear_navigation.number_badges',side_effect=[
                 [{'x':1808,'y':450,'count':2}],[{'x':600,'y':300,'count':1}],[]]):
            self.assertIs(nav.select([row]),row)
        self.assertEqual([c.args for c in e.ctrl.click.call_args_list],[(1450,520),(600,300)])
        e.run_quest.assert_not_called()

    def test_drawer_with_multiple_candidates_chooses_a_visible_quest(self):
        e=self.engine()
        rows=[{'quest_id':str(i),'name':name,'war':'奏章Ⅱ 不可逆废弃孔 伊底','war_id':403}
              for i,name in enumerate(['调查七大不可思议','突然叫出'])]
        details={'map_name':'奏章Ⅱ','current_map':rows[0]['war'],
                 'ocr_results':[word(r['name'],1450,400+i*270) for i,r in enumerate(rows)]}
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,1,details)), \
             patch('src.strategy.first_clear_navigation.number_badges',return_value=[]):
            self.assertIs(FirstClearNavigator(e).select(rows),rows[0])
        e.ctrl.click.assert_not_called()

    def test_hidden_card_scrolls_drawer_instead_of_reopening_spot(self):
        e=self.engine();row={'name':'隐藏目标','war':'奏章Ⅱ 不可逆废弃孔 伊底','war_id':403}
        details={'map_name':'奏章Ⅱ','current_map':row['war'],'ocr_results':[word('关闭',160,60)]}
        after={**details,'ocr_results':[word('隐藏目标',1450,220)]}
        with patch.object(ScreenDetector,'detect',side_effect=[(GameScreen.REGIONAL_MAP,1,details),(GameScreen.REGIONAL_MAP,1,after)]), \
             patch('src.strategy.first_clear_navigation.number_badges',return_value=[]):
            self.assertIs(FirstClearNavigator(e).select([row]),row)
        e.ctrl.swipe.assert_called_once();e.ctrl.click.assert_not_called()

    def test_same_quest_name_in_wrong_map_cannot_select_or_enter_battle(self):
        e=self.engine();row={'name':'目标','war':'奏章Ⅱ 不可逆废弃孔 伊底','war_id':403}
        details={'map_name':'奏章Ⅲ','current_map':'奏章Ⅲ 新灵长后继战 原型肇始',
                 'ocr_results':[word('目标',1450,220),word('管理室',160,60)]}
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,1,details)), \
             patch('src.strategy.first_clear_navigation.number_badges',return_value=[]):
            with self.assertRaisesRegex(RuntimeError,'画面重复'):FirstClearNavigator(e).select([row])
        self.assertTrue(all(c.args==(160,60) for c in e.ctrl.click.call_args_list))
        e.run_quest.assert_not_called()

    def test_real_school_drawer_can_match_visible_uncleared_card(self):
        e=self.engine();e._named_free_quest_card.side_effect=None
        e._named_free_quest_card.side_effect=lambda ocr,name,**kw:SmartEngine._named_free_quest_card(e,ocr,name,**kw)
        e._free_quest_label_rows.side_effect=SmartEngine._free_quest_label_rows
        row={'name':'调查七大不可思议','war':'奏章Ⅱ 不可逆废弃孔 伊底','war_id':403}
        with Image.open(Path(__file__).parent/'fixtures/first_clear_school_drawer.png') as im:
            e.ctrl.screencap.return_value=im
            self.assertIs(FirstClearNavigator(e).select([row]),row)
        e.ctrl.click.assert_not_called()

    def test_cancel_before_capture(self):
        e=self.engine();e.is_cancelled.return_value=True
        with self.assertRaisesRegex(RuntimeError,'取消'):FirstClearNavigator(e).select([])
        e.ctrl.screencap.assert_not_called()

    def test_terminal_carousel_and_currencies_do_not_keep_rewinding(self):
        e=self.engine();row={'name':'目标','war':'第六特异点 神圣圆桌领域 卡美洛','war_id':106}
        states=[(GameScreen.MAIN_MENU,1,{'ocr_results':[word('轮播广告'+str(i),1400,220),
                  word('点数'+str(i),1500,60),word('迦勒底之门',1400,780)]}) for i in range(8)]
        with patch.object(ScreenDetector,'detect',side_effect=states), \
             patch('src.strategy.first_clear_navigation.number_badges',return_value=[]):
            with self.assertRaises(RuntimeError):FirstClearNavigator(e).select([row])
        self.assertEqual(e.ctrl.swipe.call_args_list[0].args,(1400,350,1400,800))
        self.assertEqual(e.ctrl.swipe.call_args_list[1].args,(1400,800,1400,350))

    def test_inspection_of_a_known_spot_scrolls_to_hidden_card(self):
        from tests.test_chapter_navigation import TestChapterNavigation,word,frame
        e=TestChapterNavigation().engine()
        war='Lostbelt No.1 永久冻土帝国 阿纳斯塔西娅'
        header=[word('管理室',160,60),word('永久冻土帝国',1550,60)]
        spot=frame(GameScreen.REGIONAL_MAP,header+[word('地点甲',600,500)])
        drawer=frame(GameScreen.REGIONAL_MAP,[word('关闭',160,60),word('永久冻土帝国',1550,60),word('其他关卡',1450,225)])
        card=frame(GameScreen.REGIONAL_MAP,header+[word('自由关卡',1100,200),word('目标关卡',1450,225),word('AP21',1200,305)])
        e.get_current_screen.side_effect=[spot,drawer,card]
        with patch('src.strategy.smart_engine.time.sleep'):
            self.assertTrue(e.navigate_to_free_quest(war,'地点甲','目标关卡',inspect_only=True))
        e.ctrl.click.assert_called_once_with(600,500,delay_after=2.5)
        e.ctrl.swipe.assert_called_once_with(1500,790,1500,350,duration_ms=350)

    def test_inspection_does_not_close_reopen_an_absent_quest_forever(self):
        from tests.test_chapter_navigation import TestChapterNavigation,word,frame
        e=TestChapterNavigation().engine()
        header=[word('管理室',160,60),word('永久冻土帝国',1550,60)]
        spot=frame(GameScreen.REGIONAL_MAP,header+[word('地点甲',600,500)])
        drawer=frame(GameScreen.REGIONAL_MAP,[word('关闭',160,60),word('永久冻土帝国',1550,60),word('其他关卡',1450,225)])
        e.get_current_screen.side_effect=[spot]+[drawer]*7
        with patch('src.strategy.smart_engine.time.sleep'):
            self.assertFalse(e.navigate_to_free_quest('Lostbelt No.1 永久冻土帝国 阿纳斯塔西娅','地点甲','目标关卡',inspect_only=True))
        self.assertEqual(e.ctrl.click.call_count,1)
        self.assertEqual(e.ctrl.swipe.call_count,6)
