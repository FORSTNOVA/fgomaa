"""Chaldea Laplace preflight: local engine, public game data and executable skill plans."""
from __future__ import annotations

import copy
from collections import Counter
from itertools import chain
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from src.core.path_helper import get_resource_path, get_root_dir
from src.strategy.battle_plans import BattlePlanManager, SkillAction, TeamStrategy


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recommendation_signature(rec, parser):
    from src.strategy.quest_catalog import catalog_sync
    snapshot = catalog_sync().snapshot()
    return _digest({"recommendation": rec.model_dump(exclude={"laplace"}),
                    "quest_data_revision": [snapshot.get("revision"), snapshot.get("enemy_refresh_after")],
                    "servants": [s.model_dump() for s in parser.servants.values()],
                    "ces": [c.model_dump() for c in parser.craft_essences.values()],
                    "quests": parser.quest_records,
                    "quests_complete": parser.quest_records_complete,
                    "mystic_codes": parser.mystic_codes,
                    "mystic_codes_complete": parser.mystic_codes_complete})


class LaplaceError(RuntimeError):
    pass


def require_supported_np_refunds(rec, servant, slot, np_id):
    """固定概率门槛不覆盖前置即死成功时的零回收，重复宝具暂不放行。"""
    uses = sum(t.attack_mode == "np" and t.np_servant == slot for t in rec.strategy.turns.values())
    if uses < 2:
        return
    np = next(n for n in servant["noblePhantasms"] if n["id"] == np_id)
    before_death = False
    for function in np.get("functions", []):
        kind = function.get("funcType", "")
        if kind in ("instantDeath", "forceInstantDeath"):
            before_death = True
        if kind.startswith("damageNp") and before_death:
            raise LaplaceError("前置即死宝具连发尚未覆盖即死成功时的零 NP 回收，不能确认稳定三回合")


class PublicData:
    def __init__(self, root=None, cancel_event=None):
        self.root = Path(root or get_resource_path("data/laplace/public"))
        self.cancel_event = cancel_event

    def check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise LaplaceError("计算已取消")

    def download(self, path):
        request = urllib.request.Request(f"https://api.atlasacademy.io/nice/{path}",
                                         headers={"User-Agent": "FGO-MAA-Laplace/1"})
        max_bytes = 15 * 1024 * 1024
        connection_errors = (ssl.SSLError, TimeoutError, ConnectionError, IncompleteRead)
        for attempt in range(3):
            self.check_cancelled()
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise LaplaceError(f"公开游戏数据超出大小限制（{path}）")
                self.check_cancelled()
                return raw
            except (urllib.error.URLError, *connection_errors) as error:
                if isinstance(error, urllib.error.HTTPError):
                    retryable = error.code in (429, 500, 502, 503, 504)
                    error.close()
                else:
                    reason = error.reason if isinstance(error, urllib.error.URLError) else error
                    retryable = isinstance(reason, (ssl.SSLEOFError, TimeoutError, ConnectionError, IncompleteRead))
                    retryable |= getattr(reason, "reason", None) == "UNEXPECTED_EOF_WHILE_READING"
                self.check_cancelled()
                if not retryable or attempt == 2:
                    raise LaplaceError(f"Atlas 公开数据下载失败（{path}，尝试 {attempt + 1} 次）\n{error}") from error
            delay = 0.5 * (attempt + 1)
            if self.cancel_event is not None:
                self.cancel_event.wait(delay)
            else:
                time.sleep(delay)

    def get(self, region, kind, identity, phase=None, *, lore=False):
        self.check_cancelled()
        suffix = f"_{phase}" if phase is not None else ""
        if lore:
            suffix += "_lore"
        file = self.root / region / kind / f"{int(identity)}{suffix}.json"
        from src.strategy.quest_catalog import catalog_sync
        stale_after = catalog_sync().snapshot().get("enemy_refresh_after",0) if kind == "quest" else 0
        if file.exists() and file.stat().st_mtime >= stale_after:
            value = json.loads(file.read_text(encoding="utf-8"))
        else:
            path = f"{region}/{kind}/{int(identity)}"
            if phase is not None:
                path += f"/{int(phase)}"
            raw = self.download(path + ("?lore=true" if lore else ""))
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("id") != int(identity):
                raise LaplaceError("公开数据的 ID 与请求不一致")
            if phase is not None and value.get("phase") != phase:
                raise LaplaceError("新下载的关卡阶段与请求不一致，保留旧缓存")
            file.parent.mkdir(parents=True, exist_ok=True)
            temporary = file.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(raw)
            temporary.replace(file)
        self.check_cancelled()
        if not isinstance(value, dict) or value.get("id") != int(identity):
            raise LaplaceError("本地公开数据 ID 校验失败")
        if phase is not None and value.get("phase") != phase:
            raise LaplaceError("关卡阶段与请求不一致")
        return value

    def constants(self):
        file = self.root / "constData.json"
        if not file.exists():
            raise LaplaceError("缺少 Laplace 游戏常量，请修复客户端数据文件")
        return json.loads(file.read_text(encoding="utf-8"))


