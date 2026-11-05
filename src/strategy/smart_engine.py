"""
Smart Perception-driven FGO Automation Engine
真正的响应式闭环状态机 (Reactive Goal-Driven State Machine)
核心准则：每一步操作前均实时截屏 OCR 确诊当前真实界面，再严格依据当前页面执行单一原子动作。
彻底杜绝预设假定流程、消除误判与空转。
"""

from __future__ import annotations
import re
import time
from typing import Optional, Callable, Dict, Any, List, Tuple
from PIL import Image

from src.core.controller import FGOController
from src.strategy.screen_detector import ScreenDetector, GameScreen
from src.strategy.anchor_verifier import AnchorVerifier
from src.strategy.battle_plans import BattlePlanManager, TeamStrategy
from src.strategy.team_detector import TeamDetector
from src.strategy.auto_team_builder import AutoTeamBuilder, AutoTeamRecommendation, QUEST_ENEMY_PROFILES, SERVANT_TACTICAL_DB
from src.strategy.auto_party_assembler import AutoPartyAssembler
from src.parser.chaldea_parser import ChaldeaParser


class SmartEngine:
    """真实 OCR 视觉驱动的智能闭环感知决策引擎"""

    def __init__(
        self,
        controller: FGOController,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[Any] = None,
        chaldea_parser: Optional[ChaldeaParser] = None
    ):
        self.ctrl = controller
        self.log_cb = log_callback or (lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True))
        self.cancel_event = cancel_event
        self.chaldea_parser = chaldea_parser
        self.plan_mgr = BattlePlanManager()
        self.detector = TeamDetector(plan_manager=self.plan_mgr, chaldea_parser=self.chaldea_parser)
        self.team_builder = AutoTeamBuilder(self.chaldea_parser) if self.chaldea_parser else None
        self.party_assembler = AutoPartyAssembler(log_callback=self.log)
        self.current_recommendation: Optional[AutoTeamRecommendation] = None
        self.active_strategy: Optional[TeamStrategy] = None
        self.current_combat_turn = 1
        self.executed_waves: set[int] = set()
        self.gate_swipe_count = 0
        self.gate_bottom_confirm_count = 0
        self.battle_result_counted = False
        self.result_stall_count = 0
        self.party_switch_attempts = 0
        self.party_slot_to_screen_slot: Dict[int, int] = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        self.failed_combat_skills: set[Tuple[int, int]] = set()
        self._last_combat_slot1_name: str = ""
        self.last_weekly_clears = 0
        self.last_quest_clears = 0

    def _ensure_laplace(self) -> bool:
        from src.strategy.laplace import LaplaceService, recommendation_signature
        rec = self.current_recommendation
        if rec is None or self.chaldea_parser is None:
            self.active_strategy = None
            self.log("[出击停止] 缺少明确的账号与推荐阵容，无法进行 Laplace 战斗校验。")
            return False
        result = rec.laplace
        if (result.get("passed") and result.get("signature") == recommendation_signature(rec, self.chaldea_parser)
                and not self.is_cancelled()):
            self.active_strategy = rec.strategy
            return True
        if not LaplaceService().validate(rec, self.chaldea_parser, self.cancel_event, self.log):
            self.active_strategy = None
            self.log("[出击停止] Laplace 校验未通过，不点击出击。")
            return False
        self.active_strategy = rec.strategy
        return True

    def _replace_unavailable_rotation_support(self):
        rec = self.current_recommendation
        if (rec is None or rec.battle_mode != "rotation" or self.team_builder is None
                or self.chaldea_parser is None or self.is_cancelled()):
            return False
        from src.strategy.laplace import LaplaceService
        replacement = self.team_builder.recommend_for_quest(quest_id=rec.target_quest_id)
        if replacement.target_quest_id != rec.target_quest_id:
            return False
        self.log("[助战重新配队] 三打手助战不可用，重新校验同一关卡的常规连发方案。")
        if not LaplaceService().validate(replacement, self.chaldea_parser, self.cancel_event,
                                         self.log, allow_rotation=False):
            return False
        self.current_recommendation = replacement
        self.active_strategy = replacement.strategy
        return True

    def _ensure_craft_essences(self) -> bool:
        from src.strategy.craft_essence_picker import CraftEssencePicker
        if not CraftEssencePicker(self.ctrl, self.log).ensure(self.current_recommendation, self.chaldea_parser):
            self.active_strategy = None
            return False
        return True

    def _get_current_np_and_ready_skills(self) -> Tuple[Dict[int, int], Dict[Tuple[int, int], bool]]:
        """
        截取当前战斗画面，准确读取 1~3 号位实时 NP% 与 9 个从者技能的实时冷却就绪状态
        """
        np_map, ready_skills = {}, {}
        for attempt in range(3):
            if self.is_cancelled():
                return {}, {}
            img = self.ctrl.screencap(force_fresh=True)
            ready_skills = AnchorVerifier.detect_ready_skills(img)
            np_map = ScreenDetector.read_servant_np_percentages(img)
            if len(np_map) == 3:
                break
            if attempt < 2:
                self.log("  [NP复核] 局部读数未完整确认，重新截图核对。")
                time.sleep(0.2)
        return np_map, ready_skills

    def _is_forbidden_skill(self, servant_idx: int, skill_idx: int, is_final_wave: bool) -> bool:
        """
        判断某个技能是否为不可用/条件受限/自爆/永眠等技能：
        - 已经在 failed_combat_skills 释放失败黑名单中的技能，一律禁止重复尝试！
        - 哈贝特洛特 (1号位主核) 的 3 技能【守护妖精】会赋予女性队友无敌并使自身即死退场，严禁释放！
        - 奥尔加玛丽 (U-Olga Marie) 1 技能【终极UEX】严格要求 NP >= 100%，3 技能【究极宣言 EX】第三再临置灰禁用，严禁释放！
        - 奥伯龙 3 技能【梦之终结】回合末永久睡眠，严禁在非关底波次 (Wave 1/2) 提前释放！
        """
        if (servant_idx, skill_idx) in self.failed_combat_skills:
            return True
        ids = getattr(self.current_recommendation, "party_servant_ids", {}) if self.current_recommendation else {}
        logical = next((slot for slot, physical in self.party_slot_to_screen_slot.items() if physical == servant_idx), servant_idx)
        if ids:
            sid = ids.get(logical)
            if sid == 404200 and skill_idx == 3:
                return True
            if sid == 2800100 and skill_idx == 3 and not is_final_wave:
                return True

        dps_name = self.current_recommendation.slot1_dps if self.current_recommendation else ""
        sup2_name = self.current_recommendation.slot2_support if self.current_recommendation else ""
        sup4_name = self.current_recommendation.slot4_backline_support if self.current_recommendation else ""
        slot1_name = getattr(self, "_last_combat_slot1_name", "")
        combined_dps = f"{dps_name} {slot1_name}"

        # 哈贝特洛特 3 技能即死自爆
        if servant_idx == 1 and skill_idx == 3 and "哈贝特洛特" in combined_dps:
            return True

        # 奥尔加玛丽（U-Olga Marie）特殊前置条件技能限制
        if servant_idx == 1 and any(k in combined_dps for k in ["奥尔加玛丽", "所罗门", "奥尔加", "阿尼姆斯菲亚", "阳"]):
            # 1 技能【终极UEX】必须 100% NP 才能开
            # 3 技能【究极宣言 EX】第一/第二再临才能开，实战第三再临置灰
            if skill_idx in (1, 3):
                return True

        # 奥伯龙 3 技能关底前禁止
        if not is_final_wave and skill_idx == 3:
            logical_slot = next((slot for slot, physical in self.party_slot_to_screen_slot.items()
                                 if physical == servant_idx and slot in (2, 4)), None)
            if ((logical_slot == 2 and "奥伯龙" in sup2_name)
                    or (logical_slot == 4 and "奥伯龙" in sup4_name)):
                return True
        return False

    def _dynamic_top_up_and_cooldown_recovery(
        self,
        wave: int,
        is_extra_turn_in_wave: bool,
        real_turn_num: int,
        initial_np_map: Optional[Dict[int, int]] = None
    ) -> int:
        """
        动态冲浪充能补足与冷却恢复技能调度引擎：
        1. 若当前波次已进入补刀回合 (is_extra_turn_in_wave) 或总回合数 > 3 (说明后续回合有技能 CD 转好恢复)，
           自动扫描场上所有已冷却完毕 (Off-Cooldown) 的增益/充能技能并立即释放；
        2. 在点击 Attack 前实时读取 1号位主核 NP%：若 NP < 100%，按优先级依次释放场上就绪的辅助充能技或打手自充技，
           直至 1号位 NP >= 100% 或无可用充能技能，保障 3T 稳定连发冲浪！
        返回最终 1号位主核的实时 NP 百分比。
        """
        is_final_wave = (wave >= 3)
        img = self.ctrl.screencap(force_fresh=True)
        ready_skills = AnchorVerifier.detect_ready_skills(img)
        if initial_np_map is not None and 1 in initial_np_map:
            np1 = initial_np_map[1]
        else:
            full_ocr = ScreenDetector.run_ocr(img)
            np_map = AnchorVerifier.extract_servant_np_percentages(full_ocr)
            np1 = np_map.get(1, 0)

        # 阶段 A: 补刀回合 / 第4回合及以后技能 CD 转好恢复 -> 自动使用所有已恢复就绪的技能
        if is_extra_turn_in_wave or real_turn_num > 3:
            ready_list = [
                (s, sk)
                for (s, sk) in [
                    (2, 1), (2, 2), (2, 3),
                    (3, 1), (3, 2), (3, 3),
                    (1, 2),
                ]
                if ready_skills.get((s, sk), False)
                and (s, sk) not in self.failed_combat_skills
                and not self._is_forbidden_skill(s, sk, is_final_wave)
            ]
            if ready_list:
                self.log(f"  🔄 [技能CD恢复感知] 检测到第 {real_turn_num} 回合存在可用/已转好技能 {ready_list}，自动依次释放...")
                for s_idx, sk_idx in ready_list:
                    if self.is_cancelled():
                        break
                    cast_ok = self.ctrl.cast_servant_skill(s_idx, sk_idx, target_idx=1)
                    if not cast_ok:
                        self.failed_combat_skills.add((s_idx, sk_idx))
                np_map, ready_skills = self._get_current_np_and_ready_skills()
                np1 = np_map.get(1, np1)

        # 阶段 B: 实时宝具 NP 阈值闭环校验 (若 1号位 NP < 100%，动态调用剩余充能技能补满 100%)
        if np1 < 100:
            charge_candidates = [
                (3, 2), (2, 2),
                (3, 1), (2, 1),
                (1, 2),
                (3, 3), (2, 3),
            ]
            available_chargers = [
                (s, sk) for (s, sk) in charge_candidates
                if ready_skills.get((s, sk), False)
                and (s, sk) not in self.failed_combat_skills
                and not self._is_forbidden_skill(s, sk, is_final_wave)
            ]
            if available_chargers:
                self.log(f"  ⚡ [动态冲浪补NP] 当前 1号位主核宝具 NP 为 {np1}% (< 100%)，自动调用就绪技能补足充能...")
                for s_idx, sk_idx in available_chargers:
                    if self.is_cancelled() or np1 >= 100:
                        break
                    if (s_idx, sk_idx) in self.failed_combat_skills:
                        continue
                    cast_ok = self.ctrl.cast_servant_skill(s_idx, sk_idx, target_idx=1)
                    if not cast_ok:
                        self.failed_combat_skills.add((s_idx, sk_idx))
                        continue
                    np_map, ready_skills = self._get_current_np_and_ready_skills()
                    new_np1 = np_map.get(1, np1)
                    if new_np1 == np1:
                        # 释放后 NP 未变化，说明该技能不具备充能效果，加入黑名单避免重复尝试
                        self.failed_combat_skills.add((s_idx, sk_idx))
                    self.log(f"  √ [动态冲浪补NP] 释放 ({s_idx}号位-{sk_idx}技) 后，1号位主核实时 NP: {np1}% -> {new_np1}%")
                    np1 = new_np1

        return np1

    def _wait_after_attack(self, max_wait: float = 25.0) -> None:
        """
        选卡出击后的智能动画等待闭环：
        等待宝具动画、普通攻击、波次切换与敌方行动完全结束，
        一旦检测到下一回合右下角【攻击】按钮亮起或进入【战利品结算】界面即刻唤醒，
        既不提前在动画中乱点，也不多浪费一秒等待时间！
        """
        self.log("  [回合演出] 指令卡已发出，正在监控宝具演出与回合流转...")
        time.sleep(3.8)
        start_wait = time.time()
        check_count = 0

        while time.time() - start_wait < max_wait:
            if self.is_cancelled():
                return
            frame = self.ctrl.screencap(force_fresh=True)
            if AnchorVerifier.is_attack_button_ready(frame):
                self.log("  √ [回合就绪] 检测到下一回合右下角【攻击】按钮已亮起就绪！")
                return

            check_count += 1
            if check_count % 2 == 0:
                sc, _, _ = ScreenDetector.detect(frame)
                if sc in [GameScreen.BATTLE_RESULT, GameScreen.CONTINUE_QUEST, GameScreen.POPUP_DIALOG]:
                    self.log(f"  √ [战斗结束] 检测到画面已进入结算阶段 ({sc.value})！")
                    return

            time.sleep(0.85)

    def _combat_np_after_actions(self, wave, is_extra_turn_in_wave, real_turn_num, initial_np_map=None):
        if self.current_recommendation and self.current_recommendation.laplace.get("passed"):
            if is_extra_turn_in_wave:
                self.log("[Laplace 停止] 本面未按计算结果清场，停止自动补刀。")
                return None
            turn = self.active_strategy.turns.get(wave) if self.active_strategy else None
            if turn is None:
                self.log("[Laplace 停止] 当前波次没有已校验的行动，停止选卡。")
                return None
            if turn and turn.attack_mode == "facecards":
                return 0  # This action was proved without NP or a particular hand.
            logical = turn.np_servant if turn and turn.np_servant else 1
            physical = self.party_slot_to_screen_slot.get(logical, logical)
            percentages, _ = self._get_current_np_and_ready_skills()
            value = percentages.get(physical)
            if value is None or value < 100:
                self.log(f"[Laplace 停止] 技能释放后{physical}号位 NP={value if value is not None else '未识别'}，与校验方案不一致。")
                return None
            return value
        return self._dynamic_top_up_and_cooldown_recovery(wave, is_extra_turn_in_wave, real_turn_num, initial_np_map)

    def _execute_turn_actions(self, turn_data: Any, wave: int) -> bool:
        """
        按 FGO 换人服时序与动态槽位映射执行回合技能与御主技能：
        1. 检查本回合是否存在换人操作 (MasterSkillAction with swap: [p_out, p_in], 如 [2, 4])；
        2. 若有换人：
           - 优先执行属于原前排从者 (p_out) 的预设技能；
           - 打开御主技能面板并执行换人 (swap)；
           - 换人完成后，更新从者槽位到屏幕物理坐标的动态映射 (self.party_slot_to_screen_slot[p_in] = screen_slot)；
           - 依次执行新上场从者及其他从者的技能，自动将 4号位映射至当前屏幕槽位 (如 2号位)；
           - 释放剩余御主技能 (如决战用迦勒底制服 1技全员加攻+宝威增伤)；
        3. 若无换人：
           - 正常按屏幕槽位映射释放所有从者技能与御主技能。
        """
        is_final_wave = (wave >= 3)
        swap_action = next((m for m in turn_data.master_skills if m.swap), None)

        if swap_action and swap_action.swap and len(swap_action.swap) == 2:
            p_out, p_in = swap_action.swap[0], swap_action.swap[1]
            phys_out = self.party_slot_to_screen_slot.get(p_out, p_out)

            # 分离需要在换人前释放的原前排从者技能与换人后释放的技能
            has_p_in_skills = any(s.servant == p_in for s in turn_data.skills)
            pre_swap_skills: List[Any] = []
            post_swap_skills: List[Any] = []

            for s_act in turn_data.skills:
                if has_p_in_skills and s_act.servant == p_out:
                    pre_swap_skills.append(s_act)
                else:
                    post_swap_skills.append(s_act)

            # (1) 释放换人前的技能
            for s_act in pre_swap_skills:
                if self.is_cancelled():
                    return False
                s_idx = self.party_slot_to_screen_slot.get(s_act.servant, s_act.servant)
                t_idx = self.party_slot_to_screen_slot.get(s_act.target, s_act.target) if s_act.target else None
                if self._is_forbidden_skill(s_idx, s_act.skill, is_final_wave=is_final_wave):
                    if self.current_recommendation and self.current_recommendation.laplace.get("passed"):
                        self.log("[Laplace 停止] 校验方案中的技能被运行规则拦截，停止执行。")
                        return False
                    continue
                if not self.ctrl.cast_servant_skill(s_idx, s_act.skill, target_idx=t_idx):
                    return False

            # (2) 执行御主换人
            if not self.is_cancelled():
                self.log(f"  [御主技能] 换人服技能: 将前排 {phys_out} 号位 与后排 {p_in} 号位从者互换...")
                if not self.ctrl.cast_master_skill(swap_action.skill, swap=swap_action.swap):
                    return False
                # 更新槽位映射：后排 p_in 现在位于屏幕物理槽位 phys_out
                self.party_slot_to_screen_slot[p_in] = phys_out
                self.party_slot_to_screen_slot[p_out] = p_in
                self.log(f"  √ [换人槽位映射] 后排 {p_in} 号位从者已就位至屏幕第 {phys_out} 槽位！")

            # (3) 释放换人后的从者技能
            for s_act in post_swap_skills:
                if self.is_cancelled():
                    return False
                # 将 4号位或 2号位准确映射至屏幕物理槽位 (1..3)
                s_idx = self.party_slot_to_screen_slot.get(s_act.servant, s_act.servant)
                t_idx = self.party_slot_to_screen_slot.get(s_act.target, s_act.target) if s_act.target else None
                if self._is_forbidden_skill(s_idx, s_act.skill, is_final_wave=is_final_wave):
                    if self.current_recommendation and self.current_recommendation.laplace.get("passed"):
                        self.log("[Laplace 停止] 校验方案中的技能被运行规则拦截，停止执行。")
                        return False
                    continue
                if not self.ctrl.cast_servant_skill(s_idx, s_act.skill, target_idx=t_idx):
                    return False

            # (4) 释放非换人的剩余御主技能 (如加攻/宝威)
            for m_act in turn_data.master_skills:
                if m_act.swap or self.is_cancelled():
                    continue
                t_idx = self.party_slot_to_screen_slot.get(m_act.target, m_act.target) if m_act.target else 1
                self.log(f"  [御主技能] 释放第 {m_act.skill} 技能 (目标: {t_idx} 号位)...")
                if not self.ctrl.cast_master_skill(m_act.skill, target_idx=t_idx):
                    return False

        else:
            # 无换人普通回合
            if turn_data.skills:
                for s_act in turn_data.skills:
                    if self.is_cancelled():
                        return False
                    s_idx = self.party_slot_to_screen_slot.get(s_act.servant, s_act.servant)
                    t_idx = self.party_slot_to_screen_slot.get(s_act.target, s_act.target) if s_act.target else None
                    if self._is_forbidden_skill(s_idx, s_act.skill, is_final_wave=is_final_wave):
                        if self.current_recommendation and self.current_recommendation.laplace.get("passed"):
                            self.log("[Laplace 停止] 校验方案中的技能被运行规则拦截，停止执行。")
                            return False
                        continue
                    if not self.ctrl.cast_servant_skill(s_idx, s_act.skill, target_idx=t_idx):
                        return False

            if turn_data.master_skills:
                for m_act in turn_data.master_skills:
                    if self.is_cancelled():
                        return False
                    t_idx = self.party_slot_to_screen_slot.get(m_act.target, m_act.target) if m_act.target else 1
                    self.log(f"  [御主技能] 释放第 {m_act.skill} 技能 (目标: {t_idx} 号位)...")
                    if not self.ctrl.cast_master_skill(m_act.skill, target_idx=t_idx):
                        return False
        return True

    def log(self, msg: str):
        try:
            self.log_cb(msg)
        except (UnicodeEncodeError, UnicodeError):
            try:
                safe_msg = msg.encode("gbk", "ignore").decode("gbk")
                self.log_cb(safe_msg)
            except Exception:
                pass
        except Exception:
            pass

    def is_cancelled(self) -> bool:
        """检查外部是否已下发停止/中断信号"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    def get_current_screen(self, wait_still: bool = False) -> Tuple[GameScreen, Dict[str, Any]]:
        """实机截屏并通过 RapidOCR 进行文字语义感知 (支持 MAA 画面消抖等待)"""
        if wait_still:
            img = self.ctrl.wait_for_still(timeout=1.8, min_still_duration=0.15, diff_threshold=0.012)
        else:
            img = self.ctrl.screencap(force_fresh=True)
        screen, conf, details = ScreenDetector.detect(img)
        self.log(f"[OCR 视觉感知] 当前识别界面: 「{screen.value}」 (置信度: {conf:.2f} | 理由: {details.get('reason', '')})")
        return screen, details

    # ==================== 响应式闭环总状态机 ====================

    def complete_free_summon(self, pos: Tuple[int, int]) -> bool:
        """确认免费弹窗后逐帧推进，看到结果并返回卡池才算完成。"""
        self.ctrl.click(*pos, delay_after=1.5)
        confirmed = False
        result_seen = False
        deadline = time.monotonic() + 90
        for _ in range(60):
            if self.is_cancelled() or time.monotonic() >= deadline:
                break
            screen, details = self.get_current_screen(wait_still=True)
            ocr = details.get("ocr_results", [])
            if not confirmed:
                modal = "".join(text.replace(" ", "") for box, text, _ in ocr
                                if 300 < sum(p[0] for p in box) / 4 < 1650 and 200 < sum(p[1] for p in box) / 4 < 850)
                decide = ScreenDetector.find_text_coordinate(ocr, "决定", region=(950, 700, 1550, 950))
                has_cost = any(kw in modal for kw in ["消耗圣晶石", "付费", "消耗呼符", "消耗友情点"])
                if "免费召唤" in modal and not has_cost and any(kw in modal for kw in ["是否开始召唤", "是否确认执行召唤"]) and decide:
                    self.log("[免费抽卡] 已确认免费召唤正文，点击决定。")
                    self.ctrl.click(*decide, delay_after=1.0)
                    confirmed = True
                    continue
                if screen not in (GameScreen.UNKNOWN, GameScreen.LOADING):
                    self.log("[免费抽卡失败] 未确认免费弹窗，不提交召唤，停止任务。")
                    return False
            elif screen == GameScreen.SUMMON_RESULT:
                result_seen = True
                close = details.get("close_coord")
                if not close:
                    return False
                self.log("[免费抽卡] 已确认召唤结果，关闭结果页返回卡池。")
                self.ctrl.click(*close, delay_after=1.5)
                continue
            elif screen == GameScreen.SUMMON_SCREEN and result_seen:
                return True
            elif screen == GameScreen.UNKNOWN:
                # 只在动画/卡面未知画面点中央；结果页先识别，避免点到变还按钮。
                self.ctrl.click(960, 540, delay_after=0.4)
                continue
            elif screen not in (GameScreen.LOADING, GameScreen.SUMMON_SCREEN):
                self.log(f"[免费抽卡失败] 出现非预期页面 {screen.value}，停止任务。")
                return False
            time.sleep(0.5)
        self.log("[免费抽卡失败] 未在时限内确认召唤结果及返回卡池，停止任务。")
        return False

    @staticmethod
    def _has_special_fp_text(texts: List[str]) -> bool:
        # OCR 可能把“期间限定”和“友情点召唤”拆成两个文本框。
        joined = "".join(text.replace(" ", "") for text in texts)
        return any(kw in joined for kw in ("期间限定", "期間限定", "特殊友情", "特别友情", "限定友情", "活动友情", "限时友情"))

    @staticmethod
    def _is_summon_pool(ocr_results: List[Tuple], pool: str) -> bool:
        # 底栏标签只是入口，不能证明当前卡池已切换。
        body = [text.replace(" ", "") for box, text, _ in ocr_results
                if 160 < sum(p[1] for p in box) / 4 < 900]
        if pool == "story":
            return any(kw in text for text in body for kw in ("剧情召唤", "根据游戏的进度"))
        # 特殊池也消耗友情点，不能仅凭“消耗友情点”确认常规池。
        if SmartEngine._has_special_fp_text(body):
            return False
        return "友情点召唤" in body

    def switch_summon_pool(self, pool: str, details: Dict[str, Any], max_attempts: int = 8) -> bool:
        """有限次数切池；每次点击后重新截图核验，异常页面直接停止。"""
        label = "剧情" if pool == "story" else "友情"
        unchanged = 0
        for attempt in range(max_attempts):
            if self.is_cancelled():
                return False
            ocr = details.get("ocr_results", [])
            if self._is_summon_pool(ocr, pool):
                return True
            before = tuple(text for box, text, _ in ocr if 160 < sum(p[1] for p in box) / 4 < 900)
            tab_ocr = ocr if pool == "story" else [item for item in ocr if item[1].replace(" ", "") == "友情点召唤"]
            tab = ScreenDetector.find_text_coordinate(tab_ocr, label, region=(200, 900, 1600, 1080))
            pos = tab or (70, 540)
            self.log(f"[切池 {attempt + 1}/{max_attempts}] 寻找【{label}召唤】，点击{'标签' if tab else '左侧箭头'} {pos}")
            self.ctrl.click(*pos, delay_after=1.8)
            for _ in range(3):
                if self.is_cancelled():
                    return False
                screen, details = self.get_current_screen(wait_still=True)
                if screen == GameScreen.SUMMON_SCREEN:
                    break
                if screen not in (GameScreen.LOADING, GameScreen.UNKNOWN):
                    self.log(f"[切池失败] 出现非卡池页面 {screen.value}，停止任务，保留现场。")
                    return False
                time.sleep(0.4)
            if screen != GameScreen.SUMMON_SCREEN:
                self.log("[切池失败] 未能确认卡池页面，停止任务。")
                return False
            ocr = details.get("ocr_results", [])
            if self._is_summon_pool(ocr, pool):
                self.log(f"[切池确认] 已通过新截图确认【{label}召唤】。")
                return True
            after = tuple(text for box, text, _ in ocr if 160 < sum(p[1] for p in box) / 4 < 900)
            unchanged = unchanged + 1 if before == after else 0
            # 多个期间限定友情池可能有相同 OCR 正文，不能据此认定点击无效。
            # 仍由总尝试次数上限保护，绝不把限定池标记为目标已完成。
            if unchanged >= 2 and not self._has_special_fp_text(after):
                self.log("[切池失败] 连续两次点击后卡池文字未变化，停止任务。")
                return False
        self.log(f"[切池失败] 达到 {max_attempts} 次上限，仍未确认【{label}召唤】，停止任务。")
        return False

    def _select_daily_quest(self, target, ocr, position, remaining):
        from src.strategy.daily_quest_policy import discount_status, normal_ap, minimum_plan
        try:
            if self.is_cancelled():
                return None
            if position is not None:
                base_ap = normal_ap(target)
                for attempt in range(3):
                    status = discount_status(ocr, position, base_ap)
                    if status is not None:
                        break
                    if attempt == 2:
                        self.log("[日常选关停止] 未看清目标卡片 AP 或减免标识，不能视作没有活动。")
                        return None
                    delta = max(-250, min(250, 400 - position[1]))
                    self.ctrl.swipe(1400, 450, 1400, 450 + delta, duration_ms=350)
                    screen, details = self.get_current_screen(wait_still=True)
                    ocr = details.get("ocr_results", [])
                    position = ScreenDetector.find_target_quest_coordinate(ocr, target)
                    if screen != GameScreen.CHALDEA_GATE or position is None:
                        return None
                if status:
                    self.log("[日常选关] 指定关卡存在 AP 减免，保留该关卡并校验对应阵容。")
                    from src.workflows.quest_runner import resolve_queue
                    plan = resolve_queue([{"quest_id": target, "runs": remaining}], self.chaldea_parser)[0]
                    ok = self.run_quest(plan, cancel_event=self.cancel_event)
                    self.daily_progress["quest_clears"] += self.last_quest_clears
                    if self.current_recommendation:
                        self.last_daily_strategy = self.current_recommendation.strategy.name
                    if not ok or self.last_quest_clears != remaining or not self.navigate_to_main_menu():
                        return None
                    return "completed"
            plan = minimum_plan(remaining)
            self.log(f"[日常选关] 指定关卡无 AP 减免或未开放，改刷最低标称 AP：{plan.spot} / {plan.name}，{plan.ap_per_run} AP × {remaining}。")
            if not self.navigate_to_main_menu():
                return None
            ok = self.run_reactive_weekly_plan(plan, cancel_event=self.cancel_event)
            self.daily_progress["quest_clears"] += self.last_weekly_clears
            if self.current_recommendation:
                self.last_daily_strategy = self.current_recommendation.strategy.name
            if not ok or self.last_weekly_clears != remaining or not self.navigate_to_main_menu():
                return None
            return "minimum"
        except Exception as error:
            self.log(f"[日常选关停止] {error}")
            return None

    def run_reactive_daily(
        self,
        options: Optional[Dict[str, Any]] = None,
        target_quest: str = "daily_qp_extreme",
        repeat_runs: int = 3
    ) -> bool:
        """
        响应式日常任务总调度器
        每一步操作前均实时截屏 OCR 确诊当前真实界面，再根据当前页面与目标清单驱动动作。
        """
        opts = options or {}
        enable_story_summon = opts.get("enable_story_summon", True)
        enable_fp = opts.get("enable_fp", True)
        enable_quest = opts.get("enable_quest", True)
        enable_reward = opts.get("enable_reward", True)

        task_story_summon_done = not enable_story_summon
        task_fp_done = not enable_fp
        task_quest_done = (repeat_runs <= 0) or (not enable_quest)
        task_reward_done = not enable_reward

        self.daily_progress = {"story_summon_completed": False, "fp_summon_completed": False,
                               "quest_clears": 0, "master_mission_claimed": False}
        self.last_daily_strategy = None

        cleared_runs = 0
        daily_route_checked = False

        def choose_daily(position, ocr):
            nonlocal daily_route_checked, cleared_runs, task_quest_done
            route = self._select_daily_quest(target_quest, ocr, position, repeat_runs - cleared_runs)
            if route is None:
                return False
            daily_route_checked = True
            if route in ("minimum", "completed"):
                cleared_runs = self.daily_progress["quest_clears"]
                task_quest_done = True
            return True

        max_steps = 500
        step_count = 0

        self.log(">>> [启动响应式状态机] 任务清单状态:")
        self.log(f"    - 剧情常驻池免费单抽: {'待执行' if not task_story_summon_done else '已跳过/已完成'}")
        self.log(f"    - 免费友情召唤: {'待执行' if not task_fp_done else '已跳过/已完成'}")
        self.log(f"    - 日常副本打本: {f'待完成 {repeat_runs} 场' if not task_quest_done else '已跳过/已完成'}")
        self.log(f"    - 御主任务绿方块: {'待执行' if not task_reward_done else '已跳过/已完成'}")

        if not task_quest_done and self.team_builder:
            self.current_recommendation = self.team_builder.recommend_for_quest(target_quest)
            rec = self.current_recommendation
            self.active_strategy = None
            self.log(f">>> [日常候选阵容] 待选关后校验：{rec.quest_name} (敌方: {rec.enemy_class_cn} | 波次: {rec.waves})")
            self.log(f"    - 选定体系方向: 【{rec.direction_name}】")
            self.log(f"    - 决策依据: {rec.direction_reason}")
            self.log(f"    - 最优配队编制: [1号位: {rec.slot1_dps} ({rec.slot1_affinity}x克制, {rec.slot1_ce})] | [2号位: {rec.slot2_support}] | [3号位: {rec.slot3_friend_support}] | [4号位: {rec.slot4_backline_support}]")

        if not task_quest_done and self.current_recommendation is None:
            self.log("[出击停止] 未生成可由 Laplace 校验的阵容。")
            return False

        while step_count < max_steps:
            step_count += 1
            if self.is_cancelled():
                self.log("[任务中断] 接收到用户停止指令，立即安全退出！")
                return False

            # 核心铁律：每一步操作前，第一件事永远是截屏 OCR 感知当下真实界面
            screen, details = self.get_current_screen()
            if (not task_quest_done and not daily_route_checked and screen in
                    (GameScreen.SUPPORT_SELECT, GameScreen.PARTY_CONFIRM, GameScreen.BATTLE_COMBAT,
                     GameScreen.BATTLE_CARD_SELECT, GameScreen.BATTLE_RESULT, GameScreen.CONTINUE_QUEST)):
                self.active_strategy = None
                self.log("[日常选关停止] 当前已在助战、编队或战斗流程，无法核验目标关卡活动；请回到大厅或关卡列表重新运行。")
                return False

            # ---------------- 场景 1: 从者/礼装背包已满拦截 (INVENTORY_FULL) ----------------
            if screen == GameScreen.INVENTORY_FULL:
                close_pos = details.get("close_coord") or (960, 845)
                self.log(f"【出击阻断】检测到从者或概念礼装背包已达上限 (1000/1000)，关闭弹窗 ({close_pos}) 并跳过打本...")
                self.ctrl.click(close_pos[0], close_pos[1], delay_after=1.5)
                task_quest_done = True
                continue

            # ---------------- 场景 2: 达·芬奇工坊 / 礼装选择 / 从者种火详情 / 个人空间 / 商店 (子界面退出) ----------------
            all_text_str = " ".join(details.get("all_texts", []))
            if screen in [GameScreen.DAVINCI_WORKSHOP, GameScreen.CRAFT_ESSENCE_SELECT]:
                close_pos = details.get("close_coord") or ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭", region=(0, 0, 350, 160)) or (163, 63)
                self.log(f"当前处于子界面/详情页/个人空间，点击左上角【关闭】({close_pos}) 返回上一层...")
                self.ctrl.click(close_pos[0], close_pos[1], delay_after=2.0)
                continue

            # ---------------- 场景 3: 弹窗与遮罩对话框 (包含展开的物品信息界面/道具详情弹窗) ----------------
            if screen in [GameScreen.POPUP_DIALOG, GameScreen.ANNOUNCEMENT]:
                close_pos = (
                    details.get("close_coord")
                    or ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭", region=(600, 620, 1320, 980))
                    or ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭")
                    or (960, 835)
                )
                self.log(f"当前处于弹窗遮罩/物品详情界面，精准点击【关闭】位置: {close_pos}...")
                self.ctrl.click(close_pos[0], close_pos[1], delay_after=1.5)
                continue

            # ---------------- 场景 4: 礼物盒界面 (GIFT_BOX) ----------------
            if screen == GameScreen.GIFT_BOX:
                self.log("当前处于【礼物盒界面】，已取消礼物盒任务，点击左上角【关闭】返回大厅...")
                close_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭") or (121, 40)
                self.ctrl.click(close_btn[0], close_btn[1], delay_after=2.0)
                continue

            if screen == GameScreen.SUMMON_RESULT:
                close_pos = details.get("close_coord")
                if not close_pos:
                    self.log("[抽卡结果] 未确认关闭按钮，停止任务。")
                    return False
                self.log("[抽卡结果] 关闭遗留的结果页，重新确认卡池状态。")
                self.ctrl.click(*close_pos, delay_after=1.5)
                continue

            # ---------------- 场景 5: 召唤卡池界面 (SUMMON_SCREEN) ----------------
            if screen == GameScreen.SUMMON_SCREEN:
                ocr_results = details.get("ocr_results", [])

                # 1. 优先执行：剧情常驻卡池每日免费一抽
                if not task_story_summon_done:
                    # 判断当前卡池是否已经是剧情召唤卡池
                    # 核验卡池正文，排除底栏入口文字。
                    is_in_story_pool = self._is_summon_pool(ocr_results, "story")

                    if not is_in_story_pool:
                        if not self.switch_summon_pool("story", details):
                            return False
                        continue

                    # 已处于剧情卡池，检测是否有今日【免费召唤】按键 (仅限主体区域 y < 850, x < 850)
                    free_story_btn = None
                    for box, text, _ in ocr_results:
                        xs = [p[0] for p in box]; ys = [p[1] for p in box]
                        cx = int(sum(xs) / 4); cy = int(sum(ys) / 4)
                        if "免费" in text and cy < 850 and cx < 850:
                            free_story_btn = (cx, cy)
                            break

                    if free_story_btn:
                        self.log(f"√ 当前处于剧情卡池，发现每日免费单抽机会，点击【免费召唤】({free_story_btn})...")
                        if not self.complete_free_summon(free_story_btn):
                            return False
                        task_story_summon_done = True
                        self.daily_progress["story_summon_completed"] = True
                        self.log("√ 每日剧情常驻卡池免费单抽抽取完毕！")
                    else:
                        self.log("√ 剧情常驻卡池今日免费单抽已使用 (仅剩付费圣晶石抽取)，无需浪费圣晶石，标记完成！")
                        task_story_summon_done = True
                        self.daily_progress["story_summon_completed"] = True

                    time.sleep(1.2)
                    continue

                # 2. 接着执行：友情点免费十连召唤
                if not task_fp_done:
                    # 严谨判断当前卡池是否已经是友情点卡池：
                    # 核心特征：主体区域出现 "友情点召唤" 或 "消耗友情点"。
                    is_in_fp_pool = self._is_summon_pool(ocr_results, "fp")

                    if not is_in_fp_pool:
                        if not self.switch_summon_pool("fp", details):
                            return False
                        continue

                    # 已处于友情点卡池，检查是否有今日【免费】召唤机会 (仅限主体区域 y < 850)
                    free_fp_btn = None
                    for box, text, _ in ocr_results:
                        xs = [p[0] for p in box]; ys = [p[1] for p in box]
                        cx = int(sum(xs) / 4); cy = int(sum(ys) / 4)
                        if "免费" in text and cy < 850:
                            free_fp_btn = (cx, cy)
                            break

                    if free_fp_btn:
                        self.log(f"√ 当前处于友情点卡池，发现免费召唤机会，点击【免费十连】({free_fp_btn})...")
                        if not self.complete_free_summon(free_fp_btn):
                            return False
                        task_fp_done = True
                        self.daily_progress["fp_summon_completed"] = True
                        self.log("√ 免费友情十连抽取完毕！")
                    else:
                        self.log("√ 当前友情点卡池今日无免费次数 (今日已抽)，无需浪费友情点，标记完成！")
                        task_fp_done = True
                        self.daily_progress["fp_summon_completed"] = True

                    time.sleep(1.2)
                    continue

                # 3. 剧情单抽与友情十连均已处理完毕，安全点击左上角【关闭】返回大厅
                self.log("卡池召唤任务全部处理完毕，点击左上角【关闭】返回大厅...")
                close_btn = ScreenDetector.find_text_coordinate(ocr_results, "关闭", region=(0, 0, 300, 150)) or (121, 40)
                self.ctrl.click(close_btn[0], close_btn[1], delay_after=2.0)
                time.sleep(1.5)
                continue

            # ---------------- 场景 6: 迦勒底之门关卡列表 (CHALDEA_GATE) ----------------
            if screen == GameScreen.CHALDEA_GATE:
                if not task_quest_done:
                    all_text = " ".join(details.get("all_texts", []))
                    ocr_res = details.get("ocr_results", [])

                    # 1. 判断是否处于迦勒底之门大分类列表（顶部大标题为【迦勒底之门】且卡片有【每日任务】）
                    # 严格限定查找区域：必须是屏幕主体区域 (y > 180) 的每日任务卡片，杜绝点击顶部标题 bar (y < 120)
                    daily_btn = ScreenDetector.find_text_coordinate(ocr_res, "每日任务", region=(1000, 180, 1920, 600))
                    has_gate_title = any("迦勒底之门" in text for box, text, _ in ocr_res if any(p[1] < 120 for p in box))

                    if daily_btn and (has_gate_title or any(k in all_text for k in ["强化任务", "幕间物语"])):
                        self.log(f"当前处于迦勒底之门大类选择页，精准点击【每日任务】卡片入口: {daily_btn}...")
                        self.ctrl.click(daily_btn[0], daily_btn[1], delay_after=2.5)
                        self.gate_swipe_count = 0
                        self.gate_bottom_confirm_count = 0
                        continue

                    # 2. 已在每日任务具体关卡列表内，根据用户指定的 target_quest 精准匹配目标关卡
                    quest_pos = ScreenDetector.find_target_quest_coordinate(
                        ocr_res,
                        target_quest=target_quest
                    )

                    q_prof = QUEST_ENEMY_PROFILES.get(target_quest)
                    if q_prof:
                        target_name = q_prof["quest_name"]
                    elif any(k in target_quest.lower() for k in ["qp", "宝物", "宝库"]):
                        target_name = "打开宝物库之门-极级(QP本)"
                    else:
                        target_name = "搜集种火-极级(狗粮本)"

                    if quest_pos:
                        if not choose_daily(quest_pos, ocr_res):
                            return False
                        self.gate_swipe_count = 0
                        self.gate_bottom_confirm_count = 0
                    else:
                        # 持续向下滑动，直到无法继续滑动（连续 2 次滑动画面无变化，确认已抵达物理列表最底部）或达到安全上限 60 次
                        max_safety_swipes = 60
                        if self.gate_bottom_confirm_count < 2 and self.gate_swipe_count < max_safety_swipes:
                            self.gate_swipe_count += 1
                            self.log(f"当前屏幕视野内未找到目标副本【{target_name}】，持续向下滑动寻找 (第 {self.gate_swipe_count} 次滑动)...")
                            pre_swipe_img = self.ctrl.screencap(force_fresh=False)
                            self.ctrl.swipe(1400, 840, 1400, 160, duration_ms=300)
                            time.sleep(0.9)
                            post_swipe_img = self.ctrl.wait_for_still(timeout=1.5, min_still_duration=0.15, diff_threshold=0.012)

                            # 计算滑动前后右侧关卡卡片列表区域 (950, 160, 1850, 880) 的帧间差异度
                            list_diff = AnchorVerifier.compute_frame_diff(pre_swipe_img, post_swipe_img, roi=(950, 160, 1850, 880))
                            if list_diff < 0.012:
                                self.gate_bottom_confirm_count += 1
                                self.log(f"[触底检测] 列表滑动前后画面无明显变化 (diff={list_diff:.4f}, 触底确认 {self.gate_bottom_confirm_count}/2)...")
                            else:
                                self.gate_bottom_confirm_count = 0

                            # 若已连续 2 次确认无法继续下滑（物理触底），立即在底部做最终扫描
                            if self.gate_bottom_confirm_count >= 2:
                                self.log("检测到列表已彻底滑至物理最底部且无法继续下滑，正在执行底部最终扫描...")
                                bottom_ocr = ScreenDetector.run_ocr(post_swipe_img)
                                bottom_pos = ScreenDetector.find_target_quest_coordinate(bottom_ocr, target_quest=target_quest)
                                if bottom_pos:
                                    if not choose_daily(bottom_pos, bottom_ocr):
                                        return False
                                elif not choose_daily(None, bottom_ocr):
                                    return False
                                self.gate_swipe_count = 0
                                self.gate_bottom_confirm_count = 0
                        else:
                            if self.gate_swipe_count >= max_safety_swipes and self.gate_bottom_confirm_count < 2:
                                self.log("[日常选关停止] 未确认列表触底，不能断言指定关卡未开放。")
                                return False
                            if not choose_daily(None, ocr_res):
                                return False
                            self.gate_swipe_count = 0
                            self.gate_bottom_confirm_count = 0
                else:
                    self.log("日常副本场次已打满，点击左上角【关闭】返回大厅...")
                    close_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭") or (163, 63)
                    self.ctrl.click(close_btn[0], close_btn[1], delay_after=2.0)
                continue

            # ---------------- 场景 7: 助战选择界面 (SUPPORT_SELECT) ----------------
            if screen == GameScreen.SUPPORT_SELECT:
                if not task_quest_done:
                    ocr_res = details.get("ocr_results", [])
                    sup_pos = self.party_assembler.select_support_on_support_screen(
                        self.ctrl,
                        self.current_recommendation,
                        initial_ocr=ocr_res,
                    )
                    if sup_pos is None:
                        self.log("[出击停止] 未确认方案指定的助战。")
                        return False
                    self.ctrl.click(sup_pos[0], sup_pos[1], delay_after=2.5)
                else:
                    self.log("打本任务已完成，在助战选择界面点击左上角【关闭】(121, 40) 返回副本列表/大厅...")
                    close_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭") or (121, 40)
                    self.ctrl.click(close_btn[0], close_btn[1], delay_after=2.0)
                continue

            # ---------------- 场景 8: 出击编队确认界面 (PARTY_CONFIRM) ----------------
            if screen == GameScreen.PARTY_CONFIRM:
                if not task_quest_done:
                    if not self._ensure_laplace():
                        return False
                    # 等待编队卡面渲染与动效稳定，避免界面未加载完毕过早操作
                    self.log(">>> [编队视觉感知] 当前处于【出击编队确认界面】，等待画面稳定并进行全队伍从者识别与实机自动编队...")
                    fresh_img = self.ctrl.wait_for_still(timeout=2.5, min_still_duration=0.25, diff_threshold=0.012)

                    # 若自配队引擎已生成针对当前关卡敌人的最优策略，直接执行实机视觉闭环自动编队 (自动替换 1号位打手 & 2号位辅助拐 & 平衡COST)
                    if self.current_recommendation and opts.get("enable_auto_party_build", True):
                        rec = self.current_recommendation
                        assembled = self.party_assembler.assemble_party_on_confirm_screen(
                            self.ctrl,
                            rec=rec,
                            chaldea_parser=self.chaldea_parser,
                        )
                        if not assembled:
                            self.active_strategy = None
                            self.log("[任务停止] 编队未通过核验，不挂载策略、不点击出击。")
                            return False
                        fresh_img = self.ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2, diff_threshold=0.012)

                    if not self._ensure_craft_essences():
                        return False
                    fresh_img = self.ctrl.screencap(force_fresh=True)
                    fresh_ocr = ScreenDetector.run_ocr(fresh_img)

                    # 识别前排 1-3 号位从者阵容与 3T 策略
                    party, strategy, party_logs = self.detector.identify_party_from_screen(fresh_ocr)
                    for plog in party_logs:
                        self.log(f"  {plog}")

                    if self.current_recommendation:
                        rec = self.current_recommendation
                        strategy = rec.strategy
                        self.log(f"★ [智能实机自动编队就绪] 已锁定主核【{rec.slot1_dps}】+ 辅助【{rec.slot2_support}】+ 助战【{rec.slot3_friend_support}】，挂载专属 3T 策略: 「{strategy.name}」！")

                    self.active_strategy = strategy
                    self.current_combat_turn = 1
                    self.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                    self.executed_waves.clear()
                    self.battle_result_counted = False
                    self.result_stall_count = 0
                    self.party_switch_attempts = 0

                    if strategy:
                        self.log(f"√ 编队识别完成！成功锁定 3T 策略: 「{strategy.name}」，战斗局内将自动释放技能！")
                    else:
                        self.log("√ 编队识别完成！未匹配到专用 3T 预设，将采用常规选卡模式出击。")

                    start_pos = ScreenDetector.find_text_coordinate(fresh_ocr, "开始任务", region=(1400, 800, 1920, 1080)) or \
                                ScreenDetector.find_text_coordinate(fresh_ocr, "开始", region=(1400, 800, 1920, 1080)) or \
                                details.get("start_coord") or (1750, 940)

                    self.log(f"[出击确认] 编队识别就绪，闭环点击右下角金色【开始任务】按钮: {start_pos}...")
                    def _has_left_party():
                        cur_frame = self.ctrl.screencap(force_fresh=True)
                        sc, _, _ = ScreenDetector.detect(cur_frame)
                        return sc != GameScreen.PARTY_CONFIRM

                    self.ctrl.click_and_verify(start_pos[0], start_pos[1], verify_fn=_has_left_party, timeout=3.5, interval=0.8, max_retries=2)
                else:
                    self.log("日常副本场次已打满，点击左上角【返回】(121, 40) 退出编队界面...")
                    back_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "返回") or (121, 40)
                    self.ctrl.click(back_btn[0], back_btn[1], delay_after=2.0)
                continue

            # ---------------- 场景 9: 加载转场中 (LOADING) ----------------
            if screen == GameScreen.LOADING:
                self.log("过图加载中，等待 2 秒...")
                time.sleep(2.0)
                continue

            # ---------------- 场景 10: 局内战斗中 (BATTLE_COMBAT) ----------------
            if screen == GameScreen.BATTLE_COMBAT:
                self.battle_result_counted = False
                self.result_stall_count = 0
                if self.active_strategy is None and self.current_recommendation is not None:
                    self.active_strategy = self.current_recommendation.strategy

                detected_wave = details.get("wave")
                if type(detected_wave) is not int or not 1 <= detected_wave <= 3:
                    self.log("[战斗等待] 未确认当前波次，不默认第一波或执行技能。")
                    time.sleep(0.5)
                    continue
                detected_turn = details.get("turn") or self.current_combat_turn
                np_map = details.get("np_percentages") or {}
                wave = detected_wave if (1 <= detected_wave <= 3) else min(3, self.current_combat_turn)
                is_extra_turn_in_wave = (wave in self.executed_waves)

                preferred_color = "Arts"
                if self.current_recommendation and self.current_recommendation.direction in ["Arts", "Buster", "Quick"]:
                    preferred_color = self.current_recommendation.direction

                self.log(
                    f"★ [局内战斗决策] 当前波次: Wave {wave}/3 | 战场总回合: 第 {detected_turn} 回合 | "
                    f"实时宝具NP: 1号位={np_map.get(1, '?')}%, 2号位={np_map.get(2, '?')}%, 3号位={np_map.get(3, '?')}%"
                )

                # 1. 若当前是进入该波次 (Wave 1/2/3) 的首回合，按 3T 策略释放该波次预设技能 (支持换人服动态时序与后排槽位映射)
                if self.active_strategy and (wave not in self.executed_waves) and (wave in self.active_strategy.turns):
                    self.executed_waves.add(wave)
                    turn_data = self.active_strategy.turns[wave]
                    self.log(f"★ 正在执行「{self.active_strategy.name}」Wave {wave}/3 核心技能释放时序...")
                    if self._execute_turn_actions(turn_data, wave) is False:
                        self.log("[Laplace 停止] 技能或换人未执行成功，不继续释放宝具。")
                        return False
                else:
                    self.executed_waves.add(wave)

                # 2. 动态冲浪 NP 补足 & 后续回合冷却恢复技能自动释放
                final_np1 = self._combat_np_after_actions(
                    wave=wave,
                    is_extra_turn_in_wave=is_extra_turn_in_wave,
                    real_turn_num=detected_turn,
                    initial_np_map=np_map
                )
                if final_np1 is None:
                    return False

                # 3. 点击攻击并释放主核宝具 + 同色指令卡链 (凑 Arts/Buster/Quick Chain)
                np_idx = 1
                if self.active_strategy and wave in self.active_strategy.turns:
                    turn = self.active_strategy.turns[wave]
                    raw_np = turn.np_servant
                    np_idx = None if turn.attack_mode == "facecards" else self.party_slot_to_screen_slot.get(raw_np, raw_np)
                self.log("  [指令出击] 普通攻击清场（不点宝具）" if np_idx is None else f"  [指令出击] {np_idx}号位 NP={final_np1}%，释放指定宝具。")
                if self.ctrl.select_attack_and_np(np_servant_idx=np_idx, preferred_color=preferred_color, allow_support_np=False) is False:
                    return False

                self.current_combat_turn = detected_turn + 1
                self._wait_after_attack(max_wait=25.0)
                continue

            # ---------------- 场景 10.5: 选卡阶段误入/恢复 (BATTLE_CARD_SELECT) ----------------
            if screen == GameScreen.BATTLE_CARD_SELECT:
                if self.current_recommendation and self.current_recommendation.laplace.get("passed"):
                    self.log("[Laplace 停止] 意外停留在选卡页面，无法确认本回合操作，不按默认1号位补点。")
                    return False
                self.battle_result_counted = False
                self.result_stall_count = 0
                preferred_color = "Arts"
                if self.current_recommendation and self.current_recommendation.direction in ["Arts", "Buster", "Quick"]:
                    preferred_color = self.current_recommendation.direction
                self.log("当前处于战斗选卡托盘展开状态，直接激活主核宝具并优选同色指令卡...")
                self.ctrl.select_attack_and_np(np_servant_idx=1, preferred_color=preferred_color, allow_support_np=False)
                self._wait_after_attack(max_wait=25.0)
                continue

            # ---------------- 场景 11: 战利品结算面板 (BATTLE_RESULT) ----------------
            if screen == GameScreen.BATTLE_RESULT:
                ocr_res = details.get("ocr_results", [])

                # 严格保证单场战斗只计入 1 次通关次数（无论结算分几页、或用户手动干预）
                # 注意：连续出击不经过编队确认页，因此绝不能在未打完场次前清空 self.active_strategy！
                if not self.battle_result_counted:
                    self.battle_result_counted = True
                    cleared_runs += 1
                    self.daily_progress["quest_clears"] = cleared_runs
                    self.log(f"√ 战斗胜利进入结算！已完成第 {cleared_runs}/{repeat_runs} 场。")
                    self.current_combat_turn = 1
                    self.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                    self.executed_waves.clear()
                    if cleared_runs >= repeat_runs:
                        task_quest_done = True
                        self.active_strategy = None

                # 若结算画面存在【关闭】按钮（例如误触弹出的物品详情、等级提升弹窗），优先点击关闭
                popup_close = (
                    ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(600, 620, 1320, 980))
                    or ScreenDetector.find_text_coordinate(ocr_res, "关闭")
                )
                if popup_close:
                    self.log(f"检测到结算画面上方存在【关闭】按钮 ({popup_close})，点击关闭弹窗...")
                    self.ctrl.click(popup_close[0], popup_close[1], delay_after=1.2)
                    continue

                # 严禁点击 (450, 900) 等战利品掉落图标网格区域！
                # 仅单步点击右下角【下一步】按钮安全区 (1685, 1005)，避开上方 y < 920 的所有掉落物图标
                next_pos = ScreenDetector.find_text_coordinate(ocr_res, "下一步", region=(1400, 900, 1920, 1080))
                if next_pos:
                    self.log(f"当前处于战利品结算界面，单步点击右下角【下一步】: {next_pos}...")
                    pre_img = self.ctrl.screencap(force_fresh=False)
                    self.ctrl.click(next_pos[0], next_pos[1], delay_after=1.2)
                else:
                    self.log("结算阶段 (经验/羁绊/点击屏幕)，点击中央推进屏幕 (960, 750)...")
                    pre_img = self.ctrl.screencap(force_fresh=False)
                    self.ctrl.click(960, 750, delay_after=1.2)
                post_img = self.ctrl.wait_for_still(timeout=1.2, min_still_duration=0.15, diff_threshold=0.012)

                # 闭环防卡死校验：若点击右下角下一步后画面完全无变化，说明有未识别的物品详情弹窗遮挡，自动点击弹窗关闭区 (960, 835)
                diff = AnchorVerifier.compute_frame_diff(pre_img, post_img)
                if diff < 0.010:
                    self.result_stall_count += 1
                    if self.result_stall_count >= 2:
                        post_ocr = ScreenDetector.run_ocr(post_img)
                        fallback_close = (
                            ScreenDetector.find_text_coordinate(post_ocr, "关闭")
                            or ScreenDetector.find_text_coordinate(post_ocr, "不申请")
                            or (960, 835)
                        )
                        self.log(f"[闭环脱困] 结算画面连续点击无变化 (diff={diff:.4f})，自动点击弹窗【关闭】位: {fallback_close}...")
                        self.ctrl.click(fallback_close[0], fallback_close[1], delay_after=1.2)
                        self.result_stall_count = 0
                else:
                    self.result_stall_count = 0
                continue

            # ---------------- 场景 12: 连续出击弹窗 (CONTINUE_QUEST) ----------------
            if screen == GameScreen.CONTINUE_QUEST:
                ocr_res = details.get("ocr_results", [])
                # 若直接跳入连续出击弹窗且尚未计入本场通关次数，补计 1 次
                if not self.battle_result_counted:
                    self.battle_result_counted = True
                    cleared_runs += 1
                    self.daily_progress["quest_clears"] = cleared_runs
                    self.log(f"√ 已成功通关第 {cleared_runs}/{repeat_runs} 场！")
                    self.current_combat_turn = 1
                    self.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                    self.executed_waves.clear()
                    if cleared_runs >= repeat_runs:
                        task_quest_done = True
                        self.active_strategy = None

                # 若出现行动力(AP)恢复弹窗，安全关闭并结束打本，避免点击错位
                if any(kw in all_text_str for kw in ["行动力恢复", "补充AP", "果实"]):
                    close_ap = ScreenDetector.find_text_coordinate(ocr_res, "关闭") or (960, 845)
                    self.log(f"检测到行动力(AP)不足弹窗，点击【关闭】({close_ap}) 并完成打本阶段...")
                    self.ctrl.click(close_ap[0], close_ap[1], delay_after=1.5)
                    task_quest_done = True
                    continue

                if cleared_runs < repeat_runs:
                    cont_pos = (
                        ScreenDetector.find_text_coordinate(ocr_res, "连续出击", region=(960, 650, 1650, 980))
                        or ScreenDetector.find_text_coordinate(ocr_res, "再次挑战", region=(960, 650, 1650, 980))
                        or (1280, 830)
                    )
                    self.log(f"检测到连续出击确认框 ({cleared_runs}/{repeat_runs})，精准点击【连续出击】({cont_pos})...")
                    self.ctrl.click(cont_pos[0], cont_pos[1], delay_after=2.2)
                    self.battle_result_counted = False
                    self.result_stall_count = 0
                    self.current_combat_turn = 1
                    self.party_slot_to_screen_slot = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
                    self.executed_waves.clear()
                else:
                    close_cont = (
                        ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(300, 650, 960, 980))
                        or (640, 830)
                    )
                    self.log(f"已打满目标场次 ({cleared_runs}/{repeat_runs})，精准点击【关闭】({close_cont}) 退出连续出击...")
                    self.ctrl.click(close_cont[0], close_cont[1], delay_after=2.0)
                continue

            # ---------------- 场景 13: 御主任务面板 (MASTER_MISSIONS) ----------------
            if screen == GameScreen.MASTER_MISSIONS:
                if not task_reward_done:
                    from src.strategy.daily_missions import DailyMissions
                    if not DailyMissions(self).claim(details.get("ocr_results", [])):
                        self.log("[日常领奖失败] 未确认全部日常奖励已领取，停止任务。")
                        return False
                    task_reward_done = True
                    self.daily_progress["master_mission_claimed"] = True
                    self.log("√ 御主任务日常绿方块已领取完毕，点击左上角【关闭】返回大厅...")
                else:
                    self.log("御主任务已处理完毕，点击左上角【关闭】返回大厅...")
                close_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "关闭") or (121, 37)
                self.ctrl.click(close_btn[0], close_btn[1], delay_after=2.0)
                continue

            # ---------------- 场景 14: 大厅主界面 (MAIN_MENU 调度中枢) ----------------
            if screen == GameScreen.MAIN_MENU:
                # 检查所有勾选的任务是否已经全部完成
                if task_story_summon_done and task_fp_done and task_quest_done and task_reward_done:
                    self.log("√ 检查清单全部闭合：勾选的日常任务全部执行完毕！")
                    return True

                # 按优先级逐一调度进入未完成的子界面：
                # 1. 剧情常驻单抽或友情十连未完成 -> 统一调度进入【召唤】界面
                if not task_story_summon_done or not task_fp_done:
                    menu_btn = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "菜单", region=(1500, 850, 1920, 1080)) or (1780, 1020)
                    self.log(f"【任务调度】当前在大厅，下一步：每日卡池召唤，点击菜单展开 ({menu_btn})...")
                    self.ctrl.click(menu_btn[0], menu_btn[1], delay_after=1.5)

                    # 从展开的抽屉底栏精准点击【召唤】入口 (960, 960)
                    self.log("点击展开底栏【召唤】入口 (960, 960)...")
                    self.ctrl.click(960, 960, delay_after=3.0)
                    continue

                if not task_quest_done:
                    # 检查大厅当前横幅是否能看到【迦勒底之门】
                    gate_pos = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "迦勒底之门")
                    if not gate_pos:
                        self.log("大厅横幅未露出【迦勒底之门】，正在向下拉动横幅列表...")
                        self.ctrl.swipe(1400, 250, 1400, 800, duration_ms=500)
                        time.sleep(1.5)
                        gate_pos = (1400, 300)
                    self.log(f"【任务调度】当前在大厅，下一步：日常打本 ({cleared_runs}/{repeat_runs})，点击【迦勒底之门】({gate_pos})...")
                    self.ctrl.click(gate_pos[0], gate_pos[1], delay_after=2.5)
                    continue

                if not task_reward_done:
                    self.log("【任务调度】当前在大厅，下一步：领取御主任务奖励，点击任务本子 (810, 920)...")
                    self.ctrl.click(810, 920, delay_after=2.5)
                    continue

            # ---------------- 未知/过渡画面兜底 ----------------
            self.log("界面正在过渡中，等待 1.5 秒后重新感知...")
            time.sleep(1.5)

        self.log("[日常停止] 达到步骤上限，仍有勾选任务未完成，不返回成功。")
        return False

    # 兼容旧接口
    def smart_handle_announcements_and_popups(self) -> None:
        self.smart_handle_announcements()

    def smart_handle_announcements(self) -> None:
        screen, details = self.get_current_screen()
        if screen in [GameScreen.POPUP_DIALOG, GameScreen.ANNOUNCEMENT]:
            pos = details.get("close_coord") or (960, 850)
            self.ctrl.click(pos[0], pos[1], delay_after=1.0)

    def smart_claim_giftbox(self) -> bool:
        return True

    def smart_summon_fp(self) -> bool:
        return True

    def smart_battle_loop(self, target_quest: str = "daily_qp_extreme", repeat_runs: int = 3) -> int:
        return repeat_runs

    def navigate_to_main_menu(self, max_steps: int = 10) -> bool:
        """
        全自动实机导航返回终端主大厅 (MAIN_MENU):
        智能识别当前界面，从大地图、主线记录、关卡抽屉、御主任务、礼物盒、卡池、弹窗等任何界面安全返回大厅。
        """
        for step in range(max_steps):
            if self.is_cancelled():
                return False
            screen, details = self.get_current_screen()
            if screen == GameScreen.MAIN_MENU:
                self.log("√ 已确认处于终端大厅主界面 (MAIN_MENU)")
                return True

            ocr_res = details.get("ocr_results", [])
            joined = " ".join(details.get("all_texts", []))

            # 0. 战斗结算与连续出击推进 (若从战斗残留中恢复)
            if screen == GameScreen.BATTLE_RESULT:
                reject_friend = ScreenDetector.find_text_coordinate(ocr_res, "不申请", region=(300, 750, 800, 1000))
                if reject_friend:
                    self.log(f"  [大厅导航] 检测到好友申请界面，点击【不申请】({reject_friend})...")
                    self.ctrl.click(reject_friend[0], reject_friend[1], delay_after=1.5)
                    continue
                next_pos = ScreenDetector.find_text_coordinate(ocr_res, "下一步", region=(1400, 900, 1920, 1080))
                if next_pos:
                    self.log(f"  [大厅导航] 处于战利品结算界面，点击右下角金色【下一步】: {next_pos}...")
                    self.ctrl.click(next_pos[0], next_pos[1], delay_after=1.5)
                else:
                    self.log("  [大厅导航] 处于结算画面，点击屏幕中央推进 (960, 750)...")
                    self.ctrl.click(960, 750, delay_after=1.2)
                continue

            if screen == GameScreen.CONTINUE_QUEST:
                close_cont = ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(300, 650, 960, 980)) or (640, 830)
                self.log(f"  [大厅导航] 检测到连续出击弹窗，点击【关闭】退出连续出击: {close_cont}...")
                self.ctrl.click(close_cont[0], close_cont[1], delay_after=2.0)
                continue

            if screen == GameScreen.LOADING:
                time.sleep(1.5)
                continue

            # 1. 弹窗/遮罩处理
            if screen in [GameScreen.POPUP_DIALOG, GameScreen.ANNOUNCEMENT, GameScreen.INVENTORY_FULL]:
                close_coord = details.get("close_coord") or ScreenDetector.find_text_coordinate(ocr_res, "关闭") or (960, 835)
                self.log(f"  [大厅导航] 检测到弹窗遮罩 ({screen.value})，点击关闭: {close_coord}...")
                self.ctrl.click(close_coord[0], close_coord[1], delay_after=1.5)
                continue

            # 2. 章节大地图 (REGIONAL_MAP)
            if screen == GameScreen.REGIONAL_MAP or "管理室" in joined or any(k in joined for k in ["特异点", "燃烧污染都市", "邪龙百年战争"]):
                # 检查是否有关卡抽屉展开 (左上角有'关闭')
                top_close = ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(0, 0, 350, 160))
                if top_close:
                    self.log(f"  [大厅导航] 当前处于关卡抽屉展开状态，点击关闭抽屉: {top_close}...")
                    self.ctrl.click(top_close[0], top_close[1], delay_after=1.8)
                    continue
                exit_pos = ScreenDetector.find_text_coordinate(ocr_res, "管理室", region=(0, 0, 350, 160)) or (164, 62)
                self.log(f"  [大厅导航] 当前处于章节大地图，点击左上角【管理室】({exit_pos}) 返回...")
                self.ctrl.click(exit_pos[0], exit_pos[1], delay_after=2.5)
                continue

            # 3. 主线记录 (MAIN_RECORD)
            if screen == GameScreen.MAIN_RECORD or "主线记录" in joined:
                close_pos = ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(0, 0, 350, 160)) or (163, 63)
                self.log(f"  [大厅导航] 当前处于主线记录列表，点击左上角【关闭】({close_pos}) 返回大厅...")
                self.ctrl.click(close_pos[0], close_pos[1], delay_after=2.0)
                continue

            # 4. 其他具备左上角关闭/返回按键的子界面 (御主任务、礼物盒、迦勒底之门、召唤、工坊等)
            top_back = ScreenDetector.find_text_coordinate(ocr_res, "关闭", region=(0, 0, 350, 160)) or \
                       ScreenDetector.find_text_coordinate(ocr_res, "返回", region=(0, 0, 350, 160)) or \
                       (163, 63)
            self.log(f"  [大厅导航] 当前处于子界面 ({screen.value})，点击左上角返回按键: {top_back}...")
            self.ctrl.click(top_back[0], top_back[1], delay_after=2.0)

        screen, _ = self.get_current_screen()
        return screen == GameScreen.MAIN_MENU

    def auto_scan_and_solve_weekly(self) -> Any:
        from src.strategy.weekly_missions import WeeklyMissions
        return WeeklyMissions(self).solve()

    @staticmethod
    def _get_war_search_tokens(war_name: str) -> List[str]:
        from src.strategy.chapter_navigation import chapter_id as story_chapter_id, CHAPTERS as STORY_CHAPTERS
        story_id = story_chapter_id(war_name)
        if story_id:
            return list(STORY_CHAPTERS[story_id][2])
        from src.strategy.ordeal_navigation import chapter_id, CHAPTERS
        ordeal_id = chapter_id(war_name)
        if ordeal_id:
            return list(CHAPTERS[ordeal_id][1])
        tokens = []
        if "冬木" in war_name:
            tokens.extend(["冬木", "特异点F", "燃烧污染"])
        elif "奥尔良" in war_name:
            tokens.extend(["奥尔良", "第一特异点", "邪龙百年"])
        elif any(name in war_name for name in ("罗马", "七丘之城", "第二特异点")):
            tokens.extend(["罗马", "第二特异点", "永恒狂气", "永续疯狂", "七丘之城"])
        elif "俄刻阿诺斯" in war_name:
            tokens.extend(["俄刻阿诺斯", "第三特异点", "封锁终局"])
        elif "伦敦" in war_name:
            tokens.extend(["伦敦", "第四特异点", "死界魔雾"])
        elif "合众为一" in war_name or "北美" in war_name:
            tokens.extend(["合众为一", "北美", "第五特异点"])
        elif "卡美洛" in war_name:
            tokens.extend(["卡美洛", "第六特异点", "神圣圆桌"])
        elif "巴比伦尼亚" in war_name:
            tokens.extend(["巴比伦尼亚", "第七特异点", "绝对魔兽"])
        elif "所罗门" in war_name:
            tokens.extend(["所罗门", "终局特异点", "冠位时间"])
        else:
            tokens.append(war_name)
        return tokens

    @staticmethod
    def is_target_map(current_map_short: str, current_canonical: str, target_war: str) -> bool:
        """
        判断当前大地图是否与目标章节一致
        """
        from src.strategy.event_navigation import chapter_id as event_chapter_id
        target_event = event_chapter_id(target_war)
        current_event = event_chapter_id(current_canonical) or event_chapter_id(current_map_short)
        if target_event or current_event:
            return bool(target_event and target_event == current_event)
        from src.strategy.chapter_navigation import chapter_id as story_chapter_id
        target_story = story_chapter_id(target_war)
        current_story = story_chapter_id(current_canonical) or story_chapter_id(current_map_short)
        if target_story or current_story:
            return bool(target_story and target_story == current_story)
        from src.strategy.ordeal_navigation import chapter_id
        target_ordeal = chapter_id(target_war)
        current_ordeal = chapter_id(current_canonical) or chapter_id(current_map_short)
        if target_ordeal or current_ordeal:
            return bool(target_ordeal and target_ordeal == current_ordeal)
        if not current_map_short or current_map_short == "未知地图":
            return False
        target_lower = target_war.lower()
        curr_s = current_map_short.lower()
        curr_c = current_canonical.lower()

        MAP_GROUPS = [
            (["冬木", "特异点f", "燃烧污染都市"], ["冬木", "特异点f", "燃烧污染都市"]),
            (["奥尔良", "第一特异点", "第一", "邪龙百年战争"], ["奥尔良", "第一特异点", "第一", "邪龙百年战争"]),
            (["罗马", "七丘之城", "第二特异点", "第二", "永恒狂气帝国", "永续疯狂帝国"], ["罗马", "七丘之城", "第二特异点", "第二", "永恒狂气帝国", "永续疯狂帝国"]),
            (["俄刻阿诺斯", "第三特异点", "第三", "封锁终局四海"], ["俄刻阿诺斯", "第三特异点", "第三", "封锁终局四海"]),
            (["伦敦", "第四特异点", "第四", "死界魔雾都市"], ["伦敦", "第四特异点", "第四", "死界魔雾都市"]),
            (["合众为一", "北美", "第五特异点", "第五", "北美神话大战"], ["合众为一", "北美", "第五特异点", "第五", "北美神话大战"]),
            (["卡美洛", "第六特异点", "第六", "神圣圆桌领域"], ["卡美洛", "第六特异点", "第六", "神圣圆桌领域"]),
            (["巴比伦尼亚", "第七特异点", "第七", "绝对魔兽战线"], ["巴比伦尼亚", "第七特异点", "第七", "绝对魔兽战线"]),
            (["所罗门", "终局特异点", "冠位时间神殿"], ["所罗门", "终局特异点", "冠位时间神殿"]),
            (["迦勒底之门", "修炼场", "宝物库", "种火"], ["迦勒底之门", "修炼场", "宝物库", "种火"]),
        ]
        for group_curr, group_tgt in MAP_GROUPS:
            is_curr = any(k in curr_s or k in curr_c for k in group_curr)
            is_tgt = any(k in target_lower for k in group_tgt)
            if is_curr and is_tgt:
                return True
            if is_curr and not is_tgt:
                return False
            if not is_curr and is_tgt:
                return False
        return curr_s in target_lower

    @staticmethod
    def _spot_matches(target, text):
        import re
        import unicodedata
        def normalize(value):
            value = unicodedata.normalize("NFKC", value)
            value = re.sub(r"[‐‑‒–—−ー]", "-", value)
            return re.sub(r"\s+", "", value).upper()
        target, text = normalize(target), normalize(text)
        code = re.search(r"X-?([A-G])$", target)
        if code:
            return re.search(r"X-?" + code[1] + r"(?![A-Z])", text) is not None
        return bool(target) and target in text

    @staticmethod
    def _free_quest_label_rows(ocr):
        """关卡卡片兼容中文与奏章的英文Free Quest艺术字。"""
        import re
        rows = []
        english = []
        for box,text,score in ocr:
            if float(score) < .55:
                continue
            x,y = sum(p[0] for p in box)/4,sum(p[1] for p in box)/4
            token = re.sub(r'\s+','',text).casefold()
            if 900 <= x <= 1850 and 150 <= y <= 850:
                if token == '自由关卡':
                    rows.append(y)
                if 900 <= x <= 1200:
                    english.append((token,y))
        for token,y in english:
            if token == 'freequest' or (token == 'free' and any(t=='quest' and abs(y-v)<100 for t,v in english)):
                if not any(abs(y-v)<80 for v in rows):
                    rows.append(y)
        return rows

    def _single_free_quest_card(self, ocr, quest_name):
        """仅供刚精确点击目标地点后使用；需公开数据证明该地点唯一自由关卡。"""
        import json
        import re
        from src.core.path_helper import get_resource_path
        rec = self.current_recommendation
        if rec is None or not rec.laplace.get("passed"):
            return None
        from src.strategy.quest_catalog import quest_binding
        entry = quest_binding(rec.target_quest_id) or {}
        if (entry.get("spot_free_quest_count") != 1 or entry.get("name") != quest_name
                or entry.get("id") != rec.laplace.get("quest_id")):
            return None
        from src.solver.mission_solver import MasterMissionSolver
        quest = next((q for q in MasterMissionSolver().quests if q.id == rec.target_quest_id), None)
        words = []
        for box, text, _ in ocr:
            x = sum(p[0] for p in box) / 4
            y = sum(p[1] for p in box) / 4
            if 1100 <= x <= 1840 and 150 <= y <= 650:
                words.append((re.sub(r"\s+", "", text), y))
        labels = [y for y in self._free_quest_label_rows(ocr) if 150 <= y <= 650]
        aps = [int(m[1]) for text, _ in words if (m := re.fullmatch(r"AP(\d+)", text, re.I))]
        expected_ap = quest.ap if quest else rec.laplace.get("quest_ap")
        if type(expected_ap) is int and len(labels) == 1 and aps == [expected_ap] and any(text == "已获取" for text, _ in words):
            return (1450, int(labels[0] + 60))
        return None

    def _named_free_quest_card(self, ocr, quest_name, *, check_ap=True):
        import re
        import unicodedata
        rec = self.current_recommendation
        expected_ap = rec.laplace.get("quest_ap") if rec else None
        if check_ap and type(expected_ap) is not int:
            return None
        clean = lambda text: re.sub(r"\s+", "", unicodedata.normalize('NFKC',text))
        target = clean(quest_name)
        for box, text, _ in ocr:
            x, y = sum(p[0] for p in box)/4, sum(p[1] for p in box)/4
            if not (850 <= x <= 1850 and 150 <= y <= 800 and target in clean(text)):
                continue
            row = [(clean(t), sum(p[1] for p in b)/4) for b,t,_ in ocr
                   if 1100 <= sum(p[0] for p in b)/4 <= 1840
                   and y-10 <= sum(p[1] for p in b)/4 <= y+160]
            aps = [int(m[1]) for t, _ in row if (m := re.fullmatch(r"AP(\d+)",t,re.I))]
            labels = self._free_quest_label_rows(ocr)
            if len(aps) == 1 and (not check_ap or aps == [expected_ap]) and any(y-40 <= label_y <= y+180 for label_y in labels):
                return (1450, int(y))
        return None

    @staticmethod
    def _support_quest_level_matches(ocr, expected):
        import re
        levels = []
        for box,text,_ in ocr:
            x,y = sum(p[0] for p in box)/4,sum(p[1] for p in box)/4
            match = re.search(r"(?:等级|Lv\.?)(\d+)",re.sub(r"\s+","",text),re.I)
            if match and 260 <= x <= 450 and 40 <= y <= 140:
                levels.append(int(match[1]))
        return levels == [expected]

    def navigate_to_free_quest(
        self,
        war_name: str,
        spot_name: str,
        quest_name: str,
        max_steps: int = 35,
        *, inspect_only: bool = False, quest_key: str | None = None
    ) -> bool:
        """
        全自动实机地图导航器:
        支持从任何状态 (终端主界面 MAIN_MENU / 主线记录 MAIN_RECORD / 区域地图 REGIONAL_MAP)
        自动定位特异点章节 -> 区域地点 (Spot) -> 目标自由关卡卡片 (Free Quest)，
        无缝平滑进入【助战选择 (SUPPORT_SELECT)】或【队伍确认 (PARTY_CONFIRM)】界面！
        彻底杜绝停留在主界面无限轮询或盲目等待用户手动点击！
        """
        self.log(f">>> [实机地图导航] 正在自主导航前往: [{war_name}] {spot_name} - {quest_name}...")

        from src.strategy.ordeal_navigation import chapter_id, OrdealNavigator
        from src.strategy.chapter_navigation import chapter_id as story_chapter_id, ChapterNavigator
        from src.strategy.event_navigation import chapter_id as event_chapter_id, EventNavigator
        ordeal_id = chapter_id(war_name)
        ordeal = OrdealNavigator(self,ordeal_id) if ordeal_id else None
        story_id = story_chapter_id(war_name)
        story = ChapterNavigator(self,story_id) if story_id else None
        event = EventNavigator(self) if event_chapter_id(war_name) else None
        map_navigator = event or story or ordeal
        if map_navigator:
            max_steps = max(max_steps,60)
        spot_short = spot_name.split("(")[0].strip()
        selected_target_card = False
        opened_target_spot = False
        drawer_scans = 0

        for step in range(max_steps):
            if self.is_cancelled():
                self.log("[导航中断] 检测到终止信号，退出导航！")
                return False

            screen, details = self.get_current_screen()
            ocr_results = details.get("ocr_results", [])
            all_texts = details.get("all_texts", [])
            joined = " ".join(all_texts)

            if event and screen == GameScreen.MAIN_MENU:
                if not event.enter(ocr_results):
                    return False
                continue
            if event and screen in (GameScreen.MAIN_RECORD,GameScreen.ORDEAL_SELECT):
                if not self.navigate_to_main_menu():
                    return False
                continue
            if story and screen in (GameScreen.MAIN_MENU, GameScreen.MAIN_RECORD):
                if not story.enter(ocr_results,record=screen==GameScreen.MAIN_RECORD):
                    return False
                continue
            if story and screen == GameScreen.ORDEAL_SELECT:
                if not self.navigate_to_main_menu():
                    return False
                continue
            if ordeal and screen in (GameScreen.MAIN_MENU, GameScreen.ORDEAL_SELECT):
                if not ordeal.enter(ocr_results,selector=screen==GameScreen.ORDEAL_SELECT):
                    return False
                continue
            if ordeal and screen == GameScreen.MAIN_RECORD:
                if not self.navigate_to_main_menu():
                    return False
                continue

            # 1. 若已处于助战选择、队伍确认或局内战斗，导航成功达成！
            if screen in [GameScreen.SUPPORT_SELECT, GameScreen.PARTY_CONFIRM, GameScreen.BATTLE_COMBAT]:
                if inspect_only:
                    self.log('[阶段核对停止] 当前未停在地图关卡卡片，不能推测首次攻略阶段。')
                    return False
                if screen != GameScreen.BATTLE_COMBAT and (selected_target_card or self._weekly_quest_matches(details, quest_name)):
                    self.log(f"√ [导航就绪] 已核验目标关卡并进入【{screen.value}】。")
                    return True
                self.log("[导航失败] 当前助战/编队/战斗界面没有目标关卡身份依据，停止接管。")
                return False

            if screen in [GameScreen.MAIN_MENU, GameScreen.MAIN_RECORD, GameScreen.REGIONAL_MAP]:
                selected_target_card = False
            if screen in [GameScreen.MAIN_MENU, GameScreen.MAIN_RECORD]:
                opened_target_spot = False

            # 2. 检查当前视野是否已展开目标自由关卡卡片
            # 必须优先于遮罩弹窗判定，因为展开关卡卡片时左侧常驻【关卡情报/战利品一览】，绝不能误当作弹窗关闭！
            # 关键约束: 仅在当前地图确认为目标章节时才可触发卡片点击，杜绝误点非目标章节中残留展开的关卡！
            current_map_short, current_canonical = ScreenDetector.identify_regional_map(ocr_results)
            if screen == GameScreen.REGIONAL_MAP and details.get('current_map'):
                current_map_short,current_canonical = details.get('map_name','未知地图'),details['current_map']
            is_map_matched = self.is_target_map(current_map_short, current_canonical, war_name)

            q_pos = None
            if is_map_matched:
                if event:
                    from src.strategy.first_clear_progress import quest_title_rows
                    rows = quest_title_rows(ocr_results,quest_name)
                    if len(rows)==1 and any(
                            rows[0]+60<sum(p[1] for p in box)/4<rows[0]+160
                            and 1120<sum(p[0] for p in box)/4<1340
                            and re.fullmatch(r'AP\s*\d+',text)
                            for box,text,score in ocr_results if float(score)>=.55):
                        q_pos=(1450,int(rows[0]))
                else:
                    q_pos = self._named_free_quest_card(ocr_results, quest_name, check_ap=not inspect_only)
                if q_pos is None and opened_target_spot and not inspect_only:
                    q_pos = self._single_free_quest_card(ocr_results, quest_name)
                    if q_pos:
                        self.log("  √ [关卡交叉核验] 精确地点、唯一自由关卡、已通关标记和 AP 一致。")

            if q_pos:
                if inspect_only:
                    self.log(f'√ [关卡卡片核对] 已展开目标【{quest_name}】，保留卡片读取实机阶段。')
                    return True
                self.log(f"  √ [关卡卡片] 发现展开的目标自由关卡卡片，点击进入助战选择 ({q_pos})...")
                self.ctrl.click(q_pos[0], q_pos[1], delay_after=2.5)
                selected_target_card = True
                # 严格闭环验证：确认进入助战选择或出击编队界面
                for _ in range(5):
                    post_sc, _ = self.get_current_screen()
                    if post_sc in [GameScreen.SUPPORT_SELECT, GameScreen.PARTY_CONFIRM]:
                        self.log(f"√ [闭环核验] 关卡卡片点击生效，已成功进入【{post_sc.value}】！")
                        return True
                    time.sleep(1.0)
                continue

            # 3. 检查是否有真正的阻断性弹窗或详情界面遮罩 (如概念礼装详情、签到弹窗等)
            if screen in [GameScreen.POPUP_DIALOG, GameScreen.ANNOUNCEMENT, GameScreen.CRAFT_ESSENCE_SELECT]:
                close_pos = (
                    details.get("close_coord")
                    or ScreenDetector.find_text_coordinate(ocr_results, "关闭", region=(0, 0, 350, 160))
                    or ScreenDetector.find_text_coordinate(ocr_results, "关闭")
                    or (163, 63)
                )
                self.log(f"  [导航排障] 检测到阻断弹窗/礼装详情遮罩，点击【关闭】({close_pos})...")
                self.ctrl.click(close_pos[0], close_pos[1], delay_after=1.8)
                continue

            # 4. 若在特异点区域大地图 (REGIONAL_MAP): 必须严格排除大厅（绝不能包含大厅的【通知】或【活动报酬】）
            has_hall_landmark = ("通知" in joined and ScreenDetector.find_text_coordinate(ocr_results, "通知", region=(0, 0, 350, 160)) is not None) or ("活动报酬" in joined and event is None)
            is_on_map = (
                screen == GameScreen.REGIONAL_MAP
                and not has_hall_landmark
            )
            if is_on_map:
                current_map_short, current_canonical = ScreenDetector.identify_regional_map(ocr_results)
                if details.get('current_map'):
                    current_map_short,current_canonical = details.get('map_name','未知地图'),details['current_map']
                self.log(f"  [地图感知] 当前大地图识别为: 【{current_canonical}】")

                # 检查当前大地图是否与目标章节一致
                if not self.is_target_map(current_map_short, current_canonical, war_name):
                    opened_target_spot = False
                    exit_pos = details.get("exit_to_manager_coord") or ScreenDetector.find_text_coordinate(ocr_results, "管理室", region=(0, 0, 350, 150)) or (164, 62)
                    self.log(f"  [地图重定向] 当前处于【{current_canonical}】，与目标章节【{war_name}】不符！点击【管理室】({exit_pos}) 返回重选...")
                    self.ctrl.click(exit_pos[0], exit_pos[1], delay_after=3.0)
                    continue

                # 当前大地图匹配目标章节！寻找目标地点 (Spot)
                drawer_close = ScreenDetector.find_text_coordinate(ocr_results, "关闭", region=(0, 0, 350, 150))
                if drawer_close:
                    if inspect_only and opened_target_spot:
                        if drawer_scans >= 6:
                            self.log('[阶段核对停止] 已扫描目标地点的关卡列表，仍未找到准确标题，不重复关闭重开。')
                            return False
                        drawer_scans += 1
                        self.log('[首通卡片列表] 目标地点已展开，滚动查找当前阶段对应的准确关卡。')
                        self.ctrl.swipe(1500,790,1500,350,duration_ms=350)
                        time.sleep(.6)
                        continue
                    self.log("  [地图校正] 当前展开的关卡未匹配目标，先关闭抽屉再定位地点。")
                    self.ctrl.click(*drawer_close, delay_after=0.8)
                    opened_target_spot = False
                    continue
                spot_pos = None
                for box, text, _ in ocr_results:
                    xs = [p[0] for p in box]; ys = [p[1] for p in box]
                    cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
                    # 严格限定在地图地标交互区 (130 <= cy <= 900)，杜绝命中顶部章节标题横幅 (cy < 130) 与底部菜单栏 (cy > 900)
                    if not (80 <= cx <= 1840 and 130 <= cy <= 900):
                        continue
                    # 严格排除大地图顶部章节横幅艺术字文字，避免将特异点/章节名称误认为关卡地标
                    if any(kw in text for kw in ["特异点", "百年战争", "污染都市", "Lostbelt", "异闻带", "主线", "管理室", "A.D."]):
                        continue
                    if self._spot_matches(spot_short, text):
                        spot_pos = (cx, cy)
                        break

                if spot_pos:
                    if event and spot_short=='第二仓储库':
                        spot_pos=(spot_pos[0],spot_pos[1]-128)
                    if spot_pos[1] < 110:
                        self.log(f"  [视野微调] 目标地点【{spot_name}】偏上方 ({spot_pos})，向下拉动地图使其居中...")
                        self.ctrl.swipe(1000, 300, 1000, 600, duration_ms=400)
                        time.sleep(1.5)
                        continue
                    elif spot_pos[1] > 920:
                        self.log(f"  [视野微调] 目标地点【{spot_name}】偏下方 ({spot_pos})，向上拉动地图使其居中...")
                        self.ctrl.swipe(1000, 750, 1000, 450, duration_ms=400)
                        time.sleep(1.5)
                        continue
                    else:
                        self.log(f"  √ [地点锁定] 在地图上发现目标地点【{spot_name}】({spot_pos})，点击展开关卡...")
                        opened_target_spot = True
                        self.ctrl.click(spot_pos[0], spot_pos[1], delay_after=2.5)
                        continue

                if map_navigator:
                    key = quest_key or (self.current_recommendation.target_quest_id if self.current_recommendation else None)
                    try:
                        map_navigator.pan(ocr_results,key)
                    except Exception as exc:
                        self.log(f"[章节导航停止] 无法核对地图坐标资料：{exc}")
                        return False
                    time.sleep(.6)
                    continue

                # 未在当前视野发现目标地点：执行 2D 4向循环拖拽探索
                swipe_phase = step % 4
                if swipe_phase == 0:
                    self.log(f"  [地图探索] 正在向上滑动地图探索南部区域 (寻找【{spot_name}】)...")
                    self.ctrl.swipe(1000, 750, 1000, 250, duration_ms=450)
                elif swipe_phase == 1:
                    self.log(f"  [地图探索] 正在向右滑动地图探索西部区域 (寻找【{spot_name}】)...")
                    self.ctrl.swipe(600, 500, 1400, 500, duration_ms=450)
                elif swipe_phase == 2:
                    self.log(f"  [地图探索] 正在向下滑动地图探索北部区域 (寻找【{spot_name}】)...")
                    self.ctrl.swipe(1000, 250, 1000, 750, duration_ms=450)
                else:
                    self.log(f"  [地图探索] 正在向左滑动地图探索东部区域 (寻找【{spot_name}】)...")
                    self.ctrl.swipe(1400, 500, 600, 500, duration_ms=450)
                time.sleep(1.8)
                continue

            # 5. 若在主线记录章节列表 (MAIN_RECORD):
            is_singularity_screen = (
                screen == GameScreen.MAIN_RECORD
                or ("主线记录" in joined and any(k in joined for k in ["特异点", "所罗门", "巴比伦尼亚", "卡美洛", "合众为一", "罗马", "奥尔良", "冬木"]))
            )
            if is_singularity_screen:
                war_pos = None
                war_tokens = self._get_war_search_tokens(war_name)
                for box, text, _ in ocr_results:
                    xs = [p[0] for p in box]; ys = [p[1] for p in box]
                    cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
                    if 600 <= cx <= 1850 and 120 <= cy <= 980:
                        if any(tok in text for tok in war_tokens):
                            war_pos = (1450, cy)
                            break

                if war_pos:
                    self.log(f"  √ [章节锁定] 在主线记录列表发现目标特异点【{war_name}】({war_pos})，点击进入地图...")
                    self.ctrl.click(war_pos[0], war_pos[1], delay_after=3.0)
                    continue
                else:
                    self.log(f"  [列表滚动] 目标特异点尚未进入视野，向下滑动主线记录列表...")
                    self.ctrl.swipe(1400, 800, 1400, 300, duration_ms=400)
                    time.sleep(1.8)
                    continue

            # 6. 若在终端主界面 (MAIN_MENU):
            if screen == GameScreen.MAIN_MENU:
                is_part1 = any(k in war_name for k in ["特异点", "冬木", "奥尔良", "罗马", "俄刻阿诺斯", "伦敦", "合众为一", "卡美洛", "巴比伦尼亚", "所罗门", "第一", "第二", "第三", "第四", "第五", "第六", "第七"])
                is_chaldea = any(k in war_name for k in ["迦勒底", "修炼场", "宝物库", "种火"])

                if is_chaldea:
                    gate_pos = ScreenDetector.find_text_coordinate(ocr_results, "迦勒底之门") or (1400, 300)
                    self.log(f"  点击主界面【迦勒底之门】入口 ({gate_pos})...")
                    self.ctrl.click(gate_pos[0], gate_pos[1], delay_after=2.5)
                    continue

                if is_part1:
                    main_record_pos = None
                    cand_banners = []
                    for box, text, _ in ocr_results:
                        xs = [p[0] for p in box]; ys = [p[1] for p in box]
                        cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
                        if "主线记录" in text and "物语" not in text and 900 <= cx <= 1850:
                            cand_banners.append((cx, cy, text))

                    # 寻找不含 1.5 的第一部主线记录横幅
                    for bcx, bcy, btxt in cand_banners:
                        has_15 = any("1.5" in t for b, t, _ in ocr_results if abs(int(sum(p[1] for p in b) / 4) - bcy) < 60)
                        if not has_15:
                            main_record_pos = (1400, bcy)
                            break

                    if main_record_pos:
                        self.log(f"  √ [主线入口] 发现【主线记录 (第一部)】入口 ({main_record_pos})，点击进入...")
                        self.ctrl.click(main_record_pos[0], main_record_pos[1], delay_after=3.0)
                        continue
                    else:
                        self.log("  [终端滚动] 当前大厅视野未见【主线记录 1】，微调滑动大厅横幅...")
                        self.ctrl.swipe(1400, 650, 1400, 350, duration_ms=300)
                        time.sleep(1.8)
                        continue

            time.sleep(1.5)

        self.log(f"× 自动地图导航在 {max_steps} 步内未到达目标，请检查游戏画面！")
        return False

    @staticmethod
    def _weekly_quest_matches(details, quest_name):
        import re
        target = re.sub(r"[^\w]", "", quest_name.split("(")[0])
        if len(target) < 2:
            return False
        return any(target in re.sub(r"[^\w]", "", text) and sum(p[1] for p in box) / 4 < 420
                   for box, text, _ in details.get("ocr_results", []))

    def run_quest(self, plan: Any, cancel_event: Optional[Any] = None) -> bool:
        """通用单关入口；完成模拟、身份及装备核验后才允许出击。"""
        from src.strategy.quest_battle import QuestBattleExecutor
        return QuestBattleExecutor(self).run(plan, cancel_event)

    def run_reactive_weekly_plan(self, plan: Any, cancel_event: Optional[Any] = None) -> bool:
        """兼容日常/周常旧入口，所有单站共用同一执行器。"""
        return self.run_quest(plan, cancel_event)

    def claim_weekly_rewards(self) -> bool:
        from src.strategy.weekly_missions import WeeklyMissions
        return WeeklyMissions(self).claim()
