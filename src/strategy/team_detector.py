"""
Party & Team Detector - 出击确认界面编队与助战检测器
在 FGO 准备出击确认界面(Party Screen)识别当前 1-3 号位打手与助战，
并自动绑定最适合的 3T 战斗策略。
"""

from __future__ import annotations
from pathlib import Path
from typing import List, Optional, Tuple
from pydantic import BaseModel

from src.strategy.battle_plans import BattlePlanManager, TeamStrategy
from src.parser.chaldea_parser import ChaldeaParser


class DetectedParty(BaseModel):
    """识别到的队伍信息"""
    slot1_servant: str = "未知从者"
    slot2_servant: str = "未知从者"
    slot3_servant: str = "未知从者"
    support_servant: Optional[str] = None
    confidence: float = 1.0


class TeamDetector:
    """出击确认界面编队识别器"""

    def __init__(
        self,
        plan_manager: Optional[BattlePlanManager] = None,
        chaldea_parser: Optional[ChaldeaParser] = None,
    ):
        self.plan_manager = plan_manager or BattlePlanManager()
        self.chaldea_parser = chaldea_parser

    def identify_party_and_plan(
        self,
        servants_in_slots: List[str]
    ) -> Tuple[DetectedParty, Optional[TeamStrategy], List[str]]:
        """
        根据传入的 1-3 号位从者名称列表，完成：
        1. 队伍信息封装；
        2. 自动匹配预设 3T 战斗方案；
        3. 结合 Chaldea 练度数据完成被动充能与技能等级检验。
        """
        logs: List[str] = []
        s1 = servants_in_slots[0] if len(servants_in_slots) > 0 else "空"
        s2 = servants_in_slots[1] if len(servants_in_slots) > 1 else "空"
        s3 = servants_in_slots[2] if len(servants_in_slots) > 2 else "空"

        party = DetectedParty(
            slot1_servant=s1,
            slot2_servant=s2,
            slot3_servant=s3,
            support_servant=s3,  # 默认第 3 号位通常为助战拐
        )
        logs.append(f"检测到出击前排阵容: [1号位: {s1}] | [2号位: {s2}] | [3号位: {s3}]")

        # 匹配 3T 策略
        strategy = self.plan_manager.match_team_by_servants([s1, s2, s3])
        if strategy:
            logs.append(f"成功命中预设 3T 策略: 「{strategy.name}」({strategy.id})")

            # 若有 Chaldea 档案，执行练度校验
            if self.chaldea_parser:
                valid, chaldea_logs = self.plan_manager.validate_with_chaldea(strategy, self.chaldea_parser)
                logs.extend(chaldea_logs)
                if not valid:
                    logs.append("[注意] 当前队伍从者练度不满足全额充能要求，请确认是否继续出击！")
        else:
            logs.append("[未命中] 未能匹配到专用 3T 策略，将使用通用指令或默认出击。")

        return party, strategy, logs

    def detect_servants_from_screen(
        self,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> List[str]:
        """
        从出击确认编队界面的 OCR 结果中识别前排 1-3 号位与后排 4 号位从者名称。
        基于 1920x1080 分辨率编队卡面几何拓扑划分：
          Slot 1 (前排1号位): 30 <= x < 345, 180 <= y <= 880 (中心 ~195)
          Slot 2 (前排2号位): 345 <= x < 655, 180 <= y <= 880 (中心 ~500)
          Slot 3 (助战3号位): 655 <= x < 965, 180 <= y <= 880 (中心 ~810)
          Slot 4 (后排4号位): 965 <= x < 1275, 180 <= y <= 880 (中心 ~1120)
        """
        slots = ["", "", "", ""]

        aliases = {
            "杀狐": "光之高扬斯卡娅",
            "高扬斯卡娅": "光之高扬斯卡娅",
            "光之高扬斯卡娅": "光之高扬斯卡娅",
            "光狐": "光之高扬斯卡娅",
            "暗之高扬斯卡娅": "暗之高扬斯卡娅",
            "暗狐": "暗之高扬斯卡娅",
            "c呆": "阿尔托莉雅·Caster",
            "术呆": "阿尔托莉雅·Caster",
            "caster": "阿尔托莉雅·Caster",
            "阿尔托莉雅·caster": "阿尔托莉雅·Caster",
            "狂娜": "阿周那(Alter)",
            "狂周那": "阿周那(Alter)",
            "阿周那": "阿周那(Alter)",
            "小莫": "莫德雷德",
            "花嫁": "尼禄·新娘",
            "班扬": "保罗·班扬",
            "仇凛": "太空伊什塔尔",
            "宇宙凛": "太空伊什塔尔",
            "太空伊什塔尔": "太空伊什塔尔",
            "cba": "斯卡蒂",
            "水cba": "斯卡蒂(Ruler)",
            "奥伯龙": "奥伯龙",
            "摩根": "摩根",
            "千子村正": "千子村正",
            "村正": "千子村正",
            "哈贝喵": "哈贝特洛特",
            "哈贝特洛特": "哈贝特洛特",
            "陈宫": "陈宫",
            "大英雄": "阿拉什",
            "阿拉什": "阿拉什",
            "赵云": "赵云",
            "梣": "梣",
            "救世主": "梣",
            "托内莉可": "梣",
            "梅林": "梅林",
            "孔明": "诸葛孔明〔埃尔梅罗Ⅱ世〕",
            "司马懿": "司马懿〔莱妮丝〕",
            "莱妮丝": "司马懿〔莱妮丝〕",
            "伊吹童子": "伊吹童子(Berserker)",
            "狂伊吹": "伊吹童子(Berserker)",
        }

        # 收集预设库中关心的所有从者名称
        target_names = set()
        for t in self.plan_manager.teams.values():
            for m in t.match_servants:
                target_names.add(m)
        if self.chaldea_parser:
            for s in self.chaldea_parser.servants.values():
                target_names.add(s.name)

        # 1. 优先按卡位几何区域精准匹配
        for box, text, _ in ocr_results:
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            cx = int(sum(xs) / 4); cy = int(sum(ys) / 4)

            if not (180 <= cy <= 880):
                continue

            slot_idx = -1
            if 30 <= cx < 345:
                slot_idx = 0
            elif 345 <= cx < 655:
                slot_idx = 1
            elif 655 <= cx < 965:
                slot_idx = 2
            elif 965 <= cx < 1275:
                slot_idx = 3

            if slot_idx == -1:
                continue

            t_lower = text.lower()
            matched_name = None
            for alias, real_name in aliases.items():
                if alias in t_lower:
                    matched_name = real_name
                    break

            if not matched_name:
                for name in target_names:
                    if name.lower() in t_lower or (len(name) >= 2 and name in text):
                        matched_name = name
                        break

            if matched_name and not slots[slot_idx]:
                slots[slot_idx] = matched_name

        # 2. 兜底拓扑：如果在前排 1-3 号位总区域 (x <= 1350) 出现关键从者名，按顺序填入未识别的空位
        all_front_text = " ".join([
            item[1] for item in ocr_results
            if any(p[0] <= 1350 and 180 <= p[1] <= 880 for p in item[0])
        ])

        for alias, real_name in aliases.items():
            if alias in all_front_text.lower():
                for idx in range(3):
                    if not slots[idx] and real_name not in slots:
                        slots[idx] = real_name
                        break

        for name in target_names:
            if name in all_front_text:
                for idx in range(3):
                    if not slots[idx] and name not in slots:
                        slots[idx] = name
                        break

        return [
            slots[0] or "打手从者",
            slots[1] or "辅助拐",
            slots[2] or "助战从者",
            slots[3] or "后排从者"
        ]

    def identify_party_from_screen(
        self,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> Tuple[DetectedParty, Optional[TeamStrategy], List[str]]:
        """从出击确认界面的实机 OCR 识别结果直接完成队伍识别、3T 方案匹配与练度检验"""
        servants = self.detect_servants_from_screen(ocr_results)
        return self.identify_party_and_plan(servants)
