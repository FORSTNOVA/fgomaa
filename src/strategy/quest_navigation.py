"""明确关卡导航适配器；日常指定关卡不套用每日最低AP替换策略。"""
import time
import re
import unicodedata

from src.strategy.screen_detector import GameScreen, ScreenDetector
from src.strategy.anchor_verifier import AnchorVerifier


def synced_daily_coordinate(ocr, target):
    from src.strategy.quest_catalog import quest_binding
    entry = quest_binding(target)
    if entry is None or entry.get('kind') != 'daily':
        return None
    def clean(value):
        value = unicodedata.normalize('NFKC', value)
        value = re.sub(r'^每日替换\s*', '', value)
        return re.sub(r'\s+', '', value)
    name = clean(entry['name'])
    for box, text, score in ocr:
        x, y = sum(p[0] for p in box)/4, sum(p[1] for p in box)/4
        if score < .7 or clean(text) != name or not (900 < x < 1850 and 180 < y < 850):
            continue
        costs = [int(match[1]) for b,t,s in ocr if s >= .75
                 and 1100 <= sum(p[0] for p in b)/4 <= 1850
                 and y < sum(p[1] for p in b)/4 < min(940,y+180)
                 if (match := re.fullmatch(r'(?:消耗)?AP[:：]?(\d+)',clean(t),re.I))]
        if len(set(costs)) == 1 and costs[0] <= entry.get('ap',-1):
            return int(x), int(y)
    return None


def navigate_daily_quest(engine, target, max_steps=45):
    selected = False
    searched_from_top = False
    unchanged = 0
    for _ in range(max_steps):
        if engine.is_cancelled():
            return False
        screen, details = engine.get_current_screen()
        ocr = details.get('ocr_results', [])
        if screen in (GameScreen.SUPPORT_SELECT, GameScreen.PARTY_CONFIRM):
            if selected:
                return True
            engine.log('[导航停止] 当前编队/助战没有本次指定日常关卡的身份依据。')
            return False
        if screen == GameScreen.LOADING:
            time.sleep(.5)
            continue
        if screen == GameScreen.MAIN_MENU:
            pos = ScreenDetector.find_text_coordinate(ocr, '迦勒底之门', region=(900, 150, 1920, 950))
            if pos:
                engine.ctrl.click(*pos, delay_after=1.0)
            else:
                engine.ctrl.swipe(1400, 300, 1400, 800, duration_ms=350)
            continue
        if screen in (GameScreen.REGIONAL_MAP, GameScreen.MAIN_RECORD):
            if not engine.navigate_to_main_menu():
                return False
            continue
        if screen != GameScreen.CHALDEA_GATE:
            engine.log('[导航停止] 日常导航遇到非预期页面，保留现场。')
            return False
        daily = ScreenDetector.find_text_coordinate(ocr, '每日任务', region=(1000, 180, 1920, 600))
        if daily:
            engine.ctrl.click(*daily, delay_after=1.0)
            continue
        if not searched_from_top:
            for _ in range(10):
                before = engine.ctrl.screencap(force_fresh=True)
                engine.ctrl.swipe(1400, 350, 1400, 860, duration_ms=300)
                after = engine.ctrl.wait_for_still(timeout=1.5, min_still_duration=.2)
                if AnchorVerifier.compute_frame_diff(before, after, roi=(900,180,1900,950)) < .004:
                    break
            searched_from_top = True
            continue
        pos = (synced_daily_coordinate(ocr, target) if target.startswith('atlas_cn_')
               else ScreenDetector.find_target_quest_coordinate(ocr, target_quest=target))
        if pos:
            engine.ctrl.click(*pos, delay_after=1.5)
            selected = True
            continue
        before = engine.ctrl.screencap(force_fresh=True)
        engine.ctrl.swipe(1400, 840, 1400, 230, duration_ms=300)
        after = engine.ctrl.wait_for_still(timeout=1.5, min_still_duration=.2)
        unchanged = unchanged + 1 if AnchorVerifier.compute_frame_diff(before, after, roi=(900,180,1900,950)) < .004 else 0
        if unchanged >= 3:
            break
    engine.log('[导航停止] 指定日常关卡未找到或未开放，未改选其他关卡。')
    return False
