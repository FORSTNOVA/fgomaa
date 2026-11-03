"""
Anchor & Stability Verifier for FGO Automation (MAA & FGA Hybrid Architecture)
借鉴 MAA 帧稳定性判定 (Frame Stability / Debounce) 与 FGA ROI 颜色锚点 (HSV Color Anchors)
提供毫秒级、零误判的画面稳定判定、战斗选卡 Back 校验、出击金色按钮校验、
技能冷却就绪感知、从者实时 NP 百分比提取与 Wave/Turn 轮数精确提取。
"""

from __future__ import annotations
import re
from typing import Optional, Tuple, List, Dict
import cv2
import numpy as np
from PIL import Image


class AnchorVerifier:
    """高精度画面稳定性与界面物理锚点校验器"""

    BASE_WIDTH = 1920
    BASE_HEIGHT = 1080

    WAVE_PATTERN = re.compile(r"(?<!\d)(?:BATTLE|WAVE|Wave|场次)?\s*([1-3])\s*/\s*([1-5])(?!\d)", re.IGNORECASE)
    TURN_PATTERN = re.compile(r"(\d{1,2})\s*回合")
    NP_PERCENT_PATTERN = re.compile(r"(?<![\d.])(\d{1,3})\s*%(?!\d)")

    # FGA 标准前排 1~3 号位从者 9 个技能图标中心 X 坐标 (Y=870)
    SKILL_X_COORDS: Dict[int, List[int]] = {
        1: [111, 243, 375],
        2: [588, 720, 852],
        3: [1064, 1196, 1328],
    }
    COMMAND_CARD_X_COORDS: List[int] = [225, 562, 975, 1350, 1762]

    @staticmethod
    def _normalize_image(img: Image.Image) -> np.ndarray:
        """统一转换为 1920x1080 RGB numpy 数组"""
        if img.size != (AnchorVerifier.BASE_WIDTH, AnchorVerifier.BASE_HEIGHT):
            img = img.resize((AnchorVerifier.BASE_WIDTH, AnchorVerifier.BASE_HEIGHT), Image.BILINEAR)
        return np.array(img.convert("RGB"))

    @classmethod
    def compute_frame_diff(
        cls,
        img1: Image.Image,
        img2: Image.Image,
        roi: Optional[Tuple[int, int, int, int]] = None
    ) -> float:
        """
        计算两帧画面的归一化像素平均绝对差 (Mean Absolute Error, MAE)
        返回: 0.0 (完全静态) ~ 1.0 (完全不同)
        """
        arr1 = cls._normalize_image(img1)
        arr2 = cls._normalize_image(img2)

        if roi:
            x1, y1, x2, y2 = roi
            arr1 = arr1[y1:y2, x1:x2]
            arr2 = arr2[y1:y2, x1:x2]

        # 降采样为轻量灰度图加速计算 (< 2ms)
        h, w = arr1.shape[:2]
        if w > 320 or h > 180:
            scale_w = min(320, w)
            scale_h = max(1, int(h * (scale_w / w)))
            gray1 = cv2.resize(cv2.cvtColor(arr1, cv2.COLOR_RGB2GRAY), (scale_w, scale_h))
            gray2 = cv2.resize(cv2.cvtColor(arr2, cv2.COLOR_RGB2GRAY), (scale_w, scale_h))
        else:
            gray1 = cv2.cvtColor(arr1, cv2.COLOR_RGB2GRAY)
            gray2 = cv2.cvtColor(arr2, cv2.COLOR_RGB2GRAY)

        diff = cv2.absdiff(gray1, gray2)
        mae = float(np.mean(diff)) / 255.0
        return mae

    @classmethod
    def is_frame_stable(
        cls,
        img1: Image.Image,
        img2: Image.Image,
        threshold: float = 0.012,
        roi: Optional[Tuple[int, int, int, int]] = None
    ) -> Tuple[bool, float]:
        """
        判定画面是否处于绝对静止稳定状态 (消抖动)
        若差值低于 threshold，返回 (True, diff)，表明动画已播放完毕、界面已就绪
        """
        diff = cls.compute_frame_diff(img1, img2, roi=roi)
        return (diff < threshold, diff)

    @classmethod
    def is_card_select_back_button_present(cls, img: Image.Image) -> bool:
        """
        基于 FGA 标准校验战斗中选卡阶段右下角【返回 / Back】蓝色按钮是否存在
        重要排他条件：
        1. 若当前右下角圆形【攻击】按钮与右侧【御主技能】按钮仍在屏幕上 (即 is_attack_button_ready 为 True)，
           则必为指令阶段 (BATTLE_COMBAT)，绝非选卡阶段！
        2. 选卡阶段时御主技能按钮已隐藏；背景也可能呈蓝色，此时独立复核右下角返回文字。
        """
        if cls.is_attack_button_ready(img):
            return False

        arr = cls._normalize_image(img)
        # 地图背景也可能呈蓝色；此区域变蓝不能单独证明御主技能按钮存在。
        roi_mst = arr[435:510, 1745:1840]
        hsv_mst = cv2.cvtColor(roi_mst, cv2.COLOR_RGB2HSV)
        mst_mask = cv2.inRange(hsv_mst, np.array([90, 80, 80], dtype=np.uint8), np.array([130, 255, 255], dtype=np.uint8))
        mst_ratio = float(np.count_nonzero(mst_mask)) / mst_mask.size

        roi = arr[940:1050, 1620:1890]
        hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)

        lower_blue = np.array([95, 85, 85], dtype=np.uint8)
        upper_blue = np.array([132, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_blue, upper_blue)

        blue_ratio = float(np.count_nonzero(mask)) / mask.size
        if blue_ratio < 0.12:
            return False
        if mst_ratio < 0.22:
            return True
        # 蓝色背景遮住排他颜色条件时，局部双尺度核对真正的返回按钮文字。
        from src.strategy.screen_detector import ScreenDetector
        crop = Image.fromarray(arr).crop((1768, 1000, 1855, 1048))
        for scale in (1, 2):
            result, _ = ScreenDetector.get_ocr().text_recognizer(
                np.array(crop.resize((crop.width * scale, crop.height * scale))))
            if (len(result) != 1 or re.sub(r'\s+', '', result[0][0]) not in ('返回', 'Back')
                    or float(result[0][1]) < .6):
                return False
        return True

    @classmethod
    def is_start_quest_button_present(cls, img: Image.Image) -> bool:
        """
        基于 FGA 标准校验出击编队确认界面右下角【开始任务】金色大按钮是否存在
        ROI: [1550, 940, 1890, 1060]
        金色/黄色 HSV 范围: H [15, 38], S > 90, V > 110
        """
        arr = cls._normalize_image(img)
        roi = arr[940:1060, 1550:1890]
        hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)

        lower_gold = np.array([15, 90, 110], dtype=np.uint8)
        upper_gold = np.array([38, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        gold_ratio = float(np.count_nonzero(mask)) / mask.size
        return gold_ratio >= 0.15

    @classmethod
    def is_skill_confirm_dialog_open(cls, img: Image.Image) -> bool:
        """
        检测局内【技能使用】确认弹窗 (左【取消】660,615 / 右【决定】1260,615) 是否处于打开状态。
        特征：
        - 左按钮区 [588:652, 480:840] 与右按钮区 [588:652, 1080:1440] 呈高亮度银白色 (均值 > 175)；
        - 两按钮中间缝隙及上方背景 [520:652, 935:985] 为深黑半透明底框 (均值 < 45)。
        """
        arr = cls._normalize_image(img)
        btn_l = float(arr[588:652, 480:840].mean())
        btn_r = float(arr[588:652, 1080:1440].mean())
        gap = float(arr[520:652, 935:985].mean())
        return btn_l > 175.0 and btn_r > 175.0 and gap < 45.0

    @classmethod
    def is_skill_target_dialog_open(cls, img: Image.Image) -> bool:
        """
        检测局内技能【请选择对象】目标从者选择窗口 (1号位: 500,560 / 2号位: 960,560 / 3号位: 1440,560) 是否处于打开状态。
        特征：
        - 窗口右上角带有清晰高亮的银白色【X】关闭按钮 [190:235, 1635:1675] (均值 > 115)；
        - 窗口顶部标题栏 [210:290, 800:1120] 为深黑色半透明底框 (均值 < 35)；
        - 且绝非【技能使用】确认弹窗 (is_skill_confirm_dialog_open 为 False)。
        """
        if cls.is_skill_confirm_dialog_open(img):
            return False
        arr = cls._normalize_image(img)
        x_btn = float(arr[190:235, 1635:1675].mean())
        hdr = float(arr[210:290, 800:1120].mean())
        return x_btn > 115.0 and hdr < 35.0

    @classmethod
    def is_attack_button_ready(cls, img: Image.Image) -> bool:
        """
        校验右下角圆形【攻击 / Attack】青蓝色发光按钮与右侧【御主技能】按钮是否同时处于可点击就绪态
        (严格排除【技能使用】确认弹窗、【请选择对象】目标框、选卡阶段、宝具动画中、敌方回合中或技能动画中)
        """
        if cls.is_skill_confirm_dialog_open(img) or cls.is_skill_target_dialog_open(img):
            return False

        arr = cls._normalize_image(img)
        # 1. 校验圆形【攻击】按钮上半区 [815:915, 1650:1800] 的青蓝色高光与未压暗亮度 (atk_v >= 185)
        roi_atk = arr[815:915, 1650:1800]
        atk_v = float(roi_atk.max(axis=2).mean())
        if atk_v < 185.0:
            return False

        hsv_atk = cv2.cvtColor(roi_atk, cv2.COLOR_RGB2HSV)
        mask_atk = cv2.inRange(
            hsv_atk,
            np.array([82, 75, 95], dtype=np.uint8),
            np.array([118, 255, 255], dtype=np.uint8)
        )
        cyan_ratio = float(np.count_nonzero(mask_atk)) / mask_atk.size
        if cyan_ratio < 0.42:
            return False

        # 2. 校验右侧【御主技能】蓝色按钮 [435:510, 1745:1840] 同时存在 (选卡界面下该按钮消失)
        roi_mst = arr[435:510, 1745:1840]
        hsv_mst = cv2.cvtColor(roi_mst, cv2.COLOR_RGB2HSV)
        mask_mst = cv2.inRange(
            hsv_mst,
            np.array([90, 80, 80], dtype=np.uint8),
            np.array([130, 255, 255], dtype=np.uint8)
        )
        mst_ratio = float(np.count_nonzero(mask_mst)) / mask_mst.size
        return mst_ratio >= 0.25

    @classmethod
    def detect_ready_skills(cls, img: Image.Image) -> Dict[Tuple[int, int], bool]:
        """
        检测前排 1~3 号位从者共 9 个技能图标的实时冷却就绪状态 (Off-Cooldown vs On-Cooldown)。
        FGO 渲染引擎特性：
        - 处于冷却中 (剩余 X) 或封印中的技能图标，上半区 [830:885, x-30:x+30] 被施加 50% 黑色半透明遮罩，
          导致像素最大亮度严格 <= 135 (95%分位 <= 130)；
        - 已转好可释放 (Ready) 的技能图标无遮罩，边框与高光像素 95% 分位 >= 170 (实测 245~255)。
        返回: {(servant_idx, skill_idx): True/False}
        """
        arr = cls._normalize_image(img)
        ready_map: Dict[Tuple[int, int], bool] = {}

        for s_idx, xs in cls.SKILL_X_COORDS.items():
            for sk_idx, x in enumerate(xs, 1):
                box = arr[830:885, x - 30 : x + 30]
                p95 = float(np.percentile(box, 95))
                std_val = float(np.std(box))
                ready_map[(s_idx, sk_idx)] = (p95 >= 170.0 and std_val >= 25.0)

        return ready_map

    @classmethod
    def is_skill_ready(cls, img: Image.Image, servant_idx: int, skill_idx: int) -> bool:
        """检查单个从者技能 (servant_idx 1..3, skill_idx 1..3) 当前是否已冷却完毕可点击"""
        xs = cls.SKILL_X_COORDS.get(servant_idx)
        if not xs or not (1 <= skill_idx <= 3):
            return False
        arr = cls._normalize_image(img)
        x = xs[skill_idx - 1]
        box = arr[830:885, x - 30 : x + 30]
        p95 = float(np.percentile(box, 95))
        std_val = float(np.std(box))
        return p95 >= 170.0 and std_val >= 25.0

    @classmethod
    def extract_servant_np_percentages(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> Dict[int, int]:
        """
        从 OCR 结果中提取前排 1~3 号位从者当前的实时宝具 NP 百分比 (0 ~ 300)。
        国服 1920x1080 底部 NP 槽文本坐标:
        - 1号位: cx 约 395 (260 <= cx <= 530), cy 约 1002 (960 <= cy <= 1040)
        - 2号位: cx 约 875 (730 <= cx <= 1000), cy 约 1002 (960 <= cy <= 1040)
        - 3号位: cx 约 1350 (1200 <= cx <= 1480), cy 约 1002 (960 <= cy <= 1040)
        """
        np_map: Dict[int, int] = {}
        for box, text, _ in ocr_results:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            cy = sum(ys) / 4.0
            cx = sum(xs) / 4.0
            if not (955 <= cy <= 1042):
                continue

            match = cls.NP_PERCENT_PATTERN.search(text)
            if not match:
                continue

            try:
                val = int(match.group(1))
            except Exception:
                continue
            if not 0 <= val <= 300:
                continue

            if 250 <= cx <= 540:
                np_map[1] = val
            elif 720 <= cx <= 1010:
                np_map[2] = val
            elif 1190 <= cx <= 1490:
                np_map[3] = val

        return np_map

    @classmethod
    def detect_command_card_colors(cls, img: Image.Image) -> List[str]:
        """
        在选卡阶段 (BATTLE_CARD_SELECT) 识别 5 张普通指令卡的色卡类型 ("Arts", "Buster", "Quick")。
        用于优先湊齐蓝卡链 (Arts Chain +20%全队NP) / 红卡链 / 绿卡链，最大化冲浪回收与伤害。
        """
        arr = cls._normalize_image(img)
        colors: List[str] = []
        for x in cls.COMMAND_CARD_X_COORDS:
            roi = arr[690:830, x - 75 : x + 75]
            hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)

            # Arts (蓝卡): H in [95, 135]
            arts_mask = cv2.inRange(hsv, np.array([95, 80, 80], dtype=np.uint8), np.array([135, 255, 255], dtype=np.uint8))
            # Quick (绿卡): H in [38, 85]
            quick_mask = cv2.inRange(hsv, np.array([38, 80, 80], dtype=np.uint8), np.array([85, 255, 255], dtype=np.uint8))
            # Buster (红卡): H in [0, 15] or [165, 180]
            b1 = cv2.inRange(hsv, np.array([0, 90, 90], dtype=np.uint8), np.array([15, 255, 255], dtype=np.uint8))
            b2 = cv2.inRange(hsv, np.array([165, 90, 90], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
            buster_count = int(np.count_nonzero(b1)) + int(np.count_nonzero(b2))
            arts_count = int(np.count_nonzero(arts_mask))
            quick_count = int(np.count_nonzero(quick_mask))

            best = max(
                [("Arts", arts_count), ("Buster", buster_count), ("Quick", quick_count)],
                key=lambda item: item[1]
            )
            colors.append(best[0])

        return colors

    @classmethod
    def extract_battle_wave(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> Optional[int]:
        """
        从 OCR 结果中提取当前战斗轮数 (战斗场次 1/3, 2/3, 3/3)
        国服 1920x1080 位于顶部偏右区域 (cx ≈ 1361, cy ≈ 39)
        """
        for box, text, _ in ocr_results:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            cy = sum(ys) / 4.0
            cx = sum(xs) / 4.0

            if cy < 210 and cx < 1750:
                match = cls.WAVE_PATTERN.search(text)
                if match:
                    try:
                        wave = int(match.group(1))
                        if 1 <= wave <= 3:
                            return wave
                    except Exception:
                        pass
        return None

    @classmethod
    def extract_battle_turn(
        cls,
        ocr_results: List[Tuple[List[List[int]], str, float]]
    ) -> Optional[int]:
        """
        从 OCR 结果中提取当前战斗总回合数 (如 '6回合' -> 6)
        国服 1920x1080 位于顶部偏右区域 (cx ≈ 1401, cy ≈ 144)
        """
        for box, text, _ in ocr_results:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            cy = sum(ys) / 4.0
            cx = sum(xs) / 4.0

            if cy < 220 and cx > 1000:
                match = cls.TURN_PATTERN.search(text)
                if match:
                    try:
                        turn_num = int(match.group(1))
                        if 1 <= turn_num <= 99:
                            return turn_num
                    except Exception:
                        pass
        return None
