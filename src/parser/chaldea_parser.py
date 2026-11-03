"""
Chaldea & FGO Account Data Parser
解析并管理用户导入的从者练度、宝具、主动技能及附加技能数据。
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel, Field


from src.core.path_helper import get_resource_path


class ServantProfile(BaseModel):
    """单个从者练度档案"""
    svt_id: int
    name: str = "未知从者"
    lv: int = 1
    np_lv: int = 1
    skill_lv1: int = 1
    skill_lv2: int = 1
    skill_lv3: int = 1
    append_skill_lv1: int = Field(default=0, ge=0, le=10)
    append_skill_lv2: int = Field(default=0, ge=0, le=10)  # 魔力装填
    append_skill_lv3: int = Field(default=0, ge=0, le=10)
    append_skill_lv4: int = Field(default=0, ge=0, le=10)
    append_skill_lv5: int = Field(default=0, ge=0, le=10)
    fou: int = 0
    atk_fou: Optional[int] = None
    hp_fou: Optional[int] = None
    card_id: Optional[int] = None
    ascension: Optional[int] = None
    costume_id: Optional[int] = None
    skill_ids: List[int] = Field(default_factory=list)
    np_id: Optional[int] = None
    in_storage: bool = False

    @property
    def skills(self) -> List[int]:
        return [self.skill_lv1, self.skill_lv2, self.skill_lv3]

    @property
    def append_skills(self) -> List[int]:
        return [self.append_skill_lv1, self.append_skill_lv2, self.append_skill_lv3,
                self.append_skill_lv4, self.append_skill_lv5]

    def is_all_skills_max(self) -> bool:
        """主动技能是否 10/10/10"""
        return self.skill_lv1 == 10 and self.skill_lv2 == 10 and self.skill_lv3 == 10

    def get_append_np_charge(self) -> int:
        """
        获取被动 2 技能(魔力装填)带来的初始 NP 百分比
        FGO 机制: 1级=10%, 2~9级每级+1%(9级=18%), 10级质变=20%
        """
        if self.append_skill_lv2 <= 0:
            return 0
        if self.append_skill_lv2 >= 10:
            return 20
        return 9 + self.append_skill_lv2

    def has_max_append_np(self) -> bool:
        """被动充能是否满 20%"""
        return self.append_skill_lv2 >= 10


class CraftEssenceProfile(BaseModel):
    """单个概念礼装练度与词条属性档案"""
    ce_id: int
    name: str = "未知概念礼装"
    lv: int = 1
    is_mlb: bool = True                # 是否满破 (最大解放 limitCount >= 4)
    initial_np: int = 0                # 初始宝具 NP (%)，如 50, 75, 80, 100
    np_gain_rate: float = 0.0          # 宝具 NP 回收 / NP获得量提升 (%)
    np_damage_up: float = 0.0          # 宝具威力提升 (%)
    card_buff_color: str = "None"      # 色卡性能提升类型: "Arts"(蓝), "Buster"(红), "Quick"(绿), "All"(多色), "None"
    card_buff_rate: float = 0.0        # 色卡性能提升幅度 (%)
    np_per_turn: int = 0               # 每回合获得 NP (%)
    drop_bonus: str = ""               # 掉落加成: "QP", "Bond", ""

    def effect_summary(self) -> str:
        parts = []
        if self.initial_np > 0:
            parts.append(f"初始NP+{self.initial_np}%")
        if self.np_gain_rate > 0:
            parts.append(f"NP回收+{self.np_gain_rate:g}%")
        if self.np_per_turn > 0:
            parts.append(f"每回合NP+{self.np_per_turn}%")
        if self.card_buff_color != "None" and self.card_buff_rate > 0:
            c_map = {"Arts": "蓝卡", "Buster": "红卡", "Quick": "绿卡", "All": "多色卡"}
            parts.append(f"{c_map.get(self.card_buff_color, self.card_buff_color)}+{self.card_buff_rate:g}%")
        if self.np_damage_up > 0:
            parts.append(f"宝威+{self.np_damage_up:g}%")
        if self.drop_bonus:
            parts.append(f"{self.drop_bonus}加成")
        return " & ".join(parts) if parts else "白值礼装"


# 常用核心概念礼装词条元数据库 (满破 MLB 数值与未满破数值自动折算，启动时自动合并 data/craft_essences.json 全量 2505 张国服礼装)
CRAFT_ESSENCE_CATALOG: Dict[int, Dict[str, Any]] = {
    # 100% / 80% 初始 NP 礼装 (国服官方 Atlas Academy ID)
    9400340: {"name": "万华镜", "initial_np": 100, "non_mlb_np": 80, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0},
    9405320: {"name": "魔道元帅", "initial_np": 100, "non_mlb_np": 80, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0},
    9400280: {"name": "虚数魔术", "initial_np": 75, "non_mlb_np": 60, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0},
    9400180: {"name": "龙脉", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0},

    # 蓝卡 (Arts) + 50% NP + NP回收 / 宝威 复合礼装 (蓝卡队最优)
    9403340: {"name": "绘画之夏", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 10, "np_damage_up": 0, "card_buff_color": "Arts", "card_buff_rate": 10},
    9405300: {"name": "海上飞行者", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 0, "np_damage_up": 15, "card_buff_color": "Arts", "card_buff_rate": 8},
    9402610: {"name": "潜入湛蓝", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 0, "np_damage_up": 10, "card_buff_color": "Arts", "card_buff_rate": 10},
    9401850: {"name": "苍玉之魔法少女", "initial_np": 50, "non_mlb_np": 40, "np_gain_rate": 30, "np_damage_up": 0, "card_buff_color": "Arts", "card_buff_rate": 0},
    9403620: {"name": "正月的神秘", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "Arts", "card_buff_rate": 8, "np_per_turn": 5},
    9400310: {"name": "元素转换", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "Arts", "card_buff_rate": 30},

    # 红卡 (Buster) + 50% NP + 宝威 / NP回收 复合礼装 (红卡队最优)
    9402750: {"name": "天际旋荡", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 0, "np_damage_up": 10, "card_buff_color": "Buster", "card_buff_rate": 10},
    9401640: {"name": "黄金相扑～岩场～", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 0, "np_damage_up": 15, "card_buff_color": "Buster", "card_buff_rate": 0},
    9402420: {"name": "我们的征途是星辰大海", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 20, "np_damage_up": 0, "card_buff_color": "Buster", "card_buff_rate": 20},

    # 绿卡 (Quick) + 50% NP + NP回收 / 宝威 复合礼装 (绿卡队最优)
    9404220: {"name": "圣诞节的轨迹", "initial_np": 50, "non_mlb_np": 30, "np_gain_rate": 10, "np_damage_up": 0, "card_buff_color": "Quick", "card_buff_rate": 10},

    # 泛用宝威 / 每回合NP / NP回收 礼装
    9400350: {"name": "天之杯", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 50, "card_buff_color": "None", "card_buff_rate": 0},
    9400480: {"name": "黑之圣杯", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 80, "card_buff_color": "None", "card_buff_rate": 0},
    9400400: {"name": "宇宙棱镜", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0, "np_per_turn": 10},
    9402540: {"name": "小小夏日", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 30, "np_damage_up": 15, "card_buff_color": "None", "card_buff_rate": 0},

    # 羁绊 / QP 加成礼装 (拐与后排优先挂载)
    9403690: {"name": "贝拉·丽莎", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0, "drop_bonus": "QP"},
    9400780: {"name": "蒙娜丽莎", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0, "drop_bonus": "QP"},
    9403520: {"name": "迦勒底午茶时光", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0, "drop_bonus": "Bond"},
    9401970: {"name": "迦勒底午餐时光", "initial_np": 0, "non_mlb_np": 0, "np_gain_rate": 0, "np_damage_up": 0, "card_buff_color": "None", "card_buff_rate": 0, "drop_bonus": "Bond"},
}


def _load_full_craft_essence_catalog() -> None:
    """加载 data/craft_essences.json 中的全量国服概念礼装词条库"""
    try:
        ce_path = get_resource_path("data/craft_essences.json")
        if ce_path.exists():
            with open(ce_path, "r", encoding="utf-8") as f:
                raw_db = json.load(f)
            for cid_str, info in raw_db.items():
                cid = int(cid_str)
                if cid not in CRAFT_ESSENCE_CATALOG:
                    CRAFT_ESSENCE_CATALOG[cid] = info
                else:
                    # 保留手工精选词条补充的同时，确保名称与初始NP准确
                    merged = dict(info)
                    merged.update(CRAFT_ESSENCE_CATALOG[cid])
                    CRAFT_ESSENCE_CATALOG[cid] = merged
    except Exception:
        pass


_load_full_craft_essence_catalog()


class ChaldeaParser:
    """迦勒底及 FGO 导出账户数据解析器（同时管理从者与概念礼装）"""

    def __init__(self, names_dict_path: Optional[str | Path] = None):
        self.names_path = Path(names_dict_path or get_resource_path("data/servant_names.json"))
        self.id_to_name: Dict[str, str] = {}
        self.servants: Dict[int, ServantProfile] = {}
        self.storage_servants: Dict[int, ServantProfile] = {}
        self.servant_copies: List[ServantProfile] = []
        self.quest_records: Dict[int, Dict[str, int]] = {}
        self.quest_records_complete = False
        self.mystic_codes: Dict[int, int] = {}
        self.mystic_codes_complete = False
        self.craft_essences: Dict[int, CraftEssenceProfile] = {}
        self.storage_craft_essences: Dict[int, CraftEssenceProfile] = {}
        self.load_names()

    def load_names(self) -> None:
        """加载从者 ID 对照表"""
        if self.names_path.exists():
            with open(self.names_path, "r", encoding="utf-8") as f:
                self.id_to_name = json.load(f)

    def _resolve_name(self, svt_id: int, fallback_name: Optional[str] = None) -> str:
        """根据 ID 翻译从者中文名称"""
        str_id = str(svt_id)
        if str_id in self.id_to_name:
            return self.id_to_name[str_id]
        if fallback_name and fallback_name.strip():
            return fallback_name
        return f"从者_{svt_id}"

    def parse_file(self, file_path: str | Path) -> Dict[int, ServantProfile]:
        """
        解析账户 JSON 文件，自适应格式：
        1. Chaldea userdata.json (带 "servants" 键)
        2. FGO 抓包导出的账户文件 (带 "userServant" 或 "userSvt" 键)
        3. 通用从者对象列表
        """
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"Account file not found: {p}")

        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)

        return self.parse_data(data)

    @staticmethod
    def _extract_raw_payload(data: Any) -> Any:
        """
        支持从以下多种抓包/导出源自动解包：
        1. 标准 JSON 字典 / 列表
        2. FGO 官方 toplogin 原始响应结构: data["cache"]["replaced"]["userSvt"] 或 data["cache"]["updated"]["userSvt"]
        3. Charles / Fiddler / Reqable / HttpCanary 导出的 .har 抓包归档文件 (data["log"]["entries"])
        4. Base64 编码或 URL 编码的原始抓包响应体字符串
        """
        import base64
        import gzip
        from urllib.parse import unquote

        if isinstance(data, str):
            s = data.strip()
            if "%" in s[:80] or s.endswith("%3D") or s.endswith("%3d"):
                s = unquote(s).strip()
            if not (s.startswith("{") or s.startswith("[")):
                try:
                    # 补齐 Base64 padding
                    pad = len(s) % 4
                    if pad:
                        s_padded = s + ("=" * (4 - pad))
                    else:
                        s_padded = s
                    raw_b = base64.b64decode(s_padded)
                    if raw_b[:2] == b"\x1f\x8b":
                        raw_b = gzip.decompress(raw_b)
                    decoded = raw_b.decode("utf-8", errors="ignore").strip()
                    if decoded.startswith("{") or decoded.startswith("["):
                        s = decoded
                except Exception:
                    pass
            try:
                data = json.loads(s)
            except Exception:
                return data

        if isinstance(data, dict):
            # 兼容 FGO 原始抓包 toplogin 的 cache.replaced.userSvt / cache.updated.userSvt 结构
            if "cache" in data and isinstance(data["cache"], dict):
                cache = data["cache"]
                combined = {}
                keys = {"userSvt": "id", "userServant": "id", "userSvtStorage": "id",
                        "userQuest": "questId", "userSvtAppendPassiveSkill": "svtId",
                        "userSvtAppendPassiveSkillLv": "userSvtId"}
                for sub_key in ["replaced", "updated"]:
                    sub = cache.get(sub_key)
                    if not isinstance(sub, dict):
                        continue
                    for table, rows in sub.items():
                        if table in keys and isinstance(rows, list) and table in combined:
                            key = keys[table]
                            old_rows = combined[table]
                            if all(isinstance(r, dict) and key in r for r in old_rows + rows):
                                merged = {str(r[key]): r for r in old_rows}
                                merged.update({str(r[key]): r for r in rows})
                                rows = list(merged.values())
                        combined[table] = rows
                if any(k in combined for k in ("userSvt", "userServant", "userSvtStorage")):
                    combined["quest_records_complete"] = "userQuest" in cache.get("replaced", {})
                    return combined

            # 兼容 .har (HTTP Archive) 抓包文件
            if "log" in data and isinstance(data["log"], dict) and "entries" in data["log"]:
                for entry in data["log"].get("entries", []):
                    resp = entry.get("response", {})
                    content = resp.get("content", {})
                    text = content.get("text", "")
                    if not text:
                        continue
                    if content.get("encoding") == "base64":
                        try:
                            text = base64.b64decode(text).decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                    extracted = ChaldeaParser._extract_raw_payload(text)
                    if isinstance(extracted, dict) and any(k in extracted for k in (
                            "userSvt", "userServant", "userSvtStorage", "servants", "servant_copies")):
                        return extracted
                    if isinstance(extracted, list) and any(isinstance(row, dict) and
                            any(k in row for k in ("svtId", "svt_id")) for row in extracted):
                        return extracted

        return data

    def _upsert_servant(self, profile: ServantProfile) -> None:
        """写入从者档案；若存在多宝/二号机同名ID，保留练度最高（等级+技能总和+宝具等级最高）的一骑"""
        if profile.svt_id >= 8000000 or profile.svt_id < 100000:
            return
        self.servant_copies.append(profile)
        target = self.storage_servants if profile.in_storage else self.servants
        existing = target.get(profile.svt_id)
        if existing is None or (profile.lv, sum(profile.skills), profile.np_lv) > (existing.lv, sum(existing.skills), existing.np_lv):
            target[profile.svt_id] = profile

    def export_data(self) -> Dict[str, Any]:
        """保存配队所需账户状态，不包含登录会话或个人身份字段。"""
        copies = self.servant_copies or list(self.servants.values()) + list(self.storage_servants.values())
        return {"account_schema": 2,
                "servant_copies": [s.model_dump() for s in copies],
                "quest_records_complete": self.quest_records_complete,
                "userQuest": list(self.quest_records.values()),
                "mystic_codes": self.mystic_codes, "mystic_codes_complete": self.mystic_codes_complete,
                "craft_essences": {str(cid): ce.model_dump() for cid, ce in self.craft_essences.items()},
                "storage_craft_essences": {str(cid): ce.model_dump() for cid, ce in self.storage_craft_essences.items()}}

    @staticmethod
    def _chaldea_fou(cur, stat):
        prefix = "fou" + stat
        if not any(key in cur for key in (prefix, prefix + "3", prefix + "5")):
            return None
        if int(cur.get(prefix + "5", 0)) > 0:
            return 2000 + int(cur[prefix + "5"]) * 100
        if int(cur.get(prefix, 0)) > 0:
            return 1000 + int(cur[prefix]) * 20
        return int(cur.get(prefix + "3", 20)) * 50

    @staticmethod
    def _numbered_append_levels(numbers, levels) -> Dict[int, int]:
        if not isinstance(numbers, list) or not isinstance(levels, list) or len(numbers) != len(levels):
            raise ValueError("附加技能编号与等级记录不匹配")
        result = {}
        for number, level in zip(numbers, levels):
            index, value = int(number) - 100, int(level or 0)
            if not 0 <= index < 5 or not 0 <= value <= 10:
                raise ValueError("附加技能编号或等级超出有效范围")
            result[index] = value
        return result

    def _upsert_craft_essence(self, ce: CraftEssenceProfile, in_storage: bool = False) -> None:
        """写入概念礼装档案；若有同名礼装，优先保留满破 (is_mlb=True) 且等级最高的一张"""
        target = self.storage_craft_essences if in_storage else self.craft_essences
        existing = target.get(ce.ce_id)
        if existing is None or (ce.is_mlb, ce.initial_np, ce.lv) > (existing.is_mlb, existing.initial_np, existing.lv):
            target[ce.ce_id] = ce

    def _build_ce_from_item(self, ce_id: int, item: Dict[str, Any]) -> Optional[CraftEssenceProfile]:
        """根据礼装 ID 或字典属性构建 CraftEssenceProfile"""
        limit_cnt = item.get("limitCount", item.get("lb", 4 if item.get("is_mlb", True) else 0))
        is_mlb = bool(item.get("is_mlb", int(limit_cnt) >= 4))
        lv = int(item.get("lv", 20 if not is_mlb else 80))

        cat = CRAFT_ESSENCE_CATALOG.get(ce_id)
        if cat:
            init_np = cat["initial_np"] if is_mlb else cat.get("non_mlb_np", cat["initial_np"])
            scale = 1.0 if is_mlb else 0.8
            np_gain = round(cat.get("np_gain_rate", 0) * scale, 1)
            np_dmg = round(cat.get("np_damage_up", 0) * scale, 1)
            c_color = cat.get("card_buff_color", "None")
            c_rate = round(cat.get("card_buff_rate", 0) * scale, 1)
            np_turn = int(cat.get("np_per_turn", 0))
            drop_b = cat.get("drop_bonus", "")
            has_effect = (init_np > 0 or np_gain > 0 or np_dmg > 0 or c_rate > 0 or np_turn > 0 or bool(drop_b))
            if not has_effect and lv < 15 and "is_mlb" not in item:
                return None
            return CraftEssenceProfile(
                ce_id=ce_id,
                name=cat["name"],
                lv=lv,
                is_mlb=is_mlb,
                initial_np=int(init_np),
                np_gain_rate=float(np_gain),
                np_damage_up=float(np_dmg),
                card_buff_color=c_color,
                card_buff_rate=float(c_rate),
                np_per_turn=np_turn,
                drop_bonus=drop_b,
            )

        # 若为自定义礼装或带有显式词条字段的礼装对象
        if any(k in item for k in ["initial_np", "np_gain_rate", "card_buff_color", "np_damage_up", "name"]):
            return CraftEssenceProfile(
                ce_id=ce_id,
                name=item.get("name", f"概念礼装_{ce_id}"),
                lv=lv,
                is_mlb=is_mlb,
                initial_np=int(item.get("initial_np", 0)),
                np_gain_rate=float(item.get("np_gain_rate", 0.0)),
                np_damage_up=float(item.get("np_damage_up", 0.0)),
                card_buff_color=item.get("card_buff_color", "None"),
                card_buff_rate=float(item.get("card_buff_rate", 0.0)),
                np_per_turn=int(item.get("np_per_turn", 0)),
                drop_bonus=item.get("drop_bonus", ""),
            )
        return None

    def parse_ce_from_ocr_text(self, all_text: str) -> List[str]:
        """
        从实机【概念礼装选择】或【编队】画面的 OCR 文本中提取概念礼装名称及词条效果
        例如识别到：“正月的神秘 自身的迅指令卡性能提升8% & 技指令卡性能提升8% & 付与每回合宝具值增加5%的状态[最大解放]”
        """
        import re
        added: List[str] = []
        # 1. 匹配内置礼装目录中的礼装名称
        for cid, cat in CRAFT_ESSENCE_CATALOG.items():
            clean_name = cat["name"].split("(")[0].split("～")[0]
            if len(clean_name) >= 3 and clean_name in all_text:
                is_mlb = ("最大解放" in all_text) or True
                ce = self._build_ce_from_item(cid, {"is_mlb": is_mlb, "lv": 80})
                if ce:
                    self._upsert_craft_essence(ce)
                    added.append(f"{ce.name}({ce.effect_summary()})")

        # 2. 动态解析画面上展开的礼装词条描述 (如 "50%NP" / "NP获得量提升10%" / "技指令卡性能提升8%")
        init_np = 0
        m_np = re.search(r"(\d+)%\s*NP|NP\s*(\d+)%|宝具值[以]?(\d+)%", all_text, re.IGNORECASE)
        if m_np:
            init_np = int(next(g for g in m_np.groups() if g is not None))

        np_gain = 0.0
        m_gain = re.search(r"(?:NP获得量|宝具值获得量)提升\s*(\d+)%", all_text)
        if m_gain:
            np_gain = float(m_gain.group(1))

        np_dmg = 0.0
        m_dmg = re.search(r"宝具威力提升\s*(\d+)%", all_text)
        if m_dmg:
            np_dmg = float(m_dmg.group(1))

        np_turn = 0
        m_turn = re.search(r"每回合(?:宝具值|NP)增加\s*(\d+)%", all_text)
        if m_turn:
            np_turn = int(m_turn.group(1))

        color = "None"
        color_rate = 0.0
        if any(k in all_text for k in ["Arts", "技指令卡", "蓝卡"]):
            color = "Arts"
            m_c = re.search(r"(?:Arts|技)指令卡性能提升\s*(\d+)%", all_text)
            color_rate = float(m_c.group(1)) if m_c else 10.0
        elif any(k in all_text for k in ["Buster", "力指令卡", "红卡"]):
            color = "Buster"
            m_c = re.search(r"(?:Buster|力)指令卡性能提升\s*(\d+)%", all_text)
            color_rate = float(m_c.group(1)) if m_c else 10.0
        elif any(k in all_text for k in ["Quick", "迅指令卡", "绿卡"]):
            color = "Quick"
            m_c = re.search(r"(?:Quick|迅)指令卡性能提升\s*(\d+)%", all_text)
            color_rate = float(m_c.group(1)) if m_c else 10.0

        if not added and (init_np > 0 or np_gain > 0 or color != "None" or np_turn > 0):
            dyn_id = 9900000 + init_np * 10 + int(np_gain) + (1 if color == "Arts" else 2 if color == "Buster" else 3)
            dyn_ce = CraftEssenceProfile(
                ce_id=dyn_id,
                name="实机扫描概念礼装",
                lv=80,
                is_mlb=("最大解放" in all_text),
                initial_np=init_np,
                np_gain_rate=np_gain,
                np_damage_up=np_dmg,
                card_buff_color=color,
                card_buff_rate=color_rate,
                np_per_turn=np_turn,
            )
            self._upsert_craft_essence(dyn_ce)
            added.append(f"{dyn_ce.name}({dyn_ce.effect_summary()})")

        return added

    def parse_data(self, data: Any) -> Dict[int, ServantProfile]:
        fields = ("servants", "storage_servants", "servant_copies", "craft_essences",
                  "quest_records", "quest_records_complete", "mystic_codes", "mystic_codes_complete", "storage_craft_essences")
        previous = {field: getattr(self, field) for field in fields}
        try:
            return self._parse_data(data)
        except Exception:
            for field, value in previous.items():
                setattr(self, field, value)
            raise

    def _parse_data(self, data: Any) -> Dict[int, ServantProfile]:
        """从抓包响应、HAR归档、Chaldea导号字典或列表结构中同时提取从者与概念礼装"""
        data = self._extract_raw_payload(data)
        previous = (self.servants, self.storage_servants, self.servant_copies, self.craft_essences,
                    self.quest_records, self.quest_records_complete, self.mystic_codes, self.mystic_codes_complete,
                    self.storage_craft_essences)
        self.servants = {}
        self.storage_servants = {}
        self.servant_copies = []
        self.craft_essences = {}
        self.storage_craft_essences = {}
        self.quest_records = {}
        self.quest_records_complete = False
        self.mystic_codes = {}
        self.mystic_codes_complete = False

        # 解析显式的 craft_essences / craftEssences 字典或列表
        if isinstance(data, dict):
            self.quest_records_complete = bool(data.get("quest_records_complete", "userQuest" in data))
            self.mystic_codes_complete = bool(data.get("mystic_codes_complete", "userEquip" in data))
            self.mystic_codes = {int(key): int(lv) for key, lv in data.get("mystic_codes", {}).items()}
            self.mystic_codes.update({int(row["equipId"]): int(row["lv"]) for row in data.get("userEquip", []) or []})
            for row in data.get("userQuest", []) or []:
                qid = int(row["questId"])
                self.quest_records[qid] = {"questId": qid, "questPhase": int(row.get("questPhase", 0)),
                                           "clearNum": int(row.get("clearNum", 0)), "status": int(row.get("status", 0))}
            raw_ces = data.get("craft_essences") or data.get("craftEssences") or data.get("equipments")
            if isinstance(raw_ces, dict):
                for cid_str, c_info in raw_ces.items():
                    try:
                        cid = int(cid_str)
                        ce = self._build_ce_from_item(cid, c_info if isinstance(c_info, dict) else {})
                        if ce:
                            self._upsert_craft_essence(ce)
                    except Exception:
                        pass
            elif isinstance(raw_ces, list):
                for c_info in raw_ces:
                    if isinstance(c_info, dict):
                        cid = int(c_info.get("ce_id") or c_info.get("svtId") or c_info.get("id") or 0)
                        if cid > 0:
                            ce = self._build_ce_from_item(cid, c_info)
                            if ce:
                                self._upsert_craft_essence(ce)
            for cid, info in (data.get("storage_craft_essences") or {}).items():
                ce = self._build_ce_from_item(int(cid), info)
                if ce:
                    self._upsert_craft_essence(ce, in_storage=True)

        # 格式 1: 官方抓包数据 (toplogin cache.replaced.userSvt / userServant)
        if isinstance(data, dict) and data.get("account_schema") == 2:
            for row in data.get("servant_copies", []):
                self._upsert_servant(ServantProfile.model_validate(row))
        elif isinstance(data, dict) and any(k in data for k in ("userServant", "userSvt", "userSvtStorage")):
            raw_list = [(item, False) for item in (data.get("userServant") or data.get("userSvt", []))]
            raw_list += [(item, True) for item in data.get("userSvtStorage", []) or []]
            # 提取 toplogin 中单独下发的附加技能表 userSvtAppendPassiveSkill
            append_by_svt_id: Dict[int, List[int]] = {}
            unlock_by_svt_id: Dict[int, List[int]] = {}
            for app_entry in data.get("userSvtAppendPassiveSkill", []) or []:
                if isinstance(app_entry, dict):
                    asid = int(app_entry.get("svtId") or 0)
                    slvs = app_entry.get("skillLvs") or app_entry.get("appendPassiveSkillLvs") or []
                    if asid > 0 and isinstance(slvs, list):
                        append_by_svt_id[asid] = [int(x or 0) for x in slvs]
                    if asid > 0:
                        unlock_by_svt_id[asid] = [int(n) - 100 for n in app_entry.get("unlockNums", []) or []
                                                  if 100 <= int(n) < 105]
            append_by_card_id = {}
            for entry in data.get("userSvtAppendPassiveSkillLv", []) or []:
                card_id = int(entry.get("userSvtId") or 0)
                if card_id:
                    append_by_card_id[card_id] = self._numbered_append_levels(
                        entry.get("appendPassiveSkillNums"), entry.get("appendPassiveSkillLvs"))

            for item, in_storage in raw_list:
                svt_id = item.get("svtId") or item.get("id")
                if not svt_id:
                    continue
                sid_int = int(svt_id)
                # 概念礼装在 FGO userSvt 中的 svtId 范围为 9000000 ~ 9699999
                if 9000000 <= sid_int < 9700000:
                    ce = self._build_ce_from_item(sid_int, item)
                    if ce:
                        self._upsert_craft_essence(ce, in_storage=in_storage)
                    continue
                if sid_int >= 8000000:
                    continue
                app_list = [0] * 5
                for index in unlock_by_svt_id.get(sid_int, []):
                    app_list[index] = 1
                for index, value in enumerate(append_by_svt_id.get(sid_int, [])[:5]):
                    app_list[index] = value
                card_id = int(item.get("id") or 0)
                for index, value in append_by_card_id.get(card_id, {}).items():
                    app_list[index] = value
                inline = (
                    item.get("appendPassiveSkillLvs")
                    or item.get("appendSkills")
                    or []
                )
                if item.get("appendPassiveSkillNums") is not None:
                    for index, value in self._numbered_append_levels(item["appendPassiveSkillNums"], inline).items():
                        app_list[index] = value
                else:
                    app_list[:len(inline[:5])] = inline[:5]
                ap1 = item.get("appendSkillLv1", app_list[0] if len(app_list) > 0 else 0)
                ap2 = item.get("appendSkillLv2", app_list[1] if len(app_list) > 1 else 0)
                ap3 = item.get("appendSkillLv3", app_list[2] if len(app_list) > 2 else 0)
                np_lv = item.get("treasureDeviceLv1", item.get("npLv", item.get("np", 1)))
                if not np_lv or int(np_lv) <= 0:
                    continue
                profile = ServantProfile(
                    svt_id=sid_int,
                    name=self._resolve_name(sid_int, item.get("name")),
                    lv=int(item.get("lv", 1)),
                    np_lv=int(np_lv),
                    skill_lv1=int(item.get("skillLv1", 1) or 1),
                    skill_lv2=int(item.get("skillLv2", 1) or 1),
                    skill_lv3=int(item.get("skillLv3", 1) or 1),
                    append_skill_lv1=int(ap1 or 0),
                    append_skill_lv2=int(ap2 or 0),
                    append_skill_lv3=int(ap3 or 0),
                    append_skill_lv4=int(item.get("appendSkillLv4", app_list[3]) or 0),
                    append_skill_lv5=int(item.get("appendSkillLv5", app_list[4]) or 0),
                    fou=int(item.get("fou", 0) or 0),
                    atk_fou=int(item["adjustAtk"] or 0) * 10 if "adjustAtk" in item else item.get("atk_fou"),
                    hp_fou=int(item["adjustHp"] or 0) * 10 if "adjustHp" in item else item.get("hp_fou"),
                    card_id=card_id or None,
                    ascension=int(item["limitCount"]) if "limitCount" in item else None,
                    costume_id=int(item["imageLimitCount"]) if "imageLimitCount" in item else None,
                    skill_ids=[int(item.get(f"skillId{i}") or 0) for i in range(1, 4)],
                    np_id=int(item.get("treasureDeviceId") or 0) or None,
                    in_storage=in_storage,
                )
                self._upsert_servant(profile)

        # 格式 2: Chaldea 内部 userdata.json 格式
        elif isinstance(data, dict) and "servants" in data:
            svts_dict = data["servants"]
            for str_id, s_info in svts_dict.items():
                try:
                    svt_id = int(str_id)
                except ValueError:
                    continue

                cur = s_info.get("cur", s_info)
                skills = cur.get("skills", [1, 1, 1])
                appends = cur.get("append_skills", cur.get("appendSkills", [0, 0, 0]))

                profile = ServantProfile(
                    svt_id=svt_id,
                    name=self._resolve_name(svt_id, s_info.get("name")),
                    lv=cur.get("lv", 1),
                    np_lv=cur.get("np", cur.get("npLv", 1)),
                    skill_lv1=skills[0] if len(skills) > 0 else 1,
                    skill_lv2=skills[1] if len(skills) > 1 else 1,
                    skill_lv3=skills[2] if len(skills) > 2 else 1,
                    append_skill_lv1=appends[0] if len(appends) > 0 else 0,
                    append_skill_lv2=appends[1] if len(appends) > 1 else 0,
                    append_skill_lv3=appends[2] if len(appends) > 2 else 0,
                    append_skill_lv4=appends[3] if len(appends) > 3 else 0,
                    append_skill_lv5=appends[4] if len(appends) > 4 else 0,
                    fou=cur.get("fou", 0),
                    atk_fou=self._chaldea_fou(cur, "Atk"), hp_fou=self._chaldea_fou(cur, "Hp"),
                    ascension=cur.get("ascension"),
                )
                self._upsert_servant(profile)

        # 格式 3: 扁平列表
        elif isinstance(data, list):
            for item in data:
                svt_id = item.get("svtId") or item.get("id") or item.get("svt_id") or item.get("ce_id")
                if not svt_id:
                    continue
                sid_int = int(svt_id)
                if sid_int >= 9000000:
                    ce = self._build_ce_from_item(sid_int, item)
                    if ce:
                        self._upsert_craft_essence(ce)
                    continue
                skills = item.get("skills") or [
                    item.get("skill_lv1", item.get("skillLv1", 1)),
                    item.get("skill_lv2", item.get("skillLv2", 1)),
                    item.get("skill_lv3", item.get("skillLv3", 1)),
                ]
                appends = item.get("append_skills") or item.get("appendPassiveSkillLvs") or [
                    item.get("append_skill_lv1", item.get("appendSkillLv1", 0)),
                    item.get("append_skill_lv2", item.get("appendSkillLv2", 0)),
                    item.get("append_skill_lv3", item.get("appendSkillLv3", 0)),
                    item.get("append_skill_lv4", item.get("appendSkillLv4", 0)),
                    item.get("append_skill_lv5", item.get("appendSkillLv5", 0)),
                ]
                np_lv = item.get("treasureDeviceLv1", item.get("np_lv", item.get("npLv", item.get("np", 1))))
                profile = ServantProfile(
                    svt_id=sid_int,
                    name=self._resolve_name(sid_int, item.get("name")),
                    lv=item.get("lv", 1),
                    np_lv=int(np_lv or 1),
                    skill_lv1=skills[0] if len(skills) > 0 else 1,
                    skill_lv2=skills[1] if len(skills) > 1 else 1,
                    skill_lv3=skills[2] if len(skills) > 2 else 1,
                    append_skill_lv1=appends[0] if len(appends) > 0 else 0,
                    append_skill_lv2=appends[1] if len(appends) > 1 else 0,
                    append_skill_lv3=appends[2] if len(appends) > 2 else 0,
                    append_skill_lv4=appends[3] if len(appends) > 3 else 0,
                    append_skill_lv5=appends[4] if len(appends) > 4 else 0,
                    fou=item.get("fou", 0), atk_fou=item.get("atk_fou"), hp_fou=item.get("hp_fou"),
                    ascension=item.get("ascension"), costume_id=item.get("costume_id"),
                    skill_ids=item.get("skill_ids", []), np_id=item.get("np_id"),
                )
                self._upsert_servant(profile)

        if not any((self.servants, self.storage_servants, self.craft_essences, self.storage_craft_essences)):
            (self.servants, self.storage_servants, self.servant_copies, self.craft_essences,
             self.quest_records, self.quest_records_complete, self.mystic_codes, self.mystic_codes_complete,
             self.storage_craft_essences) = previous
            return {}
        return self.servants

    def get_by_id(self, svt_id: int) -> Optional[ServantProfile]:
        return self.servants.get(svt_id)

    def get_by_name(self, keyword: str) -> Optional[ServantProfile]:
        """根据名称或国服玩家常用简称(如 'C呆' / '杀狐' / '暗狐' / '旧梅' / '水CBA')查找从者"""
        kw = keyword.strip().lower()
        alias_id_map: Dict[str, List[int]] = {
            "c呆": [504500, 504800],
            "术呆": [504500, 504800],
            "阿尔托莉雅·caster": [504500, 504800],
            "阿尔托莉雅·卡斯特": [504500, 504800],
            "杀狐": [604200, 603500],
            "光之高扬斯卡娅": [604200, 603500],
            "奥伯龙": [2800100, 1300100],
            "孔明": [501900],
            "诸葛孔明": [501900],
            "梅林": [500800, 2800300],
            "旧梅": [2800300],
            "阿瓦隆女士": [2800300],
            "暗狐": [2500900],
            "暗之高扬斯卡娅": [2500900],
            "水cba": [901400, 803500],
            "rba": [901400, 803500],
            "斯卡哈·斯卡蒂(ruler)": [901400, 803500],
            "cba": [503900],
            "斯卡哈·斯卡蒂(caster)": [503900],
            "司马懿": [403200, 402400],
            "莱妮丝": [403200, 402400],
        }
        if kw in alias_id_map:
            for sid in alias_id_map[kw]:
                if sid in self.servants:
                    return self.servants[sid]
        for s in self.servants.values():
            if kw in s.name.lower():
                return s
        return None

    def has_servant(self, name_or_id: Union[str, int]) -> bool:
        if isinstance(name_or_id, int):
            return name_or_id in self.servants
        return self.get_by_name(name_or_id) is not None

    def get_core_supports(self) -> Dict[str, Optional[ServantProfile]]:
        """检查常用核心拐的持有与练度情况 (基于国服官方 Atlas Academy ID 精准匹配)"""
        core_specs = [
            ("C呆 (阿尔托莉雅·卡斯特)", [504500, 504800], ["阿尔托莉雅·卡斯特", "阿尔托莉雅·caster"]),
            ("杀狐 (光之高扬斯卡娅)", [604200, 603500], ["光之高扬斯卡娅"]),
            ("奥伯龙", [2800100, 1300100], ["奥伯龙"]),
            ("孔明 (埃尔梅罗Ⅱ世)", [501900], ["诸葛孔明"]),
            ("旧梅 / 梅林", [2800300, 500800], ["阿瓦隆女士", "梅林"]),
            ("暗狐 (暗之高扬斯卡娅)", [2500900], ["暗之高扬斯卡娅"]),
            ("水CBA / CBA", [901400, 803500, 503900], ["斯卡哈·斯卡蒂"]),
            ("莱妮丝 (司马懿)", [403200, 402400], ["莱妮丝", "司马懿"]),
        ]
        results: Dict[str, Optional[ServantProfile]] = {}
        for label, sids, name_kws in core_specs:
            found: Optional[ServantProfile] = None
            for sid in sids:
                if sid in self.servants:
                    found = self.servants[sid]
                    break
            if not found:
                for kw in name_kws:
                    for s in self.servants.values():
                        if kw.lower() in s.name.lower():
                            found = s
                            break
                    if found:
                        break
            results[label] = found
        return results

    def summary(self) -> str:
        """生成账户从者练度摘要"""
        total = len(self.servants)
        max_skills_count = sum(1 for s in self.servants.values() if s.is_all_skills_max())
        max_append2_count = sum(1 for s in self.servants.values() if s.has_max_append_np())

        lines = [
            f"=== FGO 账户数据摘要 ===",
            f"持有从者总数: {total}",
            f"3技能全满(10/10/10)从者数: {max_skills_count}",
            f"被动充能满级(20% NP)从者数: {max_append2_count}",
            f"--- 常用核心拐状态 ---"
        ]
        supports = self.get_core_supports()
        for nick, profile in supports.items():
            if profile:
                append2_text = f"被动20%NP" if profile.has_max_append_np() else f"被动NP+{profile.get_append_np_charge()}%"
                lines.append(f"  √ {nick} ({profile.name}): Lv.{profile.lv} | 宝具{profile.np_lv} | 技能 {profile.skill_lv1}/{profile.skill_lv2}/{profile.skill_lv3} | {append2_text}")
            else:
                lines.append(f"  × {nick}: 未持有")

        return "\n".join(lines)
