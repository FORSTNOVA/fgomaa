"""
Account-Driven & Enemy-Aware Auto Team Builder (智能自配队与克制决策引擎)
核心职责：
1. 解析用户导入的 FGO 账户 JSON (ChaldeaParser)，盘点已持有的核心拐数量、技能等级与实际充能额度；
2. 根据目标关卡的敌方职阶倾向 (剑/弓/枪/骑/术/杀/狂/混合) 与波次配置 (如 3-3-3)，严格执行职阶克制倍增与逆克制剔除；
3. 综合拐练度与打手卡色/自充/被动20%NP/宝具等级，自动决定配队方向（蓝卡不换人冲浪 / 红卡双杀狐换人重装 / 绿卡连发 / 多核自充速刷）；
4. 动态生成包含 1~4 号位编制、礼装建议与 T1~T3 完整技能释放指令流的 TeamStrategy，供实机选队与战斗直接执行。
"""

from __future__ import annotations
import json
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

from src.parser.chaldea_parser import ChaldeaParser, ServantProfile, CRAFT_ESSENCE_CATALOG
from src.strategy.battle_plans import TeamStrategy, TurnDirective, SkillAction, MasterSkillAction


class ServantMeta(BaseModel):
    """从者战术元数据（职阶、宝具色卡、宝具类型、自充技能位与满级充能幅度）"""
    svt_id: int
    name: str
    class_name: str           # Saber, Archer, Lancer, Rider, Caster, Assassin, Berserker, Avenger, Alterego, MoonCancer, Foreigner, Ruler, Pretender
    role: str                 # "dps_aoe", "support", "sub_dps"
    card_color: str           # "Arts", "Buster", "Quick"
    self_charge_skill: int = 0        # 自充技能位于第几个技能 (1, 2, 3; 0 表示无自充)
    max_self_charge: int = 0          # 10级时自充幅度 (如 30, 50, 80, 100)
    secondary_charge_skill: int = 0   # 第二自充技能位 (如摩根 2 技 20% 充能)
    secondary_max_charge: int = 0
    buff_skills: List[int] = Field(default_factory=list)  # 输出打手需要开启的增伤技能位


# 内置核心打手与辅助拐战术元数据库 (基于国服官方 Atlas Academy svtId 标准字典)
SERVANT_TACTICAL_DB: Dict[int, ServantMeta] = {
    # ==================== 核心拐 (Supports) ====================
    504500: ServantMeta(svt_id=504500, name="阿尔托莉雅·卡斯特", class_name="Caster", role="support", card_color="Arts", self_charge_skill=1, max_self_charge=50),
    604200: ServantMeta(svt_id=604200, name="光之高扬斯卡娅", class_name="Assassin", role="support", card_color="Buster", self_charge_skill=1, max_self_charge=50),
    2800100: ServantMeta(svt_id=2800100, name="奥伯龙", class_name="Pretender", role="support", card_color="Buster", self_charge_skill=2, max_self_charge=70),
    901400: ServantMeta(svt_id=901400, name="斯卡哈·斯卡蒂(Ruler)", class_name="Ruler", role="support", card_color="Quick", self_charge_skill=3, max_self_charge=50),
    503900: ServantMeta(svt_id=503900, name="斯卡哈·斯卡蒂(Caster)", class_name="Caster", role="support", card_color="Quick", self_charge_skill=3, max_self_charge=50),
    501900: ServantMeta(svt_id=501900, name="诸葛孔明〔埃尔梅罗Ⅱ世〕", class_name="Caster", role="support", card_color="Arts", self_charge_skill=1, max_self_charge=50),
    403200: ServantMeta(svt_id=403200, name="莱妮丝", class_name="Rider", role="support", card_color="Arts", self_charge_skill=2, max_self_charge=50),
    500800: ServantMeta(svt_id=500800, name="梅林", class_name="Caster", role="support", card_color="Buster", self_charge_skill=1, max_self_charge=20),
    2800300: ServantMeta(svt_id=2800300, name="阿瓦隆女士", class_name="Pretender", role="support", card_color="Arts", self_charge_skill=2, max_self_charge=20),

    # ==================== 狂阶与泛用Extra打手 (Universal AoE DPS) ====================
    704000: ServantMeta(svt_id=704000, name="摩根", class_name="Berserker", role="dps_aoe", card_color="Buster", self_charge_skill=1, max_self_charge=30, secondary_charge_skill=2, secondary_max_charge=20, buff_skills=[1]),
    703300: ServantMeta(svt_id=703300, name="阿周那〔Alter〕", class_name="Berserker", role="dps_aoe", card_color="Buster", self_charge_skill=2, max_self_charge=30, buff_skills=[1, 2]),
    704300: ServantMeta(svt_id=704300, name="伊吹童子(Berserker)", class_name="Berserker", role="dps_aoe", card_color="Arts", self_charge_skill=1, max_self_charge=50, buff_skills=[1, 2, 3]),
    703600: ServantMeta(svt_id=703600, name="宫本武藏(Berserker)", class_name="Berserker", role="dps_aoe", card_color="Arts", self_charge_skill=0, max_self_charge=0, buff_skills=[2, 3]),
    704400: ServantMeta(svt_id=704400, name="千利休", class_name="Berserker", role="dps_aoe", card_color="Quick", self_charge_skill=1, max_self_charge=30, buff_skills=[1, 2, 3]),
    700500: ServantMeta(svt_id=700500, name="兰斯洛特(Berserker)", class_name="Berserker", role="dps_aoe", card_color="Quick", self_charge_skill=0, max_self_charge=0, buff_skills=[3]),
    1100900: ServantMeta(svt_id=1100900, name="太空伊什塔尔", class_name="Avenger", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=50, buff_skills=[1, 3]),
    1101100: ServantMeta(svt_id=1101100, name="迦摩(Avenger)", class_name="Avenger", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=50, buff_skills=[1, 3]),
    1101500: ServantMeta(svt_id=1101500, name="尼托克丽丝〔Alter〕", class_name="Avenger", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=100, buff_skills=[1, 2, 3]),
    2300500: ServantMeta(svt_id=2300500, name="Archetype：Earth", class_name="MoonCancer", role="dps_aoe", card_color="Buster", self_charge_skill=2, max_self_charge=100, buff_skills=[1, 3]),
    2501100: ServantMeta(svt_id=2501100, name="库库尔坎", class_name="Foreigner", role="dps_aoe", card_color="Buster", self_charge_skill=2, max_self_charge=50, buff_skills=[1, 2, 3]),
    2500900: ServantMeta(svt_id=2500900, name="暗之高扬斯卡娅", class_name="Foreigner", role="sub_dps", card_color="Buster", self_charge_skill=1, max_self_charge=30, buff_skills=[1]),
    1001000: ServantMeta(svt_id=1001000, name="芦屋道满", class_name="Alterego", role="sub_dps", card_color="Quick", self_charge_skill=3, max_self_charge=80, buff_skills=[2, 3]),
    1000300: ServantMeta(svt_id=1000300, name="杀生院祈荒(Alterego)", class_name="Alterego", role="dps_aoe", card_color="Arts", self_charge_skill=1, max_self_charge=50, buff_skills=[2, 3]),

    # ==================== 骑阶打手 (克制术阶: QP宝物库 / 术之修炼场) ====================
    403500: ServantMeta(svt_id=403500, name="莱昂纳多·达·芬奇(Rider)", class_name="Rider", role="dps_aoe", card_color="Arts", self_charge_skill=1, max_self_charge=20, buff_skills=[1, 3]),
    404200: ServantMeta(svt_id=404200, name="哈贝特洛特", class_name="Rider", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=80, buff_skills=[2]),
    403800: ServantMeta(svt_id=403800, name="奥德修斯", class_name="Rider", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=30, buff_skills=[1, 2, 3]),
    404800: ServantMeta(svt_id=404800, name="曲亭马琴", class_name="Rider", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=30, buff_skills=[1, 2, 3]),
    400300: ServantMeta(svt_id=400300, name="弗朗西斯·德雷克", class_name="Rider", role="dps_aoe", card_color="Buster", self_charge_skill=3, max_self_charge=50, buff_skills=[1, 3]),
    404300: ServantMeta(svt_id=404300, name="太公望", class_name="Rider", role="dps_aoe", card_color="Quick", self_charge_skill=3, max_self_charge=50, buff_skills=[1, 2, 3]),
    400100: ServantMeta(svt_id=400100, name="美杜莎", class_name="Rider", role="dps_aoe", card_color="Quick", self_charge_skill=3, max_self_charge=20, buff_skills=[2]),

    # ==================== 剑阶打手 (克制枪阶: 枪之修炼场) ====================
    104900: ServantMeta(svt_id=104900, name="千子村正", class_name="Saber", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=50, buff_skills=[1, 3]),
    100100: ServantMeta(svt_id=100100, name="阿尔托莉雅·潘德拉贡(Saber)", class_name="Saber", role="dps_aoe", card_color="Buster", self_charge_skill=3, max_self_charge=30, buff_skills=[1, 2, 3]),
    100900: ServantMeta(svt_id=100900, name="莫德雷德(Saber)", class_name="Saber", role="dps_aoe", card_color="Buster", self_charge_skill=3, max_self_charge=30, buff_skills=[1, 3]),
    104600: ServantMeta(svt_id=104600, name="伊吹童子(Saber)", class_name="Saber", role="dps_aoe", card_color="Buster", self_charge_skill=1, max_self_charge=50, buff_skills=[1, 2, 3]),
    105100: ServantMeta(svt_id=105100, name="冲田总司〔Alter〕(Saber)", class_name="Saber", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=30, buff_skills=[1, 2, 3]),
    105300: ServantMeta(svt_id=105300, name="征氏姐妹", class_name="Saber", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=30, buff_skills=[1, 2, 3]),

    # ==================== 弓阶打手 (克制剑阶: 剑之修炼场) ====================
    200200: ServantMeta(svt_id=200200, name="吉尔伽美什(Archer)", class_name="Archer", role="dps_aoe", card_color="Buster", self_charge_skill=3, max_self_charge=30, buff_skills=[1, 3]),
    202000: ServantMeta(svt_id=202000, name="伊什塔尔(Archer)", class_name="Archer", role="dps_aoe", card_color="Buster", self_charge_skill=2, max_self_charge=50, buff_skills=[1, 2]),
    203200: ServantMeta(svt_id=203200, name="贞德(Archer)", class_name="Archer", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=40, buff_skills=[1, 2, 3]),
    204800: ServantMeta(svt_id=204800, name="杜尔伽", class_name="Archer", role="dps_aoe", card_color="Arts", self_charge_skill=1, max_self_charge=50, buff_skills=[1, 2, 3]),
    201300: ServantMeta(svt_id=201300, name="阿拉什", class_name="Archer", role="sub_dps", card_color="Buster", self_charge_skill=3, max_self_charge=30, buff_skills=[3]),

    # ==================== 枪阶打手 (克制弓阶: 弓之修炼场) ====================
    304000: ServantMeta(svt_id=304000, name="谜之Alterego·Λ", class_name="Lancer", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=60, buff_skills=[1, 2, 3]),
    304700: ServantMeta(svt_id=304700, name="珀西瓦尔", class_name="Lancer", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=20, buff_skills=[1, 2, 3]),
    304800: ServantMeta(svt_id=304800, name="妖精骑士兰斯洛特", class_name="Lancer", role="dps_aoe", card_color="Buster", self_charge_skill=3, max_self_charge=100, buff_skills=[1, 3]),
    303200: ServantMeta(svt_id=303200, name="埃列什基伽勒(Lancer)", class_name="Lancer", role="dps_aoe", card_color="Buster", self_charge_skill=2, max_self_charge=50, buff_skills=[2, 3]),
    303000: ServantMeta(svt_id=303000, name="帕尔瓦蒂", class_name="Lancer", role="dps_aoe", card_color="Quick", self_charge_skill=1, max_self_charge=10, buff_skills=[1, 2]),
    303500: ServantMeta(svt_id=303500, name="瓦尔基里", class_name="Lancer", role="dps_aoe", card_color="Quick", self_charge_skill=3, max_self_charge=20, buff_skills=[1, 3]),

    # ==================== 术阶打手 (克制杀阶: 杀之修炼场) ====================
    501200: ServantMeta(svt_id=501200, name="尼托克丽丝(Caster)", class_name="Caster", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=100, buff_skills=[1, 2]),
    503800: ServantMeta(svt_id=503800, name="齐格", class_name="Caster", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=30, buff_skills=[1, 2, 3]),
    503500: ServantMeta(svt_id=503500, name="阿纳斯塔西娅", class_name="Caster", role="dps_aoe", card_color="Arts", self_charge_skill=3, max_self_charge=50, buff_skills=[1, 2, 3]),
    504400: ServantMeta(svt_id=504400, name="陈宫", class_name="Caster", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=10, buff_skills=[2, 3]),
    505300: ServantMeta(svt_id=505300, name="雨之魔女梣", class_name="Caster", role="dps_aoe", card_color="Buster", self_charge_skill=1, max_self_charge=50, secondary_charge_skill=3, secondary_max_charge=50, buff_skills=[1, 2]),

    # ==================== 杀阶打手 (克制骑阶: 骑之修炼场) ====================
    604700: ServantMeta(svt_id=604700, name="特斯卡特利波卡", class_name="Assassin", role="dps_aoe", card_color="Arts", self_charge_skill=2, max_self_charge=50, buff_skills=[1, 2, 3]),
    603800: ServantMeta(svt_id=603800, name="鬼一法眼", class_name="Assassin", role="dps_aoe", card_color="Quick", self_charge_skill=2, max_self_charge=20, buff_skills=[1, 2, 3]),
}


