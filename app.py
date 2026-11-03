"""
FGO MAA Desktop App Launcher - 桌面客户端启动器
启动本地后端服务并拉起独立桌面原生窗口 (完全还原 MAA 客户端视觉体验)。
"""

import sys
import os
import io
import time
import socket
import logging
import tempfile
import subprocess
import webbrowser
from pathlib import Path

# 配置日志输出到本地文件 fgo_maa.log
log_file = Path(os.environ.get("TEMP", ".")) / "fgo_maa.log"
logging.basicConfig(
    filename=str(log_file),
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# 防止在 PyInstaller --noconsole 模式下因 sys.stdout/stderr 为 None 导致任意第三方库崩溃
class NullWriter(io.StringIO):
    def write(self, s):
        return len(s)
    def flush(self):
        pass

if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()

# 动态源码热挂载：若存在源码目录 (发布包同级 src 或开发工作区根目录)，置于导入最高优先级
if getattr(sys, "frozen", False):
    exe_dir = Path(sys.executable).parent
    for cand in [exe_dir, exe_dir.parent.parent]:
        if (cand / "src").exists() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
            try:
                from importlib.machinery import PathFinder
                if PathFinder in sys.meta_path:
                    sys.meta_path.remove(PathFinder)
                    sys.meta_path.insert(0, PathFinder)
            except Exception:
                pass
            break

from src.core.path_helper import get_root_dir, get_resource_path
from src.server.web_server import start_server, state


def is_port_in_use(port: int) -> bool:
    """检查指定端口是否已被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def find_available_port(start_port: int = 18888, max_attempts: int = 10) -> int:
    """自动寻找未被占用的可用端口"""
    for p in range(start_port, start_port + max_attempts):
        if not is_port_in_use(p):
            return p
    return start_port


def find_browser_app_mode_executable() -> str:
    """寻找支持 --app 独立窗口模式的浏览器 (优先 Edge, 其次 Chrome)"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


def wait_for_server_socket(port: int, timeout: float = 6.0) -> bool:
    """通过纯原生 Socket 直接探测本地服务，完全免疫任何网络代理拦截与卡顿"""
    start_t = time.time()
    while time.time() - start_t < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                s.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                resp = s.recv(512)
                if b"200 OK" in resp or b"status" in resp:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def main():
    try:
        logging.info("=== 启动 FGO MAA 客户端 ===")
        port = find_available_port(18888)
        url = f"http://127.0.0.1:{port}"
        logging.info(f"选定服务端口: {port}, URL: {url}")

        # 1. 启动轻量高效后端 HTTP 服务
        srv = start_server(port)
        state.last_heartbeat = time.time()

        # 2. 原生 Socket 快速同步验证
        logging.info("正在验证本地服务就绪状态...")
        if not wait_for_server_socket(port, timeout=6.0):
            logging.error("本地服务未能按时就绪，将尝试强制打开界面...")
        else:
            logging.info("√ 本地 HTTP 服务已成功就绪并处于监听状态！")

        # 3. 拉起独立原生客户端窗口
        browser_exe = find_browser_app_mode_executable()
        profile_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "FGO_MAA" / "webview_profile"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        proc = None
        if browser_exe:
            logging.info(f"以原生 App 模式拉起窗口: {browser_exe}")
            # 必须加入 --proxy-bypass-list 防止被用户开启的系统代理拦截 127.0.0.1
            cmd = [
                browser_exe,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost",
                "--window-size=1260,820",
                "--window-position=120,80",
            ]
            proc = subprocess.Popen(cmd)
        else:
            logging.info("通过系统默认浏览器打开界面...")
            webbrowser.open(url)

        # 4. 主进程生命周期常驻守卫
        logging.info("进入主循环生命周期常驻守护...")

        while True:
            time.sleep(2)
            now = time.time()

            # 超过 180 秒完全无网络心跳交互才视为异常超时退出
            time_since_heartbeat = now - state.last_heartbeat
            if time_since_heartbeat > 180.0:
                logging.info(f"客户端长时间无网络交互 (超过 {time_since_heartbeat:.0f}s)，正在退出程序...")
                break

    except KeyboardInterrupt:
        logging.info("接收到用户退出信号 (KeyboardInterrupt)")
    except Exception as e:
        logging.exception(f"运行时发生未捕获异常: {e}")
    finally:
        logging.info("=== FGO MAA 客户端已安全退出 ===")


if __name__ == "__main__":
    main()
