"""按模拟要求装备概念礼装；实际槽位复核通过后才允许出击。"""

from dataclasses import dataclass
import re
import time
import unicodedata
import urllib.request

import cv2
import numpy as np

from src.core.path_helper import get_resource_path, get_root_dir
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.screen_detector import ScreenDetector


class EquipmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class EquipmentTarget:
    slot: int
    ce_id: int
    name: str
    level: int
    mlb: bool
    max_level: int
    face_url: str
    servant_name: str = ""
    rarity: int = 0


class CraftEssencePicker:
    MAX_ATTEMPTS = 3  # 共三次完整流程，包含首次。
    COLUMNS = (270, 550, 835, 1120, 1405, 1690)
    SLOT_X = {1: 195, 2: 500, 4: 1115, 5: 1425, 6: 1735}

    def __init__(self, controller, log=None):
        self.ctrl = controller
        self.log = log or (lambda text: None)
        self.templates = {}
        self.last_frame = None

    def cancelled(self):
        event = self.ctrl.cancel_event
        return event is not None and event.is_set()

    def frame(self):
        if self.cancelled():
            raise EquipmentError("任务已取消")
        img = self.ctrl.wait_for_still(timeout=2.0, min_still_duration=.2, diff_threshold=.012)
        if img.size != (1920, 1080):
            img = img.resize((1920, 1080))
        self.last_frame = img
        return img, ScreenDetector.run_ocr(img)

    @staticmethod
    def normalize(text):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).replace("’", "'").replace("′", "'")

    @classmethod
    def button(cls, ocr, label, region):
        exact = [row for row in ocr if cls.normalize(row[1]) == label]
        return ScreenDetector.find_text_coordinate(exact, label, region=region)

    @classmethod
    def is_picker(cls, ocr):
        return any(cls.normalize(text) in ("概念礼装选择", "选择概念礼装") for _, text, _ in ocr)

    @classmethod
    def is_party(cls, ocr):
        text = " ".join(cls.normalize(row[1]) for row in ocr)
        masked = any(label in text for label in ("筛选设定", "装备概念礼装的变更", "概念礼装选择",
                                                  "详细信息", "概念礼装详情", "取消队伍编制"))
        return not masked and cls.button(ocr, "开始任务", (1400, 880, 1920, 1080)) is not None

    def targets(self, rec, parser):
        from src.strategy.laplace import PublicData
        if parser is None or rec is None or not rec.laplace.get("passed"):
            raise EquipmentError("缺少已通过模拟的账号和礼装方案")
        equipment = rec.party_ce_details if rec.battle_mode == "rotation" else {1: rec.slot1_ce_detail}
        if not equipment:
            raise EquipmentError("模拟方案没有明确的礼装数据")
        targets, used = [], set()
        for slot, detail in sorted(equipment.items(), key=lambda row: int(row[0])):
            slot = int(slot)
            ce_id = detail.get("ce_id")
            owned = parser.craft_essences.get(ce_id)
            if slot not in self.SLOT_X or not owned or not detail.get("is_owned"):
                raise EquipmentError(f"第{slot}号位礼装不属于可装备的自有背包礼装")
            if (owned.lv, owned.is_mlb) != (detail.get("lv"), detail.get("is_mlb")):
                raise EquipmentError("账号礼装等级或满破状态已变化，需重新模拟")
            if ce_id in used:
                raise EquipmentError("不能将未经持有份数验证的同一礼装重复分配")
            used.add(ce_id)
            data = PublicData(cancel_event=self.ctrl.cancel_event).get("CN", "equip", ce_id)
            faces = data.get("extraAssets", {}).get("faces", {}).get("equip", {})
            url = faces.get(str(ce_id), "")
            max_level = data.get("lvMax", 0)
            if not isinstance(max_level, int) or not 1 <= owned.lv <= max_level:
                raise EquipmentError("缺少礼装最大等级的可靠数据")
            if not re.fullmatch(r"https://static\.atlasacademy\.io/CN/Faces/f_\d+\.png", url):
                raise EquipmentError("缺少礼装头像资料")
            ids = rec.party_servant_ids or rec.laplace.get("party_servant_ids", {})
            servant_id = ids.get(slot, ids.get(str(slot)))
            if slot == 1 and not servant_id:
                servant_id = rec.slot1_svt_id
            servant = parser.servants.get(servant_id)
            if servant is None:
                raise EquipmentError(f"缺少第{slot}号位从者信息，无法核对礼装转移对象")
            targets.append(EquipmentTarget(slot, ce_id, data["name"], owned.lv, owned.is_mlb, max_level,
                                           url, servant.name, data.get("rarity", 0)))
        return targets

    @classmethod
    def cards(cls, ocr):
        """只读取列表卡片的等级分数；排除顶部从者等级和COST。"""
        cards = []
        seen = set()
        for box, text, score in ocr:
            if score < .55:
                continue
            token = cls.normalize(text)
            match = re.fullmatch(r"(?:等级|Lv\.?|LV\.?)?(\d{1,3})/(\d{1,3})(?:等级)?", token)
            if not match:
                continue
            x = sum(point[0] for point in box) / len(box)
            y = min(point[1] for point in box)
            if not (170 < x < 1820 and 550 <= y < 1040):
                continue
            column = min(cls.COLUMNS, key=lambda center: abs(center - x))
            top = int(y) - 30
            key = (column, top // 40)
            if key in seen or top + 260 > 1080:
                continue
            seen.add(key)
            level, cap = map(int, match.groups())
            if not 1 <= level <= cap <= 100:
                continue
            cards.append({"level": level, "cap": cap, "roi": (column - 124, top, column + 124, top + 260),
                          "point": (column, top + 130)})
        return cards

    @staticmethod
    def card_matches_stats(card, target):
        return (card["level"] == target.level and card["cap"] <= target.max_level
                and (card["cap"] == target.max_level) == target.mlb)

    def template(self, target):
        if target.ce_id not in self.templates:
            path = get_resource_path("data/templates/equip") / target.face_url.rsplit("/", 1)[-1]
            if not path.exists():
                request = urllib.request.Request(target.face_url, headers={"User-Agent": "FGO-MAA"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    content = response.read()
                raw = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
                if raw is None:
                    raise EquipmentError("礼装头像下载无效")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            raw = cv2.imread(str(path))
            if raw is None:
                raise EquipmentError("礼装头像无法读取")
            # 卡面中上部避开顶部等级和下方消耗/解放标记。
            h, w = raw.shape[:2]
            self.templates[target.ce_id] = [
                raw[int(h * .26):int(h * .62), int(w * .16):int(w * .84)],
                raw[int(h * .22):int(h * .36), int(w * .16):int(w * .84)],
            ]
        return self.templates[target.ce_id]

    def face_score(self, img, card, target):
        region = cv2.cvtColor(np.array(img.crop(card["roi"]).convert("RGB")), cv2.COLOR_RGB2BGR)
        best = -1.0
        for template in self.template(target):
            for scale in (1.65, 1.75, 1.85, 1.95, 2.05):
                candidate = cv2.resize(template, None, fx=scale, fy=scale)
                if candidate.shape[0] >= region.shape[0] or candidate.shape[1] >= region.shape[1]:
                    continue
                best = max(best, float(cv2.minMaxLoc(cv2.matchTemplate(region, candidate, cv2.TM_CCOEFF_NORMED))[1]))
        return best

    @classmethod
    def name_matches(cls, ocr, target):
        return any(cls.normalize(text) == cls.normalize(target.name)
                   and 375 <= min(point[0] for point in box) < 1360
                   and 265 <= min(point[1] for point in box) < 355
                   for box, text, score in ocr if score >= .55)

    @classmethod
    def removed_card(cls, ocr, card):
        x1, y1, x2, y2 = card["roi"]
        return cls.button(ocr, "卸除", (x1, y1, x2, y2)) is not None

    @classmethod
    def transfer_prompt(cls, ocr):
        return any("装备概念礼装的变更" in cls.normalize(text) for _, text, _ in ocr)

    @staticmethod
    def transfer_buttons(img):
        """大号单字常被OCR漏检，同时匹配已取证的“否/是”两枚字形。"""
        buttons = {}
        for label, name, roi in ((False, "no", (570, 795, 750, 895)),
                                 (True, "yes", (1170, 795, 1350, 895))):
            template = cv2.imread(str(get_resource_path(f"data/templates/equipment_ui/transfer_{name}.png")))
            if template is None:
                raise EquipmentError("缺少礼装转移确认按钮模板")
            region = cv2.cvtColor(np.array(img.crop(roi).convert("RGB")), cv2.COLOR_RGB2BGR)
            _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
            if score < .90:
                raise EquipmentError("无法同时确认礼装转移的否/是按钮")
            buttons[label] = (roi[0] + point[0] + template.shape[1] // 2,
                              roi[1] + point[1] + template.shape[0] // 2)
        return buttons

    def accept_transfer(self, img, ocr, target):
        if not self.transfer_prompt(ocr):
            return
        canonical = lambda text: re.sub(r"[\W_]", "", self.normalize(text)).casefold()
        destination = "".join(text for box, text, _ in ocr
                              if 480 <= min(point[1] for point in box) < 680)
        if not target.servant_name or canonical(target.servant_name) not in canonical(destination):
            raise EquipmentError("礼装转移目标从者与指定槽位不符或无法确认")
        yes = self.transfer_buttons(img)[True]
        self.ctrl.click(*yes, delay_after=.6)

    def back_to_party(self):
        """只关闭已识别的子页；从不点击开始任务或用盲坐标保存。"""
        for _ in range(7):
            img, ocr = self.frame()
            if self.is_party(ocr):
                return
            joined = " ".join(text for _, text, _ in ocr)
            if self.transfer_prompt(ocr):
                pos = self.transfer_buttons(img)[False]
            elif any(text in joined for text in ("取消队伍编制", "恢复成变更前的状态")):
                pos = self.button(ocr, "决定", (960, 750, 1550, 1080))
            elif "筛选设定" in joined:
                pos = self.button(ocr, "取消", (800, 880, 1450, 1080))
            elif self.is_picker(ocr) or any(text in joined for text in ("概念礼装", "详细信息", "能力")):
                pos = self.button(ocr, "关闭", (0, 0, 330, 155))
            elif any(text in joined for text in ("队伍编制", "队伍编成", "拖动修改从者配置")):
                pos = self.button(ocr, "决定", (1500, 900, 1920, 1080))
            else:
                pos = None
            if pos is None:
                raise EquipmentError("无法确认返回出击确认页的安全按钮")
            self.ctrl.click(*pos, delay_after=.7)
        raise EquipmentError("未能返回出击确认页")

    def open_slot(self, slot):
        self.back_to_party()
        self.ctrl.click(self.SLOT_X[slot], 700, delay_after=.7)
        _, ocr = self.frame()
        if not self.is_picker(ocr):
            raise EquipmentError(f"第{slot}号位未进入概念礼装选择页")

    def clear_filters(self, target=None):
        _, ocr = self.frame()
        pos = self.button(ocr, "筛选", (1300, 130, 1590, 250))
        if not self.is_picker(ocr) or pos is None:
            raise EquipmentError("未确认礼装筛选按钮")
        self.ctrl.click(*pos, delay_after=.5)
        _, ocr = self.frame()
        reset = self.button(ocr, "回到初始设定", (0, 880, 800, 1080))
        if not reset or "筛选设定" not in " ".join(row[1] for row in ocr):
            raise EquipmentError("未确认礼装筛选设定")
        self.ctrl.click(*reset, delay_after=.4)
        if target is not None and 1 <= target.rarity <= 5:
            from src.strategy.servant_picker import ServantPicker
            filters = ServantPicker(self.ctrl, self)
            if not filters.select_filter_group(f"★{target.rarity}", [f"★{n}" for n in range(1, 6)]):
                raise EquipmentError("礼装稀有度筛选未通过核验")
            self.log(f"  [礼装筛选] 仅显示★{target.rarity}，再核对目标ID、等级与满破。")
        _, ocr = self.frame()
        decide = self.button(ocr, "决定", (1250, 880, 1900, 1080))
        if not decide:
            raise EquipmentError("未确认筛选决定按钮")
        self.ctrl.click(*decide, delay_after=.5)

    @classmethod
    def static_diff(cls, before, after, ocr):
        rois = [(x1 + 35, y1 + 65, x2 - 30, y1 + 125)
                for card in cls.cards(ocr) for x1, y1, x2, y2 in [card["roi"]]]
        diffs = [AnchorVerifier.compute_frame_diff(before, after, roi=roi) for roi in rois]
        return float(np.median(diffs)) if diffs else 1.0

    def to_top(self, deadline):
        still = 0
        while time.monotonic() < deadline:
            before, ocr = self.frame()
            if not self.is_picker(ocr):
                raise EquipmentError("查找期间离开礼装列表")
            position = self.scroll_position(before)
            self.ctrl.swipe(1100, 620, 1100, 970, duration_ms=350)
            after, _ = self.frame()
            near_top = position is None or position <= .015
            # 大背包的滚动条小于1.5%仍可能遮住首行，必须看见完整首行并确认不再移动。
            first_row = any(540 <= card["roi"][1] <= 600 for card in self.cards(ocr))
            still = still + 1 if near_top and first_row and self.static_diff(before, after, ocr) < .004 else 0
            if still >= 3:
                return
        raise EquipmentError("礼装列表回到顶部超时")

    @staticmethod
    def scroll_position(img):
        pixels = np.array(img.convert("RGB"))[548:1065, 1843:1878]
        light = (pixels.min(axis=2) > 180) & (pixels.max(axis=2).astype(int) - pixels.min(axis=2) < 70)
        rows = np.flatnonzero(light.mean(axis=1) > .35)
        if not len(rows):
            return None
        runs = np.split(rows, np.where(np.diff(rows) > 1)[0] + 1)
        thumb = max(runs, key=len)
        if len(thumb) < 10 or len(thumb) >= len(pixels) - 10:
            return None
        return float(np.clip((thumb[0] - 6) / max(1, len(pixels) - len(thumb) - 12), 0, 1))

    def find(self, target, verify_only=False):
        # 大背包正常翻页不能被三分钟限制误当成“没有目标礼装”。
        deadline = time.monotonic() + 600
        self.clear_filters(target)
        self.to_top(deadline)
        still = 0
        while time.monotonic() < deadline:
            img, ocr = self.frame()
            if not self.is_picker(ocr):
                raise EquipmentError("查找期间离开礼装列表")
            for card in self.cards(ocr):
                if not self.card_matches_stats(card, target) or self.face_score(img, card, target) < .80:
                    continue
                if verify_only:
                    if self.name_matches(ocr, target) and self.removed_card(ocr, card):
                        return
                    continue
                if self.name_matches(ocr, target) and self.removed_card(ocr, card):
                    self.back_to_party()
                    return
                self.ctrl.click(*card["point"], delay_after=.5)
                selected, selected_ocr = self.frame()
                if self.transfer_prompt(selected_ocr):
                    self.accept_transfer(selected, selected_ocr, target)
                    selected, selected_ocr = self.frame()
                # 选择后再检查卸除标记对应的实际等级和满破，不能复用点击前的判断。
                current = [row for row in self.cards(selected_ocr)
                           if self.removed_card(selected_ocr, row) and self.card_matches_stats(row, target)
                           and self.face_score(selected, row, target) >= .80]
                if not self.name_matches(selected_ocr, target) or len(current) != 1:
                    raise EquipmentError(f"第{target.slot}号位所选礼装名称、等级或满破未通过复核")
                decide = self.button(selected_ocr, "决定", (1500, 370, 1880, 535))
                if decide is None:
                    raise EquipmentError("未确认礼装装备决定按钮")
                self.ctrl.click(*decide, delay_after=.7)
                self.back_to_party()
                return
            position = self.scroll_position(img)
            if position is not None and position >= .985:
                break
            self.ctrl.swipe(1100, 970, 1100, 770, duration_ms=350)
            after, _ = self.frame()
            still = still + 1 if self.static_diff(img, after, ocr) < .004 else 0
            if still >= 3:
                break
        action = "复核已装备" if verify_only else "找到可装备"
        raise EquipmentError(f"未能{action}第{target.slot}号位【{target.name} Lv{target.level} {'满破' if target.mlb else '未满破'}】")

    def equip_and_verify(self, targets):
        for target in targets:
            self.log(f"  [自动礼装] 第{target.slot}号位选择【{target.name} Lv{target.level} {'满破' if target.mlb else '未满破'}】")
            self.open_slot(target.slot)
            self.find(target)
        # 所有槽位装备后独立逐槽复核，发现被后来操作挪走的礼装也必须失败。
        for target in targets:
            self.open_slot(target.slot)
            self.find(target, verify_only=True)
            self.back_to_party()
        _, ocr = self.frame()
        if not self.is_party(ocr):
            raise EquipmentError("礼装核验后未回到出击确认页")
        for _, text, _ in ocr:
            cost = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", text)
            if cost and int(cost[2]) >= 80 and int(cost[1]) > int(cost[2]):
                raise EquipmentError("装备礼装后队伍COST超限")

    def ensure(self, rec, parser):
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            if self.cancelled():
                return False
            self.log(f"[礼装流程 {attempt}/{self.MAX_ATTEMPTS}] 选择、等级/满破核对、装备及出击前复核。")
            try:
                self.equip_and_verify(self.targets(rec, parser))
                self.log("[礼装通过] 全部必需礼装已装备到指定槽位并独立复核。")
                return True
            except Exception as error:
                if self.cancelled():
                    return False
                self.log(f"[礼装失败 {attempt}/{self.MAX_ATTEMPTS}] {error}")
                if self.last_frame is not None:
                    folder = get_root_dir() / ".app_profile/equipment_errors"
                    try:
                        folder.mkdir(parents=True, exist_ok=True)
                        self.last_frame.save(folder / f"{time.time_ns()}_attempt{attempt}.png")
                    except OSError:
                        pass
        self.log("[任务报错] 礼装完整流程累计3次仍失败，禁止出击并中断任务。")
        return False
