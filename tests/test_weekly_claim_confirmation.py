import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from PIL import Image, ImageDraw
from src.strategy.screen_detector import ScreenDetector
from src.strategy.weekly_missions import WeeklyMissions

def word(text,x,y):
    return ([[x-20,y-10],[x+20,y-10],[x+20,y+10],[x-20,y+10]],text,.95)

class TestClaimConfirmation(unittest.TestCase):
    def test_claimed_label_on_reward_icon_is_not_just_a_completion_stamp(self):
        cards=WeeklyMissions.extract_cards([word('击败20个敌人',1200,450),
            word('20/20',960,570),word('已获取',1740,515)])
        self.assertTrue(cards[0]['claimed'])

    def test_live_seven_slash_seven_is_recovered_when_ocr_reads_717(self):
        frame=Image.new('RGB',(1920,1080),(100,100,100))
        frame.paste(Image.open(Path(__file__).parent/'fixtures/weekly_claimed_counter.png'),(1270,280))
        ImageDraw.Draw(frame).rectangle((1070,185,1250,245),fill=(40,170,240))
        full=[word('周常',1160,215),word('717',1340,320)]
        recognize=ScreenDetector.run_ocr
        def ocr(img):
            return list(full) if img.size==(1920,1080) else recognize(img)
        ctrl=MagicMock()
        ctrl.wait_for_still.return_value=frame
        scanner=WeeklyMissions(SimpleNamespace(ctrl=ctrl,log=MagicMock()))
        with patch.object(ScreenDetector,'run_ocr',side_effect=ocr):
            _,words=scanner.read()
        self.assertEqual(scanner.summary(words),(7,7))
        hud=Image.open(Path(__file__).parent/'fixtures/weekly_claimed_counter.png').convert('RGB')
        draw=ImageDraw.Draw(hud)
        draw.rectangle((67,20,78,65),fill=(0,0,0))
        draw.rectangle((71,27,74,60),fill=(255,255,255))
        self.assertEqual(scanner.restore_counter_separator(hud,'717'),'717')

    def test_live_button_missing_from_fullscreen_ocr_is_read_from_crop(self):
        frame=Image.new('RGB',(1920,1080),(100,100,100))
        frame.paste(Image.open(Path(__file__).parent/'fixtures/weekly_claim_button.png'),(1000,880))
        words=[word('领取确认',960,70),word('是否领取全部报酬？',960,820)]
        button=WeeklyMissions.claim_confirmation_button(frame,words)
        self.assertIsNotNone(button)
        self.assertTrue(1200<=button[0]<=1300 and 900<=button[1]<=970)
        # 即使图片有领取按钮，没有确认标题和问题时也不提交。
        self.assertIsNone(WeeklyMissions.claim_confirmation_button(frame,words[:1]))
        self.assertIsNone(WeeklyMissions.claim_confirmation_button(frame,words[1:]))

if __name__=='__main__':
    unittest.main()
