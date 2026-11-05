"""
Battle Plans & Strategy Engine
负责 3T 战斗策略配置的加载、合法性验证(结合 Chaldea 练度数据)以及回合操作指令流生成。
"""

from __future__ import annotations
try:
    import yaml
except ImportError:
    try:
        from ruamel.yaml import YAML
        _ruamel_inst = YAML(typ='safe')
        class _YamlAdapter:
            @staticmethod
            def safe_load(stream):
                return _ruamel_inst.load(stream)
        yaml = _YamlAdapter()
    except Exception:
        yaml = None
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Literal
from pydantic import BaseModel, Field

from src.parser.chaldea_parser import ChaldeaParser, ServantProfile


class SkillAction(BaseModel):
    servant: int              # 从者位置: 1, 2, 3 (换人后可为4)
    skill: int                # 技能位置: 1, 2, 3
    target: Optional[int] = None  # 充能或增益目标: 1, 2, 3


class MasterSkillAction(BaseModel):
    skill: int                # 御主技能: 1, 2, 3
    target: Optional[int] = None
    swap: Optional[List[int]] = None  # 换人位置对 [换下位, 换上位], 如 [2, 4]


class TurnDirective(BaseModel):
    skills: List[SkillAction] = Field(default_factory=list)
    master_skills: List[MasterSkillAction] = Field(default_factory=list)
    np_servant: Optional[int] = 1
    attack_mode: Literal["np", "facecards"] = "np"


class TeamStrategy(BaseModel):
    id: str
    name: str
    match_servants: List[str]
    required_min_np_charge: int = 0
    mystic_code_id: Optional[int] = None
    turns: Dict[int, TurnDirective] = Field(default_factory=dict)


class BattlePlanManager:
    """战斗策略管理器"""

    def __init__(self, teams_config_path: Optional[str | Path] = None):
        base_dir = Path(__file__).resolve().parent.parent.parent
        self.config_path = Path(teams_config_path or base_dir / "config" / "teams.yaml")
        self.teams: Dict[str, TeamStrategy] = {}
        self.load_config()

    def load_config(self) -> None:
        """加载队伍战斗策略配置"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Teams config file not found: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)

        raw_teams = raw_data.get("teams", [])
        for item in raw_teams:
            # 格式化 turns 键为 int
            turns_dict: Dict[int, TurnDirective] = {}
            for t_num, t_data in item.get("turns", {}).items():
                turns_dict[int(t_num)] = TurnDirective(**t_data)

            team = TeamStrategy(
                id=item["id"],
                name=item["name"],
                match_servants=item.get("match_servants", []),
                required_min_np_charge=item.get("required_min_np_charge", 0),
                turns=turns_dict
            )
            self.teams[team.id] = team

    def match_team_by_servants(self, detected_servant_names: List[str]) -> Optional[TeamStrategy]:
        """
        根据出击确认界面识别到的从者名称列表，自动匹配最适合的预设 3T 策略
        例如: detected = ["摩根", "光之高扬斯卡娅", "光之高扬斯卡娅"]
              -> 匹配 "摩根红卡3T队"
        """
        detected_text = " ".join(detected_servant_names).lower()
        for team in self.teams.values():
            # 检查 match_servants 里的每一个关键词是否都命中
            if all(m.lower() in detected_text for m in team.match_servants):
                return team
        return None

    def validate_with_chaldea(
        self,
        team: TeamStrategy,
        chaldea_parser: ChaldeaParser
    ) -> Tuple[bool, List[str]]:
        """
        结合用户的 Chaldea 档案，校验该 3T 队伍的练度可行性
        (例如打手技能是否满级、被动充能是否具备要求的 20%)
        """
        logs: List[str] = []
        is_valid = True

        for s_keyword in team.match_servants:
            profile = chaldea_parser.get_by_name(s_keyword)
            if not profile:
                logs.append(f"[警告] Chaldea 档案中未找到从者: {s_keyword}")
                continue

            # 校验被动充能要求
            if team.required_min_np_charge > 0:
                actual_charge = profile.get_append_np_charge()
                if actual_charge < team.required_min_np_charge:
                    logs.append(
                        f"[充能不足] {profile.name} 被动充能为 {actual_charge}%，"
                        f"低于策略要求的 {team.required_min_np_charge}%！可能会导致无法连发。"
                    )
                    is_valid = False
                else:
                    logs.append(f"[通过] {profile.name} 满足被动 {actual_charge}% 充能要求")

            # 校验主动技能练度
            if not profile.is_all_skills_max():
                logs.append(f"[提示] {profile.name} 主动技能未全满 10 ({profile.skills})，连发稳定性可能受影响")
            else:
                logs.append(f"[通过] {profile.name} 技能 10/10/10 已全满")

        return is_valid, logs

    def format_turn_instructions(self, team: TeamStrategy, turn: int) -> List[str]:
        """将某回合的战斗指令转换为人类可读的执行步骤描述"""
        turn_data = team.turns.get(turn)
        if not turn_data:
            return [f"回合 {turn}: 无操作，直接选普通卡"]

        steps: List[str] = []

        # 1. 处理御主技能 / 换人
        for m_act in turn_data.master_skills:
            if m_act.swap:
                steps.append(f"使用御主换人技能: 将 {m_act.swap[0]} 号位 与 {m_act.swap[1]} 号位 从者互换")
            elif m_act.target:
                steps.append(f"使用御主 {m_act.skill} 技能 -> 施加给 {m_act.target} 号位从者")
            else:
                steps.append(f"使用御主 {m_act.skill} 技能 (全员加攻/增伤)")

        # 2. 处理从者主动技能
        for s_act in turn_data.skills:
            if s_act.target:
                steps.append(f"从者 {s_act.servant} 号位 释放第 {s_act.skill} 技能 -> 指定 {s_act.target} 号位")
            else:
                steps.append(f"从者 {s_act.servant} 号位 释放第 {s_act.skill} 技能")

        # 3. 释放宝具与垫卡
        if turn_data.attack_mode == "facecards":
            steps.append("点击 Attack -> 选择三张普通指令卡（已校验任意发牌，无需暴击）")
        else:
            steps.append(f"点击 Attack 按钮 -> 释放 {turn_data.np_servant} 号位从者宝具 -> 选取 2 张垫卡")

        return steps