# 目标关卡的敌方职阶、中文名与标准波次结构映射表
def _load_free_quests_into_profiles() -> None:
    """自动加载 data/free_quests_traits.json 全量自由关卡信息至 QUEST_ENEMY_PROFILES"""
    try:
        from src.core.path_helper import get_resource_path
        fq_path = get_resource_path("data/free_quests_traits.json")
        if not fq_path.exists():
            return
        with open(fq_path, "r", encoding="utf-8") as f:
            quests_data = json.load(f)
        cn_class_map = {
            "saber": "剑阶", "archer": "弓阶", "lancer": "枪阶",
            "rider": "骑阶", "caster": "术阶", "assassin": "杀阶", "berserker": "狂阶"
        }
        for q in quests_data:
            traits = q.get("traits", {})
            class_counts = {k: v for k, v in traits.items() if k in cn_class_map}
            if not class_counts:
                continue
            primary_class = max(class_counts.items(), key=lambda x: x[1])[0].capitalize()
            summary_cn = "/".join(f"{cn_class_map[k]}{v}只" for k, v in class_counts.items())
            prof = {
                "id": q["id"],
                "war": q.get("war", ""),
                "spot": q.get("spot", ""),
                "name": q.get("name", ""),
                "quest_name": f"{q.get('spot', '')} - {q.get('name', '')}",
                "enemy_class": primary_class if len(class_counts) == 1 else "Mixed",
                "enemy_classes": class_counts,
                "enemy_class_cn": summary_cn,
                "waves": [3, 3, 3],
                "desc": f"【{q.get('war', '')}】{q.get('spot', '')} - {q.get('name', '')} (敌方职阶: {summary_cn})"
            }
            QUEST_ENEMY_PROFILES[q["id"]] = prof
            QUEST_ENEMY_PROFILES[q["name"]] = prof
            QUEST_ENEMY_PROFILES[q["spot"]] = prof
            QUEST_ENEMY_PROFILES[f"{q['spot']}_{q['name']}"] = prof
            QUEST_ENEMY_PROFILES[f"{q['war']}_{q['spot']}"] = prof
    except Exception as e:
        import traceback; traceback.print_exc()


