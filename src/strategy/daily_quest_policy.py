"""Select daily farming from the target card's AP evidence, not a global banner."""
import re

from src.core.path_helper import get_resource_path
from src.solver.mission_solver import MasterMissionSolver, QuestPlan
from src.strategy.laplace import PublicData
import json


def discount_status(ocr, position, normal_ap):
    """True: reduced AP; False: observed ordinary AP; None: insufficient evidence."""
    y = position[1]
    rows = []
    for box, text, score in ocr:
        x, cy = (sum(p[i] for p in box) / 4 for i in (0, 1))
        if 900 <= x <= 1900:
            rows.append((cy, re.sub(r"\s+", "", text), score))
    following = [cy for cy, text, _ in rows if cy > y + 65 and
                 any(k in text for k in ("宝物库", "种火", "修炼场")) and
                 any(k in text for k in ("初级", "中级", "上级", "超级", "极级"))]
    bottom = min(following) - 35 if following else min(y + 230, 900)
    card = [(text, score) for cy, text, score in rows if y - 30 <= cy < bottom]
    for text, score in card:
        if score >= 0.7 and re.search(r"(?:AP|体力|行动力).*(?:减半|減半|1[/／][234]|消耗为0|消耗为零|消耗0)", text, re.I):
            return True
    costs = [int(m[1]) for text, score in card if score >= 0.75
             for m in [re.fullmatch(r"(?:消耗)?AP[:：]?(\d+)", text, re.I)] if m]
    if len(set(costs)) != 1 or normal_ap <= 0:
        return None
    return costs[0] < normal_ap if costs[0] <= normal_ap else None


def normal_ap(target):
    catalog = json.loads(get_resource_path("data/laplace/quests.json").read_text(encoding="utf-8"))
    entry = catalog[target]
    quest = PublicData().get("CN", "quest", entry["id"], entry["phase"])
    if quest.get("consumeType") != "ap" or quest.get("consume", 0) <= 0:
        raise ValueError("缺少指定日常关卡的基础 AP 数据")
    return quest["consume"]


def minimum_plan(runs):
    quests = [q for q in MasterMissionSolver().quests if q.ap > 0]
    q = min(quests, key=lambda q: (q.ap, q.id))
    return QuestPlan(quest_id=q.id, war=q.war, spot=q.spot, name=q.name,
                     ap_per_run=q.ap, runs=runs, total_ap=q.ap * runs)