class LaplaceService:
    _lock = threading.Lock()
    _cache = {}

    def __init__(self, runtime=None, data=None, jobs=None):
        self.runtime = Path(runtime or get_resource_path("runtime/laplace"))
        self.data = data or PublicData()
        self.jobs = Path(jobs or (get_root_dir() / ".app_profile/laplace/jobs"))

    def status(self):
        manifest = self.runtime / "manifest.json"
        ready = all((self.runtime / p).is_file() for p in
                    ("flutter_tester.exe", "icudtl.dat", "assets/kernel_blob.bin", "manifest.json"))
        info = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        ready = ready and info.get("protocol") == 4
        return {"available": ready, "engine": "Chaldea Laplace", "revision": info.get("revision", ""),
                "message": "本地战斗计算组件已就绪" if ready else "缺少 Laplace 计算组件，请修复客户端安装"}

    def _load_quest(self, target, cancel_event):
        self.data.cancel_event = cancel_event
        self.data.check_cancelled()
        from src.strategy.quest_catalog import quest_binding
        entry = quest_binding(target)
        if entry is None:
            raise LaplaceError("此关卡尚未绑定准确的游戏关卡 ID，不能用近似关卡代替计算")
        quest = self.data.get("CN", "quest", entry["id"], entry["phase"])
        region = "CN"
        if not quest.get("stages") or any(not s.get("enemies") for s in quest["stages"]):
            quest = self.data.get("JP", "quest", entry["id"], entry["phase"])
            region = "JP"
        if not quest.get("stages") or any(not s.get("enemies") for s in quest["stages"]):
            raise LaplaceError("没有完整敌人数据，不能将空关卡判定为通关")
        if not 1 <= len(quest["stages"]) <= 3:
            raise LaplaceError("当前自动编队仅校验三回合以内的普通周回")
        return entry, quest, region

    def _prepare(self, rec, parser, cancel_event):
        from src.strategy.auto_party_assembler import AutoPartyAssembler
        from src.parser.chaldea_parser import ServantProfile
        from src.strategy.account_versions import resolve_versions

        entry, quest, region = self._load_quest(rec.target_quest_id, cancel_event)
        assumptions = ["按推荐从者、礼装与当前导入练度计算；需在游戏中保持一致。",
                       "助战按技能 10/10/10、无礼装与附加技能计算；未包含敌方行动与点击失败。",
                       "技能/宝具版本按账号任务与形态记录判定；未核实的强化不计入。职阶刻痕等额外加成不计入。"]
        if not parser.quest_records_complete:
            assumptions.append("账号缺少完整任务快照；无完成证据的强化按未完成处理，不默认使用最新版本。")
        if region == "JP":
            assumptions.append("国服接口缺少敌人数据，敌人采用相同关卡 ID 的日服公开战斗样本。")
        dps_id = rec.slot1_svt_id
        if not dps_id:
            candidates = rec.candidate_dps_ranking
            dps_id = next((x["svt_id"] for x in candidates if x["name"] == rec.slot1_dps), 0)
        ids = {1: dps_id, 2: AutoPartyAssembler.resolve_support_servant_id(rec.slot2_support),
               3: AutoPartyAssembler.resolve_support_servant_id(rec.slot3_friend_support)}
        if rec.party_servant_ids:
            ids = dict(rec.party_servant_ids)
        swapping = any(m.swap for t in rec.strategy.turns.values() for m in t.master_skills)
        if swapping:
            ids[4] = AutoPartyAssembler.resolve_support_servant_id(rec.slot4_backline_support)
        if any(not sid for sid in ids.values()):
            raise LaplaceError("推荐阵容包含未明确的从者，无法模拟")
        equipment = dict(rec.party_ce_details) if rec.battle_mode == "rotation" else {1: rec.slot1_ce_detail}
        used_ces = set()
        for slot, ce in equipment.items():
            owned_ce = parser.craft_essences.get(ce.get("ce_id"))
            if not owned_ce or not ce.get("is_owned"):
                raise LaplaceError("缺少已持有礼装的实际数据，不能按理论礼装放行出击")
            if (owned_ce.lv, owned_ce.is_mlb) != (ce.get("lv"), ce.get("is_mlb")):
                raise LaplaceError("礼装练度已变化，请重新生成编队")
            if ce["ce_id"] in used_ces:
                raise LaplaceError("尚未确认相同礼装的持有份数，不能给多个从者重复分配")
            used_ces.add(ce["ce_id"])
        servants, team, versions = {}, [], {}
        for slot, sid in ids.items():
            svt = self.data.get("CN", "servant", sid)
            servants[sid] = svt
            owned = parser.servants.get(sid)
            if slot == 3:
                row = {"svt_id": sid, "name": svt["name"], "ascension": 4, "lv": svt["lvMax"], "np_lv": 1,
                       "skill_lv1": 10, "skill_lv2": 10, "skill_lv3": 10,
                       "append_skill_lv1": 0, "append_skill_lv2": 0, "append_skill_lv3": 0, "fou": 0}
            elif owned is None:
                raise LaplaceError(f"账号未持有推荐从者 {sid}")
            else:
                row = owned.model_dump()
            profile = ServantProfile.model_validate(row)
            try:
                resolved, notes = resolve_versions(svt, profile, parser.quest_records if slot != 3 else {})
            except ValueError as exc:
                raise LaplaceError(str(exc)) from exc
            row.update(resolved)
            row["append_levels"] = profile.append_skills
            row["atk_fou"] = profile.atk_fou if profile.atk_fou is not None else profile.fou
            row["hp_fou"] = profile.hp_fou if profile.hp_fou is not None else profile.fou
            versions[slot] = resolved
            require_supported_np_refunds(rec, svt, slot, resolved["np_id"])
            assumptions.extend(notes)
            if not 1 <= row["lv"] <= len(svt["atkGrowth"]) or not 1 <= row["np_lv"] <= 5:
                raise LaplaceError("从者等级或宝具等级无效")
            row.update(party_slot=slot, is_friend=slot == 3)
            if slot in equipment:
                row["ce"] = copy.deepcopy(equipment[slot])
            team.append(row)
        # Only the exact swap outfit is used; never treat different Mystic Codes as equivalent.
        mc_id = rec.strategy.mystic_code_id or (20 if swapping else None)
        if mc_id not in (None, 20):
            raise LaplaceError("该御主魔术礼装尚未适配自动作战")
        master_actions = any(t.master_skills for t in rec.strategy.turns.values())
        if master_actions and mc_id is None:
            raise LaplaceError("推荐方案含未指定魔术礼装的御主技能，需明确礼装后才能校验")
        if mc_id:
            if parser.mystic_codes_complete and mc_id not in parser.mystic_codes:
                raise LaplaceError("账号未持有方案所需的迦勒底战斗服")
            mc_lv = parser.mystic_codes.get(mc_id, 10)
            assumptions.append(f"御主技能绑定迦勒底战斗服 Lv{mc_lv}。")
        else:
            mc_lv = 0
        data = {"servants": list(servants.values()), "quest": quest,
                "craft_essences": [self.data.get("CN", "equip", c["ce_id"]) for c in equipment.values()],
                "mystic_codes": [self.data.get("CN", "MC", mc_id)] if mc_id else [],
                "const_data": self.data.constants(), "base_skills": []}
        for file in (self.data.root / "JP/skill").glob("*.json"):
            data["base_skills"].append(json.loads(file.read_text(encoding="utf-8")))
        forbidden = {str(w): [[slot, skill] for slot, sid in ids.items() for skill in range(1, 4)
                             if (sid == 404200 and skill == 3) or
                             (sid == 2800100 and skill == 3 and w < len(quest["stages"]))]
                     for w in range(1, 4)}
        request = {"protocol": 4, "quest_data": "quest", "quest_id": entry["id"], "phase": entry["phase"], "team": team,
                   "strategy": rec.strategy.model_dump(), "runtime_forbidden": forbidden,
                   "mystic_code_id": mc_id, "mystic_code_lv": mc_lv,
                   "allow_facecards": rec.battle_mode == "rotation",
                   "battle_mode": rec.battle_mode,
                   "scenarios": [{"name": "minimum", "random": 900, "dynamic": True, "guards": True}]}
        ce = equipment.get(1, {})
        metadata = {"quest_id": entry["id"], "phase": entry["phase"], "enemy_region": region,
                    "quest_ap": quest.get("consume"),
                    "recommended_level": int(quest["recommendLv"]) if str(quest.get("recommendLv", "")).isdigit() else None,
                    "quest_name": entry["name"], "spot": entry["spot"], "war": entry["war"],
                    "waves": [len(s["enemies"]) for s in quest["stages"]],
                    "assumptions": assumptions, "required_mystic_code_id": mc_id,
                    "required_mystic_code_level": mc_lv,
                    "required_ce_id": ce.get("ce_id"), "required_ce_name": ce.get("name", "空礼装"),
                    "required_equipment": {str(slot): c["name"] for slot, c in equipment.items()},
                    "party_servant_ids": ids, "party_versions": versions}
        class_counts = Counter(e["svt"]["className"] for s in quest["stages"] for e in s["enemies"])
        class_names = {"saber": "剑阶", "archer": "弓阶", "lancer": "枪阶", "rider": "骑阶", "caster": "术阶",
                       "assassin": "杀阶", "berserker": "狂阶"}
        metadata["enemy_class_cn"] = "/".join(f"{class_names.get(c, c)}{n}只" for c, n in class_counts.items())
        return {"request": request, "data": data}, metadata

    def _run_worker(self, payload, cancel_event=None):
        self.jobs.mkdir(parents=True, exist_ok=True)
        folder = self.jobs / uuid.uuid4().hex
        folder.mkdir()
        job_id = folder.name
        digest = _digest(payload)
        job = {**payload, "job_id": job_id, "input_digest": digest}
        file = folder / "job.json"
        file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, FGOMAA_LAPLACE_JOB=str(file.resolve()))
        command = [str(self.runtime / "flutter_tester.exe"), "--disable-vm-service", "--non-interactive",
                   "--enable-software-rendering", f"--icu-data-file-path={self.runtime / 'icudtl.dat'}",
                   f"--flutter-assets-dir={self.runtime / 'assets'}", str(self.runtime / "assets/kernel_blob.bin")]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (folder / "worker.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=self.runtime, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=flags)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise LaplaceError("计算已取消")
                    if time.monotonic() - started > 60:
                        raise LaplaceError("Laplace 计算超时，已停止")
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        output = folder / "result.json"
        if not output.exists():
            raise LaplaceError("Laplace 未返回结果；请检查本地计算组件")
        result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("job_id") != job_id or result.get("input_digest") != digest:
            raise LaplaceError("Laplace 结果与本次输入不一致，拒绝复用旧结果")
        if process.returncode and not result.get("missing_data"):
            raise LaplaceError(result.get("message", "Laplace 计算进程失败"))
        return result

    def _loop_candidates(self, rec, parser, quest):
        from src.strategy.auto_team_builder import AutoTeamBuilder, SERVANT_TACTICAL_DB
        builder = AutoTeamBuilder(parser)
        classes = dict(Counter(e["svt"]["className"] for stage in quest.get("stages", [])
                               for e in stage["enemies"])) or None
        ranked = builder.recommend_for_quest(quest_id=rec.target_quest_id, enemy_classes=classes)
        rows = ranked.candidate_dps_ranking
        # 给完全克制、泛用克制与等倍各留试算机会；同档按推荐顺序，最多6名打手。
        selected = []
        for lower, upper, limit in ((2, float("inf"), 4), (1.0001, 2, 1), (1, 1.0001, 1)):
            selected.extend([r for r in rows if lower <= r["affinity"] < upper][:limit])
        for row in rows:
            if len(selected) >= 6:
                break
            if row not in selected:
                selected.append(row)
        selected.sort(key=lambda r: (r["affinity"], r["score"]), reverse=True)
        candidates = []
        for row in selected:
            sid = row["svt_id"]
            if sid not in parser.servants:
                continue
            base = rec if rec.battle_mode == "loop" and sid == rec.slot1_svt_id else builder.recommend_for_quest(
                quest_id=rec.target_quest_id, dps_id=sid, enemy_classes=classes)
            candidates.append(base)
            current_ce = base.slot1_ce_detail.get("ce_id")
            for _, _, detail in builder.craft_essence_candidates(SERVANT_TACTICAL_DB[sid], base.direction):
                if detail["ce_id"] == current_ce:
                    continue
                alternative = builder.recommend_for_quest(quest_id=rec.target_quest_id, dps_id=sid,
                                                         ce_id=detail["ce_id"], enemy_classes=classes)
                candidates.append(alternative)
        return candidates or [rec]

    @staticmethod
    def _candidate_affinity(rec, quest):
        from src.strategy.auto_team_builder import AutoTeamBuilder, SERVANT_TACTICAL_DB
        values = []
        for wave, stage in enumerate(quest.get("stages", []), 1):
            turn = rec.strategy.turns.get(wave)
            slot = turn.np_servant if turn and turn.np_servant else 1
            sid = rec.party_servant_ids.get(slot) if rec.battle_mode == "rotation" else rec.slot1_svt_id
            meta = SERVANT_TACTICAL_DB.get(sid)
            if not meta:
                return (0, 0)
            classes = dict(Counter(e["svt"]["className"] for e in stage["enemies"]))
            values.append(AutoTeamBuilder.calc_multiclass_affinity(meta.class_name, classes))
        return (min(values), sum(values)) if values else (rec.slot1_affinity, rec.slot1_affinity)

    @staticmethod
    def _damage_headroom(rec):
        return min((d["damage"] / d["hp"] for step in rec.laplace.get("steps", [])
                    for d in step.get("damage", []) if d.get("hp", 0) > 0), default=0)

    def _expanded_candidates(self, base, parser, quest, tried):
        from src.strategy.auto_team_builder import AutoTeamBuilder, SERVANT_TACTICAL_DB
        from src.strategy.low_hp_planner import rotation_candidates

        # Use the actual failed turn to prioritize variants instead of spending
        # every fallback trial on equipment for the same high-affinity DPS.
        loops = [rec for rec in tried if rec.direction == "Arts" and rec.battle_mode == "loop"
                 and rec.laplace.get("steps")]
        loops.sort(key=lambda rec: (len(rec.laplace["steps"]),
                                    rec.laplace["steps"][-1].get("np_before_attack", 0)),
                   reverse=True)
        seen = set()
        for rec in loops:
            if rec.slot1_svt_id in seen:
                continue
            seen.add(rec.slot1_svt_id)
            if len(seen) > 4:
                break
            third = rec.strategy.turns.get(3)
            if third is None or 2 not in rec.strategy.turns:
                continue
            meta = SERVANT_TACTICAL_DB.get(rec.slot1_svt_id)
            charge = next((s for s in third.skills if meta and s.servant == 1
                           and s.skill == meta.self_charge_skill), None)
            if charge and all(charge not in rec.strategy.turns[wave].skills for wave in (1, 2)
                              if wave in rec.strategy.turns):
                variant = rec.model_copy(deep=True)
                variant.strategy.turns[3].skills.remove(charge)
                variant.strategy.turns[2].skills.append(charge)
                variant.direction_name = "打手自充提前到第2波"
                variant.strategy.name = f"{variant.slot1_dps} · {variant.direction_name}"
                yield variant
            swap = next((action for action in third.master_skills if action.swap == [2, 4]), None)
            if swap is None:
                continue
            supports = [(rec.slot4_backline_support, [s for s in third.skills if s.servant == 4])]
            for sid, name, targets in ((501900, "诸葛孔明", (1, None, None)),
                                       (403200, "莱妮丝", (None, 1, 1))):
                profile = parser.servants.get(sid)
                if profile and not profile.in_storage and name != rec.slot4_backline_support:
                    supports.append((name, [SkillAction(servant=4, skill=i, target=target)
                                            for i, target in enumerate(targets, 1)]))
            for name, skills in supports:
                schedules = [(2, [s for s in skills if name != "奥伯龙" or s.skill != 3]), (3, [])]
                if name == "奥伯龙":
                    schedules.insert(0, (2, [s for s in skills if s.skill == 1]))
                for wave, early in schedules:
                    if wave == 3 and name == rec.slot4_backline_support:
                        continue
                    # Keep the finisher for the boss and try holding the
                    # large charge when a small second-wave top-up suffices.
                    variant = rec.model_copy(deep=True)
                    variant.slot4_backline_support = name
                    turns = variant.strategy.turns
                    turns[3].skills = [s for s in turns[3].skills if s.servant != 4]
                    turns[3].skills.extend(skills)
                    if wave == 2:
                        turns[3].master_skills.remove(swap)
                        turns[2].master_skills.append(swap)
                        turns[2].skills.extend(early)
                        turns[3].skills = [s for s in turns[3].skills if s not in early]
                    variant.direction_name = f"{name}第{wave}波换人"
                    if name == "奥伯龙" and wave == 2 and len(early) == 1:
                        variant.direction_name += "，保留大充能"
                    variant.strategy.name = f"{variant.slot1_dps} · {variant.direction_name}"
                    yield variant

        if max((e["hp"] for s in quest.get("stages", []) for e in s["enemies"]), default=0) > 60000:
            yield from rotation_candidates(base, parser, quest, limit=8, expanded=True)

        classes = dict(Counter(e["svt"]["className"] for s in quest.get("stages", []) for e in s["enemies"]))
        if classes:
            builder = AutoTeamBuilder(parser)
            ranked = builder.recommend_for_quest(quest_id=base.target_quest_id, enemy_classes=classes)
            attempted = {rec.slot1_svt_id for rec in tried}
            for row in [r for r in ranked.candidate_dps_ranking if r["svt_id"] not in attempted][:4]:
                yield builder.recommend_for_quest(quest_id=base.target_quest_id,
                                                  enemy_classes=classes, dps_id=row["svt_id"])

    def validate(self, rec, parser, cancel_event=None, log=None, *, allow_rotation=True):
        from src.strategy.low_hp_planner import rotation_candidates
        log = log or (lambda message: None)
        if cancel_event is not None and cancel_event.is_set():
            rec.laplace = {"status": "cancelled", "passed": False, "message": "计算已取消"}
            return False
        try:
            if not self.status()["available"]:
                return self._validate_one(rec, parser, cancel_event, log)
            _, quest, _ = self._load_quest(rec.target_quest_id, cancel_event)
            candidates = [rec] if rec.battle_mode == "rotation" else []
            candidates += rotation_candidates(rec, parser, quest) if allow_rotation else []
            candidates += self._loop_candidates(rec, parser, quest)
            candidates.sort(key=lambda candidate: self._candidate_affinity(candidate, quest), reverse=True)
        except Exception:
            # 数据不完整时仍由原入口给出明确错误，不能换成近似关卡。
            return self._validate_one(rec, parser, cancel_event, log)
        attempts = []

        def check(candidate):
            log(f"[打手与礼装试算] 【{candidate.slot1_dps}】 / 【{candidate.slot1_ce}】，职阶倍率 {candidate.slot1_affinity}。")
            passed = self._validate_one(candidate, parser, cancel_event, log)
            attempts.append({"name": candidate.direction_name, "passed": passed,
                             "dps_id": candidate.slot1_svt_id, "dps": candidate.slot1_dps,
                             "affinity": list(self._candidate_affinity(candidate, quest)),
                             "ce_id": candidate.slot1_ce_detail.get("ce_id"), "ce": candidate.slot1_ce,
                             "message": candidate.laplace.get("message", "")})
            return passed

        expanded = self._expanded_candidates(rec, parser, quest, candidates) if allow_rotation else ()
        for index, candidate in enumerate(chain(candidates, expanded)):
            if cancel_event is not None and cancel_event.is_set():
                rec.laplace = {"status": "cancelled", "passed": False, "message": "计算已取消"}
                return False
            if candidate.battle_mode == "rotation":
                log(f"[低血量编队] 尝试 {candidate.slot1_dps} / {candidate.slot2_support} / {candidate.slot3_friend_support}；普攻未通过则尝试宝具。")
            passed = check(candidate)
            if passed:
                # 已有可行方案后，额外证明同打手的零初始NP高宝威方案。
                # 黑杯只有实际三波启动/回收均通过且伤害裕量更高时才替换。
                damage_options = [c for c in candidates[index + 1:]
                                  if candidate.battle_mode == c.battle_mode == "loop"
                                  and c.slot1_svt_id == candidate.slot1_svt_id
                                  and c.slot1_ce_detail.get("initial_np") == 0
                                  and c.slot1_ce_detail.get("np_damage_up", 0) > candidate.slot1_ce_detail.get("np_damage_up", 0)]
                if damage_options:
                    damage_option = max(damage_options, key=lambda c: c.slot1_ce_detail["np_damage_up"])
                    if check(damage_option) and self._damage_headroom(damage_option) > self._damage_headroom(candidate):
                        candidate = damage_option
                        log("[礼装优化] 零初始NP宝威礼装的三波充能与回收通过，采用伤害裕量更高的方案。")
                if cancel_event is not None and cancel_event.is_set():
                    rec.laplace = {"status": "cancelled", "passed": False, "message": "计算已取消"}
                    return False
                if candidate is not rec:
                    for field in type(rec).model_fields:
                        setattr(rec, field, copy.deepcopy(getattr(candidate, field)))
                rec.laplace["attempts"] = attempts
                return True
        cancelled = cancel_event is not None and cancel_event.is_set()
        message = (f"已试算 {len(attempts)} 组打手与礼装，均未通过；最后原因：{attempts[-1]['message']}"
                   if attempts else "没有可校验的编队方案")
        rec.laplace.update(status="cancelled" if cancelled else "failed", passed=False,
                           message="计算已取消" if cancelled else message)
        rec.laplace["attempts"] = attempts
        return False

    def _validate_one(self, rec, parser, cancel_event=None, log=None):
        log = log or (lambda message: None)
        rec.laplace = {"status": "running", "passed": False, "message": "正在进行 Laplace 战斗校验"}
        try:
            if not self.status()["available"]:
                raise LaplaceError(self.status()["message"])
            while not self._lock.acquire(timeout=0.1):
                if cancel_event is not None and cancel_event.is_set():
                    raise LaplaceError("计算已取消")
            try:
                original_signature = recommendation_signature(rec, parser)
                payload, metadata = self._prepare(rec, parser, cancel_event)
                manifest = json.loads((self.runtime / "manifest.json").read_text(encoding="utf-8"))
                key = _digest({"payload": payload, "engine": manifest, "account": original_signature})
                result = copy.deepcopy(self._cache.get(key))
                if result is None:
                    log(f"[Laplace] 正在按最低伤害浮动校验【{rec.quest_name}】的三回合方案…")
                    for attempt in range(4):
                        result = self._run_worker(payload, cancel_event)
                        missing = result.get("missing_data", [])
                        if not missing:
                            break
                        if attempt == 3:
                            raise LaplaceError("公开技能依赖仍不完整，停止校验")
                        for url in sorted(set(missing)):
                            match = re.fullmatch(r"https://api\.atlasacademy\.io/nice/(JP|CN)/(skill|common-release)/(\d+)", url)
                            if not match:
                                raise LaplaceError(f"不支持的模拟数据依赖：{url}")
                            region, kind, identity = match.groups()
                            if kind == "skill":
                                payload["data"]["base_skills"].append(self.data.get(region, kind, int(identity)))
                            else:
                                from src.strategy.battle_ai_data import BattleAIData
                                releases = BattleAIData(self.data, region).get(kind, int(identity))
                                payload["data"].setdefault("common_releases", {})[f"{region}/{identity}"] = releases
                                records = payload["data"].setdefault("release_quest_records", {})
                                for release in releases:
                                    if release["condType"] in ("questClear", "questClearPhase"):
                                        quest_id = release["condId"]
                                        record = parser.quest_records.get(quest_id)
                                        if record is None and not parser.quest_records_complete:
                                            raise LaplaceError(f"缺少解锁条件所需的任务记录：{quest_id}")
                                        records[str(quest_id)] = record or {}
                    if result.get("success"):
                        if len(self._cache) >= 16:
                            self._cache.pop(next(iter(self._cache)))
                        self._cache[key] = copy.deepcopy(result)
                if cancel_event is not None and cancel_event.is_set():
                    raise LaplaceError("计算已取消")
                if not result.get("success"):
                    raise LaplaceError(result.get("message", "Laplace 计算失败"))
                if recommendation_signature(rec, parser) != original_signature:
                    raise LaplaceError("计算期间账号练度或方案已变化，请重新计算")
                validation = result.get("validation", {})
                steps = validation.get("steps", [])
                passed = (validation.get("success") is True and not validation.get("battle_errors")
                          and len(steps) == len(metadata["waves"])
                          and all((s.get("damage") or (s.get("instant_death_clear") is True and s.get("cleared") is True and s.get("enemies"))) and (
                              (s.get("attack_mode", "np") == "np" and s.get("np_before_attack", 0) >= 100)
                              or (s.get("attack_mode") == "facecards" and s.get("facecard_proof", {}).get("all_triples") is True))
                                  for s in steps))
                compiled = result.get("compiled_strategy")
                if not passed or not compiled:
                    rec.laplace = {**metadata, "status": "failed", "passed": False,
                                   "engine": "Chaldea Laplace", "message": validation.get("stop_reason", "战斗计算未通过"),
                                   "steps": steps}
                    log("[Laplace 未通过] " + rec.laplace["message"])
                    return False
                rec.strategy = TeamStrategy(**compiled)
                if rec.battle_mode == "rotation":
                    normal = sum(s.get("attack_mode") == "facecards" for s in steps)
                    rec.direction_name = ("技能普攻清场" if normal == len(steps) else
                                          "普攻＋三打手轮换宝具" if normal else "三打手轮换宝具")
                    rec.strategy.name = rec.direction_name
                    rec.direction_reason = "按各波次真实 HP 与职阶试算；每回合指定打手，普攻不依赖暴击与特定发牌。"
                rec.waves = metadata["waves"]
                rec.direction_reason = rec.direction_reason.replace(rec.enemy_class_cn, metadata["enemy_class_cn"])
                rec.enemy_class_cn = metadata["enemy_class_cn"]
                rec.mystic_code = (f"迦勒底战斗服 Lv{metadata['required_mystic_code_level']}"
                                   if metadata["required_mystic_code_id"] else "不使用御主技能")
                manager = BattlePlanManager()
                rec.turn_descriptions = {t: manager.format_turn_instructions(rec.strategy, t) for t in rec.strategy.turns}
                rec.laplace = {**metadata, "status": "passed", "passed": True, "engine": "Chaldea Laplace",
                               "message": f"最低伤害浮动下 {len(steps)}T 模拟通关；已采用校验后的技能顺序",
                               "steps": steps, "random": 0.9, "engine_revision": manifest.get("revision", ""),
                               "signature": recommendation_signature(rec, parser)}
                log("[Laplace 通过] " + rec.laplace["message"])
                return True
            finally:
                self._lock.release()
        except Exception as error:
            rec.laplace = {"status": "cancelled" if cancel_event is not None and cancel_event.is_set() else "error",
                           "passed": False, "engine": "Chaldea Laplace", "message": str(error)}
            log("[Laplace 停止] " + str(error))
            return False
