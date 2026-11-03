import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.strategy.daily_quest_policy import discount_status, minimum_plan
from src.strategy.smart_engine import SmartEngine
from src.strategy.screen_detector import GameScreen


def word(text, y, x=1400, score=.99):
    return ([[x-50,y-10],[x+50,y-10],[x+50,y+10],[x-50,y+10]], text, score)


class TestDailyQuestPolicy(unittest.TestCase):
    def test_target_reduced_ap_or_explicit_campaign(self):
        for words in ([word("AP20",390)], [word("消耗AP减半 举办期间3日",410)],
                      [word("消耗AP1/3",410)], [word("AP0",390)]):
            self.assertTrue(discount_status(words, (1300,300),40))

    def test_other_cards_and_unrelated_bonuses_do_not_count(self):
        words = [word("AP40",390), word("羁绊获得量2倍",410),
                 word("搜集种火极级",550), word("消耗AP减半",670)]
        self.assertFalse(discount_status(words,(1300,300),40))

    def test_missing_or_conflicting_or_low_confidence_ap_is_unknown(self):
        for words in ([], [word("AP20",390,score=.4)], [word("AP20",390),word("AP40",420)]):
            self.assertIsNone(discount_status(words,(1300,300),40))

    def test_minimum_uses_weekly_catalog_and_requested_run_count(self):
        plan = minimum_plan(3)
        self.assertEqual((plan.quest_id,plan.ap_per_run,plan.runs,plan.total_ap),('fuyuki_x_a',3,3,9))

    def engine(self):
        e = SmartEngine(MagicMock(cancel_event=None),log_callback=lambda _:None)
        e.current_recommendation = SimpleNamespace(strategy=SimpleNamespace(name='validated'))
        e.daily_progress = {'quest_clears':0}
        e._ensure_laplace = MagicMock(return_value=True)
        e.navigate_to_main_menu = MagicMock(return_value=True)
        def execute(*a, **k):
            e.last_weekly_clears = 2
            return True
        e.run_reactive_weekly_plan = MagicMock(side_effect=execute)
        return e

    @patch('src.workflows.quest_runner.resolve_queue')
    @patch('src.strategy.daily_quest_policy.normal_ap',return_value=40)
    def test_discount_uses_shared_executor_then_returns_to_daily(self,_,resolve):
        e=self.engine()
        target=SimpleNamespace(quest_id='daily_qp_extreme',runs=2)
        resolve.return_value=[target]
        def execute(*args,**kwargs):
            e.last_quest_clears=2
            return True
        e.run_quest=MagicMock(side_effect=execute)
        self.assertEqual(e._select_daily_quest('daily_qp_extreme',[word('AP20',390)],(1300,300),2),'completed')
        e.run_quest.assert_called_once_with(target,cancel_event=None)
        self.assertEqual(e.daily_progress['quest_clears'],2)
        e._ensure_laplace.assert_not_called()
        e.run_reactive_weekly_plan.assert_not_called()
        e.ctrl.click.assert_not_called()

    @patch('src.strategy.daily_quest_policy.normal_ap',return_value=40)
    def test_no_discount_replans_minimum_then_returns_to_daily(self,_):
        e=self.engine()
        self.assertEqual(e._select_daily_quest('daily_qp_extreme',[word('AP40',390)],(1300,300),2),'minimum')
        self.assertEqual(e.run_reactive_weekly_plan.call_args.args[0].quest_id,'fuyuki_x_a')
        self.assertEqual(e.daily_progress['quest_clears'],2)
        e.ctrl.click.assert_not_called()
        self.assertEqual(e.navigate_to_main_menu.call_count,2)

    def test_failure_preserves_partial_clears_and_never_returns_complete(self):
        e=self.engine()
        def fail(*a,**k):
            e.last_weekly_clears=1
            return False
        e.run_reactive_weekly_plan.side_effect=fail
        self.assertIsNone(e._select_daily_quest('daily_qp_extreme',[],None,2))
        self.assertEqual(e.daily_progress['quest_clears'],1)

    def test_unknown_discount_stops_after_refresh_without_entering_any_quest(self):
        e=self.engine()
        e.get_current_screen=MagicMock(return_value=(GameScreen.CHALDEA_GATE,
            {'ocr_results':[word('打开宝物库之门极级',300)]}))
        with patch('src.strategy.daily_quest_policy.normal_ap',return_value=40):
            self.assertIsNone(e._select_daily_quest('daily_qp_extreme',[],(1300,300),2))
        e.ctrl.click.assert_not_called()
        e.run_reactive_weekly_plan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
