import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

from PIL import Image
from src.strategy.ap_recovery import (APPLE_NAMES,AppleRecovery,apple_confirmation,apple_row,
                                      AppleLimitReached,button_point,confirmation_open,current_ap,numeric_ap,
                                      refill_count,quantity_plus)
from src.strategy.screen_detector import GameScreen,ScreenDetector


def word(text,x,y,score=.9):
    return ([[x-25,y-10],[x+25,y-10],[x+25,y+10],[x-25,y+10]],text,score)


def confirmation(apple='bronze',before=0,after=10,count=1,inventory=340,maximum=15):
    name=APPLE_NAMES[apple]
    return [word('消耗果实回复行动力',960,80),word(name,770,220),
            word(f'{name}的消耗数：{count}个',960,550),word(f'现在行动力：{before}/144',960,620),
            word(f'回复后行动力：{after}/144',960,700),word(f'是否执行消耗{name}来回复行动力？',960,785),
            word('决定',1260,876),word('取消',660,876),word(f'{inventory}个持有',1370,210),
            word(str(maximum),1500,450)]


def hud(ap=0):return [word(f'行动力{ap}/144',360,1012)]


class TestRealAppleScreens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames={}
        for name in ['menu_top','menu_lower','confirm_bronze','confirm_blue','confirm_silver','confirm_gold','confirm_gold_map']:
            image=Image.open(Path(__file__).parent/'fixtures'/f'ap_{name}.png')
            cls.frames[name]=(image,ScreenDetector.run_ocr(image))

    @classmethod
    def tearDownClass(cls):
        for image,_ in cls.frames.values():image.close()

    def test_real_inventory_has_four_distinct_items_and_no_stone_option(self):
        for apple,frame,stock,gain in [('gold','menu_top',268,144),('silver','menu_top',91,72),
                                       ('blue','menu_lower',116,40),('bronze','menu_lower',340,10)]:
            with self.subTest(apple=apple):
                value=apple_row(self.frames[frame][1],apple)
                self.assertEqual((value['inventory'],value['gain']),(stock,gain))
        with self.assertRaises(ValueError):apple_row(self.frames['menu_top'][1],'stone')

    def test_real_confirmations_verify_one_item_and_expected_ap(self):
        for apple,gain in [('bronze',10),('blue',40),('silver',72),('gold',144)]:
            image,ocr=self.frames['confirm_'+apple]
            with self.subTest(apple=apple):
                value=apple_confirmation(ocr,apple,image)
                self.assertEqual(value['after_ap']-value['before_ap'],gain)
                self.assertEqual(value['before_ap'],current_ap(ocr)[0])
                self.assertTrue(confirmation_open(ocr))

    def test_other_apple_or_stone_cannot_match_the_confirmation(self):
        for apple in APPLE_NAMES:
            image,ocr=self.frames['confirm_'+apple]
            for other in set(APPLE_NAMES)-{apple}:
                with self.subTest(apple=apple,other=other),self.assertRaises(ValueError):
                    apple_confirmation(ocr,other,image)
        with self.assertRaises(ValueError):apple_confirmation(self.frames['confirm_gold'][1],'stone')

    def test_legacy_window_requires_independent_numeric_reading(self):
        image,ocr=self.frames['confirm_gold']
        with self.assertRaises(ValueError):apple_confirmation(ocr,'gold')
        with patch('src.strategy.ap_recovery.numeric_ap',side_effect=[(1,144),(160,144)]):
            with self.assertRaisesRegex(ValueError,'AP变化'):apple_confirmation(ocr,'gold',image)

    def test_map_background_low_confidence_button_requires_local_label_verification(self):
        image,ocr=self.frames['confirm_gold_map']
        value=apple_confirmation(ocr,'gold',image)
        self.assertEqual((value['before_ap'],value['after_ap'],value['confirm']),(17,161,(1260,846)))

    def test_quantity_plus_is_located_on_actual_button_and_rejects_blank(self):
        point=quantity_plus(self.frames['confirm_bronze'][0])
        self.assertTrue(1340<point[0]<1395 and 420<point[1]<490)
        with self.assertRaises(ValueError):quantity_plus(Image.new('RGB',(1920,1080),'black'))