QUEST_ENEMY_PROFILES: Dict[str, Dict[str, Any]] = {
    "daily_qp_extreme": {
        "quest_name": "打开宝物库之门 - 极级 (QP本)",
        "enemy_class": "Caster",
        "enemy_class_cn": "术阶 (Caster)",
        "waves": [3, 3, 3],
        "desc": "全3波均为术阶宝物库之门(3-3-3)，敌方蓝卡NP回收补正高达1.2倍，骑阶(2.0倍克制)、狂阶/丑阶(1.5倍克制)最优选。"
    },
    "daily_exp_extreme": {
        "quest_name": "搜集种火 - 极级 (狗粮本)",
        "enemy_class": "Mixed",
        "enemy_class_cn": "狂阶+随机混合职阶",
        "waves": [3, 3, 3],
        "desc": "全3波混合职阶种火之腕(3-3-3)，狂阶(全职阶1.5倍克制)或高宝具Extra阶(仇/月/外)泛用光炮最优选。"
    },
    "daily_training_saber": {
        "quest_name": "剑之修炼场 - 极级",
        "enemy_class": "Saber",
        "enemy_class_cn": "剑阶 (Saber)",
        "waves": [3, 3, 2],
        "desc": "全剑阶敌群，弓阶(2.0倍克制)或狂阶(1.5倍克制)最优选，严禁使用枪阶打手。"
    },
    "daily_training_archer": {
        "quest_name": "弓之修炼场 - 极级",
        "enemy_class": "Archer",
        "enemy_class_cn": "弓阶 (Archer)",
        "waves": [3, 3, 2],
        "desc": "全弓阶敌群，枪阶(2.0倍克制)或狂阶(1.5倍克制)最优选，严禁使用剑阶打手。"
    },
    "daily_training_lancer": {
        "quest_name": "枪之修炼场 - 极级",
        "enemy_class": "Lancer",
        "enemy_class_cn": "枪阶 (Lancer)",
        "waves": [3, 3, 2],
        "desc": "全枪阶敌群，剑阶(2.0倍克制)或狂阶(1.5倍克制)最优选，严禁使用弓阶打手。"
    },
    "daily_training_rider": {
        "quest_name": "骑之修炼场 - 极级",
        "enemy_class": "Rider",
        "enemy_class_cn": "骑阶 (Rider)",
        "waves": [3, 3, 2],
        "desc": "全骑阶敌群，杀阶(2.0倍克制)、狂阶/丑阶(1.5倍克制)最优选，严禁使用术阶打手。"
    },
    "daily_training_caster": {
        "quest_name": "术之修炼场 - 极级",
        "enemy_class": "Caster",
        "enemy_class_cn": "术阶 (Caster)",
        "waves": [3, 3, 2],
        "desc": "全术阶敌群，骑阶(2.0倍克制)、狂阶/丑阶(1.5倍克制)最优选，严禁使用杀阶打手。"
    },
    "daily_training_assassin": {
        "quest_name": "杀之修炼场 - 极级",
        "enemy_class": "Assassin",
        "enemy_class_cn": "杀阶 (Assassin)",
        "waves": [3, 3, 2],
        "desc": "全杀阶敌群，术阶(2.0倍克制)、狂阶/丑阶(1.5倍克制)最优选，严禁使用骑阶打手。"
    },
    "daily_training_berserker": {
        "quest_name": "狂之修炼场 - 极级",
        "enemy_class": "Berserker",
        "enemy_class_cn": "狂阶 (Berserker)",
        "waves": [3, 3, 2],
        "desc": "全狂阶敌群，全职阶均享2.0倍克制伤害（降临者享双向2.0倍克制）。"
    },
}

# 模块加载时自动载入全量周常自由关卡职阶数据
_load_free_quests_into_profiles()


class SupportCapability(BaseModel):
    """单个持有拐的技能练度与充能能力诊断结果"""
    svt_id: int
    name: str
    nick: str
    owned: bool
    lv: int = 0
    skills: List[int] = Field(default_factory=list)
    actual_charge: int = 0
    max_possible_charge: int = 0
    is_core_ready: bool = False
    note: str = ""


class AutoTeamRecommendation(BaseModel):
    """基于账户 JSON 与关卡敌人信息生成的完整自配队决策方案"""
    target_quest_id: str
    slot1_svt_id: int = 0
    battle_mode: str = "loop"
    party_servant_ids: Dict[int, int] = Field(default_factory=dict)
    party_ce_details: Dict[int, Dict[str, Any]] = Field(default_factory=dict)
    laplace: Dict[str, Any] = Field(default_factory=lambda: {"status": "not_checked", "passed": False})
    quest_name: str
    enemy_class: str
    enemy_class_cn: str
    waves: List[int]
    direction: str                   # "Arts", "Buster", "Quick", "MultiCore"
    direction_name: str              # 如 "蓝卡双C呆不换人冲浪3T" / "红卡双杀狐+奥伯龙换人3T"
    direction_reason: str            # 选择该配队方向的详细依据（结合持有拐数量与技能等级）
    slot1_dps: str                   # 1号位主核打手
    slot1_class: str
    slot1_affinity: float            # 职阶克制倍率 (2.0 / 1.5 / 1.0)
    slot1_ce: str                    # 1号位推荐概念礼装 (含词条详情)
    slot1_ce_detail: Dict[str, Any] = Field(default_factory=dict)  # 1号位匹配礼装结构化属性
    slot2_support: str               # 2号位自出拐/副核
    slot2_ce: str = "迦勒底下午茶时光 / 贝拉·丽莎 (加成礼装)"  # 2号位推荐礼装
    slot3_friend_support: str        # 3号位助战拐
    slot4_backline_support: str      # 4号位后排换人拐 (若不换人则为"任意蹭羁绊从者")
    mystic_code: str                 # 推荐御主魔术礼装 (如 "迦勒底战斗服(换人服)" 或 "任意魔术礼装(不换人)")
    supports_audit: List[SupportCapability] = Field(default_factory=list)
    owned_ces_summary: List[Dict[str, Any]] = Field(default_factory=list)
    candidate_dps_ranking: List[Dict[str, Any]] = Field(default_factory=list)
    strategy: TeamStrategy
    turn_descriptions: Dict[int, List[str]] = Field(default_factory=dict)


