"""识别国服同关卡连续出击确认框及其中的 AP。"""
import re
import unicodedata

import numpy as np

from src.strategy.screen_detector import ScreenDetector


def _normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def repeat_buttons(ocr):
    """须同时看到确认标题和两个按钮，避开助战页与补体弹窗。"""
    def locate(labels, region):
        left, top, right, bottom = region
        hits = []
        for box, text, score in ocr:
            x, y = (round(sum(point[i] for point in box) / 4) for i in (0, 1))
            if score >= .55 and left <= x <= right and top <= y <= bottom and _normalize(text) in labels:
                hits.append((x, y))
        return hits[0] if len(hits) == 1 else None

    title = locate(('是否连续进行同一关卡?',), (550, 90, 1370, 250))
    close = locate(('关闭',), (300, 650, 960, 980))
    repeat = locate(('连续出击', '再次挑战'), (961, 650, 1650, 980))
    return (close, repeat) if title and close and repeat else None


def repeat_ap(image):
    """逐行读取标签和数值，避免整屏 OCR 漏掉单独显示的消耗数字。"""
    rows = ((700, 430, 1240, 496), (700, 496, 1240, 558))
    patches = []
    for bounds in rows:
        crop = image.crop(bounds).convert('RGB')
        patches.extend(np.array(crop.resize((crop.width * scale, crop.height * scale))) for scale in (1, 2))
    lines, _ = ScreenDetector.get_ocr().text_recognizer(patches)
    patterns = (r'行动力消耗:(\d{1,3})', r'行动力现在:(\d{1,4})/(\d{1,3})')
    readings = []
    for index, pattern in enumerate(patterns):
        matches = [re.fullmatch(pattern, _normalize(text)) if score >= .65 else None
                   for text, score in lines[index * 2:index * 2 + 2]]
        if len(matches) != 2 or not all(matches) or matches[0].groups() != matches[1].groups():
            return None
        readings.append(tuple(map(int, matches[0].groups())))
    (cost,), (available, cap) = readings
    return (cost, available) if cap > 0 else None
