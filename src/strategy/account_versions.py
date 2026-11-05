"""Resolve owned skill/NP versions from account evidence, never from the latest row alone."""

# Atlas CN binds these two versions of Horizon of Light to the matching NPs.
MELUSINE_NP_FORMS = {
    304801: (888550, "蓝卡单体"),
    304802: (888575, "红卡全体"),
}


def resolve_versions(servant, profile, quest_records):
    ascension = profile.ascension if profile.ascension is not None else 4
    notes = []
    if profile.ascension is None:
        notes.append(f"{profile.name} 缺少灵基再临记录，按最终再临计算。")

    def quest_completed(qid, phase=0):
        record = quest_records.get(int(qid))
        return bool(record and int(record.get("clearNum", 0)) > 0 and
                    (int(record.get("questPhase", 0)) == 0 or int(record["questPhase"]) >= int(phase)))

    def conditions_met(conditions):
        groups = {}
        for cond in conditions:
            kind = cond.get("condType", cond.get("type"))
            target = int(cond.get("condTargetId", cond.get("targetId", 0)))
            value = int(cond.get("condNum", cond.get("value", 0)))
            if kind == "equipWithTargetCostume":
                valid = target == profile.svt_id and profile.costume_id == value
            elif kind == "questClear":
                valid = quest_completed(target)
            elif kind == "svtLimit":
                valid = target == profile.svt_id and ascension >= value
            else:
                valid = False
            groups.setdefault(int(cond.get("condGroup", 0)), []).append(valid)
        return not groups or any(all(values) for values in groups.values())

    def metadata(row, kind):
        if kind == "skills":
            matches = [s for s in row.get("skillSvts", [])
                       if int(s.get("svtId", 0)) == profile.svt_id and
                       int(s.get("num", 0)) == int(row.get("num", 0))]
            costume_matches = [s for s in matches if any(
                cond.get("condType") == "equipWithTargetCostume"
                and cond.get("condTargetId") == profile.svt_id
                and cond.get("condNum") == profile.costume_id
                for cond in s.get("releaseConditions", []))]
            if costume_matches:
                matches = sorted(costume_matches, key=lambda s: int(s.get("priority", 0)), reverse=True)
            else:
                matches = [s for s in matches if int(s.get("priority", 0)) == int(row.get("priority", 0))]
            return {**row, **matches[0]} if matches else row
        return row

    def eligible(meta):
        if profile.lv < int(meta.get("condLv", 0)) or ascension < int(meta.get("condLimitCount", 0)):
            return False
        qid = int(meta.get("condQuestId", 0))
        if qid and not quest_completed(qid, meta.get("condQuestPhase", 0)):
            return False
        if not conditions_met(meta.get("releaseConditions", [])):
            return False
        if (meta.get("script") or {}).get("strengthStatusReleaseId"):
            return False  # Unsupported commonRelease gates cannot authorize an upgrade.
        return True

    def choose(kind, slot, explicit=0):
        rows = [r for r in servant.get(kind, []) if int(r.get("num", 0)) == slot]
        if explicit:
            matched = [r for r in rows if int(r["id"]) == explicit]
            if not matched:
                raise ValueError(f"{profile.name} 的技能/宝具 ID {explicit} 与公开角色数据不匹配")
            return matched[0]
        versions = [(r, metadata(r, kind)) for r in rows]
        allowed = [(r, meta) for r, meta in versions if eligible(meta)]
        if not allowed:
            if kind == "skills" and rows:
                return None  # A skill locked behind ascension is actually unavailable.
            raise ValueError(f"无法根据账号记录确定 {profile.name} 的宝具版本")
        chosen, chosen_meta = max(allowed, key=lambda pair: int(pair[1].get("priority", 0)))
        if any(int(meta.get("priority", 0)) > int(chosen_meta.get("priority", 0)) for _, meta in versions):
            notes.append(f"{profile.name} 的{('技能' + str(slot)) if kind == 'skills' else '宝具'}使用已核实可用版本：{chosen['name']}。")
        return chosen

    np = choose("noblePhantasms", 1, profile.np_id)
    skill_ids = [*profile.skill_ids, 0, 0, 0][:3]
    form = MELUSINE_NP_FORMS.get(int(np["id"])) if profile.svt_id == 304800 else None
    if form and ascension >= 3:
        form_skill, label = form
        skill_ids[2] = form_skill
        notes.append(f"{profile.name} 使用{label}宝具「{np['name']}」及对应的第 3 技能。")
    skills = [choose("skills", i, skill_id) for i, skill_id in enumerate(skill_ids, 1)]
    return {"skill_ids": [int(s["id"]) if s else 0 for s in skills], "np_id": int(np["id"]),
            "ascension": ascension}, notes