class AutoTeamBuilder:
    """基于账户从者练度与目标关卡敌人信息的智能自配队器"""

    def __init__(self, chaldea_parser: Optional[ChaldeaParser] = None):
        if chaldea_parser is None:
            try:
                from src.core.path_helper import get_resource_path
                p = ChaldeaParser()
                acct_path = get_resource_path('data/user_account.json')
                if acct_path.exists():
                    p.parse_file(str(acct_path))
                self.parser = p
            except Exception:
                self.parser = chaldea_parser
        else:
            self.parser = chaldea_parser

    @staticmethod
    def calc_multiclass_affinity(attacker_class: str, enemy_classes: Dict[str, int]) -> float:
        """
        根据关卡内多种敌人职阶分布计算攻击方加权克制倍率。
        若攻击方对关卡内任意敌人存在逆克制 (0.5x)，在 3T 连发与周常中严格规避，返回 0.5。
        """
        total_enemies = sum(enemy_classes.values())
        if total_enemies == 0:
            return 1.0
        weighted_sum = 0.0
        for cls_name, cnt in enemy_classes.items():
            aff = AutoTeamBuilder.calc_class_affinity(attacker_class, cls_name.capitalize())
            if aff < 1.0:
                return 0.5
            weighted_sum += aff * cnt
        return round(weighted_sum / total_enemies, 3)

    @staticmethod
    @lru_cache(maxsize=1)
    def _class_relations():
        from src.core.path_helper import get_resource_path
        path = get_resource_path("data/laplace/public/constData.json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["classRelation"]
        return {}

    @staticmethod
    def calc_class_affinity(attacker_class: str, enemy_class: str) -> float:
        """
        计算 FGO 攻击方职阶对敌方职阶的伤害克制倍率
        2.0 = 完全克制, 1.5 = 狂阶/丑阶泛用克制, 1.0 = 白字, 0.5 = 被逆克制
        """
        # 与Laplace使用相同常量，覆盖Extra职阶及狂阶对狂阶的1.5倍等边界。
        ids = {"saber": "1", "archer": "2", "lancer": "3", "rider": "4", "caster": "5",
               "assassin": "6", "berserker": "7", "shielder": "8", "ruler": "9",
               "alterego": "10", "avenger": "11", "mooncancer": "23", "foreigner": "25", "pretender": "28"}
        attacker, enemy = ids.get(attacker_class.lower()), ids.get(enemy_class.lower())
        relation = AutoTeamBuilder._class_relations().get(attacker, {}).get(enemy)
        if relation is not None:
            return relation / 1000
        if enemy_class == "Berserker":
            return 2.0
        if attacker_class == "Berserker":
            return 1.5 if enemy_class != "Foreigner" else 0.5
        if enemy_class == "Mixed":
            if attacker_class in ["Berserker", "Avenger", "MoonCancer", "Foreigner", "Pretender", "Ruler", "Alterego"]:
                return 1.5 if attacker_class == "Berserker" else 1.2
            return 1.0

        counter_map = {
            "Saber": ("Lancer", "Archer"),       # 剑 克 枪(2.0), 被 弓 逆克制(0.5)
            "Archer": ("Saber", "Lancer"),       # 弓 克 剑(2.0), 被 枪 逆克制(0.5)
            "Lancer": ("Archer", "Saber"),       # 枪 克 弓(2.0), 被 剑 逆克制(0.5)
            "Rider": ("Caster", "Assassin"),     # 骑 克 术(2.0), 被 杀 逆克制(0.5)
            "Caster": ("Assassin", "Rider"),     # 术 克 杀(2.0), 被 骑 逆克制(0.5)
            "Assassin": ("Rider", "Caster"),     # 杀 克 骑(2.0), 被 术 逆克制(0.5)
        }
        if attacker_class in counter_map:
            strong_vs, weak_vs = counter_map[attacker_class]
            if enemy_class == strong_vs:
                return 2.0
            if enemy_class == weak_vs:
                return 0.5

        if attacker_class == "Alterego":
            if enemy_class in ["Rider", "Caster", "Assassin"]:
                return 1.5
            if enemy_class in ["Saber", "Archer", "Lancer"]:
                return 0.5

        if attacker_class == "Pretender":
            if enemy_class in ["Saber", "Archer", "Lancer"]:
                return 1.5
            if enemy_class in ["Rider", "Caster", "Assassin"]:
                return 0.5

        return 1.0

    @staticmethod
    def _scale_charge(base_charge: int, max_charge: int, skill_lv: int) -> int:
        """按 FGO 充能技能成长公式计算当前技能等级提供的实际 NP 充能"""
        if skill_lv <= 0:
            return 0
        if skill_lv >= 10:
            return max_charge
        diff = max_charge - base_charge
        return base_charge + int(diff * (skill_lv - 1) / 10)

    def analyze_owned_supports(self) -> Dict[str, SupportCapability]:
        """盘点账号中持有的所有核心拐及其技能等级对应的真实充能幅度 (基于国服官方 Atlas Academy ID)"""
        specs = [
            ("castoria", "C呆(术呆)", 504500, "阿尔托莉雅·卡斯特", 50),
            ("koyan_light", "杀狐", 604200, "光之高扬斯卡娅", 50),
            ("oberon", "奥伯龙", 2800100, "奥伯龙", 70),
            ("skadi_ruler", "水CBA", 901400, "斯卡哈·斯卡蒂(Ruler)", 50),
            ("skadi_caster", "CBA", 503900, "斯卡哈·斯卡蒂(Caster)", 50),
            ("waver", "孔明", 501900, "诸葛孔明", 50),
            ("reines", "司马懿(莱妮丝)", 403200, "莱妮丝", 50),
            ("lady_avalon", "阿瓦隆女士", 2800300, "阿瓦隆女士", 20),
            ("koyan_dark", "暗狐", 2500900, "暗之高扬斯卡娅", 30),
            ("arash", "阿拉什", 201300, "阿拉什", 30),
        ]

        audit: Dict[str, SupportCapability] = {}
        for key, nick, svt_id, kw, max_chg in specs:
            prof = self.parser.get_by_id(svt_id) or self.parser.get_by_name(kw)
            if not prof:
                audit[key] = SupportCapability(
                    svt_id=svt_id, name=kw, nick=nick, owned=False, max_possible_charge=max_chg,
                    note="账号未持有"
                )
                continue

            s1, s2, s3 = prof.skill_lv1, prof.skill_lv2, prof.skill_lv3
            if key == "castoria":
                c1 = self._scale_charge(20, 30, s1)
                c2 = self._scale_charge(10, 20, s2)
                actual = c1 + c2
                ready = (s1 == 10 and s2 == 10 and s3 >= 6)
                note = f"群充{c1}%+单充{c2}% (总充{actual}%), 蓝魔放Lv.{s3}"
            elif key == "koyan_light":
                actual = self._scale_charge(30, 50, s1)
                ready = (s1 == 10)  # 杀狐1技能必须10级才能减2回合CD并充50%
                note = f"单充{actual}% + 减CD({2 if s1 == 10 else 1}回合), 红魔放Lv.{s3}"
            elif key == "oberon":
                c2 = self._scale_charge(30, 50, s2)
                actual = 20 + c2
                ready = (s2 == 10)
                note = f"群充20%+单充{c2}% (总充{actual}%), 宝威翻倍Lv.{s3}"
            elif key in ["skadi_ruler", "skadi_caster"]:
                actual = self._scale_charge(30, 50, s3)
                ready = (s3 == 10)
                note = f"单充{actual}%, 绿魔放Lv.{s1}"
            elif key == "waver":
                actual = 50  # 孔明技能充能固定 20+10+20=50%
                ready = True
                note = "固定总充50% (群充20%+单充30%)"
            elif key == "reines":
                c2 = self._scale_charge(10, 20, s2)
                c3 = self._scale_charge(10, 20, s3)
                actual = 10 + c2 + c3
                ready = (actual >= 50)
                note = f"总充{actual}%"
            elif key == "lady_avalon":
                c2 = self._scale_charge(10, 20, s2)
                actual = c2
                ready = (s2 >= 6)
                note = f"群充{c2}%+蓝魔放+宝威"
            elif key == "arash":
                actual = self._scale_charge(20, 30, s3)
                ready = (s3 >= 6)
                note = f"自充{actual}%(自爆退场)"
            elif key == "koyan_dark":
                actual = self._scale_charge(20, 30, s1)
                ready = (s1 >= 6)
                note = f"全体群充{actual}%+红魔放"
            else:
                actual = max_chg
                ready = prof.is_all_skills_max()
                note = f"充能{actual}%"

            audit[key] = SupportCapability(
                svt_id=prof.svt_id,
                name=prof.name,
                nick=nick,
                owned=True,
                lv=prof.lv,
                skills=[s1, s2, s3],
                actual_charge=actual,
                max_possible_charge=max_chg,
                is_core_ready=ready,
                note=note
            )

        return audit

    def _get_dps_actual_self_charge(self, prof: ServantProfile, meta: ServantMeta) -> int:
        """计算打手当前技能等级下的真实主动自充百分比"""
        if meta.self_charge_skill <= 0 or meta.max_self_charge <= 0:
            return 0
        slv = prof.skills[meta.self_charge_skill - 1]
        if meta.max_self_charge == 30:
            base = 20
        elif meta.max_self_charge == 50:
            base = 30
        elif meta.max_self_charge == 80:
            base = 50
        elif meta.max_self_charge == 100:
            base = 50
        else:
            base = int(meta.max_self_charge * 0.6)
        return self._scale_charge(base, meta.max_self_charge, slv)

    def select_best_craft_essence(
        self, dps_meta: ServantMeta, total_own_np: int, direction: str, min_needed_np: int = 0
    ) -> Tuple[str, float, Dict[str, Any]]:
        return self.rank_craft_essences(dps_meta, total_own_np, direction, min_needed_np)[0]

    def rank_craft_essences(
        self,
        dps_meta: ServantMeta,
        total_own_np: int,
        direction: str,
        min_needed_np: int = 0
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        根据账号已持有的概念礼装库 (self.parser.craft_essences)，为当前打手计算最佳匹配礼装。
        以下评分只安排试算顺序；启动、回收和伤害是否成立仍由Laplace确认。
        评分优先级规则：
        1. 优先宝具初始 NP (initial_np)：保障 T1 启动与 3T 循环充能充裕度 (50%+ 初始 NP 核心加权)；
        2. 优先宝具 NP 回收 (np_gain_rate / np_per_turn)：蓝卡/绿卡冲浪连发稳定性核心指标；
        3. 严格匹配打手色卡 (card_buff_color == dps_meta.card_color)：对应色卡性能提升同时增加宝具伤害与宝具 NP 回收；
        4. 三重复合协同大奖 (Triple Synergy)：若同时具备【50%+初始NP】+【NP回收提升】+【对应色卡性能提升】
           (例如：蓝卡打手使用「海洋飞行员」/「绘画之夏」，红卡打手使用「曲柄启动」，绿卡打手使用「圣诞城的轨迹」)，
           给予最高优先级协同加分 (+65.0)！
        """
        owned_ces = list(self.parser.craft_essences.values())
        if not owned_ces:
            # 账号尚未抓取/导入概念礼装时，返回基于色卡与充能需求的智能理论推荐
            color_cn = {"Arts": "蓝卡", "Buster": "红卡", "Quick": "绿卡"}.get(dps_meta.card_color, "色卡")
            if dps_meta.card_color == "Arts":
                fallback_str = f"优先 50%NP+NP回收+{color_cn}提升 (如: 海洋飞行员 / 绘画之夏)"
            elif dps_meta.card_color == "Buster":
                fallback_str = f"优先 50%NP+NP回收/宝威+{color_cn}提升 (如: 曲柄启动 / 空中骑士)"
            else:
                fallback_str = f"优先 50%NP+NP回收+{color_cn}提升 (如: 圣诞城的轨迹 / 天际旋荡)"
            return [(fallback_str, 0.0, {
                "name": fallback_str,
                "initial_np": 50,
                "np_gain_rate": 10.0,
                "card_buff_color": dps_meta.card_color,
                "card_buff_rate": 10.0,
                "np_damage_up": 0.0,
                "is_owned": False,
            })]

        ranked = []

        for ce in owned_ces:
            # 过滤掉纯 QP/羁绊加成且无任何战斗词条的礼装（除非完全没有战斗礼装）
            if ce.drop_bonus and ce.initial_np == 0 and ce.np_gain_rate == 0 and ce.card_buff_rate == 0 and ce.np_damage_up == 0:
                score = 5.0
            else:
                score = 0.0

                # (1) 优先宝具初始 NP (initial_np)
                if ce.initial_np >= 50:
                    # 50% ~ 100% 初始 NP 属于核心启动礼装
                    score += 55.0 + min(ce.initial_np, 80) * 0.25
                elif ce.initial_np > 0:
                    score += ce.initial_np * 0.8

                # 若打手硬性缺少初始 NP 才能 T1 开出宝具，满足阈值的礼装获额外可行性加分，不满足则扣分
                if min_needed_np > 0:
                    if ce.initial_np >= min_needed_np:
                        score += 45.0
                    else:
                        score -= 80.0

                # (2) 优先宝具 NP 回收 (np_gain_rate & np_per_turn)
                refund_weight = 3.2 if dps_meta.card_color in ["Arts", "Quick"] else 2.2
                if ce.np_gain_rate > 0:
                    score += 28.0 + (ce.np_gain_rate * refund_weight)
                if ce.np_per_turn > 0:
                    score += 18.0 + (ce.np_per_turn * 3.5)

                # (3) 匹配打手色卡性能提升 (card_buff_color == dps_meta.card_color)
                if ce.card_buff_color == dps_meta.card_color and ce.card_buff_rate > 0:
                    score += 38.0 + (ce.card_buff_rate * 2.5)
                elif ce.card_buff_color == "All" and ce.card_buff_rate > 0:
                    score += 22.0 + (ce.card_buff_rate * 1.8)
                elif ce.card_buff_color not in ["None", "All", dps_meta.card_color] and ce.card_buff_rate > 0:
                    # 色卡不匹配（如红卡礼装给蓝卡打手）大幅降权
                    score -= 35.0

                # (4) 宝具威力提升加成
                if ce.np_damage_up > 0:
                    score += min(ce.np_damage_up, 40.0) * 0.75

                # (5) 三重复合词条最高优先级奖励：同时拥有【50%+初始NP】+【NP回收】+【匹配色卡性能提升】
                if (
                    ce.initial_np >= 50
                    and ce.np_gain_rate > 0
                    and ce.card_buff_color == dps_meta.card_color
                    and ce.card_buff_rate > 0
                ):
                    score += 65.0
                # 双重复合词条奖励：同时拥有【50%+初始NP】+【匹配色卡性能提升】+【宝具威力提升】
                elif (
                    ce.initial_np >= 50
                    and ce.np_damage_up > 0
                    and ce.card_buff_color == dps_meta.card_color
                    and ce.card_buff_rate > 0
                ):
                    score += 38.0

                # 等级与满破加分
                if ce.is_mlb:
                    score += 8.0
                score += min(ce.lv, 100) * 0.08

            mlb_tag = "满破" if ce.is_mlb else "未满破"
            display = f"{ce.name} ({mlb_tag} Lv.{ce.lv} | {ce.effect_summary()})"
            detail = {**ce.model_dump(), "effect_summary": ce.effect_summary(),
                      "ce_score": round(score, 1), "is_owned": True}
            ranked.append((display, round(max(0.0, score), 1), detail))
        ranked.sort(key=lambda row: row[2]["ce_score"], reverse=True)
        return ranked

    def craft_essence_candidates(self, dps_meta: ServantMeta, direction: str):
        """保留不同用途的礼装代表；评分只决定试算顺序，不证明能启动或连发。"""
        ranked = [r for r in self.rank_craft_essences(dps_meta, 0, direction)
                  if r[2].get("is_owned")]
        if not ranked:
            return []
        selected = []

        def add(row):
            if row[2]["ce_id"] not in {r[2]["ce_id"] for r in selected}:
                selected.append(row)

        for row in ranked[:2]:
            add(row)
        # 包括A'这类启动+色卡礼装、万华镜、纯NP获得量、每回合NP、黑杯与纯色卡。
        priorities = [
            lambda c: c["initial_np"],
            lambda c: (c["initial_np"] if c["card_buff_color"] in (dps_meta.card_color, "All")
                       and c["card_buff_rate"] > 0 else 0),
            lambda c: c["np_gain_rate"],
            lambda c: c["np_per_turn"],
            lambda c: c["np_damage_up"],
            lambda c: c["np_damage_up"] if c["initial_np"] == 0 else 0,
            lambda c: c["card_buff_rate"] if c["card_buff_color"] in (dps_meta.card_color, "All") else 0,
        ]
        for priority in priorities:
            row = max(ranked, key=lambda r: (priority(r[2]), r[1]))
            if priority(row[2]) > 0:
                add(row)
        return selected  # 最多9张，均为背包已持有的真实等级/满破礼装。

    def select_support_craft_essence(self, target_quest: str) -> str:
        """为 2 号位辅助拐匹配已持有的 QP / 羁绊加成礼装"""
        is_qp = "qp" in target_quest.lower() or "宝物" in target_quest
        preferred_bonus = "QP" if is_qp else "Bond"
        for ce in self.parser.craft_essences.values():
            if ce.drop_bonus == preferred_bonus:
                return f"{ce.name} ({'满破' if ce.is_mlb else '未满破'}: {ce.effect_summary()})"
        for ce in self.parser.craft_essences.values():
            if ce.drop_bonus:
                return f"{ce.name} ({'满破' if ce.is_mlb else '未满破'}: {ce.effect_summary()})"
        return "贝拉·丽莎 (QP加成)" if is_qp else "迦勒底下午茶时光 / 午餐时光 (羁绊加成)"

    def recommend_for_quest(
        self,
        target_quest: str = "daily_qp_extreme",
        war_name: str = "",
        spot_name: str = "",
        quest_id: str = "",
        dps_id: Optional[int] = None,
        ce_id: Optional[int] = None,
        enemy_classes: Optional[Dict[str, int]] = None,
    ) -> AutoTeamRecommendation:
        """
        根据目标关卡敌人职阶（包含自由关卡多职阶加权分布）与账户从者/拐练度，
        计算最优配队方向、克制打手、助战与 3T 技能序列
        """
        q_info = None
        if quest_id.startswith("atlas_cn_") or target_quest.startswith("atlas_cn_"):
            from src.strategy.quest_catalog import quest_profile
            q_info = quest_profile(quest_id or target_quest)
        # 1. 优先以精确 ID / 关卡名 / 地点名命中
        for cand_key in [quest_id, target_quest, spot_name]:
            if q_info is None and cand_key and cand_key in QUEST_ENEMY_PROFILES:
                q_info = QUEST_ENEMY_PROFILES[cand_key]
                break

        # 2. 遍历 profiles 模糊匹配
        if not q_info:
            combined = f"{war_name} {spot_name} {target_quest} {quest_id}".lower()
            for prof in QUEST_ENEMY_PROFILES.values():
                p_name = prof.get("name", "").lower()
                p_spot = prof.get("spot", "").lower()
                p_id = prof.get("id", "").lower()
                if (p_name and p_name in combined) or (p_spot and p_spot in combined) or (p_id and p_id in combined):
                    q_info = prof
                    break

        # 3. 关键字兜底推断
        if not q_info:
            tq_low = f"{war_name} {spot_name} {target_quest}".lower()
            if "qp" in tq_low or "宝物" in tq_low or "术" in tq_low or "caster" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_qp_extreme"]
            elif "剑" in tq_low or "saber" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_training_saber"]
            elif "弓" in tq_low or "archer" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_training_archer"]
            elif "枪" in tq_low or "lancer" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_training_lancer"]
            elif "骑" in tq_low or "rider" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_training_rider"]
            elif "杀" in tq_low or "assassin" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_training_assassin"]
            elif "狂" in tq_low or "berserker" in tq_low:
                q_info = QUEST_ENEMY_PROFILES["daily_training_berserker"]
            else:
                q_info = QUEST_ENEMY_PROFILES["daily_exp_extreme"]

        enemy_class = q_info["enemy_class"]
        enemy_classes = enemy_classes or q_info.get("enemy_classes")
        if enemy_classes:
            enemy_class = next(iter(enemy_classes)).capitalize() if len(enemy_classes) == 1 else "Mixed"
        if ce_id is not None and ce_id not in self.parser.craft_essences:
            raise ValueError("指定礼装不在当前账号背包中")
        waves = q_info.get("waves", [3, 3, 3])
        supports = self.analyze_owned_supports()

        has_castoria = supports["castoria"].owned and supports["castoria"].actual_charge >= 40
        has_koyan = supports["koyan_light"].owned and supports["koyan_light"].is_core_ready
        has_oberon = supports["oberon"].owned and supports["oberon"].actual_charge >= 60
        has_skadi = (
            (supports["skadi_ruler"].owned and supports["skadi_ruler"].is_core_ready)
            or (supports["skadi_caster"].owned and supports["skadi_caster"].is_core_ready)
        )
        has_50_charger = any(
            supports[k].owned and supports[k].actual_charge >= 50
            for k in ["oberon", "waver", "reines", "castoria", "koyan_light", "skadi_ruler", "skadi_caster"]
        )

        # 遍历账号内所有已持有的从者，结合战术库评估每一位可用 AoE 打手
        candidates: List[Dict[str, Any]] = []
        for svt_id, prof in self.parser.servants.items():
            if dps_id is not None and svt_id != dps_id:
                continue
            meta = SERVANT_TACTICAL_DB.get(svt_id)
            if not meta or meta.role == "support":
                continue

            if enemy_classes:
                affinity = self.calc_multiclass_affinity(meta.class_name, enemy_classes)
            else:
                affinity = self.calc_class_affinity(meta.class_name, enemy_class)

            # 严禁选用被敌方逆克制 (0.5x) 的从者作为主核打手
            if affinity < 1.0:
                continue

            self_chg = self._get_dps_actual_self_charge(prof, meta)
            append_chg = prof.get_append_np_charge()
            total_own_np = self_chg + append_chg

            # 针对当前账号拐配置，评估该打手适配的体系方向与得分
            best_dir = "MultiCore"
            dir_score = 0.0
            min_needed_np = 0

            if meta.card_color == "Arts" and has_castoria:
                best_dir = "Arts"
                dir_score = 95.0 if supports["castoria"].is_core_ready else 75.0
                # 双C呆 T1 群充 30+30=60%，若打手被动+自充不足40%则需礼装补足初始NP
                min_needed_np = max(0, 40 - total_own_np)
                if enemy_class in ["Caster", "Assassin", "Berserker"] and waves == [3, 3, 3]:
                    dir_score += 25.0

            elif meta.card_color == "Buster" and has_koyan:
                best_dir = "Buster"
                if has_oberon and total_own_np >= 50:
                    dir_score = 105.0
                    min_needed_np = 0
                elif total_own_np >= 50:
                    dir_score = 88.0
                    min_needed_np = 50
                else:
                    dir_score = 65.0
                    min_needed_np = max(50, 100 - total_own_np)

            elif meta.card_color == "Quick" and has_skadi:
                best_dir = "Quick"
                dir_score = 82.0
                min_needed_np = 50 if total_own_np >= 20 else 80

            else:
                best_dir = "MultiCore"
                dir_score = 50.0 + (25.0 if total_own_np >= 80 else 0.0)
                min_needed_np = 0 if total_own_np >= 80 else 50

            # 将已持有的概念礼装纳入打手与配队方向的协同评分计算
            ce_display, ce_score, ce_detail = self.select_best_craft_essence(
                dps_meta=meta,
                total_own_np=total_own_np,
                direction=best_dir,
                min_needed_np=min_needed_np
            )
            if ce_id is not None:
                ce_display, ce_score, ce_detail = next(row for row in self.rank_craft_essences(
                    meta, total_own_np, best_dir, min_needed_np) if row[2].get("ce_id") == ce_id)

            # 综合评分 = 职阶克制分 + 体系拐契合分 + 概念礼装协同分 + 宝具等级分 + 自充与被动充能分 + 等级白值分
            affinity_score = (affinity * 120.0) + (30.0 if affinity >= 1.5 else 0.0)
            np_score = prof.np_lv * 14.0
            charge_score = (total_own_np * 0.45)
            lv_score = max(0, prof.lv - 80) * 0.6
            ce_synergy_score = round(ce_score * 0.25, 1)

            total_score = round(affinity_score + dir_score + ce_synergy_score + np_score + charge_score + lv_score, 1)

            candidates.append({
                "svt_id": prof.svt_id,
                "name": prof.name,
                "class_name": meta.class_name,
                "card_color": meta.card_color,
                "lv": prof.lv,
                "np_lv": prof.np_lv,
                "skills": prof.skills,
                "append_np": append_chg,
                "self_charge": self_chg,
                "affinity": affinity,
                "direction": best_dir,
                "ce_req": ce_display,
                "ce_detail": ce_detail,
                "ce_synergy_score": ce_synergy_score,
                "score": total_score,
                "meta": meta,
            })

        # 先比较职阶克制；宝具、练度与礼装评分只在同一克制倍率内决定顺序。
        candidates.sort(key=lambda x: (x["affinity"], x["score"]), reverse=True)

        # 若账户为空或无匹配打手，使用默认摩根兜底
        if not candidates:
            if dps_id is not None:
                raise ValueError("指定打手不在当前账号可用候选中")
            fallback_meta = SERVANT_TACTICAL_DB[704000]
            fb_ce_str, _, fb_ce_detail = self.select_best_craft_essence(fallback_meta, 50, "Buster", 0)
            candidates.append({
                "svt_id": 704000,
                "name": "摩根",
                "class_name": "Berserker",
                "card_color": "Buster",
                "lv": 90,
                "np_lv": 1,
                "skills": [10, 10, 10],
                "append_np": 20,
                "self_charge": 50,
                "affinity": 1.5,
                "direction": "Buster",
                "ce_req": fb_ce_str,
                "ce_detail": fb_ce_detail,
                "ce_synergy_score": 0.0,
                "score": 250.0,
                "meta": fallback_meta,
            })

        best = candidates[0]
        best_meta: ServantMeta = best["meta"]
        direction = best["direction"]

        slot1_dps_name = best["name"]
        slot1_class_name = best["class_name"]
        slot1_affinity_val = best["affinity"]

        # 根据选定的最佳方向与打手，生成具体槽位配置与 T1~T3 技能释放方案
        if direction == "Arts":
            c_stat = supports["castoria"]
            o_stat = supports.get("oberon")
            use_oberon_swap = has_oberon
            use_waver_swap = (not use_oberon_swap) and (supports["waver"].owned or supports["reines"].owned)

            if use_oberon_swap:
                direction_name = "蓝卡双C呆+奥伯龙换人 3T 队"
                slot4_sup = "奥伯龙"
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有【{c_stat.name}】(技能 {'/'.join(map(str, c_stat.skills))}，提供{c_stat.actual_charge}%充能与蓝魔放)"
                    f" 与【{o_stat.name}】(70%充能+宝威翻倍)，针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) "
                    f"选用 {best['affinity']}倍克制 蓝卡光炮【{best['name']}】(宝具Lv.{best['np_lv']})，4号位奥伯龙换人进场核弹秒杀关底。"
                )
            elif use_waver_swap:
                sup4_name = "诸葛孔明" if supports["waver"].owned else "莱妮丝"
                direction_name = f"蓝卡双C呆+{sup4_name}换人 3T 队"
                slot4_sup = sup4_name
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有【{c_stat.name}】与50%充能拐【{sup4_name}】，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 选用 {best['affinity']}倍克制 蓝卡光炮【{best['name']}】换人3T连发。"
                )
            else:
                direction_name = "蓝卡双C呆不换人冲浪 3T 队"
                slot4_sup = "任意后排蹭羁绊从者 (无需换人)"
                mystic_code = "任意魔术礼装 (推荐极地服/新年服，无需换人)"
                direction_reason = (
                    f"检测到账号持有【{c_stat.name}】(技能 {'/'.join(map(str, c_stat.skills))}，提供{c_stat.actual_charge}%充能与蓝魔放)，"
                    f"且当前目标【{q_info['quest_name']}】敌方为{q_info['enemy_class_cn']}，"
                    f"选用 {best['affinity']}倍克制 的蓝卡光炮【{best['name']}】(宝具Lv.{best['np_lv']}) 可实现不换人稳定 3T 连发。"
                )

            slot2_sup = "阿尔托莉雅·Caster"
            slot3_sup = "助战 阿尔托莉雅·Caster"

            # Wave 1: 双C呆开启双1技(30+30=60%群充+全队加攻) + 双3技(100%蓝魔放) + 2号位C呆2技(20%单充+30%黄金律)
            t1_skills = [
                SkillAction(servant=2, skill=1),
                SkillAction(servant=2, skill=2, target=1),
                SkillAction(servant=2, skill=3, target=1),
                SkillAction(servant=3, skill=1),
                SkillAction(servant=3, skill=3, target=1),
            ]
            for b_sk in best_meta.buff_skills:
                if b_sk != best_meta.self_charge_skill or best["self_charge"] <= 30:
                    t1_skills.append(SkillAction(servant=1, skill=b_sk))

            # Wave 2: 3号位C呆2技(20%单充+30%黄金律) 补足第一波宝具回收差额
            t2_skills = [SkillAction(servant=3, skill=2, target=1)]

            # Wave 3: 换人服换上奥伯龙/孔明，技能全开，打手自充爆发
            t3_skills: List[SkillAction] = []
            t3_master: List[MasterSkillAction] = []
            if use_oberon_swap:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1),
                    SkillAction(servant=4, skill=2, target=1),
                    SkillAction(servant=4, skill=3, target=1),
                ])
            elif use_waver_swap:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1, target=1),
                    SkillAction(servant=4, skill=2),
                    SkillAction(servant=4, skill=3),
                ])
            else:
                t3_master.append(MasterSkillAction(skill=1))

            if best_meta.self_charge_skill > 0:
                t3_skills.append(SkillAction(servant=1, skill=best_meta.self_charge_skill))

            turns = {
                1: TurnDirective(skills=t1_skills, np_servant=1),
                2: TurnDirective(skills=t2_skills, np_servant=1),
                3: TurnDirective(skills=t3_skills, master_skills=t3_master, np_servant=1),
            }

        elif direction == "Buster":
            k_stat = supports["koyan_light"]
            o_stat = supports["oberon"]
            use_oberon_swap = has_oberon
            use_waver_swap = (not use_oberon_swap) and (supports["waver"].owned or supports["reines"].owned)

            if use_oberon_swap:
                direction_name = "红卡双杀狐+奥伯龙换人 3T 队"
                slot4_sup = "奥伯龙"
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有核心红拐【{k_stat.name}】(技能 {'/'.join(map(str, k_stat.skills))}，满级50%充能+减2回合CD)"
                    f" 与【{o_stat.name}】(技能 {'/'.join(map(str, o_stat.skills))}，70%充能+宝威翻倍)，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 自动选定 {best['affinity']}倍克制 红卡光炮【{best['name']}】"
                    f"(宝具Lv.{best['np_lv']}，自充{best['self_charge']}%+被动{best['append_np']}%) 组成不看回收纯充能 3T 阵容。"
                )
            elif use_waver_swap:
                sup4_name = "诸葛孔明" if supports["waver"].owned else "莱妮丝"
                direction_name = f"红卡双杀狐+{sup4_name}换人 3T 队"
                slot4_sup = sup4_name
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有核心红拐【{k_stat.name}】与50%充能拐【{sup4_name}】，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 选定 {best['affinity']}倍克制 红卡光炮【{best['name']}】换人3T连发。"
                )
            else:
                direction_name = "红卡双杀狐重装 3T 队"
                slot4_sup = "任意后排蹭羁绊从者 (无需换人)"
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有核心红拐【{k_stat.name}】(技能 {'/'.join(map(str, k_stat.skills))}，满级50%充能+减2回合CD)，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 自动选定 {best['affinity']}倍克制 红卡光炮【{best['name']}】组成 3T 阵容。"
                )

            slot2_sup = "光之高扬斯卡娅"
            slot3_sup = "助战 光之高扬斯卡娅"

            # Turn 1: 打手自充+增伤，双杀狐红魔放，自出杀狐50充
            t1_skills: List[SkillAction] = []
            for sk in best_meta.buff_skills:
                t1_skills.append(SkillAction(servant=1, skill=sk))
            if best_meta.self_charge_skill > 0 and best_meta.self_charge_skill not in best_meta.buff_skills:
                t1_skills.append(SkillAction(servant=1, skill=best_meta.self_charge_skill))
            t1_skills.extend([
                SkillAction(servant=2, skill=3, target=1),
                SkillAction(servant=3, skill=3, target=1),
                SkillAction(servant=2, skill=1, target=1),
            ])

            # Turn 2: 助战杀狐1技能50充 + 打手副充能(若有，如摩根2技)
            t2_skills: List[SkillAction] = [SkillAction(servant=3, skill=1, target=1)]
            if best_meta.secondary_charge_skill > 0:
                t2_skills.append(SkillAction(servant=1, skill=best_meta.secondary_charge_skill, target=1))

            # Turn 3: 换人服换上奥伯龙/孔明，技能全开，打手CD转好的自充再次开启
            t3_skills: List[SkillAction] = []
            t3_master: List[MasterSkillAction] = []
            if use_oberon_swap:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1),
                    SkillAction(servant=4, skill=2, target=1),
                    SkillAction(servant=4, skill=3, target=1),
                ])
            elif use_waver_swap:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1, target=1),
                    SkillAction(servant=4, skill=2),
                    SkillAction(servant=4, skill=3),
                ])
            else:
                t3_master.append(MasterSkillAction(skill=1))

            if best_meta.self_charge_skill > 0:
                t3_skills.append(SkillAction(servant=1, skill=best_meta.self_charge_skill))

            turns = {
                1: TurnDirective(skills=t1_skills, np_servant=1),
                2: TurnDirective(skills=t2_skills, np_servant=1),
                3: TurnDirective(skills=t3_skills, master_skills=t3_master, np_servant=1),
            }

        elif direction == "Quick":
            has_both_skadi = (
                supports["skadi_ruler"].owned and supports["skadi_caster"].owned
                and supports["skadi_ruler"].is_core_ready and supports["skadi_caster"].is_core_ready
            )
            use_oberon_swap = (not has_both_skadi) and has_oberon
            use_waver_swap = (not has_both_skadi) and (not use_oberon_swap) and (supports["waver"].owned or supports["reines"].owned)

            if has_both_skadi:
                direction_name = "绿卡三斯卡蒂换人 3T 队"
                slot2_sup = "斯卡哈·斯卡蒂(Ruler)"
                slot3_sup = "助战 斯卡哈·斯卡蒂(Ruler)"
                slot4_sup = "斯卡哈·斯卡蒂(Caster)"
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号同时持有满练度【水CBA】与【术CBA】，组合为顶配三斯卡蒂换人阵容，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 匹配 {best['affinity']}倍克制 绿卡光炮【{best['name']}】，"
                    f"全队可叠加高达 150% 绿魔放 + 150% 单充 + 60% 降防，达成超极限爆发。"
                )
            elif use_oberon_swap:
                sk_stat = supports["skadi_ruler"] if supports["skadi_ruler"].owned else supports["skadi_caster"]
                direction_name = "绿卡双斯卡蒂+奥伯龙换人 3T 队"
                slot2_sup = sk_stat.name
                slot3_sup = f"助战 {sk_stat.name}"
                slot4_sup = "奥伯龙"
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有满练度绿拐【{sk_stat.name}】与【奥伯龙】(70%充能+宝威翻倍)，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 匹配 {best['affinity']}倍克制 绿卡光炮【{best['name']}】换人3T连发。"
                )
            elif use_waver_swap:
                sk_stat = supports["skadi_ruler"] if supports["skadi_ruler"].owned else supports["skadi_caster"]
                sup4_name = "诸葛孔明" if supports["waver"].owned else "莱妮丝"
                direction_name = f"绿卡双斯卡蒂+{sup4_name}换人 3T 队"
                slot2_sup = sk_stat.name
                slot3_sup = f"助战 {sk_stat.name}"
                slot4_sup = sup4_name
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"
                direction_reason = (
                    f"检测到账号持有满练度绿拐【{sk_stat.name}】与50%充能拐【{sup4_name}】，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 匹配 {best['affinity']}倍克制 绿卡光炮【{best['name']}】换人3T连发。"
                )
            else:
                sk_stat = supports["skadi_ruler"] if supports["skadi_ruler"].owned else supports["skadi_caster"]
                direction_name = f"绿卡双{sk_stat.nick}不换人冲浪 3T 队"
                slot2_sup = sk_stat.name
                slot3_sup = f"助战 {sk_stat.name}"
                slot4_sup = "任意后排蹭羁绊从者 (无需换人)"
                mystic_code = "任意魔术礼装"
                direction_reason = (
                    f"检测到账号持有满练度绿拐【{sk_stat.name}】(技能 {'/'.join(map(str, sk_stat.skills))}，50%单充+绿魔放)，"
                    f"针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) 匹配 {best['affinity']}倍克制 绿卡光炮【{best['name']}】。"
                )

            # Turn 1
            t1_skills = [
                SkillAction(servant=2, skill=1, target=1),
                SkillAction(servant=2, skill=3, target=1),
                SkillAction(servant=3, skill=1, target=1),
            ]
            for b_sk in best_meta.buff_skills:
                t1_skills.append(SkillAction(servant=1, skill=b_sk))
            if best_meta.self_charge_skill > 0 and best_meta.self_charge_skill not in best_meta.buff_skills:
                t1_skills.append(SkillAction(servant=1, skill=best_meta.self_charge_skill))

            # Turn 2
            t2_skills = [
                SkillAction(servant=3, skill=3, target=1),
                SkillAction(servant=2, skill=2),
                SkillAction(servant=3, skill=2),
            ]

            # Turn 3
            t3_skills = []
            t3_master = []
            if has_both_skadi:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1, target=1),
                    SkillAction(servant=4, skill=2),
                    SkillAction(servant=4, skill=3, target=1),
                ])
            elif use_oberon_swap:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1),
                    SkillAction(servant=4, skill=2, target=1),
                    SkillAction(servant=4, skill=3, target=1),
                ])
            elif use_waver_swap:
                t3_master.append(MasterSkillAction(skill=3, swap=[2, 4]))
                t3_master.append(MasterSkillAction(skill=1))
                t3_skills.extend([
                    SkillAction(servant=4, skill=1, target=1),
                    SkillAction(servant=4, skill=2),
                    SkillAction(servant=4, skill=3),
                ])
            else:
                t3_master.append(MasterSkillAction(skill=1))

            turns = {
                1: TurnDirective(skills=t1_skills, np_servant=1),
                2: TurnDirective(skills=t2_skills, np_servant=1),
                3: TurnDirective(skills=t3_skills, master_skills=t3_master, np_servant=1),
            }

        else:
            has_arash = supports.get("arash") and supports["arash"].owned and supports["arash"].actual_charge >= 20
            if has_arash and best["name"] != "阿拉什":
                direction_name = "多核阿拉什自爆接力换人流 3T 队"
                direction_reason = (
                    f"检测到账号持有【阿拉什】(自充{supports['arash'].actual_charge}%)，采用经典多核自爆接力战术：\n"
                    f"Wave 1 由阿拉什自爆清场并离场退场，4号位辅助从者自动补位到前排；"
                    f"Wave 2/3 由 {best['affinity']}倍克制 主核【{best['name']}】在多拐增益下连续释放宝具速通关卡。"
                )
                slot1_dps_name = "阿拉什"
                slot1_class_name = "Archer"
                slot1_affinity_val = self.calc_class_affinity("Archer", enemy_class)
                slot2_sup = best["name"]
                slot3_sup = "助战 阿尔托莉雅·Caster / 光之高扬斯卡娅"
                slot4_sup = "诸葛孔明 / 奥伯龙 (4号位补位拐)"
                mystic_code = "迦勒底战斗服 / 决战用迦勒底制服 (换人服)"

                turns = {
                    1: TurnDirective(
                        skills=[SkillAction(servant=1, skill=3)],
                        np_servant=1
                    ),
                    2: TurnDirective(
                        skills=[
                            SkillAction(servant=3, skill=1),
                            SkillAction(servant=3, skill=3, target=2),
                            SkillAction(servant=2, skill=1),
                        ],
                        np_servant=2
                    ),
                    3: TurnDirective(
                        skills=[
                            SkillAction(servant=1, skill=1),
                            SkillAction(servant=1, skill=2, target=2),
                            SkillAction(servant=3, skill=2, target=2),
                        ],
                        master_skills=[MasterSkillAction(skill=1)],
                        np_servant=2
                    ),
                }
            else:
                direction_name = "多核自充 + 助战大拐速刷队"
                direction_reason = (
                    f"当前账号单色体系大拐未全满级，自动切换为【多核自充配队方向】：针对【{q_info['quest_name']}】({q_info['enemy_class_cn']}) "
                    f"选用高克制/高自充打手【{best['name']}】配合借用助战【阿尔托莉雅·Caster / 光之高扬斯卡娅】完成通关。"
                )
                slot1_dps_name = best["name"]
                slot1_class_name = best["class_name"]
                slot1_affinity_val = best["affinity"]
                slot2_sup = "哈贝特洛特 / 诸葛孔明 / 暗之高扬斯卡娅"
                slot3_sup = "助战 阿尔托莉雅·Caster / 光之高扬斯卡娅"
                slot4_sup = "后排主力输出或羁绊从者"
                mystic_code = "迦勒底战斗服"
                turns = {
                    1: TurnDirective(
                        skills=[SkillAction(servant=1, skill=1), SkillAction(servant=2, skill=2), SkillAction(servant=3, skill=1)],
                        np_servant=1
                    ),
                    2: TurnDirective(
                        skills=[SkillAction(servant=3, skill=2, target=1), SkillAction(servant=3, skill=3, target=1)],
                        np_servant=1
                    ),
                    3: TurnDirective(
                        master_skills=[MasterSkillAction(skill=1)],
                        np_servant=1
                    ),
                }

        strategy = TeamStrategy(
            id=f"auto_{direction.lower()}_{best['svt_id']}_{target_quest}",
            name=f"【智能自配】{best['name']} - {direction_name}",
            match_servants=[best["name"], slot2_sup.split(" ")[0]],
            required_min_np_charge=best["append_np"],
            mystic_code_id=20 if any(turn.master_skills for turn in turns.values()) else None,
            turns=turns
        )

        # 生成人类可读的 T1~T3 步骤描述
        from src.strategy.battle_plans import BattlePlanManager
        pm = BattlePlanManager()
        turn_descs = {
            t: pm.format_turn_instructions(strategy, t)
            for t in [1, 2, 3]
        }

        clean_candidates = [
            {k: v for k, v in c.items() if k != "meta"}
            for c in candidates
        ]

        owned_ces_list = [
            {
                "ce_id": ce.ce_id,
                "name": ce.name,
                "lv": ce.lv,
                "is_mlb": ce.is_mlb,
                "initial_np": ce.initial_np,
                "np_gain_rate": ce.np_gain_rate,
                "np_damage_up": ce.np_damage_up,
                "card_buff_color": ce.card_buff_color,
                "card_buff_rate": ce.card_buff_rate,
                "effect_summary": ce.effect_summary(),
            }
            for ce in self.parser.craft_essences.values()
        ]
        slot2_ce_str = self.select_support_craft_essence(target_quest)
        ce_detail = best.get("ce_detail", {})
        if ce_detail.get("is_owned"):
            direction_reason += f" 使用账号实际礼装【{best['ce_req']}】，由Laplace同时校验启动、NP回收与伤害。"

        return AutoTeamRecommendation(
            target_quest_id=quest_id or target_quest,
            slot1_svt_id=best["svt_id"],
            quest_name=q_info["quest_name"],
            enemy_class=enemy_class,
            enemy_class_cn=q_info["enemy_class_cn"],
            waves=waves,
            direction=direction,
            direction_name=direction_name,
            direction_reason=direction_reason,
            slot1_dps=slot1_dps_name,
            slot1_class=slot1_class_name,
            slot1_affinity=slot1_affinity_val,
            slot1_ce=best["ce_req"],
            slot1_ce_detail=ce_detail,
            slot2_support=slot2_sup,
            slot2_ce=slot2_ce_str,
            slot3_friend_support=slot3_sup,
            slot4_backline_support=slot4_sup,
            mystic_code=mystic_code,
            supports_audit=list(supports.values()),
            owned_ces_summary=owned_ces_list,
            candidate_dps_ranking=clean_candidates,
            strategy=strategy,
            turn_descriptions=turn_descs,
        )
