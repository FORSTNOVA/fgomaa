import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from src.strategy.auto_party_assembler import AutoPartyAssembler


class TestSupportSearch(unittest.TestCase):
    def run_search(self, target_page, rotation=True):
        state = {'page': 2}
        ctrl = MagicMock(cancel_event=None)
        def word(text, y):
            return ([[650,y-10],[750,y-10],[750,y+10],[650,y+10]], text, .95)
        def frame(**kwargs):
            img = Image.new('RGB', (1920,1080))
            img.info['ocr'] = [word('玩家' + str(state['page']), 350)]
            if state['page'] == target_page:
                img.info['ocr'].append(word('Archetype：Earth', 500))
            return img
        def swipe(x1,y1,x2,y2,**kwargs):
            state['page'] = max(0,state['page']-1) if y2>y1 else min(6,state['page']+1)
        ctrl.screencap.side_effect = frame
        ctrl.wait_for_still.side_effect = frame
        ctrl.swipe.side_effect = swipe
        assembler = AutoPartyAssembler(log_callback=lambda _:None)
        assembler.match_servant_in_region = MagicMock(return_value=(False,0,None))
        rec = SimpleNamespace(direction='MultiDPS' if rotation else 'Arts',
                              direction_name='test', party_servant_ids={3:2300500} if rotation else {})
        with patch('src.strategy.auto_party_assembler.ScreenDetector.run_ocr', side_effect=lambda img:img.info['ocr']), \
             patch('src.strategy.auto_party_assembler.time.sleep'):
            result = assembler.select_support_on_support_screen(ctrl,rec)
        return result, ctrl

    def test_target_below_three_pages_is_found_after_returning_to_top(self):
        result, ctrl = self.run_search(5)
        self.assertEqual(result,(700,500))
        self.assertGreater(ctrl.swipe.call_count,5)

    def test_reaches_bottom_and_stops_without_arbitrary_support(self):
        for rotation in (True,False):
            result,ctrl = self.run_search(100,rotation)
            self.assertIsNone(result)
            self.assertLess(ctrl.swipe.call_count,25)

    def test_same_name_in_other_class_is_not_selected_before_filtering(self):
        def word(text, y):
            return ([[650,y-10],[750,y-10],[750,y+10],[650,y+10]], text, .95)
        ctrl = MagicMock(cancel_event=None)
        image = Image.new('RGB', (1920,1080))
        ctrl.wait_for_still.return_value = image
        assembler = AutoPartyAssembler(log_callback=lambda _:None)
        assembler.match_servant_in_region = MagicMock(return_value=(False,0,None))
        rec = SimpleNamespace(direction='Arts', direction_name='test', party_servant_ids={})
        stale_berserker = [word('阿尔托莉雅',763)]
        with patch('src.strategy.auto_party_assembler.ScreenDetector.run_ocr', return_value=[word('阿尔托莉雅',470)]):
            result = assembler.select_support_on_support_screen(ctrl,rec,initial_ocr=stale_berserker)
        self.assertEqual(result,(700,470))
        ctrl.click.assert_called_once_with(605,180,delay_after=1.5)

    def test_no_target_in_requested_class_does_not_reuse_other_class_name(self):
        ctrl = MagicMock(cancel_event=None)
        ctrl.wait_for_still.return_value = Image.new('RGB',(1920,1080))
        assembler = AutoPartyAssembler(log_callback=lambda _:None)
        assembler.match_servant_in_region = MagicMock(return_value=(False,0,None))
        rec = SimpleNamespace(direction='Arts', direction_name='test', party_servant_ids={})
        box = [[650,750],[750,750],[750,770],[650,770]]
        with patch('src.strategy.auto_party_assembler.ScreenDetector.run_ocr',return_value=[]):
            # A stable row is needed to detect both ends of the list.
            with patch('src.strategy.auto_party_assembler.ScreenDetector.run_ocr',return_value=[(box,'其他术阶',.95)]):
                self.assertIsNone(assembler.select_support_on_support_screen(ctrl,rec,initial_ocr=[(box,'阿尔托莉雅',.95)]))


if __name__ == '__main__':
    unittest.main()
