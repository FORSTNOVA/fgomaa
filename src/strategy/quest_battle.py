"""共享单关执行器：配队模拟、导航、编队与礼装、战斗、结算。"""
from typing import Any, Optional
import time

from src.strategy.screen_detector import GameScreen, ScreenDetector
from src.strategy.auto_team_builder import SERVANT_TACTICAL_DB


class QuestBattleExecutor:
    """依赖感知引擎提供视觉/动作能力，不依赖日常、周常工作流。"""
    def __init__(self, engine):
        self.engine = engine

    def stage(self, value):
        callback = getattr(self.engine, "quest_progress_callback", None)
        if callback is not None:
            callback(value)

    def navigate(self, plan, war, spot, name):
        if getattr(plan, "kind", "free") == "daily":
            from src.strategy.quest_navigation import navigate_daily_quest
            return navigate_daily_quest(self.engine, plan.quest_id)
        if getattr(plan,'kind','free')=='event':
            from src.strategy.event_navigation import resolve_spot
            from src.strategy.quest_catalog import quest_binding
            from src.workflows.quest_runner import quest_cleared
            entry=quest_binding(plan.quest_id)
            parser=self.engine.chaldea_parser
            if entry is not None and parser is not None:
                spot=resolve_spot(entry,lambda qid,phase:quest_cleared(parser,qid,phase))['spot']
        if getattr(plan,'first_clear',False):
            from src.strategy.first_clear_progress import inspect_free_quest
            from src.strategy.quest_catalog import quest_binding
            try:
                observation=inspect_free_quest(self.engine,plan.quest_id,quest_binding(plan.quest_id))
                if observation['completed'] or observation['next_phase']!=plan.phase:
                    raise ValueError('出击前实机阶段发生变化')
                nominal=self.engine.current_recommendation.laplace['quest_ap']
                if not 0<=observation['card_ap']<=nominal or observation['card_ap']!=plan.ap_per_run:
                    raise ValueError('出击前AP消耗与首通计划不一致')
                if observation['available_ap'] is None or observation['available_ap']<observation['card_ap']:
                    raise ValueError('出击前体力不足或无法确认')
                # 实机已核对的活动AP折扣不影响战斗模拟，只用于卡片身份核对。
                self.engine.current_recommendation.laplace['quest_ap']=observation['card_ap']
            except (ValueError,RuntimeError) as exc:
                self.engine.log(f'[首通出击停止] {exc}')
                return False
        return self.engine.navigate_to_free_quest(war, spot, name)

    def prepare(self, plan):
        engine = self.engine
        quest_name, war_name, spot_name = plan.name, plan.war, plan.spot
        quest_id = getattr(plan, "quest_id", "")
        self.stage("simulating")
        if engine.team_builder:
            try:
                engine.current_recommendation = engine.team_builder.recommend_for_quest(
                    target_quest=quest_name,
                    war_name=war_name,
                    spot_name=spot_name,
                    quest_id=quest_id,
                )
                if not engine._ensure_laplace():
                    return False
                engine.active_strategy = engine.current_recommendation.strategy
                engine.log(
                    f"★ [关卡智能对策] 敌方职阶: {engine.current_recommendation.enemy_class_cn} | "
                    f"首选打手: 1号位【{engine.current_recommendation.slot1_dps}】({engine.current_recommendation.slot1_class} {engine.current_recommendation.slot1_affinity}x克制) | "
                    f"策略: {engine.current_recommendation.strategy.name}"
                )
            except Exception as e:
                engine.current_recommendation = None
                engine.active_strategy = None
                engine.log(f"[关卡停止] 智能配队推荐失败，不能沿用上一站策略: {e}")
                return False

        if not engine._ensure_laplace():
            return False
        return True

    def run(
        self,
        plan: Any,
        cancel_event: Optional[Any] = None
    ) -> bool:
        """
        响应式关卡单站执行器:
        每一步先感知当前界面，若是 SUPPORT_SELECT / PARTY_CONFIRM / BATTLE_COMBAT 则闭环执行 3T 战斗与连续出击；
        若在主大厅/地图且未进入关卡，则智能提示用户或寻找关卡卡片，杜绝任何屏幕盲点！
        """
        engine = self.engine
        runs_needed = getattr(plan, "runs", 1)
        spot_name = getattr(plan, "spot", "")
        war_name = getattr(plan, "war", "")
        quest_name = getattr(plan, "name", "")

        cleared_runs = 0
        max_steps = 300
        step_count = 0
        guidance_logged = False
        engine.last_weekly_clears = 0
        engine.last_quest_clears = 0
        engine.last_quest_started = False
        engine.battle_result_counted = False
        battle_authorized = False
        battle_started = False
        first_clear_submitted = False
        if runs_needed <= 0 or engine.is_cancelled() or (cancel_event is not None and cancel_event.is_set()):
            return False

        engine.log(f">>> [响应式关卡站点] 启动执行: [{war_name}] {spot_name} - {quest_name} (需完成 {runs_needed} 场)")

        party_assembled_for_station = False
        quest_id = getattr(plan, "quest_id", "")
        if not self.prepare(plan):
            return False
        validated_quest = engine.current_recommendation.laplace
        quest_name = validated_quest.get("quest_name", quest_name)
        spot_name = validated_quest.get("spot", spot_name)

        # 启动前先确诊界面：若未处于助战/编队/战斗状态，立即启动全自动实机地图导航！
        self.stage("navigating")
        if not self.navigate(plan, war_name, spot_name, quest_name):
            engine.log("[关卡停止] 未确认目标关卡导航成功。")
            return False

        while step_count < max_steps:
            step_count += 1
            if engine.is_cancelled() or (cancel_event is not None and cancel_event.is_set()):
                engine.log("[任务中断] 接收到用户停止指令，退出关卡执行！")
                return False

            screen, details = engine.get_current_screen()
            all_text_str = " ".join(details.get("all_texts", []))

            if screen == GameScreen.UNKNOWN and "跳过" in all_text_str and any(t in all_text_str for t in ("记录", "自动")):
                engine.log("[关卡停止] 检测到非预期剧情界面，保留现场，不自动跳过或继续刷本。")
                return False

            if any(t in all_text_str for t in ["补充AP", "行动力恢复", "恢复行动力", "AP不足", "战斗失败", "全军覆没", "全灭", "败北"]):
                engine.log("[关卡停止] AP不足或战斗失败；保留已确认场次，不补记通关。")
                return False

            if cleared_runs >= runs_needed:
                # 严格闭环要求：必须等战斗结算彻底完成，且画面已脱离结算/连续出击（处于地图或大厅）时才允许退出！
                if screen in [GameScreen.REGIONAL_MAP, GameScreen.MAIN_MENU, GameScreen.MAIN_RECORD, GameScreen.CHALDEA_GATE]:
                    engine.log(f"√ [站点达成] [{war_name}] {spot_name} 全部 {runs_needed} 场战斗已圆满完成并返回地图！")
                    return True
                # 若仍在结算界面或连续出击弹窗中，继续向下交由 BATTLE_RESULT / CONTINUE_QUEST 处理推进

            # 1. 背包满拦截
            if screen == GameScreen.INVENTORY_FULL:
                close_pos = details.get("close_coord") or (960, 845)
                engine.log("【出击阻断】检测到背包已满，关闭弹窗并终止本站...")
                engine.ctrl.click(close_pos[0], close_pos[1], delay_after=1.5)
                return False

            # 2. 弹窗/子界面遮罩关闭
            # 弹窗/编制恢复弹窗快速关闭
            if screen in [GameScreen.POPUP_DIALOG, GameScreen.ANNOUNCEMENT, GameScreen.DAVINCI_WORKSHOP, GameScreen.CRAFT_ESSENCE_SELECT] or any(k in all_text_str for k in ["取消队伍编制", "恢复成变更前的状态", "请选择想要执行的项目"]):
                if "恢复成变更前的状态" in all_text_str or "取消队伍编制" in all_text_str:
                    confirm_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "决定", region=(960, 800, 1500, 1080)) or (1256, 954)
                    engine.log(f"  [编制弹窗] 检测到取消编制确认框，点击【决定】({confirm_btn})...")
                    engine.ctrl.click(confirm_btn[0], confirm_btn[1], delay_after=1.8)
                    continue
                if "请选择想要执行的项目" in all_text_str:
                    cancel_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "取消", region=(800, 750, 1200, 950)) or (960, 850)
                    engine.log(f"  [编成弹窗] 检测到编成菜单，点击【取消】({cancel_btn})...")
                    engine.ctrl.click(cancel_btn[0], cancel_btn[1], delay_after=1.5)
                    continue
                close_pos = (
                    details.get("close_coord")
                    or ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭", region=(0, 0, 350, 160))
                    or ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭")
                    or (960, 835)
                )
                engine.log(f"检测到弹窗或详情遮罩，点击【关闭】({close_pos})...")
                engine.ctrl.click(close_pos[0], close_pos[1], delay_after=1.5)
                continue

            # 3. 助战选择界面 (SUPPORT_SELECT)
            if screen == GameScreen.SUPPORT_SELECT:
                guidance_logged = False
                ocr_res = details.get("ocr_results", [])
                expected_level = engine.current_recommendation.laplace.get("recommended_level") if engine.current_recommendation else None
                if expected_level is not None and not engine._support_quest_level_matches(ocr_res, expected_level):
                    engine.log("[出击停止] 助战页的推荐等级与目标关卡不符或无法确认，禁止进入编队。")
                    return False
                engine.log(f"当前处于【助战选择界面】，正在智能选择助战从者...")
                sup_pos = engine.party_assembler.select_support_on_support_screen(
                    engine.ctrl,
                    engine.current_recommendation,
                    initial_ocr=ocr_res,
                )
                if sup_pos is None:
                    if engine._replace_unavailable_rotation_support():
                        continue
                    engine.log("[出击停止] 未确认方案指定的助战。")
                    return False
                engine.ctrl.click(sup_pos[0], sup_pos[1], delay_after=2.5)
                # 严格闭环验证：确认助战选择生效，成功进入出击编队确认界面
                for _ in range(5):
                    post_sc, _ = engine.get_current_screen()
                    if post_sc in [GameScreen.PARTY_CONFIRM, GameScreen.BATTLE_COMBAT]:
                        engine.log(f"√ [闭环核验] 助战选择生效，已成功进入【{post_sc.value}】！")
                        break
                    time.sleep(1.0)
                continue

            # 4. 出击确认编队界面 (PARTY_CONFIRM)
            if screen == GameScreen.PARTY_CONFIRM:
                self.stage("assembling")
                if not engine._ensure_laplace():
                    return False
                guidance_logged = False
                engine.log(">>> [编队确认] 当前处于【出击编队确认界面】...")
                fresh_img = engine.ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2, diff_threshold=0.012)

                # 若本站尚未完成换人组队，立即在实机界面自动将1号位换为对策打手、2/4号位换为核心辅助
                if engine.current_recommendation:
                    rec = engine.current_recommendation
                    engine.log(
                        f">>> [实机自动组队] 正在根据关卡敌方职阶调整出击阵容: "
                        f"1号位【{rec.slot1_dps}】({rec.slot1_class} {rec.slot1_affinity}x克制), "
                        f"2号位【{rec.slot2_support}】, 4号位【{rec.slot4_backline_support}】..."
                    )
                    assembled = engine.party_assembler.assemble_party_on_confirm_screen(
                        engine.ctrl,
                        rec=rec,
                        chaldea_parser=engine.chaldea_parser,
                    )
                    if not assembled:
                        engine.active_strategy = None
                        engine.log("[关卡停止] 编队未通过核验，不点击出击。")
                        return False
                    party_assembled_for_station = True
                    fresh_img = engine.ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2, diff_threshold=0.012)

                self.stage("equipping")
                if not engine._ensure_craft_essences():
                    return False
                fresh_img = engine.ctrl.screencap(force_fresh=True)
                fresh_ocr = ScreenDetector.run_ocr(fresh_img)
                # 真实实机阵容识别与策略自适应匹配
                detected_party, detected_strat, team_logs = engine.detector.identify_party_from_screen(fresh_ocr)
                for tl in team_logs:
                    engine.log(f"  [实机阵容感知] {tl}")

                rec = engine.current_recommendation
                slot1_is_recommended = False
                if rec and rec.slot1_dps:
                    # 检查 1 号位识别名称是否与推荐打手一致
                    if rec.slot1_dps in detected_party.slot1_servant or detected_party.slot1_servant in rec.slot1_dps:
                        slot1_is_recommended = True
                    else:
                        dps_id = None
                        for sid, meta in SERVANT_TACTICAL_DB.items():
                            if meta.name == rec.slot1_dps or rec.slot1_dps in meta.name:
                                dps_id = sid
                                break
                        if dps_id:
                            m_ok, _ = engine.party_assembler.verify_slot_has_servant(fresh_img, 1, dps_id)
                            if m_ok:
                                slot1_is_recommended = True

                if rec and slot1_is_recommended:
                    engine.active_strategy = rec.strategy
                    engine.log(f"√ 实机打手与对策方案一致，锁定策略: 「{engine.active_strategy.name}」")
                else:
                    engine.active_strategy = None
                    engine.log("[Laplace 停止] 实机主核与计算阵容不一致，不切换为未经校验的策略。")
                    return False

                engine.current_combat_turn = 1
                engine.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                engine.executed_waves.clear()
                engine.battle_result_counted = False
                engine.result_stall_count = 0

                start_pos = ScreenDetector.find_text_coordinate(fresh_ocr, "开始任务", region=(1400, 800, 1920, 1080)) or \
                            ScreenDetector.find_text_coordinate(fresh_ocr, "开始", region=(1400, 800, 1920, 1080)) or \
                            (1750, 940)
                engine.log(f"出击确认就绪，点击右下角金色【开始任务】大按钮: {start_pos}...")
                if getattr(plan,'first_clear',False) and first_clear_submitted:
                    engine.log('[首通停止] 本阶段已经提交过出击，未确认结果前不重复消耗AP。')
                    return False
                first_clear_submitted = True
                engine.ctrl.click(start_pos[0], start_pos[1], delay_after=2.5)
                battle_authorized = True
                battle_started = False
                # 严格闭环验证：确认出击指令生效，画面进入过图加载或局内战斗
                for _ in range(6):
                    post_sc, _ = engine.get_current_screen()
                    if post_sc in [GameScreen.LOADING, GameScreen.BATTLE_COMBAT, GameScreen.BATTLE_CARD_SELECT]:
                        engine.last_quest_started = True
                        if post_sc in [GameScreen.BATTLE_COMBAT, GameScreen.BATTLE_CARD_SELECT]:
                            battle_started = True
                        engine.log(f"√ [闭环核验] 出击指令生效，画面已成功进入【{post_sc.value}】！")
                        break
                    time.sleep(1.0)
                continue

            # 5. 加载中
            if screen == GameScreen.LOADING:
                time.sleep(2.0)
                continue

            # 6. 局内战斗中 (BATTLE_COMBAT)
            if screen == GameScreen.BATTLE_COMBAT:
                self.stage("fighting")
                if not battle_authorized:
                    engine.log("[关卡停止] 未确认本站出击操作，不能接管或计入其他战斗。")
                    return False
                battle_started = True
                guidance_logged = False
                engine.last_quest_started = True
                engine.result_stall_count = 0
                detected_wave = details.get("wave")
                if type(detected_wave) is not int or not 1 <= detected_wave <= 3:
                    engine.log("[战斗等待] 未确认当前波次，不默认第一波或执行技能。")
                    time.sleep(0.5)
                    continue
                detected_turn = details.get("turn") or engine.current_combat_turn
                np_map = details.get("np_percentages") or {}
                wave = detected_wave if (1 <= detected_wave <= 3) else min(3, engine.current_combat_turn)
                is_extra_turn_in_wave = (wave in engine.executed_waves)

                combat_ocr = details.get("ocr_results", [])
                slot1_txt = " ".join([t for b, t, _ in combat_ocr if any(80 <= p[0] <= 480 and 920 <= p[1] <= 1080 for p in b)])
                if slot1_txt:
                    engine._last_combat_slot1_name = slot1_txt

                preferred_color = "Arts"
                if engine.current_recommendation and engine.current_recommendation.direction in ["Arts", "Buster", "Quick"]:
                    preferred_color = engine.current_recommendation.direction

                engine.log(
                    f"★ [局内战斗] Wave {wave}/3 | 第 {detected_turn} 回合 | 实时 NP: 1号位={np_map.get(1, '?')}%, 2号位={np_map.get(2, '?')}%, 3号位={np_map.get(3, '?')}%"
                )

                # 释放技能前进行局内核验
                if engine.active_strategy and (wave not in engine.executed_waves) and (wave in engine.active_strategy.turns):
                    if wave == 1 and detected_turn == 1:
                        strat_name = engine.active_strategy.name
                        if "村正" in strat_name and ("赵云" in slot1_txt or "托内莉可" in slot1_txt or "梣" in slot1_txt or "阿尔托莉雅" in slot1_txt):
                            engine.log(f"★ [安全防护] 局内核验发现 1号位为【{slot1_txt.strip()}】，与原定策略「{strat_name}」不符，安全转入自适应通用模式，避免错放技能！")
                            engine.active_strategy = None
                        elif "摩根" in strat_name and ("赵云" in slot1_txt or "村正" in slot1_txt):
                            engine.log(f"★ [安全防护] 局内核验发现 1号位为【{slot1_txt.strip()}】，与原定策略「{strat_name}」不符，安全转入自适应通用模式！")
                            engine.active_strategy = None
                    engine.executed_waves.add(wave)
                    if engine.active_strategy is not None:
                        turn_data = engine.active_strategy.turns[wave]
                        engine.log(f"★ 执行「{engine.active_strategy.name}」Wave {wave}/3 技能时序...")
                        if engine._execute_turn_actions(turn_data, wave) is False:
                            engine.log("[Laplace 停止] 技能或换人未执行成功，不继续释放宝具。")
                            return False
                else:
                    engine.executed_waves.add(wave)

                # 动态冲浪与冷却恢复技能释放
                final_np1 = engine._combat_np_after_actions(
                    wave=wave,
                    is_extra_turn_in_wave=is_extra_turn_in_wave,
                    real_turn_num=detected_turn,
                    initial_np_map=np_map
                )
                if final_np1 is None:
                    return False

                # 宝具与选卡
                np_idx = 1
                if engine.active_strategy and wave in engine.active_strategy.turns:
                    turn = engine.active_strategy.turns[wave]
                    raw_np = turn.np_servant
                    np_idx = None if turn.attack_mode == "facecards" else engine.party_slot_to_screen_slot.get(raw_np, raw_np)

                # 若当前主核 NP 未满 100%，检查场上是否有其他从者 NP >= 100% 可以开宝具直接清场
                if final_np1 < 100 and not (engine.current_recommendation and engine.current_recommendation.laplace.get("passed")):
                    alt_np = next((s for s in [3, 2] if np_map.get(s, 0) >= 100), None)
                    if alt_np:
                        engine.log(f"  [智能选核] 1号位 NP 未满 ({final_np1}%)，改由满充能的 {alt_np} 号位从者 (NP={np_map.get(alt_np)}%) 释放宝具！")
                        np_idx = alt_np

                engine.log("  [指令出击] 普通攻击清场（不点宝具）" if np_idx is None else f"  [指令出击] 释放 {np_idx} 号位宝具。")
                if engine.ctrl.select_attack_and_np(np_servant_idx=np_idx, preferred_color=preferred_color, allow_support_np=False) is False:
                    return False

                engine.current_combat_turn = detected_turn + 1
                engine._wait_after_attack(max_wait=25.0)
                continue

            # 7. 战斗选卡误入 (BATTLE_CARD_SELECT)
            if screen == GameScreen.BATTLE_CARD_SELECT:
                if engine.current_recommendation and engine.current_recommendation.laplace.get("passed"):
                    engine.log("[Laplace 停止] 意外停留在选卡页面，不按默认1号位补点。")
                    return False
                if not battle_authorized:
                    return False
                battle_started = True
                engine.log("处于选卡状态，激活宝具并选卡...")
                engine.ctrl.select_attack_and_np(np_servant_idx=1, preferred_color="Arts", allow_support_np=False)
                engine._wait_after_attack(max_wait=25.0)
                continue

            # 8. 连续出击弹窗 (CONTINUE_QUEST) - 提升到结算前处理，避免连续出击被战利品结算误判或误关
            has_continue_panel = (
                screen == GameScreen.CONTINUE_QUEST
                or any(kw in all_text_str for kw in ["连续出击", "再次挑战", "是否连续进行同一关卡"])
            )
            if has_continue_panel:
                ocr_res = details.get("ocr_results", [])
                if not engine.battle_result_counted:
                    if not battle_started or not battle_authorized or not any(kw in all_text_str for kw in ["连续出击", "再次挑战", "是否连续进行同一关卡"]):
                        engine.log("[关卡停止] 未观察到本站战斗完成，不能从弹窗补记通关。")
                        return False
                    engine.battle_result_counted = True
                    cleared_runs += 1
                    engine.last_weekly_clears = cleared_runs
                    engine.last_quest_clears = cleared_runs
                    self.stage("settling")
                    battle_authorized = False
                    engine.log(f"√ 已通关第 {cleared_runs}/{runs_needed} 场 [{spot_name}]！")

                if cleared_runs < runs_needed:
                    cont_pos = (
                        ScreenDetector.find_text_coordinate(ocr_res, "连续出击", region=(960, 650, 1650, 980))
                        or ScreenDetector.find_text_coordinate(ocr_res, "再次挑战", region=(960, 650, 1650, 980))
                    )
                    if cont_pos is None:
                        return False
                    engine.log(f"点击【连续出击】继续完成第 {cleared_runs+1}/{runs_needed} 场 ({cont_pos})...")
                    engine.ctrl.click(cont_pos[0], cont_pos[1], delay_after=2.2)
                    engine.battle_result_counted = False
                    battle_authorized = True
                    battle_started = False
                    engine.result_stall_count = 0
                    engine.current_combat_turn = 1
                    engine.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                    engine.executed_waves.clear()
                else:
                    close_cont = (
                        ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(300, 650, 960, 980))
                    )
                    if close_cont is None:
                        return False
                    engine.log(f"本站目标场次已打满 ({cleared_runs}/{runs_needed})，点击【关闭】退出连续出击...")
                    engine.ctrl.click(close_cont[0], close_cont[1], delay_after=2.0)
                    # 闭环确认：等待画面切回大地图或主界面
                    for _ in range(6):
                        post_sc, _ = engine.get_current_screen()
                        if post_sc in [GameScreen.REGIONAL_MAP, GameScreen.MAIN_MENU, GameScreen.MAIN_RECORD]:
                            engine.log(f"√ [闭环核验] 已确认安全返回地图 ({post_sc.value})，本站执行完毕！")
                            return True
                        time.sleep(1.0)
                    engine.log("[关卡停止] 未确认离开连续出击弹窗，不能进入下一站。")
                    return False
                continue

            # 9. 战利品结算 (BATTLE_RESULT)
            if screen == GameScreen.BATTLE_RESULT:
                self.stage("settling")
                ocr_res = details.get("ocr_results", [])
                if not engine.battle_result_counted:
                    if not battle_started or not battle_authorized:
                        engine.log("[关卡停止] 结算页没有本站战斗依据，不补记通关。")
                        return False
                    engine.battle_result_counted = True
                    cleared_runs += 1
                    engine.last_weekly_clears = cleared_runs
                    engine.last_quest_clears = cleared_runs
                    self.stage("settling")
                    battle_authorized = False
                    engine.log(f"√ 战斗胜利！完成 [{spot_name}] 第 {cleared_runs}/{runs_needed} 场。")
                    engine.current_combat_turn = 1
                    engine.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                    engine.executed_waves.clear()
                    if cleared_runs >= runs_needed:
                        engine.log(f"√ 本站 [{spot_name}] 目标场次已全部打完 ({cleared_runs}/{runs_needed})！")

                # 严禁在存在连续出击意图时误触关闭
                if not any(kw in all_text_str for kw in ["连续出击", "再次挑战", "是否连续进行同一关卡"]):
                    popup_close = ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(600, 620, 1320, 980))
                    if popup_close:
                        engine.log(f"结算界面检测到【关闭】按钮 ({popup_close})，点击关闭...")
                        engine.ctrl.click(popup_close[0], popup_close[1], delay_after=1.2)
                        continue

                reject_friend = ScreenDetector.find_text_coordinate(ocr_res, "不申请", region=(300, 750, 800, 1000))
                if reject_friend:
                    engine.log(f"检测到好友申请界面，点击【不申请】({reject_friend})...")
                    engine.ctrl.click(reject_friend[0], reject_friend[1], delay_after=1.5)
                    continue

                next_pos = ScreenDetector.find_text_coordinate(ocr_res, "下一步", region=(1400, 900, 1920, 1080))
                if next_pos:
                    engine.log(f"当前处于战利品结算界面，点击右下角金色【下一步】: {next_pos}...")
                    engine.ctrl.click(next_pos[0], next_pos[1], delay_after=1.2)
                else:
                    engine.log("结算阶段 (经验/羁绊/点击屏幕)，点击中央推进屏幕 (960, 750)...")
                    engine.ctrl.click(960, 750, delay_after=1.2)
                continue

            # 若在主大厅、主线记录列表或区域地图中且未进入关卡，调用全自动实机地图导航
            if screen in [GameScreen.MAIN_MENU, GameScreen.MAIN_RECORD, GameScreen.REGIONAL_MAP]:
                if getattr(plan,'first_clear',False) and first_clear_submitted:
                    engine.log('[首通停止] 出击后意外返回地图且没有结算依据，保留现场。')
                    return False
                engine.log(f"当前处于【{screen.value}】，调用全自动地图寻路进入【{war_name} -> {spot_name}】...")
                nav_ok = self.navigate(plan, war_name, spot_name, quest_name)
                if nav_ok:
                    continue
                engine.log("[关卡停止] 目标关卡导航失败，不继续其他站点。")
                return False

            # 若仍未进入，输出智能导航引导
            if not guidance_logged:
                guidance_logged = True
                if battle_authorized:
                    engine.log('[战斗过渡等待] 动画画面暂未识别，等待指令阶段或结算，不执行额外点击。')
                elif getattr(plan,'first_clear',False):
                    engine.log('[首通等待] 等待实机界面稳定，不能确认身份时不会出击。')
                else:
                    engine.log(f"★ [关卡智能引导] 当前站点: 【{war_name} -> {spot_name} ({quest_name})】(已完成 {cleared_runs}/{runs_needed} 场)")
                    engine.log("    请在模拟器中点击进入该关卡至【助战选择】或【队伍确认】界面，MAA 将实时感知并全自动接管！")

            time.sleep(2.0)

        engine.log("[关卡停止] 超过步骤上限，未确认本站安全结束。")
        return False

