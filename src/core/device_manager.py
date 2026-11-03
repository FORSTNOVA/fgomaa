"""
Device Manager - 智能设备探测与连接管理模块
支持自动寻找 adb 路径、扫描常见模拟器进程与动态端口、一键检测与连接。
"""

from __future__ import annotations
import os
import shutil
import subprocess
import re
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from pydantic import BaseModel


class DeviceInfo(BaseModel):
    serial: str
    state: str
    model: str = "Unknown"
    product: str = "Unknown"
    resolution: str = "Unknown"
    is_emulator: bool = False


class DeviceManager:
    """Android 设备与模拟器管理器"""

    # 常见模拟器默认默认安装路径与端口预设
    COMMON_EMULATOR_PORTS = [
        16384, 16416, 16448, 16480,  # MuMu 12 (多开实例递增)
        7555,                         # MuMu 6 / 旧版
        5555, 5556, 5557,             # 雷电模拟器 / BlueStacks
        21503, 21513,                 # 逍遥模拟器
        62001, 62025, 62026,          # 夜神模拟器
    ]

    COMMON_SEARCH_DIRS = [
        r"D:\MuMuPlayer",
        r"C:\Program Files\Netease\MuMuPlayer-12.0",
        r"C:\Program Files\MuMuPlayer-12.0",
        r"D:\leidian\LDPlayer9",
        r"C:\leidian\LDPlayer9",
        r"C:\Program Files\Microvirt\MEmu",
        r"C:\Program Files\Nox\bin",
    ]

    def __init__(self, custom_adb_path: Optional[str] = None):
        self.adb_path = self.find_adb(custom_adb_path)

    def find_adb(self, preferred_path: Optional[str] = None) -> str:
        """寻找可用的 adb.exe 路径"""
        # 1. 优先使用用户指定路径
        if preferred_path and Path(preferred_path).exists():
            return str(preferred_path)

        # 2. 检查系统环境变量 PATH
        which_adb = shutil.which("adb")
        if which_adb:
            return which_adb

        # 3. 常见模拟器安装路径搜索
        for base_dir in self.COMMON_SEARCH_DIRS:
            p = Path(base_dir)
            if p.exists():
                for adb_candidate in p.rglob("adb.exe"):
                    return str(adb_candidate)

        return "adb"  # 兜底默认

    def run_adb(self, args: List[str], timeout: int = 5) -> Tuple[int, str]:
        """执行 adb 命令"""
        cmd = [self.adb_path] + args
        try:
            creationflags = 0
            startupinfo = None
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0

            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
                creationflags=creationflags,
                startupinfo=startupinfo
            )
            return res.returncode, res.stdout.strip()
        except Exception as e:
            return -1, str(e)

    def scan_and_connect_emulators(self) -> None:
        """利用 socket 极速探测本地处于监听状态的模拟器端口并尝试连接"""
        import socket
        for port in self.COMMON_EMULATOR_PORTS:
            addr = f"127.0.0.1:{port}"
            # 先用 socket 探测端口是否可达 (0.05秒超时)，避免 adb connect 阻塞
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.05)
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        self.run_adb(["connect", addr], timeout=2)
            except Exception:
                continue

    def get_connected_devices(self, auto_connect: bool = True) -> List[DeviceInfo]:
        """获取所有当前已连接的设备列表"""
        if auto_connect:
            self.scan_and_connect_emulators()

        code, out = self.run_adb(["devices", "-l"])
        if code != 0:
            return []

        devices: List[DeviceInfo] = []
        lines = out.splitlines()

        for line in lines[1:]:  # 跳过第一行 "List of devices attached"
            line = line.strip()
            if not line or line.startswith("*"):
                continue

            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                state = parts[1]

                # 解析 model 与 product
                model_match = re.search(r"model:(\S+)", line)
                product_match = re.search(r"product:(\S+)", line)

                model = model_match.group(1) if model_match else "Unknown"
                product = product_match.group(1) if product_match else "Unknown"
                is_emulator = "127.0.0.1" in serial or "emulator" in serial

                # 获取分辨率 (仅对在线 device 查询)
                resolution = "Unknown"
                if state == "device":
                    _, size_out = self.run_adb(["-s", serial, "shell", "wm", "size"])
                    res_match = re.search(r"(\d+x\d+)", size_out)
                    if res_match:
                        resolution = res_match.group(1)

                devices.append(DeviceInfo(
                    serial=serial,
                    state=state,
                    model=model,
                    product=product,
                    resolution=resolution,
                    is_emulator=is_emulator
                ))

        # 智能优先级排序：模拟器 (127.0.0.1:16416 / is_emulator=True) 优先排在前面，防止用户插着充电手机时被误选
        devices.sort(key=lambda d: (
            0 if "16416" in d.serial else (1 if d.is_emulator else 2),
            0 if d.state == "device" else 1
        ))

        return devices


if __name__ == "__main__":
    manager = DeviceManager()
    print(f"找到 ADB 路径: {manager.adb_path}")
    print("正在扫描并识别已连接设备...")
    devs = manager.get_connected_devices(auto_connect=True)
    if not devs:
        print("未检测到任何已连接的设备或运行中的模拟器。")
    else:
        print(f"成功识别到 {len(devs)} 台设备:")
        for idx, d in enumerate(devs, 1):
            print(f"  [{idx}] 序列号/地址: {d.serial}")
            print(f"      状态: {d.state}")
            print(f"      机型: {d.model} ({d.product})")
            print(f"      分辨率: {d.resolution}")
            print(f"      是否模拟器: {'是' if d.is_emulator else '否'}")
