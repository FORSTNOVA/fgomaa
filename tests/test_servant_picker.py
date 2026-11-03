import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from src.strategy.servant_picker import CLASS_LABELS, ServantPicker


def word(text, x, y):
    return ([[x - 40, y - 15], [x + 40, y - 15], [x + 40, y + 15], [x - 40, y + 15]], text, 0.95)


def picker_frame(shade=80):
    img = Image.new("RGB", (1920, 1080), (shade, shade, shade))
    img.info["ocr"] = [word("从者选择", 1720, 50), word("关闭", 163, 64),
                       word("筛选", 1460, 195), word("等级20/20", 500, 350)]
    return img


class FilterUI:
    """会实际切换颜色/按钮状态的离线筛选界面。"""
    def __init__(self):
        self.stage = "picker"
        self.positions = {f"★{n}": (400 + (5 - n) * 280, 367) for n in range(1, 6)}
        for i, label in enumerate(CLASS_LABELS.values()):
            self.positions[label] = (400 + i % 5 * 280, 600 + i // 5 * 120)
        self.selected = {label: True for label in self.positions}
        self.hidden_effect = True
        self.reset_count = 0
        self.toggle_enabled = True
        self.ctrl = MagicMock()
        self.ctrl.cancel_event = None
        self.ctrl.click.side_effect = self.click
        self.ctrl.wait_for_still.side_effect = self.frame

    def click(self, x, y, **kwargs):
        if (x, y) == (1460, 195):
            self.stage = "filter"
        elif (x, y) == (338, 955):
            self.reset_count += 1
            self.hidden_effect = False
            self.selected = {label: False for label in self.positions}
        elif (x, y) == (1583, 955):
            self.stage = "picker"
        elif self.toggle_enabled:
            for label, pos in self.positions.items():
                if pos == (x, y):
                    self.selected[label] = not self.selected[label]

    def frame(self, **kwargs):
        if self.stage == "picker":
            return picker_frame()
        img = Image.new("RGB", (1920, 1080), (30, 40, 60))
        draw = ImageDraw.Draw(img)
        ocr = [word("筛选设定", 960, 100), word("稀有度", 960, 260),
               word("回到初始设定", 338, 955), word("决定", 1583, 955)]
        for label, (x, y) in self.positions.items():
            color = (230, 230, 230) if self.selected[label] else (55, 110, 220)
            draw.rectangle((x - 100, y - 35, x + 100, y + 35), fill=color)
            ocr.append(word(label, x, y))
        img.info["ocr"] = ocr
        return img


class TestServantPicker(unittest.TestCase):
    def setUp(self):
        self.ui = FilterUI()
        self.asm = SimpleNamespace(log=MagicMock(), match_servant_in_region=MagicMock())
        self.picker = ServantPicker(self.ui.ctrl, self.asm)
        ocr_patch = patch("src.strategy.servant_picker.ScreenDetector.run_ocr", side_effect=lambda img: img.info["ocr"])
        ocr_patch.start()
        self.addCleanup(ocr_patch.stop)

    def test_filters_habetrot_by_rider_and_four_stars(self):
        self.assertTrue(self.picker.apply_filters(404200))
        self.assertEqual({k for k, v in self.ui.selected.items() if v}, {"骑兵", "★4"})
        self.assertFalse(self.ui.hidden_effect)
        self.assertEqual(self.ui.reset_count, 1)

    def test_transient_missing_entry_is_reread_before_any_filter_click(self):
        first=True
        def ocr(image):
            nonlocal first
            if first:first=False;return []
            return image.info['ocr']
        with patch('src.strategy.servant_picker.ScreenDetector.run_ocr',side_effect=ocr):
            self.assertTrue(self.picker.apply_filters(304700))
        self.assertEqual(sum(c.args==(1460,195) for c in self.ui.ctrl.click.call_args_list),1)

    def test_filter_dialog_transition_waits_without_reclicking_entry(self):
        missed=False
        def ocr(image):
            nonlocal missed
            if self.ui.stage=='filter' and not missed:missed=True;return []
            return image.info['ocr']
        with patch('src.strategy.servant_picker.ScreenDetector.run_ocr',side_effect=ocr):
            self.assertTrue(self.picker.apply_filters(304700))
        self.assertEqual(sum(c.args==(1460,195) for c in self.ui.ctrl.click.call_args_list),1)

    def test_return_to_picker_transition_is_reread_before_failure(self):
        missed=False
        def ocr(image):
            nonlocal missed
            if self.ui.stage=='picker' and self.ui.reset_count and not missed:missed=True;return []
            return image.info['ocr']
        with patch('src.strategy.servant_picker.ScreenDetector.run_ocr',side_effect=ocr):
            self.assertTrue(self.picker.apply_filters(304700))
        self.assertEqual(sum(c.args==(1583,955) for c in self.ui.ctrl.click.call_args_list),1)

    def test_unrecognized_entry_has_bounded_read_only_wait(self):
        with patch('src.strategy.servant_picker.ScreenDetector.run_ocr',return_value=[]) as ocr:
            self.assertFalse(self.picker.apply_filters(304700))
        self.assertEqual(ocr.call_count,4);self.ui.ctrl.click.assert_not_called()

    def test_filters_oberon_by_pretender_and_five_stars(self):
        self.assertTrue(self.picker.apply_filters(2800100))
        self.assertEqual({k for k, v in self.ui.selected.items() if v}, {"身披角色者", "★5"})

    def test_already_open_filter_dialog_is_supported(self):
        self.ui.stage = "filter"
        self.assertTrue(self.picker.apply_filters(504500))
        self.assertNotIn((1460, 195), [c.args for c in self.ui.ctrl.click.call_args_list])

    def test_unknown_metadata_clears_old_filters_without_guessing(self):
        self.assertTrue(self.picker.apply_filters(99999999))
        self.assertFalse(any(self.ui.selected.values()))
        self.assertFalse(self.ui.hidden_effect)

    def test_selected_button_is_not_toggled_off(self):
        self.ui.stage = "filter"
        self.assertTrue(self.picker.set_filter_button("骑兵"))
        self.ui.ctrl.click.assert_not_called()

    def test_button_click_must_change_the_selected_state(self):
        self.ui.stage = "filter"
        self.ui.selected["骑兵"] = False
        self.ui.toggle_enabled = False
        self.assertFalse(self.picker.set_filter_button("骑兵"))
        self.assertEqual(self.ui.ctrl.click.call_count, 1)

    def test_select_group_removes_other_selected_options(self):
        self.ui.stage = "filter"
        self.assertTrue(self.picker.select_filter_group("★4", [f"★{n}" for n in range(1, 6)]))
        self.assertEqual({k for k, v in self.ui.selected.items() if v and k.startswith("★")}, {"★4"})

    def setup_search(self, frames):
        self.picker.apply_filters = MagicMock(return_value=True)
        self.picker.to_top = MagicMock(return_value=True)
        self.picker.frame = MagicMock(side_effect=frames)
        self.picker.scroll_position = MagicMock(return_value=None)
        self.asm.match_servant_in_region.return_value = (False, 0.1, (0, 0))
        # Navigation tests isolate the separate card-level matching gate.
        self.picker.match_target_at_level = MagicMock(side_effect=lambda *args: self.asm.match_servant_in_region())

    def test_search_continues_beyond_eight_pages_and_low_levels(self):
        self.setup_search([picker_frame(20 + i * 10) for i in range(12)])
        self.asm.match_servant_in_region.side_effect = [(False, 0.1, (0, 0))] * 10 + [(True, 0.9, (500, 420))]
        self.assertTrue(self.picker.select(404200, "哈贝特洛特", 80))
        self.assertEqual(self.asm.match_servant_in_region.call_count, 11)
        self.assertEqual(self.ui.ctrl.swipe.call_count, 10)
        self.assertEqual(self.ui.ctrl.click.call_args.args, (500, 420))

    def test_bottom_page_is_scanned_before_returning_failure(self):
        self.setup_search([picker_frame(), picker_frame()])
        self.picker.scroll_position.return_value = 0.99
        self.assertFalse(self.picker.select(404200, "哈贝特洛特"))
        self.asm.match_servant_in_region.assert_called_once()
        self.ui.ctrl.swipe.assert_not_called()

    def test_bottom_page_target_is_not_missed(self):
        self.setup_search([picker_frame()])
        self.picker.scroll_position.return_value = 0.99
        self.asm.match_servant_in_region.return_value = (True, 0.9, (500, 420))
        self.assertTrue(self.picker.select(404200, "哈贝特洛特"))

    def test_no_scrollbar_short_list_stops_after_three_unchanged_swipes(self):
        self.setup_search([picker_frame() for _ in range(5)])
        self.assertFalse(self.picker.select(404200, "哈贝特洛特"))
        self.assertEqual(self.ui.ctrl.swipe.call_count, 3)
        self.assertEqual(self.asm.match_servant_in_region.call_count, 4)

    def test_unexpected_return_to_party_is_not_selection_success(self):
        img = picker_frame()
        img.info["ocr"] = [word("开始任务", 1750, 1000)]
        self.setup_search([img])
        self.assertFalse(self.picker.select(404200, "哈贝特洛特"))
        self.ui.ctrl.click.assert_not_called()

    def test_filtered_search_does_not_start_after_filter_failure(self):
        self.picker.apply_filters = MagicMock(return_value=False)
        self.assertFalse(self.picker.select(404200, "哈贝特洛特"))
        self.asm.match_servant_in_region.assert_not_called()

    def test_cancellation_stops_without_any_click(self):
        self.ui.ctrl.cancel_event = MagicMock()
        self.ui.ctrl.cancel_event.is_set.return_value = True
        self.assertFalse(self.picker.select(404200, "哈贝特洛特"))
        self.ui.ctrl.click.assert_not_called()

    def test_scrolling_to_top_is_not_limited_to_two_swipes(self):
        self.picker.frame = MagicMock(side_effect=[picker_frame(i * 20 + 30) for i in range(5)])
        self.picker.scroll_position = MagicMock(side_effect=[0.9, 0.6, 0.3, 0.0])
        self.assertTrue(self.picker.to_top(float("inf")))
        self.assertEqual(self.ui.ctrl.swipe.call_count, 3)

    def test_scrollbar_top_and_bottom_from_pixels(self):
        for y, expected in ((280, 0.0), (980, 1.0)):
            with self.subTest(y=y):
                img = Image.new("RGB", (1920, 1080), (20, 30, 40))
                ImageDraw.Draw(img).rectangle((1844, y, 1877, y + 80), fill=(240, 240, 240))
                self.assertAlmostEqual(ServantPicker.scroll_position(img), expected, delta=0.02)


if __name__ == "__main__":
    unittest.main()
