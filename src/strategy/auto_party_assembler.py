"""

In-Game Auto Party Assembler (实机视觉闭环自动编队引擎)

核心职责：

1. 在【助战选择界面 (SUPPORT_SELECT)】自动点击对应职阶筛选项 (术/杀/特)，精准锁定并选择推荐助战拐 (如 C呆 / 杀狐 / 水CBA / 奥伯龙)；

2. 在【出击编队确认界面 (PARTY_CONFIRM)】通过 OpenCV 多尺度头像模板匹配 + OCR 等级感知，自动核验 1 号位 (主核打手)、2 号位 (自出辅助拐) 与 3 号位 (助战拐)；

3. 若当前队伍与智能自配队推荐不一致，自动点进对应槽位进入【从者选择 (SERVANT_SELECT)】，按等级导航并视觉锁定目标从者头像完成一键自动换人编队；

4. 若检测到队伍总 COST 超上限 (如 114/113)，自动卸除 6 号位后排冗余从者释放 COST，确保 100% 顺利出击。

"""

from __future__ import annotations

import re
import json

import time

import urllib.request

from pathlib import Path

from typing import Optional, Tuple, Dict, List, Callable, Any

import cv2

import numpy as np

from PIL import Image

from src.core.path_helper import get_resource_path

from src.strategy.screen_detector import ScreenDetector, GameScreen

from src.strategy.auto_team_builder import AutoTeamRecommendation, SERVANT_TACTICAL_DB

