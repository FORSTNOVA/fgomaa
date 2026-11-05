"""Run a selected battle recording against an explicitly bound live quest."""
from pathlib import Path
from types import SimpleNamespace
from io import BytesIO
import json
import threading
import time
import uuid

import cv2
import numpy as np
from PIL import Image

from src.core.controller import FGOController
from src.core.device_manager import DeviceManager
from src.core.path_helper import get_root_dir
from src.strategy.ap_recovery import AppleRecovery
from src.strategy.craft_essence_picker import CraftEssencePicker, EquipmentTarget
from src.strategy.interactive_simulation import InteractiveSimulation
from src.strategy.quest_battle import QuestBattleExecutor
from src.strategy.quest_resources import inspect_battle_quest
from src.strategy.recorded_battle import compile_recorded_battle
from src.strategy.screen_detector import GameScreen, ScreenDetector
from src.strategy.smart_engine import SmartEngine
from src.workflows.battle_runner import BattleRequest, resolve_battle_request


class RecordedFarmRunner:
    def __init__(self, parser, device_manager=None, *, simulation=None, log=None, report_root=None):
        self.parser = parser
        self.device_manager = device_manager or DeviceManager()
        self.simulation = simulation or InteractiveSimulation()
        self.log = log or (lambda _: None)
        self.report_root = Path(report_root or get_root_dir()/'.app_profile/recorded_farm')

    @staticmethod
    def wait_for(engine, states, seconds, *, combat_turn=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not engine.is_cancelled():
            screen, details = engine.get_current_screen()
            if screen in states:
                if screen != GameScreen.BATTLE_COMBAT or combat_turn is None:
                    return screen, details
                wave, turn = details.get('wave'), details.get('turn')
                if wave is not None and turn is not None and (wave, turn) >= combat_turn:
                    return screen, details
            time.sleep(.5)
        raise RuntimeError(f'未确认第{combat_turn[0]}面第{combat_turn[1]}回合' if combat_turn else
                           '未确认预期界面，停止录制执行')

    def verify_party(self, engine, party, data):
        ctrl = engine.ctrl
        picker = CraftEssencePicker(ctrl, self.log)
        image, ocr = picker.frame()
        if not picker.is_party(ocr):
            raise RuntimeError('未确认出击编队界面')
        for code in data['mystic_codes']:
            if not self.matches_mystic_code(image, code):
                raise RuntimeError(f"御主礼装与录制的{code['name']}不一致或无法确认")
        servants = {servant['id']:servant for servant in data['servants']}
        for row in party:
            slot, sid = row['slot'], row['svt_id']
            x1, _, x2, _ = engine.party_assembler.SLOT_ROIS[slot]
            if row['source'] == 'support' and not ScreenDetector.find_text_coordinate(
                    ocr, '助战', (x1, 260, x2, 330)):
                raise RuntimeError(f'未确认第{slot}号位为借用助战')
            engine.party_assembler._get_servant_face_crops(sid,servants[sid])
            matched, score = engine.party_assembler.verify_slot_has_servant(image, slot, sid)
            self.log(f"[录制编队] {slot}号位 {row['name']} 置信度 {score:.3f}")
            if not matched:
                raise RuntimeError(f"第{slot}号位从者与录制不一致或无法确认")
            if not engine.party_assembler.verify_slot_level(image, slot, row['lv'], ocr):
                raise RuntimeError(f"第{slot}号位从者等级与录制不一致或无法确认")
        equips = {ce['id']:ce for ce in data['craft_essences']}
        for row in party:
            ce = row['ce']
            if ce is None:
                continue
            slot = row['slot']
            equip = equips[ce['ce_id']]
            face = equip['extraAssets']['faces']['equip'][str(equip['id'])]
            target = EquipmentTarget(slot, ce['ce_id'], ce['name'], ce['lv'], ce['is_mlb'],
                                     equip['lvMax'], face, row['name'], equip['rarity'])
            if row['source'] == 'support':
                x1, _, x2, _ = engine.party_assembler.SLOT_ROIS[slot]
                if picker.face_score(image, {'roi':(x1, 558, x2, 815)}, target) < .75:
                    raise RuntimeError(f"第{slot}号位助战礼装与录制不一致或无法确认")
            else:
                picker.open_slot(slot)
                try:
                    ce_image, ce_ocr = picker.frame()
                    current = [card for card in picker.cards(ce_ocr) if picker.removed_card(ce_ocr,card)]
                    if len(current)==1:
                        if (not picker.name_matches(ce_ocr,target) or not picker.card_matches_stats(current[0],target)
                                or picker.face_score(ce_image,current[0],target)<.80):
                            raise RuntimeError(f'第{slot}号位当前礼装与录制不一致')
                    else:
                        picker.find(target, verify_only=True)
                finally:
                    picker.back_to_party()
            self.log(f"[录制礼装] {slot}号位 {ce['name']} 核验通过")

    @staticmethod
    def matches_mystic_code(image, code):
        from src.server.battle_assets import cached_image
        roi = np.asarray(image.convert('RGB'))[962:1079,20:139].copy()
        for url in code.get('extraAssets',{}).get('item',{}).values():
            rgba = np.asarray(Image.open(BytesIO(cached_image(url))).convert('RGBA'))
            for size in range(66,113,2):
                template = cv2.resize(rgba,(size,size))
                mask = (template[:,:,3]>230).astype('uint8')*255
                score = cv2.matchTemplate(roi,template[:,:,:3].copy(),cv2.TM_SQDIFF_NORMED,mask=mask)
                if np.any(np.isfinite(score) & (score<.05)):
                    return True
        return False

    def execute_turns(self, engine, turns):
        ctrl = engine.ctrl
        for turn in turns:
            screen, details = self.wait_for(engine,
                (GameScreen.BATTLE_COMBAT, GameScreen.BATTLE_RESULT, GameScreen.CONTINUE_QUEST), 50,
                combat_turn=(turn.wave, turn.turn))
            if screen != GameScreen.BATTLE_COMBAT or (details.get('wave'), details.get('turn')) != (turn.wave, turn.turn):
                raise RuntimeError(f"预期第{turn.wave}面第{turn.turn}回合，实际识别为 {screen.value} "
                                   f"{details.get('wave')}/{details.get('turn')}")
            self.log(f'[录制指令] 第{turn.wave}面第{turn.turn}回合')
            for action in turn.actions:
                if engine.is_cancelled():
                    raise RuntimeError('任务已取消')
                if action.kind == 'master':
                    ok = ctrl.cast_master_skill(action.skill, target_idx=action.target, swap=action.swap)
                else:
                    ok = ctrl.cast_servant_skill(action.actor, action.skill, target_idx=action.target)
                if not ok:
                    raise RuntimeError(f'第{turn.wave}面第{turn.turn}回合的技能未确认生效')
            np_slots = [slot for slot in turn.chain if slot is not None]
            if np_slots:
                np_map, _ = engine._get_current_np_and_ready_skills()
                if any(np_map.get(slot, 0) < 100 for slot in np_slots):
                    raise RuntimeError(f'录制指定的宝具未确认满充能，识别结果 {np_map}')
            if not ctrl.play_recorded_cards(turn.chain, turn.colors):
                raise RuntimeError('实机选卡未确认完成')
            engine._wait_after_attack(max_wait=32)
        self.wait_for(engine, (GameScreen.BATTLE_RESULT,), 55)

    def start_battle(self, engine, turns, on_submit):
        ctrl = engine.ctrl
        ocr = ScreenDetector.run_ocr(ctrl.screencap(force_fresh=True))
        start = ScreenDetector.find_text_coordinate(ocr, '开始任务', (1400, 800, 1920, 1080))
        if start is None or engine.is_cancelled():
            raise RuntimeError('未确认出击按钮')
        on_submit()
        ctrl.click(*start, delay_after=2)
        self.execute_turns(engine, turns)

    def run(self, recording_id, device_serial, *, bronze=False, cancel_event=None):
        cancel_event = cancel_event or threading.Event()
        recognition, session, turns = compile_recorded_battle(self.simulation, recording_id)
        quest = recognition['quest']
        request = BattleRequest(quest_id=quest['catalog_key'], auto_battle=True, device_serial=device_serial)
        _, target = resolve_battle_request(request, self.parser)
        support = next((row for row in recognition['party'] if row['source']=='support'), None)
        if support is None or support['slot'] != 3:
            raise ValueError('当前实机助战选择需录制在第3槽位')
        if (target.game_id, target.phase) != (quest['game_id'], quest['phase']):
            raise ValueError('实机关卡目录与进场快照不一致')
        devices = self.device_manager.get_connected_devices(auto_connect=False, include_resolution=False)
        if not any(device.serial == device_serial and device.state == 'device' for device in devices):
            raise ValueError('所选模拟器不在线')
        folder = self.report_root/uuid.uuid4().hex
        folder.mkdir(parents=True)
        report = {'recording_id':recording_id, 'status':'checking', 'confirmed_clears':0,
                  'submitted':False, 'estimated_ap_spent':0, 'apple_reserved':0, 'quest':quest}

        def persist():
            self.simulation.write(folder/'report.json', report)

        ctrl = FGOController(device_serial=device_serial, adb_path=self.device_manager.adb_path,
                             cancel_event=cancel_event, log_callback=self.log)
        engine = SmartEngine(ctrl, chaldea_parser=self.parser, cancel_event=cancel_event, log_callback=self.log)
        engine.first_clear_observation_dir = folder/'observations'
        try:
            observation = inspect_battle_quest(engine, target)
            report['observation'] = observation
            cost, available = observation['card_ap'], observation['available_ap']
            if type(cost) is not int or type(available) is not int:
                raise RuntimeError('未确认目标关卡的实际消耗和体力')
            if available < cost:
                if not bronze:
                    raise RuntimeError(f'体力 {available}/{cost}，等待自然恢复')
                def reserve(event):
                    if event.get('count') != 1 or report['apple_reserved']:
                        raise RuntimeError('本次最多可使用一颗青铜果实')
                    report['apple_reserved'] = 1
                    persist()
                AppleRecovery(ctrl, log_callback=self.log, cancel_event=cancel_event,
                              report_dir=folder/'apple').restore_once('bronze', cost, reserve, max_count=1)
                observation = inspect_battle_quest(engine, target)
                if observation['card_ap'] != cost or (observation['available_ap'] or 0) < cost:
                    raise RuntimeError('恢复后未确认足够体力')
                report['observation'] = observation
            self.log(f"[录制关卡] {quest['name']} 阶段 {quest['phase']}，消耗 {cost} AP")
            engine.quest_resource_check = lambda _: not engine.is_cancelled()
            if not QuestBattleExecutor(engine).navigate(target, target.war, target.spot, target.name):
                raise RuntimeError('无法进入已经核对的关卡')
            _, details = self.wait_for(engine, (GameScreen.SUPPORT_SELECT,), 18)
            recommend = SimpleNamespace(direction='Arts', direction_name='录制队伍',
                                        party_servant_ids={3:support['svt_id']})
            pos = engine.party_assembler.select_support_on_support_screen(ctrl, recommend,
                                                                          initial_ocr=details.get('ocr_results'))
            if pos is None:
                raise RuntimeError('未识别到录制指定的助战从者')
            ctrl.click(*pos, delay_after=1.5)
            self.wait_for(engine, (GameScreen.PARTY_CONFIRM,), 15)
            self.verify_party(engine, recognition['party'], session['payload']['data'])
            def submit():
                report.update(submitted=True, estimated_ap_spent=cost, status='fighting')
                persist()
            self.start_battle(engine, turns, submit)
            report.update(status='completed', confirmed_clears=1)
        except Exception as exc:
            report.update(status='cancelled' if cancel_event.is_set() else 'failed', error=str(exc))
            self.log('[录制刷本停止] '+str(exc))
        finally:
            try:
                ctrl.screencap(force_fresh=True).save(folder/'last.png')
            except Exception as exc:
                self.log('[录制截图保存失败] '+str(exc))
            finally:
                persist()
        return report
