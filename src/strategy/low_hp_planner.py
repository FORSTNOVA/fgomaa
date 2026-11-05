"""Bounded low-HP candidates. Charge estimates only rank; Laplace must prove every candidate."""
from itertools import permutations

from src.strategy.auto_team_builder import AutoTeamBuilder, SERVANT_TACTICAL_DB
from src.strategy.battle_plans import TeamStrategy, TurnDirective, SkillAction


def rotation_candidates(base, parser, quest, limit=3, *, expanded=False):
    stages = quest.get("stages", [])
    if not 1 <= len(stages) <= 3 or any(not s.get("enemies") for s in stages):
        return []
    if not expanded and max(e["hp"] for s in stages for e in s["enemies"]) > 60000:
        return []
    builder = AutoTeamBuilder(parser)
    owned, normal_owned = [], []
    for sid, profile in parser.servants.items():
        meta = SERVANT_TACTICAL_DB.get(sid)
        if not meta or meta.role == "support" or sid in (201300, 504400) or profile.lv < 40 or profile.in_storage:
            continue
        charge = builder._get_dps_actual_self_charge(profile, meta) + profile.get_append_np_charge()
        if meta.secondary_charge_skill:
            charge += meta.secondary_max_charge * profile.skills[meta.secondary_charge_skill - 1] // 10
        equipment = [ce for ce in parser.craft_essences.values() if ce.initial_np + charge >= 100]
        normal_owned.append((sid, profile, meta, charge, equipment))
        # Melusine's large charge requires a specific ascension transition.
        # The imported profile has no ascension field, so do not assume it.
        if sid != 304800 and (charge >= 100 or equipment):
            owned.append((sid, profile, meta, charge, equipment))
    def affinity(meta, stage):
        return min(builder.calc_class_affinity(meta.class_name, e["svt"]["className"].capitalize())
                   for e in stage["enemies"])

    owned.sort(key=lambda row: (min(affinity(row[2], s) for s in stages), row[1].np_lv, row[1].lv), reverse=True)
    owned = owned[:10 if expanded else 6]
    borrowed = [SERVANT_TACTICAL_DB[sid] for sid in (2300500, 501200, 1101500)]
    ranked = []
    for first, second in permutations(owned, 2):
        for friend in borrowed:
            metas = {1: first[2], 2: second[2], 3: friend}
            np_levels = {1: first[1].np_lv, 2: second[1].np_lv, 3: 1}
            def order_score(order):
                affinities = [affinity(metas[slot], stage) for slot, stage in zip(order, stages)]
                # 同克制关系时，把高宝具等级的自有打手留给高血量波次。
                # 这只是试算顺序；最终伤害仍必须经 Laplace 校验。
                burst = sum(a * (1 + .2 * (np_levels[slot] - 1)) * max(e["hp"] for e in stage["enemies"])
                            for a, slot, stage in zip(affinities, order, stages))
                return sum(affinities), burst
            best_order = max(permutations((1, 2, 3)), key=lambda order:
                order_score(order))
            score = sum(affinity(metas[slot], stage) for slot, stage in zip(best_order, stages))
            score += (first[1].np_lv + second[1].np_lv) * 0.04
            ranked.append((score, first, second, friend, best_order, False))
    ranked.sort(key=lambda row: row[0], reverse=True)
    # Spend the bounded trials on different owned pairs before trying another
    # borrower for the same pair (a conditional charge may fail in simulation).
    groups = {}
    for row in ranked:
        pair = tuple(sorted((row[1][0], row[2][0])))
        group = groups.setdefault(pair, [])
        if not any(r[3].svt_id == row[3].svt_id for r in group):
            group.append(row)
    ranked = [group[i] for i in range(3) for group in groups.values() if i < len(group)]
    # A normal-card team does not need starting NP. Try one such team even when
    # the account has no two independently charged DPS; the engine must prove it.
    if max(e["hp"] for s in stages for e in s["enemies"]) <= 5000 and len(normal_owned) >= 2:
        normal_owned.sort(key=lambda row: (min(affinity(row[2], s) for s in stages), row[1].lv), reverse=True)
        ranked.insert(0, (0, normal_owned[0], normal_owned[1], borrowed[0], (1, 2, 3), True))
        limit += 1
    results, seen = [], set()
    for _, first, second, friend, order, normal_seed in ranked:
        identity = (tuple(sorted((first[0], second[0]))), friend.svt_id, normal_seed)
        if identity in seen:
            continue
        seen.add(identity)
        assigned = {}
        used = set()
        for slot, row in ((1, first), (2, second)):
            if normal_seed or row[3] >= 100:
                continue
            candidates = sorted((c for c in row[4] if c.ce_id not in used),
                                key=lambda c: (c.initial_np, c.card_buff_rate + c.np_damage_up, c.lv), reverse=True)
            if not candidates:
                break
            ce = candidates[0]
            assigned[slot] = {**ce.model_dump(), "is_owned": True, "effect_summary": ce.effect_summary()}
            used.add(ce.ce_id)
        else:
            rec = base.model_copy(deep=True)
            rec.battle_mode = "rotation"
            rec.party_servant_ids = {1: first[0], 2: second[0], 3: friend.svt_id}
            rec.party_ce_details = assigned
            rec.slot1_svt_id = first[0]
            rec.slot1_dps = first[1].name
            rec.slot1_class = first[2].class_name
            rec.slot1_affinity = min(affinity(first[2], s) for s in stages)
            rec.slot1_ce_detail = assigned.get(1, {})
            rec.slot1_ce = assigned[1]["name"] if 1 in assigned else "空礼装（不计礼装加成）"
            rec.slot2_support = second[2].name
            rec.slot2_ce = assigned[2]["name"] if 2 in assigned else "空礼装（不计礼装加成）"
            rec.slot3_friend_support = f"助战 {friend.name}"
            rec.slot4_backline_support = "任意后排蹭羁绊从者 (无需换人)"
            rec.direction = "MultiDPS"
            rec.direction_name = "三打手轮换宝具 / 低血量技能普攻候选"
            rec.mystic_code = "不使用御主技能"
            rec.direction_reason = "按实际敌人 HP 尝试三打手轮换，普攻只在最低浮动、无暴击且任意发牌均可清场时采用。"
            turns = {}
            for turn, slot in enumerate(order[:len(stages)], 1):
                meta = {1: first[2], 2: second[2], 3: friend}[slot]
                skill_numbers = list(dict.fromkeys(meta.buff_skills +
                    [s for s in (meta.self_charge_skill, meta.secondary_charge_skill) if s]))
                if normal_seed:
                    skill_numbers = []
                skill_numbers = [s for s in skill_numbers if not (meta.svt_id == 404200 and s == 3)]
                turns[turn] = TurnDirective(skills=[SkillAction(servant=slot, skill=s, target=slot) for s in skill_numbers],
                                             np_servant=slot)
            rec.strategy = TeamStrategy(id=f"rotation_{first[0]}_{second[0]}_{friend.svt_id}_{base.target_quest_id}",
                name="三打手轮换 / 普攻候选", match_servants=[rec.slot1_dps, rec.slot2_support], turns=turns)
            results.append(rec)
            if len(results) >= limit:
                break
    return results
