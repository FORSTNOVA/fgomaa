"""Verify daily rewards from refreshed pages, including the game's automatic claims."""
import re

import cv2
import numpy as np

from src.strategy.screen_detector import ScreenDetector


class DailyMissions:
    def __init__(self, engine):
        self.engine, self.ctrl, self.log = engine, engine.ctrl, engine.log

    def read(self):
        img = self.ctrl.wait_for_still(timeout=2, min_still_duration=0.2)
        if img.size != (1920, 1080):
            img = img.resize((1920, 1080))
        return img, ScreenDetector.run_ocr(img)

    @staticmethod
    def active(img, ocr):
        pos = ScreenDetector.find_text_coordinate(ocr, "日常", region=(850, 130, 1080, 270))
        if pos is None:
            return False
        x, y = pos
        arr = np.array(img.convert("RGB"))[y-22:y+22, x-75:x+75]
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        return ((hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 135) &
                (hsv[:, :, 1] > 75) & (hsv[:, :, 2] > 160)).mean() > 0.35

    @staticmethod
    def summary(ocr):
        for box, text, _ in ocr:
            x, y = (sum(p[i] for p in box)/4 for i in (0, 1))
            m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text.strip())
            if m and 850 <= x <= 1640 and 280 <= y <= 360:
                done, total = int(m[1]), int(m[2])
                if 0 < total <= 10 and 0 <= done <= total:
                    return done, total
        return None

    @staticmethod
    def cards(ocr):
        items = []
        for box, text, _ in ocr:
            x, y = (sum(p[i] for p in box)/4 for i in (0, 1))
            if 900 <= x <= 1880 and 370 <= y <= 1060:
                items.append((y, x, re.sub(r"\s+", "", text)))
        titles = [(y, text) for y, x, text in items if x < 1640 and
                  re.fullmatch(r"通关\d+次关卡|完成所有的每日御主任务", text)]
        return {title: any(1600 < x and -30 <= sy-y <= 150 and
                          text in ("已自动领取", "已领取", "领取完毕")
                          for sy, x, text in items) for y, title in titles}

    def scan(self):
        seen = {}
        for page in range(5):
            if self.engine.is_cancelled():
                return False
            img, ocr = self.read()
            if not self.active(img, ocr):
                return False
            summary = self.summary(ocr)
            if summary is None or summary[0] != summary[1]:
                self.log("[日常领奖停止] 未确认全部日常任务完成。")
                return False
            for title, claimed in self.cards(ocr).items():
                seen[title] = seen.get(title, False) or claimed
            if len(seen) == summary[1] and all(seen.values()):
                self.log(f"[日常领奖确认] {len(seen)}项任务均已领取（含已自动领取）。")
                return True
            if page < 4:
                self.ctrl.swipe(1430, 930, 1430, 470, duration_ms=400)
        self.log("[日常领奖停止] 未取得全部任务已领取的证据。")
        return False

    def claim(self, initial_ocr):
        tab = ScreenDetector.find_text_coordinate(initial_ocr, "日常", region=(850, 130, 1080, 270))
        if tab is None or self.engine.is_cancelled():
            return False
        self.ctrl.click(*tab, delay_after=1)
        for _ in range(3):
            img, ocr = self.read()  # Never reuse the previous tab's claim button.
            if not self.active(img, ocr) or self.engine.is_cancelled():
                return False
            for _ in range(5):
                if ScreenDetector.find_text_coordinate(ocr, "全部", region=(1400, 270, 1910, 370)):
                    break
                toggle = next((ScreenDetector.find_text_coordinate(ocr, label, region=(1400, 270, 1910, 370))
                               for label in ("可领取", "进行中", "已达成")
                               if ScreenDetector.find_text_coordinate(ocr, label, region=(1400, 270, 1910, 370))), None)
                if toggle is None or self.engine.is_cancelled():
                    return False
                self.ctrl.click(*toggle, delay_after=0.6)
                img, ocr = self.read()
                if not self.active(img, ocr):
                    return False
            else:
                return False
            button = ScreenDetector.find_text_coordinate(ocr, "统一领取", region=(1400, 270, 1910, 370))
            if button is not None:
                self.ctrl.click(*button, delay_after=0.8)
                for _ in range(5):
                    img, popup = self.read()
                    if self.active(img, popup):
                        break
                    close = (ScreenDetector.find_text_coordinate(popup, "关闭", region=(500, 600, 1400, 1000)) or
                             ScreenDetector.find_text_coordinate(popup, "决定", region=(500, 600, 1400, 1000)))
                    if close is None or self.engine.is_cancelled():
                        return False
                    self.ctrl.click(*close, delay_after=0.6)
            # Four daily cards fit in two screens; return to their top before scanning.
            for _ in range(3):
                if self.engine.is_cancelled():
                    return False
                self.ctrl.swipe(1430, 470, 1430, 930, duration_ms=400)
            if self.scan():
                return True
            if button is None:
                return False
        return False