class TestAppleRecoveryGuards(unittest.TestCase):
    def test_only_bottom_hud_is_actual_ap_not_prediction(self):
        self.assertEqual(current_ap(hud(4)+confirmation(before=4,after=14)),(4,144))
        with self.assertRaises(ValueError):current_ap(confirmation(before=4,after=14))

    def test_confirmation_quantity_button_and_statement_must_match(self):
        for ocr in [confirmation(count=2),[w for w in confirmation() if w[1]!='取消'],
                    [w for w in confirmation() if not w[1].startswith('是否执行')],
                    confirmation()+[word('决定',1261,878)]]:
            with self.assertRaises(ValueError):apple_confirmation(ocr,'bronze')

    def test_batch_confirmation_requires_exact_expected_count_and_only_copper(self):
        self.assertEqual(apple_confirmation(confirmation(count=3,after=30),'bronze',expected_count=3)['count'],3)
        for apple in ['blue','silver','gold']:
            with self.subTest(apple=apple),self.assertRaises(ValueError):
                apple_confirmation(confirmation(apple,count=2),apple,expected_count=2)

    def test_copper_count_is_minimum_to_reach_quest_ap(self):
        for available,count in [(0,3),(1,2),(4,2),(10,2),(11,1),(20,1),(21,0),(22,0)]:
            self.assertEqual(refill_count('bronze',21,available),count)
        self.assertEqual(refill_count('silver',21,0),1)

    def test_zero_stock_ambiguous_count_or_wrong_menu_is_rejected(self):
        menu=[word('行动力回复',960,80),word('消耗道具回复行动力',960,120),word('赤铜果实',770,660),
              word('0个持有',1370,660),word('消耗1个，回复行动力10（+10）',960,750)]
        with self.assertRaisesRegex(ValueError,'库存不足'):apple_row(menu,'bronze')
        with self.assertRaises(ValueError):apple_row(menu+[word('10个持有',1371,662)],'bronze')
        with self.assertRaises(ValueError):apple_row(menu[2:],'bronze')

    def test_local_numeric_disagreement_is_never_replaced_by_estimate(self):
        image=Image.new('RGB',(1920,1080))
        engine=MagicMock();engine.text_recognizer.side_effect=[([('14/144',.9)],None),([('114/144',.9)],None)]
        with patch.object(ScreenDetector,'get_ocr',return_value=engine):
            with self.assertRaisesRegex(ValueError,'不一致'):numeric_ap(image,(1025,520,1235,582))

    def test_local_button_wrong_text_or_two_scale_disagreement_never_confirms(self):
        image=Image.new('RGB',(1920,1080));engine=MagicMock()
        for texts in [('取消','决定'),('决定','取消')]:
            engine.text_recognizer.side_effect=[([(text,.9)],None) for text in texts]
            with patch.object(ScreenDetector,'get_ocr',return_value=engine):
                with self.assertRaisesRegex(ValueError,'局部文字复核失败'):
                    button_point([],'决定',(1000,800,1500,940),(1260,846),.55,image)

    def recovery(self,frames):
        controller=MagicMock();helper=AppleRecovery(controller)
        helper.frame=MagicMock(side_effect=frames)
        helper.find_apple=MagicMock(return_value={'point':(770,660),'inventory':340,'gain':10})
        return helper,controller

    def run_restore(self,frames,submit=None,event=None):
        helper,controller=self.recovery(frames);helper.cancel_event=event
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,{},.99)):
            result=helper.restore_once('bronze',5,submit or (lambda e:None))
        return result,helper,controller

    def test_actual_ap_verification_returns_only_after_menu_closed_and_scene_verified(self):
        result,helper,controller=self.run_restore([(None,hud()),(None,confirmation()),
                                                  (None,hud(10)),(None,hud(10))])
        self.assertEqual((result['status'],result['after_ap']),('verified',10))
        self.assertEqual(sum(c.args[:2]==(1260,876) for c in controller.click.call_args_list),1)

    def test_submit_is_before_decide_and_exception_prevents_resource_use(self):
        helper,controller=self.recovery([(None,hud()),(None,confirmation())])
        def submit(event):
            self.assertEqual(len(controller.click.call_args_list),2)
            raise RuntimeError('persist failed')
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.MAIN_MENU,{},.99)):
            with self.assertRaisesRegex(RuntimeError,'persist failed'):helper.restore_once('bronze',5,submit)
        self.assertFalse(any(c.args[:2]==(1260,876) for c in controller.click.call_args_list))

    def test_unverified_ap_after_submission_never_repeats_decide(self):
        helper,controller=self.recovery([(None,hud()),(None,confirmation())]+[(None,hud())]*8)
        submit=MagicMock()
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.MAIN_MENU,{},.99)):
            with self.assertRaisesRegex(ValueError,'不重复使用'):helper.restore_once('bronze',5,submit)
        submit.assert_called_once()
        self.assertEqual(sum(c.args[:2]==(1260,876) for c in controller.click.call_args_list),1)

    def test_natural_recovery_to_sufficient_ap_cancels_without_submission(self):
        helper,controller=self.recovery([(None,hud(4)),(None,confirmation(before=5,after=15)),(None,hud(5))])
        submit=MagicMock()
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.MAIN_MENU,{},.99)):
            self.assertIsNone(helper.restore_once('bronze',5,submit))
        submit.assert_not_called();self.assertIn((660,876),[c.args[:2] for c in controller.click.call_args_list])

    def test_already_sufficient_ap_and_cancel_do_not_open_menu(self):
        helper,controller=self.recovery([(None,hud(10))])
        self.assertIsNone(helper.restore_once('gold',5,MagicMock()));controller.click.assert_not_called()
        event=threading.Event();event.set();helper.cancel_event=event
        with self.assertRaises(RuntimeError):helper.restore_once('gold',5,MagicMock())
        controller.click.assert_not_called()

    def test_unexpected_scene_or_gain_never_submits_use(self):
        for scene,gain in [(GameScreen.SUPPORT_SELECT,10),(GameScreen.MAIN_MENU,144)]:
            helper,controller=self.recovery([(None,hud())]);helper.find_apple.return_value['gain']=gain
            submit=MagicMock()
            with patch.object(ScreenDetector,'detect',return_value=(scene,{},.99)):
                with self.assertRaises(ValueError):helper.restore_once('bronze',5,submit)
            submit.assert_not_called()

    def test_copper_batch_reads_each_increment_and_submits_one_transaction(self):
        helper,controller=self.recovery([(None,hud(4)),(None,confirmation(before=4,after=14)),
                                         (None,confirmation(before=4,after=24,count=2)),
                                         (None,hud(24)),(None,hud(24))])
        submit=MagicMock()
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,{},.99)), \
             patch('src.strategy.ap_recovery.quantity_plus',return_value=(1362,453)):
            result=helper.restore_once('bronze',21,submit,max_count=3)
        self.assertEqual((result['count'],result['after_ap']),(2,24))
        self.assertEqual(submit.call_args.args[0]['count'],2);submit.assert_called_once()
        self.assertEqual(sum(c.args[:2]==(1362,453) for c in controller.click.call_args_list),1)
        self.assertEqual(sum(c.args[:2]==(1260,876) for c in controller.click.call_args_list),1)

    def test_three_copper_items_are_selected_with_two_acknowledged_plus_clicks(self):
        helper,controller=self.recovery([(None,hud()),(None,confirmation()),
                                         (None,confirmation(after=20,count=2)),(None,confirmation(after=30,count=3)),
                                         (None,hud(30)),(None,hud(30))])
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,{},.99)), \
             patch('src.strategy.ap_recovery.quantity_plus',return_value=(1362,453)):
            result=helper.restore_once('bronze',21,MagicMock(),max_count=3)
        self.assertEqual(result['count'],3)
        self.assertEqual(sum(c.args[:2]==(1362,453) for c in controller.click.call_args_list),2)

    def test_insufficient_limit_or_inventory_or_game_maximum_never_consumes_partial_batch(self):
        for limit,stock,maximum in [(2,340,15),(3,2,15),(3,340,2)]:
            with self.subTest(limit=limit,stock=stock,maximum=maximum):
                helper,controller=self.recovery([(None,hud()),(None,confirmation(inventory=stock,maximum=maximum))])
                helper.find_apple.return_value['inventory']=stock;submit=MagicMock()
                with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,{},.99)):
                    with self.assertRaises(ValueError):helper.restore_once('bronze',21,submit,max_count=limit)
                submit.assert_not_called();self.assertEqual(controller.click.call_count,2)

    def test_unknown_increment_acknowledgement_is_not_reclicked_or_confirmed(self):
        for ocr in [confirmation(after=10,count=1),confirmation(after=30,count=3),confirmation(after=21,count=2)]:
            helper,controller=self.recovery([(None,hud()),(None,confirmation()),(None,ocr)])
            submit=MagicMock()
            with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,{},.99)), \
                 patch('src.strategy.ap_recovery.quantity_plus',return_value=(1362,453)):
                with self.assertRaises(ValueError):helper.restore_once('bronze',21,submit,max_count=3)
            submit.assert_not_called()
            self.assertEqual(sum(c.args[:2]==(1362,453) for c in controller.click.call_args_list),1)
            self.assertFalse(any(c.args[:2]==(1260,876) for c in controller.click.call_args_list))

    def test_unverified_batch_reserves_full_quantity_without_reconfirming(self):
        helper,controller=self.recovery([(None,hud()),(None,confirmation()),
                                         (None,confirmation(after=20,count=2)),(None,confirmation(after=30,count=3))]
                                        +[(None,hud())]*8)
        submit=MagicMock()
        with patch.object(ScreenDetector,'detect',return_value=(GameScreen.REGIONAL_MAP,{},.99)), \
             patch('src.strategy.ap_recovery.quantity_plus',return_value=(1362,453)):
            with self.assertRaisesRegex(ValueError,'不重复使用'):helper.restore_once('bronze',21,submit,max_count=3)
        submit.assert_called_once();self.assertEqual(submit.call_args.args[0]['count'],3)
        self.assertEqual(sum(c.args[:2]==(1260,876) for c in controller.click.call_args_list),1)


if __name__=='__main__':unittest.main()
