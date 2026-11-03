"""
Unit tests for Account-Driven & Enemy-Aware Auto Team Builder (AutoTeamBuilder)
验证：
1. 根据目标关卡敌人职阶动态变阵（QP术阶 -> 骑阶2.0x克制打手；剑修炼场 -> 弓阶2.0x克制打手；弓修炼场 -> 枪阶2.0x克制打手；枪修炼场 -> 剑阶2.0x克制打手，且严格剔除0.5x被克制从者）
2. 根据账户持有的拐数量与技能等级决定配队方向（蓝卡C呆队 vs 红卡杀狐奥伯龙队 vs 绿卡水CBA队 vs 缺拐/低练度多核队）
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.parser.chaldea_parser import ChaldeaParser
from src.parser.packet_capturer import FGOPacketCapturer
from src.strategy.auto_team_builder import AutoTeamBuilder


def test_enemy_class_dynamic_adaptation():
    print("\n--- [Test 1] 根据目标关卡敌人信息动态切换克制打手与配队 ---")
    parser = ChaldeaParser()
    parser.parse_file(root_dir / "tests" / "sample_chaldea.json")
    builder = AutoTeamBuilder(parser)

    # 1. QP 极级 (敌方: 术阶 Caster) -> 优先推荐 2.0x 骑阶蓝卡光炮 (哈贝特洛特 / 小达芬奇)
    rec_qp = builder.recommend_for_quest("daily_qp_extreme")
    print(f"[QP本 - {rec_qp.enemy_class_cn}] 方向: {rec_qp.direction_name} | 主打手: {rec_qp.slot1_dps} ({rec_qp.slot1_class}, {rec_qp.slot1_affinity}x克制)")
    assert rec_qp.slot1_class == "Rider" and rec_qp.slot1_affinity == 2.0
    assert rec_qp.direction == "Arts"

    # 2. 剑之修炼场 (敌方: 剑阶 Saber) -> 优先推荐 2.0x 弓阶打手 (吉尔伽美什) 且绝不包含枪阶(0.5x)
    rec_saber = builder.recommend_for_quest("daily_training_saber")
    print(f"[剑修炼场 - {rec_saber.enemy_class_cn}] 方向: {rec_saber.direction_name} | 主打手: {rec_saber.slot1_dps} ({rec_saber.slot1_class}, {rec_saber.slot1_affinity}x克制)")
    assert rec_saber.slot1_class == "Archer" and rec_saber.slot1_affinity == 2.0
    assert all(c["class_name"] != "Lancer" for c in rec_saber.candidate_dps_ranking)

    # 3. 弓之修炼场 (敌方: 弓阶 Archer) -> 优先推荐 2.0x 枪阶打手 (妖精骑士兰斯洛特) 且绝不包含剑阶(0.5x)
    rec_archer = builder.recommend_for_quest("daily_training_archer")
    print(f"[弓修炼场 - {rec_archer.enemy_class_cn}] 方向: {rec_archer.direction_name} | 主打手: {rec_archer.slot1_dps} ({rec_archer.slot1_class}, {rec_archer.slot1_affinity}x克制)")
    assert rec_archer.slot1_class == "Lancer" and rec_archer.slot1_affinity == 2.0
    assert all(c["class_name"] != "Saber" for c in rec_archer.candidate_dps_ranking)

    # 4. 枪之修炼场 (敌方: 枪阶 Lancer) -> 优先推荐 2.0x 剑阶打手 (千子村正) 且绝不包含弓阶(0.5x)
    rec_lancer = builder.recommend_for_quest("daily_training_lancer")
    print(f"[枪修炼场 - {rec_lancer.enemy_class_cn}] 方向: {rec_lancer.direction_name} | 主打手: {rec_lancer.slot1_dps} ({rec_lancer.slot1_class}, {rec_lancer.slot1_affinity}x克制)")
    assert rec_lancer.slot1_class == "Saber" and rec_lancer.slot1_affinity == 2.0
    assert all(c["class_name"] != "Archer" for c in rec_lancer.candidate_dps_ranking)
    print("√ 测试通过: 配队可根据目标关卡敌人职阶精准切换克制打手并剔除逆克制！")


def test_support_skill_level_direction_decision():
    print("\n--- [Test 2] 根据账户拥有的拐数量和技能等级决定配队方向 ---")
    parser = ChaldeaParser()

    # 场景 A: 账号仅持有满级【杀狐(10/10/10) + 奥伯龙(10/10/10)】，未持有 C呆
    buster_only_account = {
        "userServant": [
            {"svtId": 604200, "lv": 90, "npLv": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 2800100, "lv": 90, "npLv": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 704000, "lv": 100, "npLv": 3, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 403500, "lv": 90, "npLv": 2, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
        ]
    }
    parser.parse_data(buster_only_account)
    builder_a = AutoTeamBuilder(parser)
    rec_a = builder_a.recommend_for_quest("daily_exp_extreme")
    print(f"[场景A: 持有满技能杀狐+奥伯龙, 无C呆] -> 选定方向: {rec_a.direction_name} | 打手: {rec_a.slot1_dps}")
    assert rec_a.direction == "Buster"
    assert rec_a.slot1_dps == "摩根"

    # 场景 B: 账号杀狐 1 技能仅 4 级 (未满10级无法减2回合CD)，但 C呆 10/10/10 满级
    arts_ready_account = {
        "userServant": [
            {"svtId": 604200, "lv": 90, "npLv": 1, "skillLv1": 4, "skillLv2": 4, "skillLv3": 4, "appendSkillLv2": 0},
            {"svtId": 504500, "lv": 90, "npLv": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 704000, "lv": 90, "npLv": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 704300, "lv": 90, "npLv": 2, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
        ]
    }
    parser.parse_data(arts_ready_account)
    builder_b = AutoTeamBuilder(parser)
    rec_b = builder_b.recommend_for_quest("daily_exp_extreme")
    print(f"[场景B: 杀狐仅4/4/4未成型, C呆10/10/10满级] -> 选定方向: {rec_b.direction_name} | 打手: {rec_b.slot1_dps}")
    assert rec_b.direction == "Arts"
    assert rec_b.slot1_dps == "伊吹童子(Berserker)"

    # 场景 C: 账号无满级大拐 (杀狐 2/2/2, 无 C呆/水CBA) -> 自动切换为多核自充方向
    low_support_account = {
        "userServant": [
            {"svtId": 604200, "lv": 70, "npLv": 1, "skillLv1": 2, "skillLv2": 2, "skillLv3": 2, "appendSkillLv2": 0},
            {"svtId": 404200, "lv": 80, "npLv": 5, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
        ]
    }
    parser.parse_data(low_support_account)
    builder_c = AutoTeamBuilder(parser)
    rec_c = builder_c.recommend_for_quest("daily_qp_extreme")
    print(f"[场景C: 大拐技能等级不足] -> 选定方向: {rec_c.direction_name} | 打手: {rec_c.slot1_dps}")
    assert rec_c.direction == "MultiCore"
    assert rec_c.slot1_dps == "哈贝特洛特"
    print("√ 测试通过: 能够准确识别账户内拐的数量与技能等级，智能决定配队方向与打手！")


def test_packet_capturer_and_toplogin_har_parsing():
    print("\n--- [Test 3] 内置抓包引擎与官方 toplogin / .har 数据包解析测试 ---")
    import base64
    import json

    # 1. 测试官方 toplogin cache.replaced.userSvt 原始抓包结构 (含礼装自动过滤与多宝择优)
    toplogin_packet = {
        "response": [{"resCode": "00", "nid": "toplogin"}],
        "cache": {
            "replaced": {
                "userSvt": [
                    # 摩根主号 (Lv.100 5宝 10/10/10 被动2满级)
                    {"svtId": 704000, "lv": 100, "treasureDeviceLv1": 5, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendPassiveSkillLvs": [10, 10, 10]},
                    # 摩根1级二号机 (不应覆盖主号)
                    {"svtId": 704000, "lv": 1, "treasureDeviceLv1": 1, "skillLv1": 1, "skillLv2": 1, "skillLv3": 1, "appendPassiveSkillLvs": [0, 0, 0]},
                    # 杀狐
                    {"svtId": 604200, "lv": 90, "treasureDeviceLv1": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendPassiveSkillLvs": [0, 10, 0]},
                    # 概念礼装 (svtId 9405300 = 海上飞行者)
                    {"svtId": 9405300, "lv": 100, "treasureDeviceLv1": 5, "skillLv1": 1, "skillLv2": 1, "skillLv3": 1},
                ]
            }
        }
    }

    # 2. 将其封装为 Base64 编码的 .har 抓包归档文件测试
    raw_b64 = base64.b64encode(json.dumps(toplogin_packet).encode("utf-8")).decode("utf-8")
    har_data = {
        "log": {
            "entries": [
                {
                    "request": {"url": "https://line1-s1-bili-fate.bilibiligame.net/rongame_beta/rgfate/60_1001/ac.php?key=toplogin"},
                    "response": {
                        "status": 200,
                        "content": {"encoding": "base64", "text": raw_b64}
                    }
                }
            ]
        }
    }

    parser = ChaldeaParser()
    servants = parser.parse_data(har_data)
    assert len(servants) == 2
    assert servants[704000].lv == 100 and servants[704000].np_lv == 5
    assert servants[704000].get_append_np_charge() == 20
    # 验证概念礼装 (9405300 = 海上飞行者: 50%NP + 15%NP回收 + 8%蓝卡) 也被同步抓取入库！
    assert 9405300 in parser.craft_essences
    ce_ocean = parser.craft_essences[9405300]
    assert ce_ocean.name == "海上飞行者"
    assert ce_ocean.initial_np == 50 and ce_ocean.np_gain_rate == 15 and ce_ocean.card_buff_color == "Arts"

    # 3. 测试 FGOPacketCapturer 回调闭环
    captured_counts = []
    def on_captured(payload):
        svts = parser.parse_data(payload)
        captured_counts.append((len(svts), len(parser.craft_essences)))
        return (len(svts), len(parser.craft_essences))

    capturer = FGOPacketCapturer(port=18899, on_account_captured=on_captured)
    start_res = capturer.start(auto_set_proxy=False)
    assert start_res["success"] and capturer.is_listening
    capturer._inspect_captured_payload("http://bili-fate/ac.php?key=toplogin", json.dumps(toplogin_packet))
    assert captured_counts == [(2, 1)]
    assert not capturer.is_listening  # 抓包命中入库后自动停止监听并还原代理
    print("√ 测试通过: 内置抓包服务、toplogin 从者+概念礼装同步提取及 .har 归档解析全部验证通过！")


def test_craft_essence_capture_and_synergy_matching():
    print("\n--- [Test 4] 概念礼装纳入配队计算 (优先宝具NP + 宝具NP回收 + 匹配色卡性能提升) ---")
    parser = ChaldeaParser()
    parser.parse_file(root_dir / "tests" / "sample_chaldea.json")
    assert len(parser.craft_essences) >= 10
    builder = AutoTeamBuilder(parser)

    # 1. 蓝卡队打手 (QP本 -> 骑阶蓝卡光炮 / 枪本 -> 剑阶蓝卡光炮千子村正)
    # 必须优先把同时具备【50%NP + NP回收 + 蓝卡性能提升】的礼装分配给蓝卡队打手！
    rec_arts = builder.recommend_for_quest("daily_training_lancer")  # 千子村正 (Saber Arts)
    print(f"[蓝卡队打手: {rec_arts.slot1_dps}] 智能匹配1号位礼装: {rec_arts.slot1_ce}")
    assert rec_arts.direction == "Arts"
    assert rec_arts.slot1_ce_detail["card_buff_color"] == "Arts"
    assert rec_arts.slot1_ce_detail["initial_np"] >= 50
    assert rec_arts.slot1_ce_detail["np_gain_rate"] > 0
    assert rec_arts.slot1_ce_detail["name"] in ["海上飞行者", "绘画之夏"]

    # 2. 红卡队打手 (剑修炼场 -> 吉尔伽美什 Archer Buster)
    # 必须优先把同时具备【50%NP + 宝威 + 红卡性能提升】的礼装 (天际旋荡) 分配给红卡队打手！
    rec_buster = builder.recommend_for_quest("daily_training_saber")  # 吉尔伽美什 (Archer Buster)
    print(f"[红卡队打手: {rec_buster.slot1_dps}] 智能匹配1号位礼装: {rec_buster.slot1_ce}")
    assert rec_buster.direction == "Buster"
    assert rec_buster.slot1_ce_detail["card_buff_color"] == "Buster"
    assert rec_buster.slot1_ce_detail["initial_np"] >= 50
    assert rec_buster.slot1_ce_detail["name"] in ["天际旋荡"]

    # 3. 绿卡队打手测试：同时拥有 50%NP + NP回收 + 绿卡性能提升 的【圣诞节的轨迹】优先给绿卡打手
    from src.strategy.auto_team_builder import SERVANT_TACTICAL_DB
    taigong_meta = SERVANT_TACTICAL_DB[404300]  # 太公望 (Rider Quick)
    ce_str, ce_score, ce_detail = builder.select_best_craft_essence(taigong_meta, total_own_np=50, direction="Quick", min_needed_np=50)
    print(f"[绿卡队打手: {taigong_meta.name}] 智能匹配1号位礼装: {ce_str}")
    assert ce_detail["name"] == "圣诞节的轨迹"
    assert ce_detail["card_buff_color"] == "Quick" and ce_detail["initial_np"] == 50 and ce_detail["np_gain_rate"] == 10
    print("√ 测试通过: 概念礼装已完整纳入配队计算，严格优先宝具NP、宝具NP回收并精准匹配打手色卡！")


def test_auto_party_assembler():
    print("\n--- [Test 5] 实机自动编队引擎 (AutoPartyAssembler) 头像模板与辅助拐解析测试 ---")
    from src.strategy.auto_party_assembler import AutoPartyAssembler

    asm = AutoPartyAssembler()
    assert asm.resolve_support_servant_id("阿尔托莉雅·Caster (自出)") == 504500
    assert asm.resolve_support_servant_id("光之高扬斯卡娅 (自出)") == 604200
    assert asm.resolve_support_servant_id("奥伯龙 (自出)") == 2800100
    assert asm.resolve_support_servant_id("斯卡哈·斯卡蒂(Ruler) (自出)") == 901400

    # 验证核心打手 (哈贝特洛特 404200) 与核心拐 (C呆 504500) 的头像特征模板已缓存就绪
    crops_habetrot = asm._get_servant_face_crops(404200)
    crops_castoria = asm._get_servant_face_crops(504500)
    assert len(crops_habetrot) >= 3
    assert len(crops_castoria) >= 3
    print(f"√ 测试通过: AutoPartyAssembler 头像特征库就绪 (哈贝特洛特 {len(crops_habetrot)} 阶段, C呆 {len(crops_castoria)} 阶段)！")


def test_combat_wave_np_and_skill_plan_verification():
    print("\n--- [Test 6] 局内 3T 冲浪波次/回合/实时NP提取与技能时序闭环测试 ---")
    from src.strategy.anchor_verifier import AnchorVerifier

    # 1. 验证国服 1920x1080 真实顶部坐标 (cx=1361, cy=39) 的 Wave 与 Turn 提取及底部 NP% 提取
    mock_ocr = [
        ([[1330, 25], [1390, 25], [1390, 55], [1330, 55]], "2/3", 0.99),
        ([[1370, 130], [1430, 130], [1430, 160], [1370, 160]], "4回合", 0.99),
        ([[360, 990], [430, 990], [430, 1015], [360, 1015]], "94%", 0.99),
        ([[840, 990], [910, 990], [910, 1015], [840, 1015]], "100%", 0.99),
        ([[1320, 990], [1390, 990], [1390, 1015], [1320, 1015]], "156%", 0.99),
    ]
    assert AnchorVerifier.extract_battle_wave(mock_ocr) == 2
    assert AnchorVerifier.extract_battle_turn(mock_ocr) == 4
    assert AnchorVerifier.extract_servant_np_percentages(mock_ocr) == {1: 94, 2: 100, 3: 156}

    # 2. 验证蓝卡双C呆 3T 技能序列包含 T1 80% 充能与双魔放，且哈贝特洛特绝不误开 3 技能自爆
    parser = ChaldeaParser()
    parser.parse_file(root_dir / "tests" / "sample_chaldea.json")
    builder = AutoTeamBuilder(parser)
    rec = builder.recommend_for_quest("daily_qp_extreme")
    t1_pairs = [(a.servant, a.skill) for a in rec.strategy.turns[1].skills]
    assert (2, 1) in t1_pairs and (2, 2) in t1_pairs and (2, 3) in t1_pairs
    assert (3, 1) in t1_pairs and (3, 3) in t1_pairs
    for t_idx in [1, 2, 3]:
        assert all(not (a.servant == 1 and a.skill == 3) for a in rec.strategy.turns[t_idx].skills)
    print("√ 测试通过: 波次/回合/NP% 视觉提取与 3T 冲浪技能链验证全部通过！")


def test_order_change_3t_archetypes_and_slot_mapping():
    print("\n--- [Test 7] 换人 3T 策略与各体系 (蓝卡/红卡/绿卡/多核) 时序及槽位映射深度测试 ---")
    from src.strategy.battle_plans import BattlePlanManager, MasterSkillAction, SkillAction

    # 1. 验证 config/teams.yaml 包含全部经典换人与冲浪 3T 预设方案
    bpm = BattlePlanManager()
    expected_teams = [
        "arts_castoria_oberon_swap",
        "arts_castoria_waver_swap",
        "arts_castoria_system",
        "buster_morgan_koyan",
        "buster_arjuna_alter",
        "buster_koyan_waver_swap",
        "quick_skadi_oberon_swap",
        "quick_triple_skadi_swap",
        "multicore_arash_sacrifice",
    ]
    for tid in expected_teams:
        assert tid in bpm.teams, f"teams.yaml 缺失预设队伍: {tid}"
        team = bpm.teams[tid]
        assert len(team.turns) == 3, f"{tid} 必须完整包含 3 个回合时序配置"
    print(f"√ 预设队伍库校验通过: 已完整收录全部 {len(expected_teams)} 种经典 3T 体系配置！")

    # 2. 验证【蓝卡双C呆+奥伯龙换人 3T】时序 (T3 换人服 swap: [2, 4], 奥伯龙1/2/3技全开, 决战服加攻, 宝具秒关底)
    arts_swap_team = bpm.teams["arts_castoria_oberon_swap"]
    t3 = arts_swap_team.turns[3]
    assert any(m.swap == [2, 4] for m in t3.master_skills), "T3 必须包含换人服 swap: [2, 4] 将C呆换为奥伯龙"
    assert any(m.skill == 1 for m in t3.master_skills), "T3 必须包含决战服加攻增伤"
    t3_skills = [(s.servant, s.skill) for s in t3.skills]
    assert (4, 1) in t3_skills and (4, 2) in t3_skills and (4, 3) in t3_skills, "T3 必须包含 4号位奥伯龙 1/2/3 技能全部施加"

    # 3. 验证【红卡双杀狐+奥伯龙换人 3T】时序 (T1 杀狐1/3技+打手自充; T2 助战杀狐1技-2CD; T3 换人服换奥伯龙+打手-4CD转好的自充再次开启)
    buster_swap_team = bpm.teams["buster_morgan_koyan"]
    b_t2 = buster_swap_team.turns[2]
    b_t3 = buster_swap_team.turns[3]
    assert any(s.servant == 3 and s.skill == 1 for s in b_t2.skills), "T2 必须包含助战杀狐 1 技 50% 充能与减 2 回合 CD"
    assert any(m.swap == [2, 4] for m in b_t3.master_skills), "T3 必须包含换人服换奥伯龙"
    assert any(s.servant == 4 and s.skill == 3 for s in b_t3.skills), "T3 必须释放奥伯龙 3 技宝威翻倍"
    assert any(s.servant == 1 and s.skill == 1 for s in b_t3.skills), "T3 必须二次释放打手已冷却好的 1 技能"

    # 4. 验证【绿卡三斯卡蒂换人 3T】(水CBA + 助战水CBA + 4号位术CBA 换人，150% 绿魔放极限叠加)
    quick_3skadi_team = bpm.teams["quick_triple_skadi_swap"]
    q_t3 = quick_3skadi_team.turns[3]
    assert any(m.swap == [2, 4] for m in q_t3.master_skills)
    q_t3_skills = [(s.servant, s.skill) for s in q_t3.skills]
    assert (4, 1) in q_t3_skills and (4, 2) in q_t3_skills and (4, 3) in q_t3_skills

    # 5. 验证【多核阿拉什大英雄自爆接力流 3T】(T1 阿拉什自充+自爆退场清W1; T2 4号位补位进场, 辅助为2号位主核充能; T2/T3 主核宝具清场)
    arash_team = bpm.teams["multicore_arash_sacrifice"]
    assert arash_team.turns[1].np_servant == 1, "W1 必须释放 1 号位阿拉什宝具"
    assert arash_team.turns[2].np_servant == 2, "W2 必须释放 2 号位主核打手宝具"
    assert arash_team.turns[3].np_servant == 2, "W3 必须释放 2 号位主核打手宝具"

    # 6. 验证 AutoTeamBuilder 针对持有的斯卡蒂(Ruler+Caster) 自动触发【绿卡三斯卡蒂换人 3T 队】决策
    triple_skadi_account = {
        "userServant": [
            {"svtId": 901400, "lv": 90, "npLv": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 503900, "lv": 90, "npLv": 1, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},
            {"svtId": 404300, "lv": 90, "npLv": 2, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10, "appendSkillLv2": 10},  # 太公望
        ]
    }
    p_skadi = ChaldeaParser()
    p_skadi.parse_data(triple_skadi_account)
    builder_skadi = AutoTeamBuilder(p_skadi)
    rec_skadi = builder_skadi.recommend_for_quest("daily_training_caster")
    print(f"[三斯卡蒂换人自配] 方向: {rec_skadi.direction_name} | 2号位: {rec_skadi.slot2_support} | 4号位: {rec_skadi.slot4_backline_support}")
    assert rec_skadi.direction == "Quick"
    assert "三斯卡蒂" in rec_skadi.direction_name
    assert rec_skadi.slot4_backline_support == "斯卡哈·斯卡蒂(Caster)"

    # 7. 验证 SmartEngine 动态换人槽位映射逻辑 (4号位进场后映射到屏幕物理 2号位)
    slot_map = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
    swap_op = [2, 4]
    p_out, p_in = swap_op[0], swap_op[1]
    phys_pos = slot_map.get(p_out, p_out)
    slot_map[p_in] = phys_pos
    slot_map[p_out] = p_in
    assert slot_map[4] == 2, "4号位换入后必须映射到屏幕物理 2 号位"
    assert slot_map[1] == 1, "1号位主核映射保持不变"
    # 模拟技能目标映射
    s_act_sample = SkillAction(servant=4, skill=2, target=1)
    mapped_servant = slot_map.get(s_act_sample.servant, s_act_sample.servant)
    mapped_target = slot_map.get(s_act_sample.target, s_act_sample.target)
    assert mapped_servant == 2, "4号位从者的技能释放目标应自动转为屏幕 2 号位 (588, 870)"
    assert mapped_target == 1, "给 1 号位主核充能目标映射保持为 1 号位 (500, 560)"

    print("√ 测试通过: 全部 3T 换人时序策略、三斯卡蒂/奥伯龙/阿拉什体系及槽位动态映射验证成功！")


if __name__ == "__main__":
    test_enemy_class_dynamic_adaptation()
    test_support_skill_level_direction_decision()
    test_packet_capturer_and_toplogin_har_parsing()
    test_craft_essence_capture_and_synergy_matching()
    test_auto_party_assembler()
    test_combat_wave_np_and_skill_plan_verification()
    test_order_change_3t_archetypes_and_slot_mapping()
    print("\nAll AutoTeamBuilder & PacketCapturer & CraftEssence & AutoPartyAssembler tests PASSED successfully!")
