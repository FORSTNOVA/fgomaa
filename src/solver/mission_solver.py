"""
FGO Weekly Master Mission ILP Solver (基于整数线性规划的最少 AP 周常规划器)
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds


from src.core.path_helper import get_resource_path


class QuestInfo(BaseModel):
    id: str
    war: str
    spot: str
    name: str
    ap: int
    enemies_count: int = 0
    traits: Dict[str, int] = Field(default_factory=dict)

    def objective_count(self, trait: str) -> int:
        return self.enemies_count if trait == "enemies" else self.traits.get(trait, 0)


class MissionRequirement(BaseModel):
    """单个周常任务要求"""
    trait: str
    count: int
    description: str = ""
    is_event: bool = False
    progress_cur: int = 0
    progress_total: int = 0


class QuestPlan(BaseModel):
    """求解出的单关卡执行计划"""
    quest_id: str
    war: str
    spot: str
    name: str
    ap_per_run: int
    runs: int
    total_ap: int


class SolverResult(BaseModel):
    """周常规划总体输出"""
    success: bool
    total_ap: int = 0
    total_runs: int = 0
    plans: List[QuestPlan] = Field(default_factory=list)
    achieved: Dict[str, int] = Field(default_factory=dict)
    message: str = "Success"
    event_missions: List[str] = Field(default_factory=list)
    completed_missions: List[str] = Field(default_factory=list)
    requirements: List[MissionRequirement] = Field(default_factory=list)
    missions_verified: bool = False
    all_completed: bool = False
    rewards_claimed: bool = False


class MasterMissionSolver:
    """周常御主任务最低 AP 规划器"""

    def __init__(
        self,
        quests_data_path: Optional[str | Path] = None,
        mapping_data_path: Optional[str | Path] = None,
    ):
        self.quests_path = Path(quests_data_path or get_resource_path("data/free_quests_traits.json"))
        self.mapping_path = Path(mapping_data_path or get_resource_path("data/trait_mapping.json"))

        self.quests: List[QuestInfo] = []
        self.trait_mappings: Dict[str, Any] = {}
        self.load_data()

    def load_data(self) -> None:
        """加载关卡数据与特性词条映射表"""
        if not self.quests_path.exists():
            raise FileNotFoundError(f"Quests data not found at: {self.quests_path}")
        with open(self.quests_path, "r", encoding="utf-8") as f:
            raw_quests = json.load(f)
            self.quests = [QuestInfo(**q) for q in raw_quests]

        if self.mapping_path.exists():
            with open(self.mapping_path, "r", encoding="utf-8") as f:
                self.trait_mappings = json.load(f)

    def parse_chinese_mission(
        self,
        text: str,
        progress_cur: Optional[int] = None,
        progress_total: Optional[int] = None
    ) -> Optional[MissionRequirement]:
        """
        从 OCR 文本或中文描述中提取任务特性和所需击杀/获得数量
        支持：
          - 单行或多行换行拼接文本
          - 模糊提取击杀数 (如 "败15个"、"击败15个"、"50份")
          - 智能识别活动限定战利品 (is_event=True)，杜绝误归类为常驻战利品
          - 结合目标进行度 (cur/total) 计算剩余待击杀量 (remaining = total - cur)
        """
        text_clean = text.strip()
        if not text_clean:
            return None

        # 排除汇总标题与无需打本的标题行
        if any(ign in text_clean for ign in [
            "主要报酬", "已完成任务", "完成本周所有的御主任务", "完成本周所有", "统一领取", "全部领取"
        ]):
            return None

        # 统一归一化各种全半角中英文括号，便于模式匹配
        norm = text_clean.replace("『", "【").replace("「", "【").replace("[", "【") \
                         .replace("』", "】").replace("」", "】").replace("]", "】")
        # 仅修复“持有X之力”中单边漏识别的括号，保留属性条件本身。
        norm = re.sub(r"持有[【1]?([天地人星兽])】?之力", r"持有【\1】之力", norm)

        # 优先使用外部传入的进行度，或在文本中正则寻找 X/Y
        cur = progress_cur
        total = progress_total
        if cur is None or total is None:
            prog_match = re.search(r"(\d+)\s*/\s*(\d+)", norm)
            if prog_match:
                cur = int(prog_match.group(1))
                total = int(prog_match.group(2))

        # 提取目标总数
        count = total
        if count is None:
            num_match = re.search(r"(\d+)\s*(?:个|次|份)", norm)
            if not num_match:
                num_match = re.search(r"(?:击败|败|获得|消灭|完成|战胜|累计)\s*(\d+)", norm)
            if num_match:
                count = int(num_match.group(1))
            else:
                return None

        if count <= 0 or (cur is not None and total is not None and not 0 <= cur <= total):
            return None

        # 若已知当前完成度，计算剩余数量
        if cur is not None and total is not None:
            rem = total - cur
        else:
            rem = count

        # 1. 识别活动限定专属战利品/活动任务
        # 例如: "在『终结·万圣节2026』中重新累计获得战利品【死亡拨片】50个"
        if any(ev_kw in norm for ev_kw in ["终结·万圣节", "万圣节", "活动", "死亡拨片", "地狱T恤", "朋克摇滚龙", "限定"]) or \
           (re.search(r"在【.+】中", norm) and "战利品" in norm):
            return MissionRequirement(
                trait="event_item",
                count=max(0, rem),
                description=text_clean,
                is_event=True,
                progress_cur=cur or 0,
                progress_total=total or count
            )

        # 2. 识别通用特性 (显式精准规则优先，杜绝单字误判)
        matched_trait = None

        # 属性 (attributes)
        if "【地】" in norm or "地之力" in norm or "地属性" in norm:
            matched_trait = "earth"
        elif "【天】" in norm or "天之力" in norm or "天属性" in norm:
            matched_trait = "sky"
        elif "【人】" in norm or "人之力" in norm or "人属性" in norm:
            matched_trait = "human"
        elif "【星】" in norm or "星之力" in norm or "星属性" in norm:
            matched_trait = "star"
        elif "【兽】" in norm or "兽之力" in norm or "兽属性" in norm:
            matched_trait = "beast"

        # 特性 (traits)
        elif "【超巨大】" in norm or "超巨大" in norm:
            matched_trait = "super_giant"
        elif "【龙】" in norm or "龙特性" in norm or "龙之" in norm:
            matched_trait = "dragon"
        elif "【魔性】" in norm or "魔性" in norm:
            matched_trait = "demonic"
        elif "【死灵】" in norm or "死灵" in norm:
            matched_trait = "undead"
        elif "【神性】" in norm or "神性" in norm:
            matched_trait = "divine"
        elif "【人型】" in norm or "人型" in norm:
            matched_trait = "humanoid"
        elif "【猛兽】" in norm or "野兽" in norm:
            matched_trait = "wildbeast"
        elif "【机械】" in norm or "机械" in norm:
            matched_trait = "mechanical"
        elif "【罗马】" in norm or "罗马" in norm:
            matched_trait = "roman"
        elif "【鬼】" in norm or "鬼特性" in norm:
            matched_trait = "oni"
        elif "【王】" in norm or "王特性" in norm:
            matched_trait = "king"
        elif "【浮空】" in norm or "浮空" in norm:
            matched_trait = "floating"

        # 阵营与职阶
        elif "【秩序】" in norm or "秩序" in norm:
            matched_trait = "lawful"
        elif "【混沌】" in norm or "混沌" in norm:
            matched_trait = "chaotic"
        elif "【中立】" in norm or "中立" in norm:
            matched_trait = "neutral"
        elif "【善】" in norm or "善属性" in norm:
            matched_trait = "good"
        elif "【恶】" in norm or "恶属性" in norm:
            matched_trait = "evil"
        elif "【中庸】" in norm or "中庸" in norm:
            matched_trait = "balance"
        elif "【枪】" in norm or "枪阶" in norm:
            matched_trait = "lancer"
        elif "【剑】" in norm or "剑阶" in norm:
            matched_trait = "saber"
        elif "【弓】" in norm or "弓阶" in norm:
            matched_trait = "archer"
        elif "【骑】" in norm or "骑阶" in norm:
            matched_trait = "rider"
        elif "【术】" in norm or "术阶" in norm:
            matched_trait = "caster"
        elif "【杀】" in norm or "杀阶" in norm:
            matched_trait = "assassin"
        elif "【狂】" in norm or "狂阶" in norm:
            matched_trait = "berserker"

        # 通用战利品 / 关卡完成
        elif "【从者】" in norm or "从者" in norm:
            matched_trait = "servant"
        elif "战利品" in norm or "道具" in norm:
            matched_trait = "drops"
        elif "完成" in norm and "关卡" in norm:
            matched_trait = "quest_clears"
        elif re.fullmatch(r"(?:击败|败|消灭|战胜)\d+个敌人(?:[（(]部分首领[、,，]战斗中被召唤出来的敌人除外[）)])?",
                          re.sub(r"\s+", "", norm)):
            matched_trait = "enemies"

        # 字典回退匹配
        if not matched_trait:
            for group_name in ["traits", "attributes", "alignments", "classes"]:
                mapping = self.trait_mappings.get(group_name, {})
                for ch_kw, en_key in mapping.items():
                    if f"【{ch_kw}】" in norm or (len(ch_kw) >= 2 and ch_kw in norm):
                        matched_trait = en_key
                        break
                if matched_trait:
                    break

        if matched_trait:
            # 当前数据只有独立计数，不能把“某特性的从者”当成任意同特性敌人。
            explicit_traits = {item for item in re.findall(r"【([^】]+)】", norm)
                               if any(item in self.trait_mappings.get(group, {}) for group in ("traits", "attributes", "alignments", "classes"))}
            if ("从者" in norm and matched_trait != "servant") or len(explicit_traits) > 1:
                return None
            return MissionRequirement(
                trait=matched_trait,
                count=max(0, rem),
                description=text_clean,
                is_event=False,
                progress_cur=cur or 0,
                progress_total=total or count
            )

        return None

    def parse_missions(self, texts: List[str]) -> List[MissionRequirement]:
        requirements = []
        for text in texts:
            if not isinstance(text, str):
                raise ValueError("任务文本格式无效")
            req = self.parse_chinese_mission(text)
            if req is None:
                if "完成本周所有" in text:
                    continue
                raise ValueError("无法解析任务，不能忽略：" + text)
            requirements.append(req)
        return requirements

    def solve(
        self,
        requirements: List[MissionRequirement],
        allowed_wars: Optional[List[str]] = None,
        max_runs_per_quest: int = 50,
    ) -> SolverResult:
        """
        利用整数线性规划 (MILP) 求解最低 AP 消耗的关卡组合

        min  c^T x
        s.t. A x >= b
             0 <= x <= max_runs, x in Z
        """
        # 0. 智能分离活动限定任务、已达成任务与待刷常规任务
        event_missions: List[str] = []
        completed_missions: List[str] = []
        active_reqs: List[MissionRequirement] = []

        if not requirements:
            return SolverResult(success=False, message="没有有效任务需求，不能确认周常状态。")

        for req in requirements:
            if req.count <= 0:
                completed_missions.append(f"{req.description} (已达成: {req.progress_cur}/{req.progress_total})")
            elif req.is_event:
                event_missions.append(f"{req.description} (进度: {req.progress_cur}/{req.progress_total})")
            else:
                active_reqs.append(req)

        if event_missions:
            return SolverResult(success=False, event_missions=event_missions,
                                completed_missions=completed_missions, requirements=requirements,
                                message="仍有未完成的活动限定任务，当前关卡库不能完整规划：" + "；".join(event_missions))

        # 全部有效需求（包含活动任务）均有完成证据。
        if not active_reqs:
            msg = "已核验的任务目标均已达成，仍需单独确认奖励领取。"
            return SolverResult(
                success=True,
                total_ap=0,
                total_runs=0,
                plans=[],
                event_missions=event_missions,
                completed_missions=completed_missions,
                requirements=requirements,
                all_completed=True,
                message=msg
            )

        # 1. 过滤允许的关卡
        candidate_quests = [
            q for q in self.quests
            if not allowed_wars or any(w in q.war for w in allowed_wars)
        ]

        if not candidate_quests:
            return SolverResult(
                success=False,
                total_ap=0,
                total_runs=0,
                plans=[],
                event_missions=event_missions,
                completed_missions=completed_missions,
                message="No candidate quests found matching the filter."
            )

        # 过滤出当前关卡库可满足的需求项
        solvable_reqs: List[MissionRequirement] = []
        unsolvable_notes: List[str] = []
        for req in active_reqs:
            if any(q.objective_count(req.trait) > 0 for q in candidate_quests):
                solvable_reqs.append(req)
            else:
                unsolvable_notes.append(f"{req.trait}")

        if unsolvable_notes:
            return SolverResult(
                success=False,
                total_ap=0,
                total_runs=0,
                plans=[],
                event_missions=event_missions,
                completed_missions=completed_missions,
                message=f"当前关卡库无对应产出: {', '.join(unsolvable_notes)}"
            )

        n_reqs = len(solvable_reqs)
        n_quests = len(candidate_quests)

        # 2. 构建目标向量 c (各关卡消耗的 AP)
        c = np.array([q.ap for q in candidate_quests], dtype=float)

        # 3. 构建约束矩阵 A 与下界向量 b
        A = np.zeros((n_reqs, n_quests), dtype=float)
        b = np.zeros(n_reqs, dtype=float)

        for i, req in enumerate(solvable_reqs):
            b[i] = float(req.count)
            for j, quest in enumerate(candidate_quests):
                A[i, j] = float(quest.objective_count(req.trait))

        # 5. 求解 MILP
        # A x >= b 即 -inf <= -A x <= -b，但 scipy LinearConstraint 支持 lb <= A x <= ub
        constraints = LinearConstraint(A, lb=b, ub=np.inf)
        integrality = np.ones(n_quests)  # 全部为整数变量
        bounds = Bounds(lb=0, ub=max_runs_per_quest)

        res = milp(c=c, integrality=integrality, constraints=constraints, bounds=bounds)

        if not res.success or res.x is None:
            return SolverResult(
                success=False,
                total_ap=0,
                total_runs=0,
                plans=[],
                event_missions=event_missions,
                completed_missions=completed_missions,
                message=f"MILP solver failed: {res.status}"
            )

        # 6. 解析求解结果
        x_sol = np.round(res.x).astype(int)
        plans: List[QuestPlan] = []
        total_ap = 0
        total_runs = 0

        for j, runs in enumerate(x_sol):
            if runs > 0:
                q = candidate_quests[j]
                cost = q.ap * runs
                total_ap += cost
                total_runs += runs
                plans.append(QuestPlan(
                    quest_id=q.id,
                    war=q.war,
                    spot=q.spot,
                    name=q.name,
                    ap_per_run=q.ap,
                    runs=runs,
                    total_ap=cost,
                ))

        # 统计各项实际达成的数量
        achieved: Dict[str, int] = {}
        for req in active_reqs:
            actual = sum(
                q.objective_count(req.trait) * runs
                for q, runs in zip(candidate_quests, x_sol)
            )
            achieved[req.trait] = int(actual)

        # 按章节名称及 AP 排序输出，方便自动化顺序导航
        plans.sort(key=lambda p: (p.war, p.ap_per_run))

        if any(achieved.get(req.trait, 0) < req.count for req in active_reqs):
            return SolverResult(success=False, message="求解结果未覆盖全部任务需求，停止执行。")

        return SolverResult(
            success=True,
            total_ap=total_ap,
            total_runs=total_runs,
            plans=plans,
            achieved=achieved,
            event_missions=event_missions,
            completed_missions=completed_missions,
            requirements=requirements,
            message="Optimal plan solved successfully."
        )
