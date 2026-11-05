"""
FGO Screen State Detector - 真正的 OCR 深度学习视觉驱动感知器
基于 RapidOCR (ch_PP-OCRv4) 神经网络，真实提取游戏界面文字与物理坐标。
通过空间拓扑与语义上下文严谨判别，彻底消除假弹窗与界面误判。
"""

from __future__ import annotations
import re
import numpy as np
from PIL import Image, ImageOps
from enum import Enum
from typing import Tuple, Dict, Any, Optional, List
from rapidocr_onnxruntime import RapidOCR
from src.strategy.anchor_verifier import AnchorVerifier


class GameScreen(str, Enum):
    UNKNOWN = "unknown"                     # 未知/转场中
    LOADING = "loading"                     # 黑色加载/转圈
    MAIN_MENU = "main_menu"                 # 主界面 (Terminal 终端大厅)
    ANNOUNCEMENT = "announcement"           # 真正的公告/签到遮罩弹窗 (非大厅左上角通知按钮)
    POPUP_DIALOG = "popup_dialog"           # 道具详情/圣晶片信息/提示确认弹窗
    INVENTORY_FULL = "inventory_full"       # 从者/礼装背包超上限提示
    GIFT_BOX = "gift_box"                   # 礼物盒列表界面
    SUMMON_SCREEN = "summon_screen"         # 召唤卡池界面
    SUMMON_RESULT = "summon_result"         # 抽卡结果展示中
    CHALDEA_GATE = "chaldea_gate"           # 迦勒底之门内部关卡列表 (QP/种火/修炼场)
    SUPPORT_SELECT = "support_select"       # 助战选择界面 (顶级优先级)
    PARTY_CONFIRM = "party_confirm"         # 出击确认编队界面 (有开始任务按钮)
    BATTLE_COMBAT = "battle_combat"         # 战斗局内回合中 (右下角 ATTACK)
    BATTLE_CARD_SELECT = "battle_card"      # 指令卡/宝具卡选择中 (右下角 Back)
    BATTLE_RESULT = "battle_result"         # 战斗胜利/战利品结算面板
    CONTINUE_QUEST = "continue_quest"       # 连续出击/恢复AP提示弹窗
    MASTER_MISSIONS = "master_missions"     # 御主任务面板
    DAVINCI_WORKSHOP = "davinci_workshop"   # 达·芬奇工坊 / 商店界面 (左上角关闭)
    SERVANT_SELECT = "servant_select"       # 从者选择界面 (请点击要替换的从者)
    CRAFT_ESSENCE_SELECT = "craft_essence_select" # 概念礼装选择/详情界面 (左上角关闭)
    ORDEAL_SELECT = "ordeal_select"           # 奏章地球区域与章节入口
    MAIN_RECORD = "main_record"             # 主线记录 / 特异点章节选择列表
    REGIONAL_MAP = "regional_map"           # 章节区域大地图 (如奥尔良、冬木大地图)