class AutoPartyAssembler:

    """实机自动编队与助战筛选执行器"""

    # 1920x1080 下 PARTY_CONFIRM 六个从者槽位的 (x1, y1, x2, y2) 区域与点击中心 (cx, cy)

    SLOT_ROIS: Dict[int, Tuple[int, int, int, int]] = {

        1: (30, 180, 345, 600),

        2: (345, 180, 650, 600),

        3: (650, 180, 955, 600),

        4: (965, 180, 1270, 600),

        5: (1270, 180, 1575, 600),

        6: (1575, 180, 1885, 600),

    }

    SLOT_CLICK_COORDS: Dict[int, Tuple[int, int]] = {

        1: (195, 340),

        2: (500, 340),

        3: (805, 340),

        4: (1115, 340),

        5: (1425, 340),

        6: (1735, 340),

    }

    SLOT_CE_CLICK_COORDS: Dict[int, Tuple[int, int]] = {

        1: (195, 660),

        2: (500, 660),

        3: (805, 660),

        4: (1115, 660),

        5: (1425, 660),

        6: (1735, 660),

    }

    # 1920x1080 下 SUPPORT_SELECT 顶部职阶切换栏坐标

    SUPPORT_CLASS_TAB_COORDS: Dict[str, Tuple[int, int]] = {
        "All": (55, 180),
        "Saber": (165, 180),
        "Archer": (275, 180),
        "Lancer": (385, 180),
        "Rider": (495, 180),
        "Caster": (605, 180),
        "Assassin": (715, 180),
        "Berserker": (825, 180),
        "Extra": (935, 180),
        "Mix": (1045, 180),
    }

    # 已知常用从者的灵衣 (Costume) 头像编号映射表

    KNOWN_SERVANT_COSTUMES: Dict[int, List[int]] = {

        2800100: [28001300, 28001400],  # 奥伯龙 (灵衣: 灵基再临/夏日/白礼服)

        604200: [6042010],              # 光之高扬斯卡娅

        504500: [5045010],              # 阿尔托莉雅·Caster

        704000: [7040010],              # 摩根

        100100: [1001010, 1001020],     # 阿尔托莉雅·Saber

    }

    def __init__(self, log_callback: Optional[Callable[[str], None]] = None):

        self.log_cb = log_callback or (lambda msg: print(msg, flush=True))

        self.faces_dir = get_resource_path("data/templates/faces")

        self.faces_dir.mkdir(parents=True, exist_ok=True)

        self._template_cache: Dict[int, List[np.ndarray]] = {}

    def log(self, msg: str) -> None:

        self.log_cb(msg)

    def _get_servant_face_crops(self, svt_id: int, metadata=None) -> List[np.ndarray]:

        """获取指定从者各灵基阶段 (0~3) 及灵衣阶段的头像中心特征裁剪图 (支持本地缓存与按需拉取)"""

        if svt_id in self._template_cache:

            return self._template_cache[svt_id]

        crops: List[np.ndarray] = []

        target_ids = [f"{svt_id}{stg}" for stg in [0, 1, 2, 3]] + [str(cid) for cid in self.KNOWN_SERVANT_COSTUMES.get(svt_id, [])]

        # 已缓存的公开从者资料包含灵衣头像；不能只依赖少数常用角色的手工表。
        metadata_path = get_resource_path(f"data/laplace/public/CN/servant/{svt_id}.json")
        if metadata is not None or metadata_path.exists():
            try:
                if metadata is None:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                faces = metadata.get("extraAssets", {}).get("faces", {})
                for url in (*faces.get("ascension", {}).values(), *faces.get("costume", {}).values()):
                    match = re.fullmatch(r"https://static\.atlasacademy\.io/CN/Faces/f_(\d+)\.png", url)
                    if match and match.group(1) not in target_ids:
                        target_ids.append(match.group(1))
            except (ValueError, OSError, AttributeError, TypeError):
                pass

        for icon_id in target_ids:

            p = self.faces_dir / f"f_{icon_id}.png"

            if not p.exists() or p.stat().st_size < 500:

                url = f"https://static.atlasacademy.io/CN/Faces/f_{icon_id}.png"

                try:

                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

                    with urllib.request.urlopen(req, timeout=5) as r:

                        data = r.read()

                        if len(data) > 500:

                            p.write_bytes(data)

                except Exception:

                    pass

            if p.exists() and p.stat().st_size >= 500:

                arr = np.frombuffer(p.read_bytes(), np.uint8)

                icon = cv2.imdecode(arr, cv2.IMREAD_COLOR)

                if icon is not None:

                    h, w = icon.shape[:2]

                    # 截取中心面部核心区域，避开四角职阶图标、顶部等级数字与底部消耗数字遮挡

                    crop = icon[int(h * 0.24):int(h * 0.76), int(w * 0.22):int(w * 0.86)]

                    # 排除近乎纯黑或纯色极低对比度特征裁剪图 (如奥伯龙伏提庚全黑背景 stage 3，避免纯色底板假匹配)

                    if float(np.std(crop)) >= 16.0:

                        crops.append(crop)

                    # 已编入并携带礼装时COST会遮住头像下缘，增加避开该区域的面部特征。
                    upper = icon[int(h * 0.24):int(h * 0.64), int(w * 0.22):int(w * 0.86)]
                    if float(np.std(upper)) >= 16.0:
                        crops.append(upper)

        self._template_cache[svt_id] = crops

        return crops

    def match_servant_in_region(

        self,

        pil_img: Image.Image,

        svt_id: int,

        roi: Optional[Tuple[int, int, int, int]] = None,

        scales: Optional[List[float]] = None,

        threshold: float = 0.72,

    ) -> Tuple[bool, float, Tuple[int, int]]:

        """

        在指定屏幕区域内使用多尺度模板匹配寻找目标从者头像

        返回: (是否命中, 最高相似度得分, 屏幕绝对点击坐标 (cx, cy))

        """

        crops = self._get_servant_face_crops(svt_id)

        if not crops:

            return False, 0.0, (0, 0)

        screen_bgr = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)

        ox, oy = 0, 0

        if roi:

            x1, y1, x2, y2 = roi

            ox, oy = x1, y1

            screen_bgr = screen_bgr[y1:y2, x1:x2]

        sh, sw = screen_bgr.shape[:2]

        if scales is None:

            scales = [1.55, 1.70, 1.85, 2.00, 2.15, 2.30]

        best_score = -1.0

        best_pt = (0, 0)

        for crop in crops:

            ch, cw = crop.shape[:2]

            for sc in scales:

                rw, rh = int(cw * sc), int(ch * sc)

                if rw >= sw or rh >= sh or rw < 20 or rh < 20:

                    continue

                resized = cv2.resize(crop, (rw, rh))

                res = cv2.matchTemplate(screen_bgr, resized, cv2.TM_CCOEFF_NORMED)

                _, max_val, _, max_loc = cv2.minMaxLoc(res)

                if max_val > best_score:

                    best_score = float(max_val)

                    best_pt = (ox + max_loc[0] + rw // 2, oy + max_loc[1] + rh // 2)

        return (best_score >= threshold), round(best_score, 4), best_pt

    def verify_slot_has_servant(self, pil_img: Image.Image, slot_idx: int, svt_id: int) -> Tuple[bool, float]:

        """检验 PARTY_CONFIRM 界面的第 slot_idx 号位当前是否为 svt_id 从者"""

        roi = self.SLOT_ROIS.get(slot_idx)

        if not roi:

            return False, 0.0

        matched, score, _ = self.match_servant_in_region(

            pil_img,

            svt_id=svt_id,

            roi=roi,

            scales=[1.95, 2.10, 2.20, 2.35, 2.50],

            threshold=0.72,

        )

        return matched, score

    @staticmethod

    def resolve_support_servant_id(support_name: str) -> Optional[int]:

        """将推荐的 2 号位辅助拐名称解析为标准国服 svtId"""

        s_low = support_name.lower()

        if "阿尔托莉雅" in s_low or "c呆" in s_low or "术呆" in s_low or "卡斯特" in s_low:

            return 504500

        if "光之高扬斯卡娅" in s_low or "杀狐" in s_low:

            return 604200

        if "奥伯龙" in s_low:

            return 2800100

        if "斯卡蒂(ruler)" in s_low or "水cba" in s_low:

            return 901400

        if "斯卡蒂" in s_low or "cba" in s_low:

            return 503900

        if "孔明" in s_low or "埃尔梅罗" in s_low:

            return 501900

        if "莱妮丝" in s_low or "司马懿" in s_low:

            return 403200

        if "阿瓦隆女士" in s_low or "旧梅" in s_low:

            return 2800300

        if "梅林" in s_low:

            return 500800

        if "暗之高扬斯卡娅" in s_low or "暗狐" in s_low:

            return 2500900

        for sid, meta in SERVANT_TACTICAL_DB.items():

            if meta.name in support_name or support_name in meta.name:

                return sid

        return None

    @staticmethod

    def _extract_visible_levels_in_grid(ocr_results: List[Tuple[List[List[int]], str, float]]) -> List[int]:

        """提取【从者选择】网格中当前可见的从者等级列表 (排除左上角当前已装备槽位卡片)"""

        levels: List[int] = []

        for box, text, _ in ocr_results:

            xs = [p[0] for p in box]

            ys = [p[1] for p in box]

            cx = int(sum(xs) / 4)

            cy = int(sum(ys) / 4)

            if cy < 240:

                continue

            # 排除左上角第一张「选择中」固定卡位 (cx < 420, cy < 550)，避免其低等级干扰列表滚动位置判断

            if cx < 420 and cy < 550:

                continue

            m = re.search(r"等级\s*(\d{1,3})", text)

            if m:

                lv = int(m.group(1))

                if 1 <= lv <= 120:

                    levels.append(lv)

        return levels

    def select_servant_in_picker(

        self,

        ctrl: Any,

        svt_id: int,

        svt_name: str,

        target_lv: int = 90,

    ) -> bool:

        """

        在【从者选择】界面按目标职阶与稀有度筛选，再从顶部逐屏查找到底。

        """

        from src.strategy.servant_picker import ServantPicker

        return ServantPicker(ctrl, self).select(svt_id, svt_name, target_lv)

    @staticmethod
    def verify_slot_level(img, slot_idx, expected_level, ocr=None):
        """队伍页等级位于卡面下方，不能用同角色头像推断等级。"""
        if ocr is None:
            ocr = ScreenDetector.run_ocr(img)
        x1, _, x2, _ = AutoPartyAssembler.SLOT_ROIS[slot_idx]
        levels = set()
        for box, text, score in ocr:
            x = sum(p[0] for p in box)/len(box)
            y = sum(p[1] for p in box)/len(box)
            if not (x1+20 <= x <= x1+150 and 780 <= y <= 830) or score < .55:
                continue
            match = re.fullmatch(r"(?:等级)?(\d{1,3})(?:级)?",text.replace(" ",""))
            if match:
                levels.add(int(match[1]))
        if levels == {expected_level}:
            return True
        # 全图方向分类有时把90读成06；只按原图方向局部重读，不倒推数字。
        for roi in ((x1+25,780,x1+160,833),(x1+35,785,x1+135,830)):
            crop = img.crop(roi).convert("RGB")
            readings = []
            for scale in (1,2):
                patch = np.array(crop.resize((crop.width*scale,crop.height*scale)))
                rows, _ = ScreenDetector.get_ocr()(patch,use_cls=False)
                values = set()
                for _, text, score in (rows or []):
                    match = re.fullmatch(r"(?:等级)?(\d{1,3})(?:级)?",text.replace(" ",""))
                    try:
                        confidence = float(score)
                    except (TypeError, ValueError):
                        continue
                    if match and confidence >= .55:
                        values.add(int(match[1]))
                readings.append(values)
            if all(values == {expected_level} for values in readings):
                return True
        return False

    def assemble_party_on_confirm_screen(

        self,

        ctrl: Any,

        rec: AutoTeamRecommendation,

        chaldea_parser: Optional[Any] = None,

    ) -> bool:

        """

        在【出击编队确认界面 (PARTY_CONFIRM)】执行全自动实机编队：

        1. 校验 1 号位是否为推荐主核打手 (rec.slot1_dps)，若不是则自动点进 1 号位替换；

        2. 校验 2 号位是否为推荐辅助拐 (rec.slot2_support)，若不是则自动点进 2 号位替换；

        3. 校验总 COST 是否超上限 (如 114/113)，若超限则自动卸除 6 号位后排从者释放 COST。

        """

        # 1. 解析 1 号位打手 ID 与等级，以及 2 号位辅助拐 ID 与等级

        dps_id: Optional[int] = rec.party_servant_ids.get(1)

        dps_lv: int = 90

        for candidate in ([] if dps_id else rec.candidate_dps_ranking):
            if candidate.get("name") == rec.slot1_dps:
                dps_id = int(candidate.get("svt_id", 0)) or None
                dps_lv = int(candidate.get("lv", 90))
                break

        if not dps_id:

            for sid, meta in SERVANT_TACTICAL_DB.items():

                if meta.name == rec.slot1_dps:

                    dps_id = sid

                    break

        if chaldea_parser and dps_id and dps_id in chaldea_parser.servants:

            dps_lv = chaldea_parser.servants[dps_id].lv

        sup_id = rec.party_servant_ids.get(2) or self.resolve_support_servant_id(rec.slot2_support)

        sup_lv = 90

        if chaldea_parser and sup_id and sup_id in chaldea_parser.servants:

            sup_lv = chaldea_parser.servants[sup_id].lv

        elif sup_id == 504500:

            sup_lv = 100

        sup4_id: Optional[int] = None

        sup4_lv: int = 90

        if rec.slot4_backline_support and "任意" not in rec.slot4_backline_support:

            sup4_id = self.resolve_support_servant_id(rec.slot4_backline_support)

            if chaldea_parser and sup4_id and sup4_id in chaldea_parser.servants:

                sup4_lv = chaldea_parser.servants[sup4_id].lv

        sup3_id = rec.party_servant_ids.get(3) or self.resolve_support_servant_id(rec.slot3_friend_support)
        if (not dps_id or not sup_id or not sup3_id
                or "/" in rec.slot2_support or "/" in rec.slot3_friend_support
                or (rec.slot4_backline_support and "任意" not in rec.slot4_backline_support
                    and (not sup4_id or "/" in rec.slot4_backline_support))):
            self.log("  [编队失败] 推荐名单未解析为明确的从者 ID，停止编队，不沿用未知阵容。")
            return False
        if ctrl.cancel_event is not None and ctrl.cancel_event.is_set():
            return False

        img = ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)

        # 2. 检查当前 1 号位、2 号位与 4 号位后排从者是否已就位

        slot1_ok, s1_score = self.verify_slot_has_servant(img, 1, dps_id) if dps_id else (True, 1.0)

        # 未识别和其他打手都不等于推荐主核；不能只改名称后沿用旧策略。
        slot3_ok, _ = self.verify_slot_has_servant(img, 3, sup3_id)
        if not slot3_ok:
            self.log(f"  [编队失败] 3号位未确认推荐助战【{rec.slot3_friend_support}】，请重新选择助战，停止出击。")
            return False

        slot2_ok, s2_score = self.verify_slot_has_servant(img, 2, sup_id) if sup_id else (True, 1.0)

        slot4_ok, s4_score = self.verify_slot_has_servant(img, 4, sup4_id) if sup4_id else (True, 1.0)
        slot1_ok = slot1_ok and self.verify_slot_level(img,1,dps_lv)
        slot2_ok = slot2_ok and self.verify_slot_level(img,2,sup_lv)
        if sup4_id:
            slot4_ok = slot4_ok and self.verify_slot_level(img,4,sup4_lv)

        log_str = (

            f">>> [实机自动编队核验] 1号位目标【{rec.slot1_dps}】: {'√ 已就位' if slot1_ok else '× 需替换'} (score={s1_score:.2f}) | "

            f"2号位目标【{rec.slot2_support}】: {'√ 已就位' if slot2_ok else '× 需替换'} (score={s2_score:.2f})"

        )

        if sup4_id:

            log_str += f" | 4号位目标【{rec.slot4_backline_support}】: {'√ 已就位' if slot4_ok else '× 需替换'} (score={s4_score:.2f})"

        self.log(log_str)

        # Special Case: 若 2 号位辅助拐 (如 C呆) 当前恰好在 1 号位，而 1 号位打手 (如 哈贝特洛特) 尚未上场：

        # 先把 1 号位替换为打手 (此时 C呆 被替下)，再点 2 号位把 C呆 编入 2 号位！

        if not slot1_ok and dps_id:

            s1_x, s1_y = self.SLOT_CLICK_COORDS[1]

            self.log(f"  [自动编队] 点击 1 号位槽位 ({s1_x}, {s1_y})，准备换入打手【{rec.slot1_dps}】...")

            ctrl.click(s1_x, s1_y, delay_after=2.2)

            picked = self.select_servant_in_picker(ctrl, svt_id=dps_id, svt_name=rec.slot1_dps, target_lv=dps_lv)

            if not picked:
                self.log(f"  [编队失败] 未能选入推荐打手【{rec.slot1_dps}】，停止任务。")
                return False

            img = ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)

            if not self.verify_slot_has_servant(img, 1, dps_id)[0]:
                self.log(f"  [编队失败] 点击后仍未确认1号位【{rec.slot1_dps}】，停止任务。")
                return False

            slot2_ok, s2_score = self.verify_slot_has_servant(img, 2, sup_id) if sup_id else (True, 1.0)

            if sup4_id:

                slot4_ok, s4_score = self.verify_slot_has_servant(img, 4, sup4_id)

        if not slot2_ok and sup_id:

            s2_x, s2_y = self.SLOT_CLICK_COORDS[2]

            self.log(f"  [自动编队] 点击 2 号位槽位 ({s2_x}, {s2_y})，准备编入辅助拐【{rec.slot2_support}】...")

            ctrl.click(s2_x, s2_y, delay_after=2.2)

            if not self.select_servant_in_picker(ctrl, svt_id=sup_id, svt_name=rec.slot2_support, target_lv=sup_lv):
                self.log(f"  [编队失败] 未能选入2号位【{rec.slot2_support}】，停止任务。")
                return False

            img = ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)

            if not self.verify_slot_has_servant(img, 2, sup_id)[0]:
                self.log(f"  [编队失败] 点击后仍未确认2号位【{rec.slot2_support}】，停止任务。")
                return False

            if sup4_id:

                slot4_ok, s4_score = self.verify_slot_has_servant(img, 4, sup4_id)

        if not slot4_ok and sup4_id:

            s4_x, s4_y = self.SLOT_CLICK_COORDS[4]

            self.log(f"  [自动编队] 点击 4 号位槽位 ({s4_x}, {s4_y})，准备编入后排换人辅助【{rec.slot4_backline_support}】...")

            ctrl.click(s4_x, s4_y, delay_after=2.2)

            if not self.select_servant_in_picker(ctrl, svt_id=sup4_id, svt_name=rec.slot4_backline_support, target_lv=sup4_lv):
                self.log(f"  [编队失败] 未能选入4号位【{rec.slot4_backline_support}】，停止任务。")
                return False

            img = ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)

            if not self.verify_slot_has_servant(img, 4, sup4_id)[0]:
                self.log(f"  [编队失败] 点击后仍未确认4号位【{rec.slot4_backline_support}】，停止任务。")
                return False

        # 3. 检查总消耗 COST 是否超上限 (例如 "114/113")

        ocr_res = ScreenDetector.run_ocr(img)

        for _, text, _ in ocr_res:

            m_cost = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", text)

            if m_cost:

                cur_c, max_c = int(m_cost.group(1)), int(m_cost.group(2))

                if cur_c > max_c and max_c >= 80:

                    self.log(f"  [COST 自动平衡] 检测到当前队伍总消耗 ({cur_c}/{max_c}) 超出上限，自动卸除 6 号位后排冗余从者...")

                    s6_x, s6_y = self.SLOT_CLICK_COORDS[6]

                    ctrl.click(s6_x, s6_y, delay_after=2.0)

                    rm_img = ctrl.wait_for_still(timeout=1.6, min_still_duration=0.15)

                    rm_ocr = ScreenDetector.run_ocr(rm_img)

                    rm_pos = ScreenDetector.find_text_coordinate(rm_ocr, "卸除") or (284, 415)

                    ctrl.click(rm_pos[0], rm_pos[1], delay_after=2.0)

                    img = ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)

                    ocr_res = ScreenDetector.run_ocr(img)

                    break

        # 4. 若因更换从者进入了【队伍编制】编辑子状态 (右下角为蓝色的【决定】按钮)，自动点击【决定】保存并返回【队伍确认】

        joined_after = " ".join(item[1] for item in ocr_res)

        if any(k in joined_after for k in ["拖动修改从者配置", "队伍编成", "职阶情报", "决定"]) and "开始任务" not in joined_after:

            confirm_pos = ScreenDetector.find_text_coordinate(ocr_res, "决定", region=(1500, 900, 1900, 1050)) or (1700, 980)

            self.log(f"  [自动编队保存] 点击右下角【决定】{confirm_pos} 保存编队变更并返回出击确认页...")

            ctrl.click(confirm_pos[0], confirm_pos[1], delay_after=2.2)

        # 保存后重新检查实际槽位，选择器返回成功不代表换人已经生效。
        if ctrl.cancel_event is not None and ctrl.cancel_event.is_set():
            return False
        final_img = ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)
        final_ocr = ScreenDetector.run_ocr(final_img)
        if ScreenDetector.find_text_coordinate(final_ocr, "开始任务", region=(1400, 800, 1920, 1080)) is None:
            self.log("  [编队失败] 未确认已返回出击确认页，停止任务。")
            return False
        targets = [(1, dps_id), (2, sup_id), (3, sup3_id)]
        if sup4_id:
            targets.append((4, sup4_id))
        for slot, svt_id in targets:
            if not self.verify_slot_has_servant(final_img, slot, svt_id)[0]:
                self.log(f"  [编队失败] 最终核验第{slot}号位与推荐不一致，停止任务。")
                return False
        for slot, level in [(1,dps_lv),(2,sup_lv)] + ([(4,sup4_lv)] if sup4_id else []):
            if not self.verify_slot_level(final_img,slot,level,final_ocr):
                self.log(f"  [编队失败] 第{slot}号位实际等级与推荐Lv{level}不一致或无法确认，停止出击。")
                return False
        for _, text, _ in final_ocr:
            cost = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", text)
            if cost and int(cost.group(2)) >= 80 and int(cost.group(1)) > int(cost.group(2)):
                self.log("  [编队失败] COST 仍超限，停止任务。")
                return False
        self.log("  [编队通过] 推荐从者槽位核验完成，保持原推荐及对应战斗策略。")
        return True

    def select_support_on_support_screen(

        self,

        ctrl: Any,

        rec: Optional[AutoTeamRecommendation],

        initial_ocr: Optional[List[Tuple[List[List[int]], str, float]]] = None,

    ) -> Optional[Tuple[int, int]]:

        """

        在【助战选择界面 (SUPPORT_SELECT)】根据智能自配队方向自动切换职阶 Tab 并视觉/OCR 锁定推荐好友助战拐

        返回: 选中的助战卡片点击坐标 (cx, cy)

        """

        if not rec:

            return (960, 380)

        dir_type = rec.direction

        if rec.party_servant_ids.get(3):
            target_svt_id = rec.party_servant_ids[3]
            meta = SERVANT_TACTICAL_DB[target_svt_id]
            target_tab = meta.class_name if meta.class_name in self.SUPPORT_CLASS_TAB_COORDS else "Extra"
            pref_kws = [meta.name]
            if target_svt_id == 2300500:
                pref_kws = ["Archetype"]

        elif dir_type == "Arts":

            target_tab = "Caster"

            target_svt_id = 504500

            pref_kws = ["阿尔托莉雅", "卡斯特", "Caster", "术呆", "C呆"]

        elif dir_type == "Buster":

            target_tab = "Assassin"

            target_svt_id = 604200

            pref_kws = ["高扬斯卡娅", "杀狐", "奥伯龙"]

        elif dir_type == "Quick":

            target_tab = "Extra"

            target_svt_id = 901400

            pref_kws = ["斯卡蒂", "斯卡哈"]

        else:

            target_tab = "Caster"

            target_svt_id = 504500

            pref_kws = ["阿尔托莉雅", "高扬斯卡娅", "奥伯龙"]

        def _find_support_in_current_view(img: Image.Image, ocr_res: List[Tuple[List[List[int]], str, float]]) -> Optional[Tuple[int, int]]:

            # 1. 优先通过 OCR 关键字定位助战名称

            # 注意: 点击坐标必须落在从者名字与空白行区域 (x=700 左右)，绝不可点在 x=960~1100 (那是礼装图标，会误弹概念礼装详情卡片)！

            for kw in pref_kws:

                pos = ScreenDetector.find_text_coordinate(ocr_res, kw, region=(100, 220, 1800, 960))

                if pos:

                    self.log(f"  √ [助战锁定] 按【{rec.direction_name}】需求 OCR 锁定助战【{kw}】(点击从者卡片: 700, {pos[1]})...")

                    return (700, pos[1])

            # 2. 备选通过 OpenCV 助战头像模板匹配定位

            matched, score, pt = self.match_servant_in_region(

                img,

                svt_id=target_svt_id,

                roi=(40, 210, 520, 980),

                scales=[1.25, 1.40, 1.55, 1.70, 1.85],

                threshold=0.72,

            )

            if matched:

                self.log(f"  √ [助战锁定] 按【{rec.direction_name}】需求视觉匹配助战头像 (ID:{target_svt_id}, score={score:.2f}, 点击: 700, {pt[1]})...")

                return (700, pt[1])

            return None

        # 同名不同职阶（如术/狂阿尔托莉雅）不能用筛选前的列表直接选中。
        # 先固定目标职阶，丢弃 initial_ocr，再识别实际筛选结果。

        tab_coord = self.SUPPORT_CLASS_TAB_COORDS.get(target_tab, (605, 180))

        self.log(f"  [助战筛选] 先切换顶部【{target_tab}】职阶栏 {tab_coord}，再识别指定助战...")

        ctrl.click(tab_coord[0], tab_coord[1], delay_after=1.5)

        def list_signature(ocr):
            return tuple((re.sub(r"\s+", "", text), round(sum(p[1] for p in box) / 32))
                         for box, text, _ in ocr
                         if 350 <= sum(p[0] for p in box) / 4 <= 1150
                         and 260 <= sum(p[1] for p in box) / 4 <= 980)

        # 职阶标签可能保留上次滚动位置，先回顶再完整查找。
        previous, same = None, 0
        for _ in range(60):
            if ctrl.cancel_event is not None and ctrl.cancel_event.is_set():
                return None
            cur_img = ctrl.wait_for_still(timeout=1.5, min_still_duration=0.15)
            cur_ocr = ScreenDetector.run_ocr(cur_img)
            found_pos = _find_support_in_current_view(cur_img, cur_ocr)
            if found_pos:
                return found_pos
            signature = list_signature(cur_ocr)
            same = same + 1 if signature and signature == previous else 0
            if same >= 2:
                break
            previous = signature
            ctrl.swipe(960, 350, 960, 850, duration_ms=400)
        else:
            self.log("  [助战停止] 未确认列表顶部，停止选择。")
            return None

        previous, same = None, 0
        for scroll_idx in range(60):
            if ctrl.cancel_event is not None and ctrl.cancel_event.is_set():
                return None

            cur_img = ctrl.wait_for_still(timeout=1.5, min_still_duration=0.15)

            cur_ocr = ScreenDetector.run_ocr(cur_img)

            found_pos = _find_support_in_current_view(cur_img, cur_ocr)

            if found_pos:

                return found_pos

            signature = list_signature(cur_ocr)
            same = same + 1 if signature and signature == previous else 0
            if same >= 2:
                self.log("  [助战查找] 已确认列表底部，未找到指定助战。")
                break
            previous = signature
            self.log(f"  [助战查找] 继续向下滑动寻找指定助战（第 {scroll_idx + 1} 屏）...")
            ctrl.swipe(960, 820, 960, 350, duration_ms=450)
            time.sleep(0.6)

        if rec.party_servant_ids.get(3):
            self.log("  [助战停止] 未确认三打手方案指定的助战，不以第一张卡片替代。")
            return None
        self.log("  [助战停止] 未确认推荐助战，不以第一张卡片替代。")
        return None

