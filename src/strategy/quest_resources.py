"""指定关卡出击前读取真实 AP；仅导航到卡片，不点击进入。"""
import re
import unicodedata

from src.strategy.ap_recovery import current_ap
from src.strategy.first_clear_progress import inspect_free_quest
from src.strategy.quest_catalog import quest_binding
from src.strategy.quest_navigation import navigate_daily_quest, synced_daily_coordinate
from src.strategy.screen_detector import GameScreen, ScreenDetector


def daily_card_ap(ocr, position, nominal):
    y = position[1]
    costs = [int(match[1]) for box, text, score in ocr
             if float(score) >= .75 and 1100 <= sum(p[0] for p in box)/4 <= 1850
             and y < sum(p[1] for p in box)/4 < min(940, y+180)
             if (match := re.fullmatch(r'(?:消耗)?AP[:：]?(\d+)',
                 re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)), re.I))]
    if len(costs) != 1 or type(nominal) is not int or not 0 <= costs[0] <= nominal:
        raise ValueError('不能唯一确认指定日常关卡的实际 AP')
    return costs[0]


def inspect_battle_quest(engine, target):
    entry = quest_binding(target.quest_id)
    if entry is None or entry['id'] != target.game_id or entry['phase'] != target.phase:
        raise ValueError('目标关卡绑定发生变化')
    if target.kind != 'daily':
        # 旧版内置自由本只记录最后阶段；这批内置关卡的布局均为三阶段。
        if 'phases' not in entry and target.kind == 'free' and target.phase == 3:
            entry = {**entry, 'phases':[1, 2, 3]}
        observation = inspect_free_quest(engine, target.quest_id, entry)
        actual_phase = entry['phase'] if observation['completed'] else observation['next_phase']
        if actual_phase != target.phase:
            raise ValueError(f'实机当前为阶段 {actual_phase}，模拟目标为阶段 {target.phase}。请使用自动首通推进前面的阶段')
        return observation
    if not navigate_daily_quest(engine, target.quest_id, inspect_only=True):
        raise ValueError('未能找到指定日常关卡卡片')
    observations = []
    for _ in range(2):
        if engine.is_cancelled():
            raise RuntimeError('任务已取消')
        image = engine.ctrl.screencap(force_fresh=True)
        screen, _, _ = ScreenDetector.detect(image)
        if screen != GameScreen.CHALDEA_GATE:
            raise ValueError('日常 AP 核对时界面发生变化')
        ocr = ScreenDetector.run_ocr(image)
        position = (synced_daily_coordinate(ocr, target.quest_id) if target.quest_id.startswith('atlas_cn_')
                    else ScreenDetector.find_target_quest_coordinate(ocr, target_quest=target.quest_id))
        if position is None:
            raise ValueError('日常 AP 核对时未确认目标关卡')
        nominal = target.ap_per_run
        if type(nominal) is not int:
            nominal = engine.current_recommendation.laplace.get('quest_ap')
        observations.append({'quest_id':target.game_id, 'phase':target.phase,
                             'card_ap':daily_card_ap(ocr, position, nominal),
                             'available_ap':current_ap(ocr)[0]})
    if observations[0]['card_ap'] != observations[1]['card_ap']:
        raise ValueError('连续两帧的日常 AP 消耗不一致')
    return observations[-1]