class ScreenDetector:
    """真实 OCR 驱动的高精度游戏界面感知器"""

    # 全局单例 RapidOCR 引擎
    _ocr_engine: Optional[RapidOCR] = None

    @classmethod
    def get_ocr(cls) -> RapidOCR:
        if cls._ocr_engine is None:
            cls._ocr_engine = RapidOCR()
        return cls._ocr_engine

    @classmethod
    def run_ocr(cls, pil_img: Image.Image) -> List[Tuple[List[List[int]], str, float]]:
        """
        对整张屏幕运行 RapidOCR 深度学习文本识别
        返回: [(bbox, text, score), ...]
        """
        ocr = cls.get_ocr()
        arr = np.array(pil_img.convert("RGB"))
        results, _ = ocr(arr)
        if not results:
            return []

        cleaned = []
        for box, text, score in results:
            try:
                s = float(score)
            except Exception:
                s = 0.9
            cleaned.append((box, text.strip(), s))
        return cleaned

    @classmethod
    def read_battle_turn(cls, image: Image.Image) -> Optional[int]:
        crop = image.crop((1290, 120, 1460, 164)).convert('RGB')
        contrast = ImageOps.grayscale(crop).point(lambda value: 255 if value < 165 else 0).convert('RGB')
        for sample, fill in ((crop, 0), (contrast, 255)):
            for _, text, score in cls.run_ocr(ImageOps.expand(sample, (10, 8), fill=(fill,) * 3)):
                match = AnchorVerifier.TURN_PATTERN.fullmatch(text)
                if match and score >= .5 and int(match[1]) > 0:
                    return int(match[1])
        return None

    @classmethod
    def identify_regional_map(cls, ocr_results: List[Tuple], image=None) -> Tuple[str, str]:
        """
        高精度解析当前区域大地图的特异点/章节名称
        返回: (简要地名, 标准化全称)
        例如: ("奥尔良", "第一特异点 邪龙百年战争 奥尔良")
        """
        from src.strategy.ordeal_navigation import map_identity
        from src.strategy.chapter_navigation import map_identity as story_map_identity
        story = story_map_identity(ocr_results)
        if story is not None:
            return story
        ordeal = map_identity(ocr_results)
        if ordeal is not None:
            return ordeal
        from src.strategy.event_navigation import map_identity as event_map_identity
        event = event_map_identity(ocr_results)
        if event is not None:
            return event
        MAP_DEFINITIONS = [
            ("冬木", "特异点F 燃烧污染都市 冬木", ["冬木", "燃烧污染都市", "特异点F", "A.D.2004", "AD2004"]),
            ("奥尔良", "第一特异点 邪龙百年战争 奥尔良", ["奥尔良", "邪龙百年战争", "第一特异点", "A.D.1431", "AD1431"]),
            ("罗马", "第二特异点 永恒狂气帝国 罗马", ["罗马", "七丘之城", "永恒狂气帝国", "永续疯狂帝国", "第二特异点", "A.D.0060", "AD0060"]),
            ("俄刻阿诺斯", "第三特异点 封锁终局四海 俄刻阿诺斯", ["俄刻阿诺斯", "封锁终局四海", "第三特异点", "A.D.1573", "AD1573"]),
            ("伦敦", "第四特异点 死界魔雾都市 伦敦", ["伦敦", "死界魔雾都市", "第四特异点", "A.D.1888", "AD1888"]),
            ("合众为一", "第五特异点 北美神话大战 合众为一", ["合众为一", "北美神话大战", "第五特异点", "A.D.1783", "AD1783"]),
            ("卡美洛", "第六特异点 神圣圆桌领域 卡美洛", ["卡美洛", "神圣圆桌领域", "第六特异点", "A.D.1273", "AD1273"]),
            ("巴比伦尼亚", "第七特异点 绝对魔兽战线 巴比伦尼亚", ["巴比伦尼亚", "绝对魔兽战线", "第七特异点", "B.C.2600", "BC2600", "B.C.2655", "BC2655"]),
            ("所罗门", "终局特异点 冠位时间神殿 所罗门", ["所罗门", "冠位时间神殿", "终局特异点", "A.D.2017", "AD2017"]),
            ("Ordeal Call", "Ordeal Call", ["白纸化地球"]),
            ("迦勒底之门", "迦勒底之门", ["迦勒底之门", "每日任务", "修练场", "修炼场", "宝物库", "种火集集"]),
        ]

        top_texts = []
        all_texts = []
        for box, text, _ in ocr_results:
            ys = [p[1] for p in box]
            cy = sum(ys) / 4
            clean_t = re.sub(r"\s+", "", text)
            all_texts.append(clean_t)
            if cy <= 200:
                top_texts.append(clean_t)

        top_joined = " ".join(top_texts)
        for short_name, canonical_name, kws in MAP_DEFINITIONS:
            for kw in kws:
                if kw in top_joined:
                    return short_name, canonical_name

        all_joined = " ".join(all_texts)
        for short_name, canonical_name, kws in MAP_DEFINITIONS:
            for kw in kws:
                if kw in all_joined:
                    return short_name, canonical_name

        if image is not None:
            event = event_map_identity(ocr_results,image)
            if event is not None:
                return event
        return "未知地图", "未知章节大地图"

    @classmethod
    def read_servant_np_percentages(cls, image: Image.Image) -> Dict[int, int]:
        """独立放大每个NP数字；不以全屏OCR或理论NP填补缺失读数。"""
        if image.size != (1920, 1080):
            image = image.resize((1920, 1080))
        image = image.convert("RGB")
        crops = []
        for left in (335, 815, 1290):
            crop = image.crop((left, 982, left + 120, 1017))
            crops.extend(np.array(crop.resize((120 * scale, 35 * scale))) for scale in (2, 3))
        # 固定数字行直接送识别器，避免检测器把NP槽边框当作文字区域。
        recognized, _ = cls.get_ocr().text_recognizer(crops)
        values = {}
        for slot in range(1, 4):
            readings = []
            for text, score in recognized[(slot - 1) * 2:slot * 2]:
                token = re.sub(r"\s+", "", text)
                # NP数值区只显示整数；字形阴影会被识别为100中的小数点(10.0%)。
                token = re.sub(r"(?<=\d)[.,](?=\d)", "", token)
                match = re.fullmatch(r"(\d{1,3})%[-~>]*", token)
                readings.append(int(match[1]) if match and score >= 0.6 and 0 <= int(match[1]) <= 300 else None)
            if len(readings) == 2 and readings[0] is not None and readings[0] == readings[1]:
                values[slot] = readings[0]
        return values

    @classmethod
    def find_text_coordinate(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]],
        target_keyword: str,
        region: Optional[Tuple[int, int, int, int]] = None
    ) -> Optional[Tuple[int, int]]:
        """
        在 OCR 识别结果中寻找包含关键词的目标，支持指定区域筛选 [min_x, min_y, max_x, max_y]
        自动忽略按钮文字中间的空格（例如 '关 闭' 匹配 '关闭'），返回精确的物理点击中心坐标 (center_x, center_y)
        """
        clean_kw = re.sub(r"\s+", "", target_keyword)
        for box, text, score in ocr_results:
            clean_text = re.sub(r"\s+", "", text)
            if target_keyword in text or (clean_kw and clean_kw in clean_text):
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                cx = int(sum(xs) / 4)
                cy = int(sum(ys) / 4)
                if region:
                    rx1, ry1, rx2, ry2 = region
                    if not (rx1 <= cx <= rx2 and ry1 <= cy <= ry2):
                        continue
                return cx, cy
        return None

    @classmethod
    def find_target_quest_coordinate(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]],
        target_quest: str = "daily_qp_extreme"
    ) -> Optional[Tuple[int, int]]:
        """
        根据用户指定的目标关卡（如 QP 宝库 / 经验种火 / 修炼场），结合难度要求精准匹配点击坐标
        严格校验难度等级（如极级绝不误点初级/中级/上级）与安全点击区域（避开右下角菜单遮挡区）。
        """
        tq_lower = target_quest.lower()
        is_qp = any(k in tq_lower for k in ["qp", "宝物", "宝库", "门"])
        is_exp = any(k in tq_lower for k in ["exp", "种火", "狗粮"])

        training_map = {
            "saber": ["剑之修炼场"],
            "archer": ["弓之修炼场"],
            "lancer": ["枪之修炼场"],
            "rider": ["骑之修炼场"],
            "caster": ["术之修炼场"],
            "assassin": ["杀之修炼场"],
            "berserker": ["狂之修炼场"],
        }
        matched_training = None
        for tk, kws in training_map.items():
            if tk in tq_lower or any(k in target_quest for k in kws):
                matched_training = kws
                break

        if is_qp:
            target_kws = ["宝物", "宝库", "打开宝物库"]
        elif is_exp:
            target_kws = ["种火", "搜集种火"]
        elif matched_training:
            target_kws = matched_training
        else:
            target_kws = [target_quest]

        # 严格确定目标难度等级
        if "extreme" in tq_lower or "极级" in tq_lower or "daily_training_" in tq_lower:
            req_diffs = ["极级"]
            forbidden_diffs = ["初级", "中级", "上级", "超级"]
        elif "super" in tq_lower or "超级" in tq_lower:
            req_diffs = ["超级", "极级"]
            forbidden_diffs = ["初级", "中级", "上级"]
        else:
            req_diffs = ["极级", "超级", "上级", "中级", "初级"]
            forbidden_diffs = []

        # 策略 1: 同一文本框内同时包含关卡名称和目标难度（例如：“打开宝物库之门极级”）
        for box, text, score in ocr_results:
            if any(kw in text for kw in target_kws) and any(d in text for d in req_diffs):
                if any(fd in text for fd in forbidden_diffs):
                    continue
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
                # 必须处于列表安全点击视野内 (160 <= cy <= 860)，避开底部边缘与右下角菜单按钮
                if 900 <= cx <= 1850 and 160 <= cy <= 860:
                    return cx, cy

        # 策略 2: 关卡名称和目标难度分属不同 OCR 框，但在同一水平卡片行（Y 坐标相差 <= 65）
        target_boxes = []
        diff_boxes = []
        for box, text, score in ocr_results:
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
            if not (900 <= cx <= 1850 and 160 <= cy <= 860):
                continue
            if any(kw in text for kw in target_kws) and not any(fd in text for fd in forbidden_diffs):
                target_boxes.append((cx, cy, text))
            if any(d in text for d in req_diffs) and not any(fd in text for fd in forbidden_diffs):
                diff_boxes.append((cx, cy, text))

        for t_cx, t_cy, _ in target_boxes:
            for d_cx, d_cy, _ in diff_boxes:
                if abs(t_cy - d_cy) <= 65:
                    return t_cx, t_cy

        return None

    @classmethod
    def find_attack_button_coordinate(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> Optional[Tuple[int, int]]:
        """寻找右下角真实的 ATTACK 攻击按钮 (精确排斥'攻击威力提升'等活动文案)"""
        for box, text, _ in ocr_results:
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            cx = int(sum(xs) / 4); cy = int(sum(ys) / 4)
            if 1400 <= cx <= 1920 and 750 <= cy <= 1080:
                t = text.strip().upper()
                if t in ["ATTACK", "ATTACK!", "攻击", "ATTACK !"]:
                    return cx, cy
        return None

    @classmethod
    def find_highest_difficulty_daily_quest(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> Optional[Tuple[int, int, str]]:
        """
        在每日任务关卡列表中，寻找视野内最高难度（极级 > 超级 > 上级）的关卡点击坐标与名称。
        当指定的 QP/经验 关卡今日未开放时作为智能兜底，保证完成体力消耗与通关指标。
        """
        pref_diff = ["极级", "超级", "上级", "中级", "初级"]
        for diff in pref_diff:
            for box, text, _ in ocr_results:
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                cx = int(sum(xs) / 4); cy = int(sum(ys) / 4)
                if 800 <= cx <= 1850 and 150 <= cy <= 920:
                    if diff in text and any(k in text for k in ["修炼场", "每日替换", "宝物库", "搜集种火", "收集", "宝库", "门"]):
                        return cx, cy, text
        for diff in pref_diff:
            for box, text, _ in ocr_results:
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                cx = int(sum(xs) / 4); cy = int(sum(ys) / 4)
                if 1200 <= cx <= 1850 and 150 <= cy <= 920 and diff in text:
                    return cx, cy, f"日常副本({diff})"
        return None

    @classmethod
    def detect(cls, pil_img: Image.Image) -> Tuple[GameScreen, float, Dict[str, Any]]:
        """
        基于 OCR 全文拓扑分析的界面状态机
        严格遵循 FGO 界面层级与空间互斥规则，杜绝误报。
        """
        # 1. 黑屏转场或明确加载文本检测
        arr = np.array(pil_img.convert("RGB"))
        if arr.mean() < 12:
            return GameScreen.LOADING, 0.99, {"reason": "黑屏转场加载中"}

        # 2. 全屏 RapidOCR 深度分析
        ocr_results = cls.run_ocr(pil_img)
        all_texts = [item[1] for item in ocr_results]
        joined = " ".join(all_texts)

        details: Dict[str, Any] = {
            "all_texts": all_texts,
            "ocr_results": ocr_results
        }

        # ------------------ 规则 0: 过图加载与 Tips 界面 (Loading) ------------------
        if any(kw in joined for kw in ["小贴士", "加载中", "Now Loading", "NOW LOADING", "正在连接"]):
            details["reason"] = "检测到过图加载中 / 小贴士画面"
            return GameScreen.LOADING, 0.99, details

        # 3. 寻找全屏是否有“关闭”按钮（弹窗的核心指征）、弹窗底部居中“关闭”按钮与左上角导航退出按钮
        close_pos = cls.find_text_coordinate(ocr_results, "关闭")
        top_close_pos = cls.find_text_coordinate(ocr_results, "关闭", region=(0, 0, 300, 150)) or \
                        cls.find_text_coordinate(ocr_results, "返回", region=(0, 0, 300, 150))
        center_close_pos = cls.find_text_coordinate(ocr_results, "关闭", region=(600, 620, 1320, 980))
        has_top_close = top_close_pos is not None

        # ------------------ 规则 1: 从者/礼装超上限弹窗 (Inventory Full) ------------------
        if any(kw in joined for kw in ["从者数已超上限", "概念礼装数已超上限", "超上限", "当前持有数 1000/1000"]):
            details["close_coord"] = center_close_pos or close_pos or (960, 845)
            details["reason"] = "检测到【从者/礼装数超上限】出击阻断弹窗"
            return GameScreen.INVENTORY_FULL, 0.99, details

        # ------------------ 规则 1.5: 迦勒底之门关卡列表特征预判 ------------------
        has_gate_quest = any(kw in joined for kw in [
            "每日任务", "搜集种火", "打开宝物库", "修炼场",
            "每日替换", "强化任务", "幕间物语", "迦勒底之门"
        ])

        # ------------------ 规则 1.6: 出击编队确认界面 (Party Confirm, 顶级优先级) ------------------
        is_party_confirm = (
            not has_gate_quest and
            "菜单" not in joined and
            (
                any(kw in joined for kw in ["开始任务", "开始关卡", "队伍确认", "出击确认", "替换的从者", "队伍编队"]) or
                (cls.find_text_coordinate(ocr_results, "开始任务", region=(1400, 800, 1920, 1080)) is not None) or
                ("队伍" in joined and any(kw in joined for kw in ["总消耗", "先发", "支援", "配置", "自动变还", "职阶情报", "御主装备"]))
            )
        )
        if is_party_confirm:
            start_coord = cls.find_text_coordinate(ocr_results, "开始任务", region=(1400, 800, 1920, 1080)) or \
                          cls.find_text_coordinate(ocr_results, "开始", region=(1400, 800, 1920, 1080)) or \
                          (1750, 1010)
            details["start_coord"] = start_coord
            details["reason"] = f"检测到编队确认特征与出击按钮 (点击坐标: {start_coord})"
            return GameScreen.PARTY_CONFIRM, 0.99, details

        # ------------------ 规则 1.65: 连续出击 / 补充 AP 弹窗 (CONTINUE_QUEST, 必须高于助战选择) ------------------
        # 避免连续出击文本中包含“将在助战选择之后...”导致误判为助战选择！
        if any(kw in joined for kw in ["连续出击", "再次挑战", "是否连续进行同一关卡", "补充AP"]):
            details["reason"] = "检测到连续出击确认面板"
            return GameScreen.CONTINUE_QUEST, 0.99, details

        # ------------------ 规则 1.7: 助战选择界面 (Support Select, 顶级优先级) ------------------
        is_support_select = (
            any(kw in joined for kw in ["助战选择", "支援选择", "列表更新", "更新列表"]) and
            not is_party_confirm and
            not any(kw in joined for kw in ["队伍确认", "开始任务", "队伍编成", "队伍编队", "总消耗", "替换的从者", "连续出击", "是否连续进行同一关卡"])
        )
        if is_support_select:
            details["reason"] = "检测到【助战选择】大标题或【列表更新】按键"
            return GameScreen.SUPPORT_SELECT, 0.99, details

        # ------------------ 规则 2: 御主任务面板 (MASTER_MISSIONS) ------------------
        # 必须高于道具详情弹窗（因为任务界面右上角有“圣晶片交换”，容易误判为弹窗）
        if ("御主任务" in joined or ("日常" in joined and "周常" in joined and "限定" in joined)) and (close_pos is not None or "已完成任务" in joined or "统一领取" in joined):
            details["reason"] = "检测到御主任务面板文本"
            return GameScreen.MASTER_MISSIONS, 0.99, details

        # 抽卡结果页的编队/强化/变还按钮不是商店或详情页入口特征。
        has_summon_inventory = any(kw in joined for kw in ["所持概念礼装", "持有从者", "所持从者", "所持指令纹章"])
        if has_top_close and ("已召唤" in joined or (has_summon_inventory and sum(kw in joined for kw in ["编队", "强化", "变还"]) >= 2)):
            details["close_coord"] = top_close_pos
            details["reason"] = "检测到召唤结果与库存汇总，优先于商店和礼装详情"
            return GameScreen.SUMMON_RESULT, 0.98, details

        # ------------------ 规则 2.4: 从者选择界面 (SERVANT_SELECT) ------------------
        # 严格排除出击编队确认界面（编队界面下方会有提示“请点击想要替换的从者、概念礼装”，但右下角有“开始任务”按钮）
        has_start_task = "开始任务" in joined or cls.find_text_coordinate(ocr_results, "开始任务", region=(1400, 800, 1920, 1080)) is not None
        if not has_start_task and any(kw in joined for kw in ["从者选择", "选择从者", "请点击要替换的从者"]):
            close_coord = top_close_pos or (163, 63)
            details["close_coord"] = close_coord
            details["reason"] = f"检测到【从者选择】界面 (返回按键: {close_coord})"
            return GameScreen.SERVANT_SELECT, 0.99, details

        # ------------------ 规则 2.5: 概念礼装选择 / 从者种火详情 / 个人空间 / 图鉴等子界面 (CRAFT_ESSENCE_SELECT) ------------------
        detail_title = any(cls.find_text_coordinate(ocr_results, kw, region=(0, 0, 1920, 320)) for kw in [
            "概念礼装选择", "选择概念礼装", "请点击想要装备的概念礼装",
            "个人空间", "Myroom", "图鉴（剧情）", "图鉴（从者）", "图鉴（画廊）",
            "灵基一览", "战斗形象", "从者详情", "概念礼装详情"
        ])
        has_summon_layout = any(kw in joined for kw in ["灵基召唤", "召唤一览", "友情点召唤", "免费召唤", "圣晶石召唤"])
        detail_fields = sum(kw in joined for kw in ["最大HP", "最大ATK", "经验值", "保有技能", "持有技能"])
        if has_top_close and (detail_title or (not has_summon_layout and detail_fields >= 2)):
            close_coord = top_close_pos or (163, 63)
            details["close_coord"] = close_coord
            details["reason"] = f"检测到子界面/从者种火详情/个人空间/礼装详情/选择面板 (关闭按键: {close_coord})"
            return GameScreen.CRAFT_ESSENCE_SELECT, 0.99, details

        from src.strategy.ordeal_navigation import is_selector, map_identity
        from src.strategy.chapter_navigation import map_identity as story_map_identity
        if is_selector(ocr_results):
            details["reason"] = "检测到奏章地球区域及章节选择页"
            details["close_coord"] = top_close_pos
            return GameScreen.ORDEAL_SELECT, 0.99, details

        # ------------------ 规则 2.6: 主线记录 / 特异点章节选择列表 (MAIN_RECORD) ------------------
        from src.strategy.chapter_navigation import interlude_list
        if interlude_list(ocr_results):
            details["reason"] = "检测到主线物语章节选择列表"
            details["close_coord"] = top_close_pos
            return GameScreen.MAIN_RECORD, 0.99, details
        record_header = any('主线记录' in text and float(score)>=.55 and sum(p[1] for p in box)/4<150
                            for box,text,score in ocr_results)
        if "主线记录" in joined and not cls.find_text_coordinate(ocr_results,"通知",region=(0,0,350,160)) and (
                record_header or any(kw in joined for kw in ["特异点", "所罗门", "巴比伦尼亚", "卡美洛", "合众为一", "罗马", "奥尔良", "冬木", "第一", "第二", "第七"])):
            close_coord = top_close_pos or (163, 63)
            details["close_coord"] = close_coord
            details["reason"] = "检测到【主线记录 / 特异点章节选择】列表界面"
            return GameScreen.MAIN_RECORD, 0.99, details

        # ------------------ 规则 2.7: 章节区域大地图 (REGIONAL_MAP) ------------------
        # 严格要求：必须具有真实的【管理室】按钮（cx<350, cy<160），或者处于关卡抽屉展开态（左上角有'关闭'且带有大地图地标文本）
        # 严厉排斥：若左上角存在【通知】按钮或右上角有【活动报酬】，说明依然处于终端大厅，绝不可误判为大地图！
        has_top_notice = cls.find_text_coordinate(ocr_results, "通知", region=(0, 0, 350, 160)) is not None
        has_manager_room = (cls.find_text_coordinate(ocr_results, "管理室", region=(0, 0, 350, 160)) is not None) or ("管理室" in joined)
        from src.strategy.event_navigation import map_identity as event_map_identity
        event_identity = event_map_identity(ocr_results)
        if (event_identity is None and not has_top_notice and not has_start_task and not record_header
                and (has_manager_room or has_top_close) and cls.identify_regional_map(ocr_results)[0]=='未知地图'):
            event_identity = event_map_identity(ocr_results,pil_img)

        is_real_regional_map = (
            not has_top_notice and
            not has_start_task and
            not ("助战选择" in joined) and
            not ("主线记录" in joined) and
            (
                has_manager_room or event_identity is not None or (has_top_close and (map_identity(ocr_results) is not None or story_map_identity(ocr_results) is not None)) or
                (has_top_close and any(kw in joined for kw in [
                    "邪龙百年战争", "燃烧污染都市", "永恒狂气帝国", "永续疯狂帝国", "封锁终局四海",
                    "死界魔雾都市", "北美神话大战", "神圣圆桌领域", "绝对魔兽战线", "自由关卡"
                ]))
            )
        )
        if is_real_regional_map:
            short_map, canonical_map = event_identity or cls.identify_regional_map(ocr_results)
            exit_pos = cls.find_text_coordinate(ocr_results, "管理室", region=(0, 0, 350, 160)) or (164, 62)
            details["map_name"] = short_map
            details["current_map"] = canonical_map
            details["exit_to_manager_coord"] = exit_pos
            details["reason"] = f"检测到【章节区域大地图: {canonical_map}】界面"
            return GameScreen.REGIONAL_MAP, 0.98, details

        # ------------------ 规则 3: 达·芬奇工坊 / 商店界面 (Davinci Workshop) ------------------
        if any(kw in joined for kw in ["达·芬奇工房", "达·芬奇工坊", "魔力棱镜交换", "纯净棱镜", "灵基变还", "活动道具交换"]):
            close_coord = top_close_pos or close_pos or (121, 40)
            details["close_coord"] = close_coord
            details["reason"] = f"检测到【达·芬奇工坊】商店界面文本 (关闭按键: {close_coord})"
            return GameScreen.DAVINCI_WORKSHOP, 0.99, details

        # ------------------ 规则 3.2: 连续出击 / 补充 AP 弹窗 (CONTINUE_QUEST) ------------------
        # 必须高于通用弹窗与结算面板，确保结算末尾弹出【连续出击 / 关闭】双按钮时精准识别
        if any(kw in joined for kw in ["连续出击", "再次挑战", "行动力恢复", "补充AP"]):
            details["reason"] = "检测到连续出击确认面板"
            return GameScreen.CONTINUE_QUEST, 0.99, details

        # ------------------ 规则 3.3: 战后好友申请界面 (归入 POPUP_DIALOG 精准点击【不申请】) ------------------
        if any(kw in joined for kw in ["好友申请", "不申请", "与这位御主成为好友"]):
            reject_pos = cls.find_text_coordinate(ocr_results, "不申请") or (450, 900)
            details["close_coord"] = reject_pos
            details["reason"] = f"检测到战后好友申请界面 (点击不申请: {reject_pos})"
            return GameScreen.POPUP_DIALOG, 0.99, details

        # ------------------ 规则 3.35: 战斗局内【技能使用】确认与【请选择对象】目标弹窗 (优先于物品详情) ------------------
        has_skill_confirm = (
            ("技能使用" in joined and any(kw in joined for kw in ["决定", "取消", "消耗自身", "充能时间"]) and "撤退" not in joined)
            or ("无法使用技能" in joined or "能量不足" in joined)
            or AnchorVerifier.is_skill_confirm_dialog_open(pil_img)
        )
        if has_skill_confirm:
            # 若弹出了【无法使用技能/能量不足】次级错误弹窗，优先点击该弹窗的【关闭】按钮 (约 960, 820)
            sub_close = cls.find_text_coordinate(ocr_results, "关闭", region=(600, 650, 1320, 950))
            if sub_close or ("无法使用技能" in joined or "能量不足" in joined):
                details["close_coord"] = sub_close or (960, 820)
                details["reason"] = f"检测到技能无法使用错误提示弹窗 (点击关闭: {details['close_coord']})"
                return GameScreen.POPUP_DIALOG, 0.99, details

            cancel_pos = cls.find_text_coordinate(ocr_results, "取消", region=(400, 500, 800, 700)) or (580, 615)
            details["close_coord"] = cancel_pos
            details["confirm_coord"] = (1260, 615)
            details["reason"] = f"检测到战斗局内【技能使用】确认弹窗 (关闭/取消坐标: {cancel_pos})"
            return GameScreen.POPUP_DIALOG, 0.99, details

        if AnchorVerifier.is_skill_target_dialog_open(pil_img) or any(kw in joined for kw in ["请选择对象", "选择对象"]):
            details["close_coord"] = (500, 560)
            details["reason"] = "检测到未闭合的技能【请选择对象】窗口，默认选定 1号位主核 (500, 560)"
            return GameScreen.POPUP_DIALOG, 0.99, details

        # ------------------ 规则 3.4: 展开的物品信息界面 / 道具详情 / 确认信息弹窗 (POPUP_DIALOG) ------------------
        # 必须优先于 BATTLE_RESULT 判定！当结算界面误触掉落物展开【道具详情 / 持有数 / 关闭】弹窗时，
        # 即使背景边缘仍残留“战利品”字样，也必须优先识别为弹窗并点击底部居中【关闭】按钮 (960, 835)！
        has_item_detail_kw = any(kw in joined for kw in [
            "道具详情", "主要获取来源", "持有道具一览", "圣晶石的碎片", "圣晶片",
            "持有数量", "持有数", "所持数"
        ]) or ("详细信息" in joined and "查看详细信息" not in joined)
        has_modal_center_close = (
            center_close_pos is not None and
            not any(kw in joined for kw in ["礼物盒", "全部领取", "御主任务", "灵基召唤", "召唤十次", "友情点召唤"])
        )
        if has_item_detail_kw or has_modal_center_close:
            details["close_coord"] = center_close_pos or close_pos or (960, 835)
            details["reason"] = f"检测到展开的物品信息界面/道具详情弹窗 (关闭坐标: {details['close_coord']})"
            return GameScreen.POPUP_DIALOG, 0.99, details

        # 战斗局内长按状态详情面板 / 误触打开的战斗菜单 / 遗留的技能确认或目标选择弹窗
        if any(kw in joined for kw in ["状态数值", "状态变化", "宝具详细", "概念礼装特有技能"]):
            details["close_coord"] = (1628, 85)
            details["reason"] = "检测到战斗局内从者【状态数值/状态变化】详情面板 (点击右上角 X: 1628, 85)"
            return GameScreen.POPUP_DIALOG, 0.99, details

        if "撤退" in joined and any(kw in joined for kw in ["技能使用确认", "音量设定", "宝具演出速度"]):
            details["close_coord"] = (1760, 145)
            details["reason"] = "检测到战斗局内误触展开的【战斗菜单】弹窗 (点击右上角 X: 1760, 145)"
            return GameScreen.POPUP_DIALOG, 0.99, details

        # ------------------ 规则 3.5: 战斗胜利战利品与经验结算面板 (BATTLE_RESULT) ------------------
        if any(kw in joined for kw in ["战利品", "获得道具", "羁绊点数", "御主经验", "BATTLE FINISH", "NEXT", "获得了战利品", "距离下一等级", "战斗结果", "获得经验值", "请点击游戏界面", "礼装经验", "羁绊"]):
            # 若结算画面上出现了左上角或居中的【关闭】按钮，说明有子页面或弹窗遮挡，优先关闭
            if close_pos is not None:
                details["close_coord"] = center_close_pos or top_close_pos or close_pos
                details["reason"] = f"检测到结算界面上方存在带【关闭】按钮的弹窗遮罩 ({details['close_coord']})"
                return GameScreen.POPUP_DIALOG, 0.99, details
            details["reason"] = "检测到战斗胜利与战利品结算文本"
            return GameScreen.BATTLE_RESULT, 0.99, details

        # ------------------ 规则 5: 真正的公告 / 签到遮罩弹窗 ------------------
        has_announcement_header = any(kw in joined for kw in [
            "游戏公告", "公告", "每日签到奖励", "连续登录奖励", "维护通知", "版本更新公告", "重要公告", "游戏更新", "签到奖励"
        ])
        top_x_close = cls.find_text_coordinate(ocr_results, "X", region=(1700, 30, 1920, 160))
        has_close_btn_with_notice = ("公告" in joined or "签到" in joined or "维护" in joined) and (close_pos is not None or top_x_close is not None)

        if has_announcement_header or has_close_btn_with_notice:
            details["close_coord"] = top_x_close or close_pos or (1860, 90)
            details["reason"] = "检测到遮罩公告/签到弹窗及关闭按钮"
            return GameScreen.ANNOUNCEMENT, 0.98, details

        # ------------------ 规则 7.5: 迦勒底之门关卡列表 (CHALDEA_GATE) ------------------
        # 内部关卡选择项特征：包含关卡关键词且左上角具有关闭/返回导航按键，且非编队/助战
        if (has_gate_quest or any(kw in joined for kw in ["极级", "超级", "上级"])) and has_top_close and not is_party_confirm and not is_support_select:
            details["reason"] = "检测到【迦勒底之门】内部副本选项与左上角返回按键"
            return GameScreen.CHALDEA_GATE, 0.97, details

        # 提取当前战斗 Wave 轮数与总回合数 (若在战斗画面中)
        wave_num = AnchorVerifier.extract_battle_wave(ocr_results)
        if wave_num:
            details["wave"] = wave_num
        turn_num = AnchorVerifier.extract_battle_turn(ocr_results)
        if turn_num:
            details["turn"] = turn_num

        # ------------------ 规则 8: 指令卡 / 宝具选择中 (BATTLE_CARD_SELECT) ------------------
        # FGA 权威判定：必须优先于 BATTLE_COMBAT 判定（因为选卡界面顶部依然残留 BATTLE 1/3 文本）
        # 核心标志：右下角出现【Back】蓝色按钮（OCR 或 HSV 颜色锚点命中）
        has_card_back = (cls.find_text_coordinate(ocr_results, "Back", region=(1400, 750, 1920, 1080)) is not None) or \
                        AnchorVerifier.is_card_select_back_button_present(pil_img)
        has_command_cards = any(kw in joined for kw in ["Buster", "Arts", "Quick", "COMMAND", "宝具卡"])

        # 战斗选卡界面必须具有真实的局内战斗 HUD 特征，严禁在菜单、公告、商店中因包含蓝色按钮而误判为选卡！
        is_in_real_battle_context = (
            wave_num is not None or
            turn_num is not None or
            any(kw in joined for kw in ["战斗场次", "回合数", "敌方单位", "暴击星"]) or
            bool(re.search(r"(BATTLE|WAVE|Wave)\s*\d", joined))
        )

        is_card_select = not has_top_close and not is_party_confirm and not is_support_select and is_in_real_battle_context and (
            has_card_back or (has_command_cards and not cls.find_attack_button_coordinate(ocr_results))
        )
        if is_card_select:
            details["reason"] = "检测到战斗选卡界面 (右下角 Back 按键/指令卡托盘展开)"
            return GameScreen.BATTLE_CARD_SELECT, 0.98, details

        # ------------------ 规则 8.5: 战斗局内回合就绪 (BATTLE_COMBAT) ------------------
        # 国服特征：顶部【战斗场次】/【回合数】/【敌方单位】，且右下角青蓝色【攻击】按钮必须处于发光可点击就绪态！
        # 若仅有顶部战斗文字但右下角【攻击】按钮未出现，说明正处于宝具/攻击动画、波次切换或敌方行动回合中，
        # 必须返回 LOADING 等待动画结束，绝不可在动画期间空点技能或误加回合数！
        attack_pos = cls.find_attack_button_coordinate(ocr_results)
        attack_ready = AnchorVerifier.is_attack_button_ready(pil_img)
        has_battle_hud = (
            wave_num is not None or
            turn_num is not None or
            any(kw in joined for kw in ["战斗场次", "回合数", "敌方单位", "暴击星", "战斗速度", "NextPhase"]) or
            bool(re.search(r"(BATTLE|WAVE|Wave)\s*\d", joined))
        )
        is_combat_context = (
            not has_top_close and
            not is_party_confirm and
            not is_support_select and
            not has_gate_quest and
            not is_card_select
        )
        if is_combat_context and attack_ready:
            if wave_num is None or turn_num is None:
                # 全屏 OCR 会漏读较小的数字，放大独立 HUD 复核缺失项。
                hud = pil_img.crop((1150, 0, 1480, 170)).resize((990, 510))
                counter_ocr = [([[x / 3 + 1150, y / 3] for x, y in box], text, score)
                               for box, text, score in cls.run_ocr(hud)]
                details["battle_counter_ocr"] = counter_ocr
                if wave_num is None:
                    wave_num = AnchorVerifier.extract_battle_wave(counter_ocr)
                    if wave_num is not None:
                        details["wave"] = wave_num
                if turn_num is None:
                    turn_num = AnchorVerifier.extract_battle_turn(counter_ocr) or cls.read_battle_turn(pil_img)
                    if turn_num is not None:
                        details["turn"] = turn_num
            if wave_num is None:
                details["reason"] = "战斗指令已就绪，但未能确认波次，等待重新识别"
                return GameScreen.UNKNOWN, 0.6, details
            details["np_percentages"] = AnchorVerifier.extract_servant_np_percentages(ocr_results)
            details["ready_skills"] = AnchorVerifier.detect_ready_skills(pil_img)
            details["reason"] = (
                f"检测到战斗指令阶段就绪 (Wave={wave_num or '?'}, Turn={turn_num or '?'}, "
                f"NP={details['np_percentages']})"
            )
            return GameScreen.BATTLE_COMBAT, 0.99, details

        if is_combat_context and (has_battle_hud or attack_pos is not None):
            details["reason"] = "战斗动画 / 宝具演出 / 敌方回合进行中，等待右下角【攻击】按钮就绪"
            return GameScreen.LOADING, 0.95, details

        # ------------------ 规则 11: 礼物盒列表界面 (GIFT_BOX) ------------------
        if ("礼物盒" in joined or "全部领取" in joined) and (close_pos is not None or "礼物数" in joined):
            details["reason"] = "检测到礼物盒列表文本及按键"
            return GameScreen.GIFT_BOX, 0.98, details

        # ------------------ 规则 12: 御主任务面板 (MASTER_MISSIONS) ------------------
        if ("御主任务" in joined or ("日常" in joined and "周常" in joined and "限定" in joined)) and (close_pos is not None or "已完成任务" in joined):
            details["reason"] = "检测到御主任务面板文本"
            return GameScreen.MASTER_MISSIONS, 0.98, details

        # ------------------ 规则 13: 真正的召唤界面 (SUMMON_SCREEN) ------------------
        has_summon_header = any(kw in joined for kw in ["灵基召唤", "召唤一览"])
        has_summon_buttons = any(kw in joined for kw in ["友情点召唤", "圣晶石召唤", "免费召唤", "召唤十次", "呼符召唤"])
        # 卡池必须有召唤特征，且绝非大厅
        if (has_summon_header or has_summon_buttons) and (close_pos is not None or "灵基召唤" in joined):
            details["reason"] = "检测到卡池召唤文本与抽卡界面特征"
            return GameScreen.SUMMON_SCREEN, 0.98, details

        # ------------------ 规则 15: 主界面 (MAIN_MENU 终端大厅) ------------------
        # 大厅最核心的复合地标：
        # 1. 右上角【活动报酬】/【Reward】
        # 2. 左上角【通知】且左下角有【行动力】/【等级】
        # 3. 右下角有【菜单】且绝无左上角的【关闭】/【返回】按键
        has_hall_top_right = "活动报酬" in joined or "Reward" in joined
        has_hall_ap = "行动力" in joined or "等级" in joined
        has_hall_menu = (cls.find_text_coordinate(ocr_results, "菜单", region=(1500, 800, 1920, 1080)) is not None) or \
                        (cls.find_text_coordinate(ocr_results, "MENU", region=(1500, 800, 1920, 1080)) is not None)
        has_top_notice = cls.find_text_coordinate(ocr_results, "通知", region=(0, 0, 350, 160)) is not None
        has_manager_room = (cls.find_text_coordinate(ocr_results, "管理室", region=(0, 0, 350, 160)) is not None) or ("管理室" in joined)

        # 终端大厅最核心的标志：左上角【通知】按钮 + 右下角【菜单】+ 左下角行动力/AP，且绝无大地图【管理室】或子界面左上角【关闭】！
        if not has_top_close and not has_manager_room and (has_top_notice or has_hall_top_right or (has_hall_menu and (has_hall_ap or "周常任务" in joined or "任务" in joined))):
            details["reason"] = "检测到终端大厅复合地标 (活动报酬/行动力/通知/菜单且无左上角返回关闭)"
            return GameScreen.MAIN_MENU, 0.98, details

        # 兜底容错: 未命中任何已知界面地标时，必须标记为过渡/未知，绝不可草率假定为主大厅
        details["reason"] = f"过渡画面或特征不明确，保持等待 (识别词: {all_texts[:4]})"
        return GameScreen.UNKNOWN, 0.50, details
