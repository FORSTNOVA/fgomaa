"""从者选择：目标筛选、到顶后逐页搜索、到达底部才结束。"""

import json
import re
import time

import cv2
import numpy as np

from src.core.path_helper import get_resource_path
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.screen_detector import ScreenDetector


CLASS_LABELS = {
    "saber": "剑士", "archer": "弓兵", "lancer": "枪兵", "rider": "骑兵",
    "caster": "魔术师", "assassin": "暗匿者", "berserker": "狂战士",
    "shielder": "盾兵", "ruler": "裁定者", "avenger": "复仇者",
    "moonCancer": "月之癌", "alterEgo": "他人格", "foreigner": "降临者",
    "pretender": "身披角色者", "beast": "兽",
}


class ServantPicker:
    GRID = (140, 275, 1800, 1065)

    def __init__(self, controller, assembler):
        self.ctrl = controller
        self.assembler = assembler
        self.log = assembler.log

    def cancelled(self):
        return self.ctrl.cancel_event is not None and self.ctrl.cancel_event.is_set()

    def frame(self):
        img = self.ctrl.wait_for_still(timeout=1.6, min_still_duration=0.2)
        if img.size != (1920, 1080):
            img = img.resize((1920, 1080))
        return img

    def wait_ocr(self, predicate):
        """切换页面时有限等待新的识别帧，只核对状态，不重复点击。"""
        for _ in range(4):
            if self.cancelled():
                return None
            ocr = ScreenDetector.run_ocr(self.frame())
            if predicate(ocr):
                return ocr
            self.ctrl._safe_sleep(.2)
        return None

    @staticmethod
    def is_picker(ocr):
        texts = "".join(t.replace(" ", "") for _, t, _ in ocr)
        return "筛选设定" not in texts and any(t in texts for t in ("从者选择", "选择从者", "请点击想要替换的从者", "请点击要替换的从者"))

    @staticmethod
    def button(ocr, label, region=None):
        matches = [item for item in ocr if item[1].replace(" ", "") == label]
        return ScreenDetector.find_text_coordinate(matches, label, region=region)

    @staticmethod
    def button_selected(img, pos):
        """游戏筛选按钮白色为选中，蓝色为未选中；不明确时不盲点。"""
        x, y = pos
        patch = np.array(img.convert("RGB"))[max(0, y - 24):y + 24, max(0, x - 85):x + 85]
        if patch.size == 0:
            return None
        hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
        white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 150)
        blue = (hsv[:, :, 0] >= 90) & (hsv[:, :, 0] <= 135) & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 80)
        if white.mean() > 0.55:
            return True
        if blue.mean() > 0.55:
            return False
        return None

    def filter_ocr(self, img, labels):
        """全屏漏读按钮时单独识别卡框，避免将漏字误当成尚未滚到。"""
        ocr = ScreenDetector.run_ocr(img)
        if self.button(ocr, "筛选设定") is None:
            return ocr
        region = (200, 200, 1680, 880)
        missing = {label for label in labels if self.button(ocr, label, region) is None}
        if not missing:
            return ocr
        hsv = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2HSV)
        blue = (hsv[:, :, 0] >= 90) & (hsv[:, :, 0] <= 135) & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 80)
        white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 150)
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        mask[200:880, 200:1680] = (blue | white)[200:880, 200:1680].astype(np.uint8) * 255
        # 内外两道边框都可能形成轮廓，优先内框并按中心去重。
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        rectangles = sorted((cv2.boundingRect(c) for c in contours), key=lambda r:r[2]*r[3])
        seen = []
        known = set(CLASS_LABELS.values()) | {f"★{n}" for n in range(1, 6)}
        for x, y, w, h in rectangles:
            if not (230 <= w <= 285 and 60 <= h <= 110):
                continue
            center = (x + w // 2, y + h // 2)
            if any(abs(center[0]-px) < 20 and abs(center[1]-py) < 20 for px, py in seen):
                continue
            seen.append(center)
            if any(text.replace(" ", "") in known and abs(center[0]-sum(p[0] for p in box)/4) < 100
                   and abs(center[1]-sum(p[1] for p in box)/4) < 30 for box, text, _ in ocr):
                continue
            left, top = x + 12, y + 8
            patch = np.array(img.crop((left, top, x+w-12, y+h-8)).convert("RGB"))
            read, _ = ScreenDetector.get_ocr()(patch, use_cls=False)
            for box, text, score in read or []:
                label = text.replace(" ", "")
                if label in missing and float(score) >= .55:
                    ocr.append(([[px+left, py+top] for px, py in box], label, float(score)))
                    missing.remove(label)
                    self.log(f"  [筛选局部识别] 已确认漏读按钮【{label}】。")
            if not missing:
                break
        return ocr

    @staticmethod
    def scroll_position(img):
        """主列表右侧滚动条；无滚动条的短列表返回 None。"""
        arr = np.array(img.convert("RGB"))[278:1064, 1843:1878]
        light = (arr.min(axis=2) > 180) & (arr.max(axis=2).astype(int) - arr.min(axis=2) < 70)
        ys = np.flatnonzero(light.mean(axis=1) > 0.35)
        if not len(ys):
            return None
        runs = np.split(ys, np.where(np.diff(ys) > 1)[0] + 1)
        run = max(runs, key=len)
        if len(run) < 12 or len(run) >= len(arr) - 10:
            return None
        # 白色内芯在轨道两端约有 6px 边框留白，短行程滚动条尤其需要扣除。
        travel = max(1, len(arr) - len(run) - 12)
        return float(np.clip((run[0] - 6) / travel, 0, 1))

    @classmethod
    def list_frame_diff(cls, before, after, ocr=None):
        """比较卡片内部，忽略短列表空白处持续播放的背景动画。"""
        ocr = ScreenDetector.run_ocr(before) if ocr is None else ocr
        diffs = []
        for box, text, _ in ocr:
            # 方向分类器有时会把“等级90/90”读成带尾字的“06/06等级”。
            if text.count("/") != 1 or not re.search(r"\d{1,3}/\d{1,3}", text.replace(" ", "")):
                continue
            x = min(p[0] for p in box)
            y = max(p[1] for p in box)
            if not (180 <= x <= 1800 and 275 <= y <= 970):
                continue
            roi = (max(140, int(x) - 40), int(y) + 12,
                   min(1800, int(max(p[0] for p in box)) + 12), min(1065, int(y) + 95))
            diffs.append(AnchorVerifier.compute_frame_diff(before, after, roi=roi))
        return max(diffs) if diffs else AnchorVerifier.compute_frame_diff(before, after, roi=cls.GRID)

    def set_filter_button(self, label, desired=True):
        for attempt in range(2):
            if self.cancelled():
                return False
            img = self.frame()
            ocr = self.filter_ocr(img, [label])
            if self.button(ocr, "筛选设定") is None:
                return False
            pos = self.button(ocr, label, region=(200, 200, 1680, 880))
            if pos is None:
                return False
            selected = self.button_selected(img, pos)
            if selected is desired:
                return True
            if selected is None or attempt:
                return False
            self.ctrl.click(*pos, delay_after=0.4)
        return False

    def select_filter_group(self, target, labels):
        img = self.frame()
        ocr = self.filter_ocr(img, labels)
        for label in labels:
            pos = self.button(ocr, label, region=(200, 200, 1680, 880))
            if pos is None:
                if label == target:
                    return False
                continue
            selected = self.button_selected(img, pos)
            if selected is None:
                return False
            if selected != (label == target) and not self.set_filter_button(label, label == target):
                return False
        return True

    def apply_filters(self, svt_id):
        path = get_resource_path("data/servant_filter_metadata.json")
        metadata = json.loads(path.read_text(encoding="utf-8")).get(str(svt_id), {}) if path.exists() else {}
        class_label = CLASS_LABELS.get(metadata.get("className"))
        rarity = metadata.get("rarity")
        rarity_label = f"★{rarity}" if isinstance(rarity, int) and 1 <= rarity <= 5 else None
        ocr = self.wait_ocr(lambda rows: self.button(rows, "筛选设定") is not None or
                            (self.is_picker(rows) and self.button(rows, "筛选", region=(1000, 130, 1630, 250)) is not None))
        if ocr is None:
            self.log("  [筛选失败] 未确认从者选择页或筛选入口，停止操作。")
            return False
        if self.button(ocr, "筛选设定") is None:
            if not self.is_picker(ocr):
                self.log("  [筛选失败] 未确认从者选择页，停止操作。")
                return False
            pos = self.button(ocr, "筛选", region=(1000, 130, 1630, 250))
            if pos is None:
                return False
            self.ctrl.click(*pos, delay_after=0.5)
            ocr = self.wait_ocr(lambda rows: self.button(rows, "筛选设定") is not None)
            if ocr is None:
                return False
        if self.button(ocr, "筛选设定") is None:
            return False
        reset = self.button(ocr, "回到初始设定", region=(100, 880, 800, 1050))
        if reset is None:
            return False
        self.ctrl.click(*reset, delay_after=0.4)
        # 重置隐藏的技能/属性等旧条件，再滚回筛选顶部。
        for _ in range(30):
            if self.cancelled():
                return False
            ocr = ScreenDetector.run_ocr(self.frame())
            if self.button(ocr, "筛选设定") is None:
                return False
            if self.button(ocr, "稀有度") is not None:
                break
            self.ctrl.swipe(1000, 300, 1000, 800, duration_ms=300)
        else:
            return False
        if rarity_label and not self.select_filter_group(rarity_label, [f"★{n}" for n in range(1, 6)]):
            return False
        if class_label:
            for _ in range(5):
                if self.cancelled():
                    return False
                ocr = self.filter_ocr(self.frame(), [class_label])
                if self.button(ocr, class_label, region=(200, 200, 1680, 880)):
                    break
                self.ctrl.swipe(1000, 780, 1000, 400, duration_ms=300)
            else:
                return False
            if not self.select_filter_group(class_label, CLASS_LABELS.values()):
                return False
        if not class_label and not rarity_label:
            self.log("  [筛选] 缺少可靠角色元数据，已清除旧条件，改为全量查找。")
        else:
            self.log(f"  [筛选] 目标条件：{class_label or '不限职阶'} / {rarity_label or '不限稀有度'}")
        ocr = ScreenDetector.run_ocr(self.frame())
        pos = self.button(ocr, "决定", region=(1200, 880, 1800, 1050))
        if pos is None or self.button(ocr, "筛选设定") is None or self.cancelled():
            return False
        self.ctrl.click(*pos, delay_after=0.6)
        return self.wait_ocr(self.is_picker) is not None

    def to_top(self, deadline):
        img = self.frame()
        still = 0
        while time.monotonic() < deadline and not self.cancelled():
            ocr = ScreenDetector.run_ocr(img)
            if not self.is_picker(ocr):
                return False
            pos = self.scroll_position(img)
            if pos is not None and pos <= 0.015:
                return True
            self.ctrl.swipe(1000, 360, 1000, 920, duration_ms=350)
            after = self.frame()
            diff = self.list_frame_diff(img, after, ocr)
            still = still + 1 if diff < 0.004 else 0
            if still >= 3:
                return self.is_picker(ScreenDetector.run_ocr(after))
            img = after
        return False

    def select(self, svt_id, svt_name, target_lv=90):
        self.log(f"  [自动编队] 筛选并查找【{svt_name} (Lv.{target_lv}, ID:{svt_id})】，直至列表底部。")
        if self.cancelled() or not self.apply_filters(svt_id):
            self.log("  [选人失败] 筛选界面或筛选结果未通过核验，停止任务。")
            return False
        deadline = time.monotonic() + 1200  # 异常保护时限，不以页数或角色等级结束正常搜索。
        if not self.to_top(deadline):
            self.log("  [选人失败] 未确认列表顶部，停止任务。")
            return False
        previous = None
        previous_ocr = None
        still = 0
        page = 0
        while time.monotonic() < deadline and not self.cancelled():
            img = self.frame()
            ocr = ScreenDetector.run_ocr(img)
            if not self.is_picker(ocr):
                self.log("  [选人失败] 查找期间离开从者列表，停止任务。")
                return False
            page += 1
            matched, score, pt = self.match_target_at_level(img, ocr, svt_id, target_lv)
            if matched and 180 <= pt[0] <= 1800 and 275 <= pt[1] <= 1040:
                # “卸除”卡片是已经编入的从者，不能当作可选目标点击。
                remove = ScreenDetector.find_text_coordinate(ocr, "卸除", region=(pt[0] - 130, pt[1], pt[0] + 130, min(1080, pt[1] + 220)))
                if remove is None:
                    self.log(f"  [视觉锁定] 第{page}屏找到【{svt_name}】(score={score:.2f})，点击后交由编队槽位复核。")
                    self.ctrl.click(*pt, delay_after=1.0)
                    return True
            pos = self.scroll_position(img)
            if pos is not None and pos >= 0.985:
                self.log(f"  [列表到底] 已扫描{page}屏，未定位到【{svt_name}】。")
                break
            if previous is not None:
                diff = self.list_frame_diff(previous, img, previous_ocr)
                still = still + 1 if diff < 0.004 else 0
                if still >= 3:
                    self.log(f"  [列表停止移动] 连续3次滑动后内容不变，已扫描{page}屏。")
                    break
            previous = img
            previous_ocr = ocr
            self.log(f"  [列表导航] 继续向下查找，第{page}屏（直到列表底部）...")
            self.ctrl.swipe(1000, 850, 1000, 350, duration_ms=350)
        if self.cancelled():
            return False
        # 未找到只能失败，返回编队页也不能视为选人成功。
        ocr = ScreenDetector.run_ocr(self.frame())
        if self.is_picker(ocr):
            close = self.button(ocr, "关闭", region=(0, 0, 350, 160))
            if close:
                self.ctrl.click(*close, delay_after=0.8)
        self.log(f"  [选人失败] 未选入【{svt_name}】，不会继续出击。")
        return False

    def match_target_at_level(self, img, ocr, svt_id, target_lv):
        """同名副本先按当前等级限制卡片区域，再匹配头像。"""
        candidates = []
        for box, text, confidence in ocr:
            token = text.replace(" ", "")
            x = sum(p[0] for p in box) / len(box)
            y = min(p[1] for p in box)
            if token.count("/") != 1 or not (180 <= x <= 1800 and 285 <= y <= 1040):
                continue
            # 倒置等级栏需要按原图方向重读，不能把06/06猜成90/90。
            level = re.search(r"(?<!\d)(\d{1,3})/(\d{1,3})(?!\d)", token)
            if confidence < .55 or not level or int(level[1]) != target_lv:
                x1, y1 = min(p[0] for p in box), min(p[1] for p in box)
                x2, y2 = max(p[0] for p in box), max(p[1] for p in box)
                crop = img.crop((max(0,x1-10),max(0,y1-8),min(1920,x2+10),min(1080,y2+8))).convert("RGB")
                reads = []
                for scale in (1,2):
                    patch = np.array(crop.resize((crop.width*scale,crop.height*scale)))
                    read, _ = ScreenDetector.get_ocr()(patch,use_cls=False)
                    if not read:
                        # 已知等级栏的小行文字放大后偶尔不再被检测框命中，直接识别该行。
                        words, _ = ScreenDetector.get_ocr().text_recognizer(cv2.cvtColor(patch,cv2.COLOR_RGB2BGR))
                        read = [(None,text,score) for text,score in words]
                    fractions = set()
                    for _, value, score in (read or []):
                        match = re.search(r"(?<!\d)(\d{1,3})/(\d{1,3})(?!\d)",value.replace(" ",""))
                        if match and float(score) >= .55:
                            fractions.add((int(match[1]),int(match[2])))
                    reads.append(fractions)
                if len(reads[0]) != 1 or reads[0] != reads[1] or next(iter(reads[0]))[0] != target_lv:
                    continue
                actual, cap = next(iter(reads[0]))
                level = re.search(r"(\d+)/(\d+)",f"{actual}/{cap}")
            if not level or int(level[1]) != target_lv or not target_lv <= int(level[2]) <= 120:
                continue
            center = min((270,550,835,1120,1405,1690),key=lambda value:abs(value-x))
            roi = (max(140,center-135),max(275,int(y)-40),min(1800,center+135),min(1065,int(y)+240))
            result = self.assembler.match_servant_in_region(
                img, svt_id=svt_id, roi=roi, scales=[1.65,1.75,1.85,1.95,2.05],threshold=.72)
            if result[0]:
                candidates.append(result)
        return max(candidates,key=lambda result:result[1]) if candidates else (False,0.0,(0,0))
