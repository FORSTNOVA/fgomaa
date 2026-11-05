"""
FGO Live Controller - 实机/模拟器 ADB 触控与视讯控制模块 (MAA 原生架构升级版)
采用 MAA 同款的 In-Memory Raw Screencap 内存原始像素流技术，彻底消除磁盘 IO 与 broken PNG 错误。
"""

from __future__ import annotations
import sys
import time
import struct
import threading
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List, Callable, Any
import random
from PIL import Image

# 保证独立运行与包导入正常
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.core.device_manager import DeviceManager
from src.strategy.anchor_verifier import AnchorVerifier


class FGOController:
    """FGO 实机触控与图像捕获控制器 (MAA 级 Raw Screencap)"""

    BASE_WIDTH = 1920
    BASE_HEIGHT = 1080

    def __init__(
        self,
        device_serial: Optional[str] = None,
        adb_path: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[Any] = None
    ):
        device_mgr = DeviceManager(custom_adb_path=adb_path)
        self.adb_path = device_mgr.adb_path
        self.log_cb = log_callback or (lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}"))
        self.cancel_event = cancel_event

        if not device_serial:
            devices = device_mgr.get_connected_devices(auto_connect=False, include_resolution=False)
            if not any(device.is_emulator and device.state == "device" for device in devices):
                devices = device_mgr.get_connected_devices(auto_connect=True, include_resolution=False)
            devs = [device for device in devices if device.state == "device"]
            if not devs:
                raise RuntimeError("未检测到任何已连接的设备或模拟器！")
            self.device_serial = devs[0].serial
        else:
            self.device_serial = device_serial

        self.width, self.height = self._get_screen_size()

        # 帧缓存与线程互斥锁 (消除前端轮询与后台工作流的 ADB 争用)
        self._screencap_lock = threading.Lock()
        self._last_frame: Optional[Image.Image] = None
        self._last_frame_time: float = 0.0
        self._cache_ttl: float = 0.25  # 250ms 内重复请求直接走高速内存缓存

    @staticmethod
    def _run_cmd(cmd: List[str], timeout: Optional[float] = None, text: bool = False) -> subprocess.CompletedProcess:
        """执行系统子进程命令，在 Windows 下静默无窗运行，彻底杜绝弹出任何控制台/PowerShell窗口"""
        creationflags = 0
        startupinfo = None
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE

        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=text,
            creationflags=creationflags,
            startupinfo=startupinfo,
            errors="ignore" if text else None
        )

    def log(self, msg: str):
        self.log_cb(msg)

    def _get_screen_size(self) -> Tuple[int, int]:
        cmd = [self.adb_path, "-s", self.device_serial, "shell", "wm", "size"]
        res = self._run_cmd(cmd, timeout=5, text=True)
        import re
        match = re.search(r"(\d+)x(\d+)", res.stdout)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            return (max(w, h), min(w, h))
        return (self.BASE_WIDTH, self.BASE_HEIGHT)

    def _scale_coord(self, x: int, y: int) -> Tuple[int, int]:
        real_x = int(x * self.width / self.BASE_WIDTH)
        real_y = int(y * self.height / self.BASE_HEIGHT)
        return real_x, real_y

    def _safe_sleep(self, duration: float) -> None:
        """支持取消事件的高响应度分段休眠，彻底消除关闭后残余点击"""
        end_time = time.time() + duration
        while time.time() < end_time:
            if self.cancel_event is not None and self.cancel_event.is_set():
                break
            time.sleep(min(0.08, max(0.01, end_time - time.time())))

    def click(self, x: int, y: int, delay_after: float = 0.5) -> None:
        """真实发送 ADB 触控点击 (支持外部停止信号立即熔断)"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return
        rx, ry = self._scale_coord(x, y)
        cmd = [self.adb_path, "-s", self.device_serial, "shell", "input", "tap", str(rx), str(ry)]
        self._run_cmd(cmd)
        if delay_after > 0:
            self._safe_sleep(delay_after)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        """真实发送 ADB 滑动 (支持外部停止信号立即熔断)"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return
        rx1, ry1 = self._scale_coord(x1, y1)
        rx2, ry2 = self._scale_coord(x2, y2)
        cmd = [
            self.adb_path, "-s", self.device_serial, "shell", "input", "swipe",
            str(rx1), str(ry1), str(rx2), str(ry2), str(duration_ms)
        ]
        self._run_cmd(cmd)
        self._safe_sleep(0.5)

    def press_back(self) -> None:
        """模拟点击 Android 返回键"""
        cmd = [self.adb_path, "-s", self.device_serial, "shell", "input", "keyevent", "4"]
        self._run_cmd(cmd)
        time.sleep(0.5)

    # ==================== MAA 规范：内存级极速 Raw Screencap 与消抖检测 ====================

    def screencap(self, force_fresh: bool = False, wait_still: bool = False, *, require_capture: bool = False) -> Image.Image:
        """
        MAA 同款内存无损截屏 (ScreencapRaw)
        1. 纯内存管道读取原始未压缩 RGBA 数据流，零磁盘 IO；
        2. 全局互斥锁 + 高速缓存保护，杜绝多线程资源抢占与 broken PNG 错误；
        3. 速度提升 4~5 倍 (仅需 200ms)；
        4. 支持 wait_still 自动消抖动等待画面静止。
        """
        if wait_still:
            return self.wait_for_still()

        now = time.time()
        # 1. 检查高速缓存
        if not force_fresh and self._last_frame and (now - self._last_frame_time < self._cache_ttl):
            return self._last_frame

        with self._screencap_lock:
            # 双重检验，避免锁排队后重复读取
            if not force_fresh and self._last_frame and (time.time() - self._last_frame_time < self._cache_ttl):
                return self._last_frame

            try:
                # 优先采用 MAA ScreencapRaw 原始像素流模式 (不带 -p 选项)
                cmd = [self.adb_path, "-s", self.device_serial, "exec-out", "screencap"]
                res = self._run_cmd(cmd, timeout=5)
                raw_data = res.stdout

                if res.returncode == 0 and len(raw_data) >= 16:
                    w, h, fmt = struct.unpack("<III", raw_data[:12])
                    expected_payload = w * h * 4
                    # 检查字节长度是否完整
                    if len(raw_data) >= 16 + expected_payload:
                        img = Image.frombytes("RGBA", (w, h), raw_data[16:16 + expected_payload])
                        self._last_frame = img
                        self._last_frame_time = time.time()
                        return img
            except Exception as e:
                self.log(f"[截屏警告] Raw模式捕获异常，尝试安全回退: {e}")

            # 优雅兜底机制：若 Raw 模式未成功，使用带锁的内存 PNG 模式
            try:
                cmd_fallback = [self.adb_path, "-s", self.device_serial, "exec-out", "screencap", "-p"]
                res = self._run_cmd(cmd_fallback, timeout=5)
                data = res.stdout
                if res.returncode == 0 and data.startswith(b"\x89PNG"):
                    import io
                    if data[4:8] != b"\r\n\x1a\n":
                        data = data.replace(b"\r\r\n", b"\r\n").replace(b"\r\n", b"\n")
                    img = Image.open(io.BytesIO(data))
                    img.load()
                    self._last_frame = img
                    self._last_frame_time = time.time()
                    return img
            except Exception as e2:
                self.log(f"[截屏错误] 内存截屏兜底失败: {e2}")

            if require_capture:
                raise RuntimeError(f"无法从 {self.device_serial} 获取截图")

            # 极端异常情况下，若有上一帧有效画面直接返回，保证系统绝不崩溃
            if self._last_frame:
                return self._last_frame

            # 若完全无帧，返回黑色安全图像
            black_fallback = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 255))
            return black_fallback

    def wait_for_still(
        self,
        timeout: float = 2.5,
        min_still_duration: float = 0.20,
        check_interval: float = 0.12,
        diff_threshold: float = 0.012,
        roi: Optional[Tuple[int, int, int, int]] = None
    ) -> Image.Image:
        """
        MAA 同款画面消抖动/静止检测 (Frame Stability / Debounce)
        连续采样计算相邻帧之间的像素差异 (MAE)，当在 min_still_duration 时间内
        波动均低于 diff_threshold 时，判定画面已经完全稳定（动画/滚动/弹窗过渡结束），
        立即返回稳定的高清帧。彻底避免在界面转场过渡阶段识别导致的乱序与误判。
        """
        start_time = time.time()
        prev_img = self.screencap(force_fresh=True)
        still_start: Optional[float] = None

        while time.time() - start_time < timeout:
            if self.cancel_event is not None and self.cancel_event.is_set():
                return prev_img
            self._safe_sleep(check_interval)

            curr_img = self.screencap(force_fresh=True)
            is_stable, diff = AnchorVerifier.is_frame_stable(prev_img, curr_img, threshold=diff_threshold, roi=roi)

            now = time.time()
            if is_stable:
                if still_start is None:
                    still_start = now
                elif (now - still_start) >= min_still_duration:
                    return curr_img
            else:
                still_start = None

            prev_img = curr_img

        return prev_img

    def click_and_verify(
        self,
        x: int,
        y: int,
        verify_fn: Callable[[], bool],
        timeout: float = 3.0,
        interval: float = 0.4,
        max_retries: int = 2,
        jitter: int = 3
    ) -> bool:
        """
        MAA / Sense-Act-Verify 闭环动作校验器：
        执行点击后，绝不盲目假定成功，而是通过 verify_fn 实时校验状态流转是否发生。
        若在 interval 内未满足条件，自动附加 ±jitter 像素微扰进行重试重发。
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return False

        for attempt in range(max_retries + 1):
            cur_x = x if attempt == 0 else x + random.randint(-jitter, jitter)
            cur_y = y if attempt == 0 else y + random.randint(-jitter, jitter)

            self.click(cur_x, cur_y, delay_after=0.1)

            # 轮询验证回调是否达成
            wait_end = time.time() + interval
            while time.time() < wait_end:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    return False
                if verify_fn():
                    return True
                self._safe_sleep(0.08)

        return False

    # ==================== 真实日常业务实机操作 ====================

    def dismiss_popups(self, times: int = 4) -> None:
        """真实清理每日签到、公告与更新提示弹窗"""
        self.log(f"正在实机清理弹窗公告 (连续尝试 {times} 次点击关闭)...")
        for _ in range(times):
            self.click(1650, 950, delay_after=0.6)
            self.click(960, 950, delay_after=0.6)

    def claim_giftbox(self) -> None:
        """真实领取礼物盒全部物品"""
        self.log("正在实机点击主界面【礼物盒】图标 (1780, 200)...")
        self.click(1780, 200, delay_after=1.5)
        self.log("正在点击【全部领取】按钮 (1380, 930)...")
        self.click(1380, 930, delay_after=1.0)
        self.log("正在点击确认领取 (960, 850)...")
        self.click(960, 850, delay_after=0.8)
        self.log("返回主界面...")
        self.click(1650, 950, delay_after=0.5)
        self.press_back()
        time.sleep(1.0)

    def summon_free_fp(self) -> None:
        """真实进行每日免费友情 10 连"""
        self.log("正在点击底栏【召唤】按钮 (1150, 1000)...")
        self.click(1150, 1000, delay_after=2.0)
        self.log("正在点击友情池【免费召唤10次】按钮 (960, 750)...")
        self.click(960, 750, delay_after=1.0)
        self.log("正在点击【确认召唤】按钮 (1150, 750)...")
        self.click(1150, 750, delay_after=1.5)
        self.log("正在快速跳过抽卡出目动画...")
        for _ in range(8):
            self.click(1600, 950, delay_after=0.4)
        self.log("抽卡完毕，点击返回...")
        self.press_back()
        time.sleep(1.0)

    def enter_chaldea_gate_and_quest(self) -> None:
        """真实进入迦勒底之门并选择顶部极级日常本"""
        self.log("正在点击主界面【迦勒底之门】入口 (1400, 300)...")
        self.click(1400, 300, delay_after=2.0)
        self.log("正在点击日常极级副本 (1000, 320)...")
        self.click(1000, 320, delay_after=1.5)

    def select_first_support(self) -> None:
        """真实在助战列表选择第 1 位助战好友 (安全点击从者卡片区域 700, 380，避开礼装图标)"""
        self.log("进入助战选择界面，正在选择第 1 位助战好友 (700, 380)...")
        self.click(700, 380, delay_after=2.0)

    def start_quest(self) -> None:
        """真实在出击确认界面点击右下角金色【开始任务】大按钮"""
        self.log("出击确认就绪，正在点击右下角金色【开始任务】大按钮 (1680, 980)...")
        self.click(1680, 980, delay_after=1.0)

    def wait_for_combat_command_ready(
        self,
        timeout: float = 6.0,
        target_idx: Optional[int] = None
    ) -> bool:
        """
        基于用户准则【前置界面确认 -> 执行动作 -> 获取新截图 -> 验证动作结果】的战斗指令就绪闭环：
        1. 若弹出【无法使用技能/能量不足】次级弹窗遮罩：
           点击【关闭】(960, 845) -> 获取新截图验证 -> 若底层有技能确认框点击【取消】(637, 635) -> 获取新截图验证 -> 返回 False
        2. 若弹出【技能使用】确认框：
           点击【决定】(1260, 615) -> 获取新截图验证动作结果：
           - 若弹出无法使用技能错误：执行关闭并取消流程，返回 False
           - 若依然停留在确认框（【决定】置灰禁用，如形态不符）：点击【取消】(637, 635) 退出 -> 验证关闭 -> 返回 False
           - 若弹出【请选择对象】：选定目标从者并等待动效
        3. 循环等待至右下角【攻击】(Attack) 按钮亮起发光，返回 True。
        """
        start_t = time.time()
        deadline = start_t + timeout
        hard_deadline = deadline + 12.0
        target_x_map = {1: 500, 2: 960, 3: 1440}
        confirm_attempts = 0
        target_attempts = 0

        while time.time() < min(deadline, hard_deadline):
            if self.cancel_event is not None and self.cancel_event.is_set():
                return False
            frame = self.screencap(force_fresh=True)

            # 优先检查是否存在【无法使用技能/能量不足】次级弹窗并严格闭环清理
            check_ocr = []
            try:
                from src.strategy.screen_detector import ScreenDetector
                check_ocr = ScreenDetector.run_ocr(frame)
                has_err = any("无法使用技能" in t or "能量不足" in t for _, t, _ in check_ocr)
                if has_err:
                    sub_close = ScreenDetector.find_text_coordinate(check_ocr, "关闭", region=(600, 650, 1320, 950)) or (960, 845)
                    self.log(f"  [技能闭环] 检测到技能无法使用弹窗，点击【关闭】({sub_close})...")
                    self.click(sub_close[0], sub_close[1], delay_after=0.55)
                    
                    # 动作后获取新截图，验证错误遮罩已关闭
                    after_err_frame = self.screencap(force_fresh=True)
                    after_ocr = ScreenDetector.run_ocr(after_err_frame)
                    
                    # 若底层残留【技能使用】确认框，点击【取消】(637, 635)
                    if any("技能使用" in t or "取消" in t for _, t, _ in after_ocr):
                        cancel_pos = ScreenDetector.find_text_coordinate(after_ocr, "取消", region=(400, 500, 800, 750)) or (637, 635)
                        self.log(f"  [技能闭环] 安全点击【取消】({cancel_pos}) 退出底层技能确认框...")
                        self.click(cancel_pos[0], cancel_pos[1], delay_after=0.55)
                        # 再次获取新截图验证确认框已关闭
                        fresh_f = self.screencap(force_fresh=True)
                        if AnchorVerifier.is_attack_button_ready(fresh_f):
                            self.log("  [技能闭环] 指令阶段已恢复就绪！")
                    return False
            except Exception:
                pass

            is_confirm = AnchorVerifier.is_skill_confirm_dialog_open(frame)
            if not is_confirm:
                try:
                    from src.strategy.screen_detector import ScreenDetector
                    is_confirm = any("技能使用" in t for _, t, _ in check_ocr)
                except Exception:
                    pass

            if is_confirm:
                confirm_attempts += 1
                if confirm_attempts == 1:
                    self.log("  [技能闭环] 检测到【技能使用】确认框，点击【决定】(1260, 615)...")
                    self.click(1260, 615, delay_after=0.55)
                    
                    # 严格执行：动作后获取新截图，验证动作结果
                    post_frame = self.screencap(force_fresh=True)
                    try:
                        from src.strategy.screen_detector import ScreenDetector
                        post_ocr = ScreenDetector.run_ocr(post_frame)
                        # 检查是否弹出了【无法使用技能/能量不足】
                        if any("无法使用技能" in t or "能量不足" in t for _, t, _ in post_ocr):
                            self.log("  [技能闭环] 点击决定后触发【无法使用技能】错误，点击【关闭】(960, 845)...")
                            self.click(960, 845, delay_after=0.55)
                            # 验证后关闭底层
                            f3 = self.screencap(force_fresh=True)
                            f3_ocr = ScreenDetector.run_ocr(f3)
                            if any("技能使用" in t or "取消" in t for _, t, _ in f3_ocr):
                                self.log("  [技能闭环] 安全点击【取消】(637, 635) 退出...")
                                self.click(637, 635, delay_after=0.55)
                            return False
                        
                        # 检查是否依然停留在【技能使用】（说明【决定】按钮置灰无反应，如形态不符）
                        if any("技能使用" in t for _, t, _ in post_ocr):
                            self.log("  [技能闭环] 点击决定无响应（按钮置灰或条件不满足），点击【取消】(637, 635) 安全退出...")
                            self.click(637, 635, delay_after=0.55)
                            return False
                    except Exception:
                        pass
                    # Confirmation and target selection each consume OCR and
                    # touch time; give the next phase its own bounded wait.
                    deadline = min(hard_deadline, time.time() + timeout)
                else:
                    self.log("  [技能闭环] 技能使用确认重试未果，点击【取消】(637, 635) 安全退出...")
                    self.click(637, 635, delay_after=0.55)
                    return False
                continue

            if (AnchorVerifier.is_skill_target_dialog_open(frame)
                    and any("请选择对象" in text.replace(" ", "") for _, text, _ in check_ocr)):
                target_attempts += 1
                if target_attempts > 2:
                    self.log("  [技能闭环] 目标框连续未关闭，停止本次技能。")
                    return False
                t_idx = target_idx or 1
                tx = target_x_map.get(t_idx, 500)
                self.log(f"  [技能闭环] 检测到【请选择对象】目标框，选定 {t_idx} 号位从者 ({tx}, 560)...")
                self.click(tx, 560, delay_after=0.55)
                # 动作后获取新截图验证目标框已关闭
                time.sleep(0.4)
                deadline = min(hard_deadline, time.time() + timeout)
                continue

            if AnchorVerifier.is_attack_button_ready(frame):
                self._safe_sleep(0.12)
                return True

            self._safe_sleep(0.22)

        # 超时退出前再次核验：若仍有技能确认框残留，必须点击【取消】关闭，绝不可阻断后续出击
        try:
            from src.strategy.screen_detector import ScreenDetector
            final_frame = self.screencap(force_fresh=True)
            final_ocr = ScreenDetector.run_ocr(final_frame)
            if any("技能使用" in t for _, t, _ in final_ocr):
                self.log("  [技能闭环] 退出前检测到残留技能确认框，点击【取消】(637, 635)...")
                self.click(637, 635, delay_after=0.6)
        except Exception:
            pass

        return AnchorVerifier.is_attack_button_ready(self.screencap(force_fresh=True))

    def cast_servant_skill(self, servant_idx: int, skill_idx: int, target_idx: Optional[int] = None) -> bool:
        """
        基于 FGA 标准 1080p 坐标与冷却/确认弹窗/目标框三重闭环释放从者技能：
        1. 确认当前处于可释放技能的指令阶段 (自动清理任何未关闭的确认框/目标框)；
        2. 视觉检测该技能图标是否已转好冷却 (Off-Cooldown)，若处于冷却中则立即跳过；
        3. 点击技能图标 (x, 870)；
        4. 闭环处理【技能使用 -> 决定 (1260, 615)】与【请选择对象 -> 目标从者 (500/960/1440, 560)】，
           并等待技能动画结束且右下角【攻击】按钮重新亮起后再返回，返回是否成功释放。
        """
        base_x = {
            1: [111, 243, 375],
            2: [588, 720, 852],
            3: [1064, 1196, 1328]
        }
        if servant_idx not in base_x or not (1 <= skill_idx <= 3):
            return False

        # 1. 确保当前处于可释放技能的指令阶段
        if not self.wait_for_combat_command_ready(timeout=4.0, target_idx=target_idx or 1):
            self.log(f"  [技能等待超时] 当前未处于指令就绪态，暂缓释放 {servant_idx}号位第{skill_idx}技能")
            return False

        # 2. 检查技能是否处于冷却中 (剩余 X 暗色遮罩)
        pre_frame = self.screencap(force_fresh=True)
        if not AnchorVerifier.is_skill_ready(pre_frame, servant_idx, skill_idx):
            self.log(f"  [技能冷却跳过] {servant_idx} 号位从者第 {skill_idx} 技能尚在冷却中 (CD未转好)，跳过...")
            return False

        x = base_x[servant_idx][skill_idx - 1]
        self.log(f"  [实机操作] 正在释放 {servant_idx} 号位从者第 {skill_idx} 技能 (坐标: {x}, 870)...")
        self.click(x, 870, delay_after=0.62)

        # 若第一次点击未触发确认框、目标框或动画 (攻击按钮依然亮着且技能仍显示未进入冷却)，补点一次
        mid_frame = self.screencap(force_fresh=True)
        if AnchorVerifier.is_attack_button_ready(mid_frame) and AnchorVerifier.is_skill_ready(mid_frame, servant_idx, skill_idx):
            # 确认框下的攻击按钮仍可能命中颜色锚点；此时补点技能会关闭确认框。
            from src.strategy.screen_detector import ScreenDetector
            modal_ocr = ScreenDetector.run_ocr(mid_frame)
            modal_open = (AnchorVerifier.is_skill_confirm_dialog_open(mid_frame) or
                          AnchorVerifier.is_skill_target_dialog_open(mid_frame) or
                          any("技能使用" in text or "请选择对象" in text.replace(" ", "")
                              for _, text, _ in modal_ocr))
            if not modal_open:
                self.click(x, 870, delay_after=0.62)

        # 3. 闭环处理【技能使用 -> 决定】、【请选择对象 -> 目标从者】及等待技能动效结束，严格返回闭环结果
        success = self.wait_for_combat_command_ready(timeout=6.0, target_idx=target_idx or 1)
        if not success:
            return False
        if AnchorVerifier.is_skill_ready(self.screencap(force_fresh=True), servant_idx, skill_idx):
            self.log(f"  [技能未生效] {servant_idx}号位第{skill_idx}技能未进入冷却，停止后续操作。")
            return False
        return True

    def cast_master_skill(self, skill_idx: int, target_idx: Optional[int] = None, swap: Optional[Tuple[int, int] | List[int]] = None) -> bool:
        """
        基于国服 1920x1080 实测坐标释放御主技能或换人服：
        御主技能展开按钮: (1790, 470)
        技能 1~3 位置: (1360, 470), (1490, 470), (1610, 470)
        """
        if not self.wait_for_combat_command_ready(timeout=4.0, target_idx=target_idx or 1):
            return False

        self.log("  [实机操作] 打开右侧【御主技能】面板 (坐标: 1790, 470)...")
        self.click(1790, 470, delay_after=0.65)

        skill_x = {1: 1360, 2: 1490, 3: 1610}.get(skill_idx, 1360)
        self.log(f"  [实机操作] 点击第 {skill_idx} 个御主技能 (坐标: {skill_x}, 470)...")
        self.click(skill_x, 470, delay_after=0.65)

        # 若开启了技能使用确认，先点击【决定】(1260, 615)
        conf_frame = self.screencap(force_fresh=True)
        if AnchorVerifier.is_skill_confirm_dialog_open(conf_frame):
            self.log("  [御主技能闭环] 检测到【技能使用】确认框，点击【决定】(1260, 615)...")
            self.click(1260, 615, delay_after=0.65)

        if swap:
            x_map = {1: 210, 2: 510, 3: 810, 4: 1110, 5: 1410, 6: 1710}
            self.log(f"  [实机操作] 正在执行换人: 将 {swap[0]} 号位 换为 {swap[1]} 号位...")
            self.click(x_map[swap[0]], 540, delay_after=0.45)
            self.click(x_map[swap[1]], 540, delay_after=0.45)
            self.log("  [实机操作] 点击【开始更换】(960, 950)...")
            self.click(960, 950, delay_after=0.8)
            return self.wait_for_combat_command_ready(timeout=7.5, target_idx=1)
        else:
            return self.wait_for_combat_command_ready(timeout=5.5, target_idx=target_idx or 1)

    def open_command_cards(self) -> Optional[Image.Image]:
        self.log("  [实机操作] 点击右下角圆形【攻击】大按钮 (坐标: 1725, 900)...")

        card_select_ready = False
        card_frame: Optional[Image.Image] = None
        for attempt in range(3):
            if self.cancel_event is not None and self.cancel_event.is_set():
                return None
            self.click(1725, 900, delay_after=0.35)

            wait_until = time.time() + 1.4
            while time.time() < wait_until:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    return None
                frame = self.screencap(force_fresh=True)
                if AnchorVerifier.is_card_select_back_button_present(frame):
                    card_select_ready = True
                    card_frame = frame
                    break
                self._safe_sleep(0.1)

            if card_select_ready:
                break
            self.log(f"  [选卡重试] 选卡界面尚未展开，重试点击攻击按钮 ({attempt + 1}/3)...")

        if card_select_ready:
            self.log("  √ [FGA 闭环确认] 检测到右下角【Back】蓝色按键就绪，选卡托盘已展开！")
        else:
            self.log("  [选卡停止] 未确认选卡托盘已展开，不继续盲点。")
            return None
        return card_frame

    def play_recorded_cards(self, chain, color_priority) -> bool:
        """整数表示已核对的宝具槽位，None 表示按实机卡色优先级补一张普通卡。"""
        np_slots = [slot for slot in chain if slot is not None]
        if len(chain) != 3 or len(set(np_slots)) != len(np_slots) or any(
                slot not in (1, 2, 3) for slot in np_slots):
            return False
        if self.open_command_cards() is None:
            return False
        self._safe_sleep(.55)
        frame = self.screencap(force_fresh=True)
        if not AnchorVerifier.is_card_select_back_button_present(frame):
            return False
        colors = AnchorVerifier.detect_command_card_colors(frame)
        if len(colors) != 5 or any(color not in color_priority for color in colors):
            return False
        normal = iter(sorted(zip(colors, [225, 562, 975, 1350, 1762]),
                             key=lambda row: color_priority.index(row[0])))
        self.log(f"[录制选卡] 实机手牌 {colors}，优先级 {color_priority}")
        for slot in chain:
            if self.cancel_event is not None and self.cancel_event.is_set():
                return False
            if slot is None:
                self.click(next(normal)[1], 750, delay_after=.28)
            else:
                self.click({1:700, 2:960, 3:1220}[slot], 320, delay_after=.28)
        return True

    def select_attack_and_np(
        self,
        np_servant_idx: Optional[int] = 1,
        preferred_color: str = "Arts",
        allow_support_np: bool = False
    ) -> bool:
        """打开放置区，按当前路线选宝具和同色指令卡。"""
        card_frame = self.open_command_cards()
        if card_frame is None:
            return False
        # 1. 激活指定主核打手宝具卡 (默认仅点选 np_servant_idx，不乱开辅助拐宝具)
        np_coords = {1: 700, 2: 960, 3: 1220}
        if np_servant_idx is not None:
            if np_servant_idx not in np_coords:
                return False
            target_np_x = np_coords[np_servant_idx]
            self.log(f"  [实机操作] 激活 {np_servant_idx} 号位宝具卡 ({target_np_x}, 320)...")
            self.click(target_np_x, 320, delay_after=0.28)
        if allow_support_np and np_servant_idx is not None:
            for idx, x_pos in np_coords.items():
                if idx != np_servant_idx:
                    self.click(x_pos, 320, delay_after=0.18)

        # 2. 识别 5 张指令卡色卡，优先选择与体系匹配的色卡凑齐同色 Chain (特别是蓝卡队凑 3 蓝链全队 +20% NP)
        card_xs = [225, 562, 975, 1350, 1762]
        if card_frame is not None:
            try:
                # Back按钮出现时卡面可能还在展开，判色须使用当前帧。
                card_frame = self.screencap(force_fresh=True)
                colors = AnchorVerifier.detect_command_card_colors(card_frame)
                paired = list(zip(card_xs, colors))
                paired.sort(key=lambda item: (0 if item[1] == preferred_color else 1))
                ordered_xs = [item[0] for item in paired]
                color_desc = ", ".join(f"C{i+1}:{c}" for i, c in enumerate(colors))
                self.log(f"  [智能选卡] 指令卡色卡识别: [{color_desc}] -> 优先点选 {preferred_color} 卡凑齐色卡链...")
            except Exception:
                ordered_xs = card_xs
        else:
            ordered_xs = card_xs

        selected = 0
        needed = 3 if np_servant_idx is None else 2
        for cx in ordered_xs[:needed]:
            if self.cancel_event is not None and self.cancel_event.is_set():
                break
            self.click(cx, 750, delay_after=0.20)
            selected += 1
        return selected == needed


    def handle_battle_settlement(self, wait_seconds: int = 18) -> None:
        """真实等待宝具播放并跳过战斗胜利结算面板"""
        self.log(f"正在等待宝具动画与战斗胜利 (等待 {wait_seconds} 秒)...")
        time.sleep(wait_seconds)

        self.log("战斗结束，正在实机连续点击跳过战利品掉落、羁绊升级与御主经验面板...")
        for _ in range(12):
            self.click(1600, 900, delay_after=0.5)
            self.click(960, 900, delay_after=0.2)

        self.log("结算点击完成！")

    def claim_master_missions(self) -> None:
        """真实领取御主任务奖励"""
        self.log("正在点击【御主任务】面板入口 (1700, 400)...")
        self.click(1700, 400, delay_after=1.5)
        self.log("正在点击【一键领取】奖励按钮 (1600, 920)...")
        self.click(1600, 920, delay_after=1.0)
        self.log("点击确认关闭弹窗...")
        self.click(960, 850, delay_after=0.5)
        self.press_back()
        time.sleep(1.0)
