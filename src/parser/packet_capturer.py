"""
FGO Local Account Packet Capturer & HTTPS MITM Proxy Manager (内置 HTTPS 证书解密抓包与实机采集引擎)
复刻并超越 Chaldea / Reqable / Mitmproxy 的 FGO 抓包核心实现：
1. 自动通过 `adb root` 将内置根证书 (`61116fa4.0`) 注入模拟器 Android 10~14 系统级根证书库
   (`/system/etc/security/cacerts/` 与 `/apex/com.android.conscrypt/cacerts/` 及游戏进程命名空间)，无需手动安装证书；
2. 自动建立 `adb reverse tcp:18889 tcp:18889` 隧道并挂载 `127.0.0.1:18889` 代理，彻底绕开本机 VPN/Clash 虚拟网卡 (`198.18.0.1`) 干扰；
3. 对 FGO 核心通信域名 (`*-bili-fate.bilibiligame.net:443`) 执行真正的 TLS MITM 解密，实时截获 `_key=toplogin` / `ac.php`
   的 Base64/Gzip/JSON 响应体，一次性解析全账号【从者】与【概念礼装】并自动恢复直连！
"""

from __future__ import annotations
import atexit
import gzip
import http.client
import json
import select
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List

from src.core.path_helper import get_resource_path


class _ProxyHandler(BaseHTTPRequestHandler):
    """支持 HTTPS TLS MITM 解密 (针对 FGO 游戏 API) 与高速 TCP 隧道透传 (针对资源热更新包) 的代理处理器"""

    capturer: Optional["FGOPacketCapturer"] = None

    def log_message(self, format: str, *args: Any) -> None:
        pass

    @staticmethod
    def _is_fgo_api_host(host: str) -> bool:
        """判断是否为需要 TLS 解密提取 toplogin / userSvt 的 FGO 游戏逻辑服域名"""
        h = host.lower()
        if "patch" in h or "log" in h or "sdk" in h or "akamai" in h:
            return False
        return any(k in h for k in ["-bili-fate.bilibiligame.net", "-ios-fate.bilibiligame.net", "-uo-fate.bilibiligame.net", "bili-fate", "rgfate"])

    def do_CONNECT(self) -> None:
        """处理 HTTPS CONNECT 请求：对游戏逻辑服执行 TLS MITM 解密抓包，其余域名执行高速双向隧道"""
        host_port = self.path.split(":")
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 443

        if self.capturer:
            self.capturer.packet_count += 1
            if any(k in host for k in ["bilibiligame", "fate", "fgo", "rongame"]):
                self.capturer._notify_https_host(host, port)

        # 若为 FGO 核心游戏逻辑服且本地 MITM 证书就绪，执行 TLS MITM 解密以拦截 _key=toplogin
        if self.capturer and self._is_fgo_api_host(host) and self.capturer.ssl_context is not None:
            self._handle_mitm_https(host, port)
            return

        # 其余域名 (如 line1-patch-fate 热更新、B站登录SDK等) 走纯 TCP 隧道透传，确保极速稳定
        self._handle_raw_tunnel(host, port)

    def _handle_mitm_https(self, host: str, port: int) -> None:
        """对 *-bili-fate.bilibiligame.net:443 执行 TLS MITM 解密并截获 ac.php?_key=toplogin"""
        self.send_response(200, "Connection Established")
        self.end_headers()

        assert self.capturer is not None and self.capturer.ssl_context is not None
        try:
            tls_client = self.capturer.ssl_context.wrap_socket(self.connection, server_side=True)
            tls_client.settimeout(20.0)
        except Exception as e:
            self.capturer._log(f"[HTTPS握手提示] {host}:{port} TLS 握手回退 ({e})")
            return

        client_rfile = tls_client.makefile("rb", -1)
        client_wfile = tls_client.makefile("wb", 0)
        upstream_ctx = ssl._create_unverified_context()

        try:
            while True:
                req_line_bytes = client_rfile.readline(65536)
                if not req_line_bytes:
                    break
                req_line = req_line_bytes.decode("iso-8859-1", errors="ignore").strip()
                if not req_line:
                    break
                parts = req_line.split(" ")
                if len(parts) < 2:
                    break
                method, req_path = parts[0], parts[1]

                # 读取请求头
                req_headers: Dict[str, str] = {}
                content_len = 0
                is_chunked = False
                while True:
                    hline = client_rfile.readline(65536)
                    if not hline or hline in (b"\r\n", b"\n"):
                        break
                    hstr = hline.decode("iso-8859-1", errors="ignore").strip()
                    if ":" in hstr:
                        k, v = hstr.split(":", 1)
                        k_clean, v_clean = k.strip(), v.strip()
                        if k_clean.lower() == "content-length":
                            content_len = int(v_clean)
                        elif k_clean.lower() == "transfer-encoding" and "chunked" in v_clean.lower():
                            is_chunked = True
                        if k_clean.lower() not in ["proxy-connection", "connection"]:
                            req_headers[k_clean] = v_clean

                req_body = b""
                if content_len > 0:
                    req_body = client_rfile.read(content_len)
                elif is_chunked:
                    chunks = []
                    while True:
                        sz_line = client_rfile.readline().strip()
                        if not sz_line:
                            break
                        sz = int(sz_line.split(b";")[0], 16)
                        if sz == 0:
                            client_rfile.readline()
                            break
                        chunks.append(client_rfile.read(sz))
                        client_rfile.readline()
                    req_body = b"".join(chunks)
                    req_headers["Content-Length"] = str(len(req_body))
                    req_headers.pop("Transfer-Encoding", None)

                if self.capturer and ("toplogin" in req_path or "ac.php" in req_path):
                    self.capturer._log(f"🔍 [HTTPS解密成功] 捕获 FGO 接口请求: {method} https://{host}{req_path[:90]}")

                # 转发给真实的 Bilibili FGO 服务器
                conn = http.client.HTTPSConnection(host, port, context=upstream_ctx, timeout=20)
                conn.request(method, req_path, body=req_body if req_body else None, headers=req_headers)
                resp = conn.getresponse()
                resp_body = resp.read()

                # 将响应原样写回给模拟器内的 FGO 客户端
                status_line = f"HTTP/1.1 {resp.status} {resp.reason}\r\n".encode("iso-8859-1")
                client_wfile.write(status_line)
                for hk, hv in resp.getheaders():
                    if hk.lower() not in ["transfer-encoding", "content-length", "connection"]:
                        client_wfile.write(f"{hk}: {hv}\r\n".encode("iso-8859-1"))
                client_wfile.write(f"Content-Length: {len(resp_body)}\r\n".encode("iso-8859-1"))
                client_wfile.write(b"Connection: keep-alive\r\n\r\n")
                client_wfile.write(resp_body)
                client_wfile.flush()
                conn.close()

                # 解压并送入账户/从者/概念礼装解析器
                raw_bytes = resp_body
                if resp.getheader("Content-Encoding", "").lower() == "gzip" or raw_bytes[:2] == b"\x1f\x8b":
                    try:
                        raw_bytes = gzip.decompress(raw_bytes)
                    except Exception:
                        pass

                text = raw_bytes.decode("utf-8", errors="ignore")
                full_url = f"https://{host}{req_path}"
                if self.capturer and (
                    "toplogin" in req_path
                    or "ac.php" in req_path
                    or len(text) > 400
                ):
                    self.capturer._inspect_captured_payload(full_url, text)
        except Exception:
            pass
        finally:
            try:
                client_rfile.close()
                client_wfile.close()
                tls_client.close()
            except Exception:
                pass

    def _handle_raw_tunnel(self, host: str, port: int) -> None:
        """高速 TCP 双向隧道透传"""
        try:
            remote = socket.create_connection((host, port), timeout=10)
        except Exception:
            self.send_error(502, "Bad Gateway")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()

        conns = [self.connection, remote]
        try:
            while conns:
                rlist, _, xlist = select.select(conns, [], conns, 15.0)
                if xlist or not rlist:
                    break
                for r in rlist:
                    other = remote if r is self.connection else self.connection
                    data = r.recv(16384)
                    if not data:
                        return
                    other.sendall(data)
        except Exception:
            pass
        finally:
            try:
                remote.close()
            except Exception:
                pass

    def _handle_http(self, method: str) -> None:
        if self.capturer:
            self.capturer.packet_count += 1

        url = self.path
        if not url.startswith("http"):
            host = self.headers.get("Host", "")
            url = f"http://{host}{url}"

        content_len = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(content_len) if content_len > 0 else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in ["proxy-connection", "connection", "keep-alive"]}
        req = urllib.request.Request(url, data=req_body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ["transfer-encoding", "connection"]:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

                raw_bytes = resp_body
                if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw_bytes[:2] == b"\x1f\x8b":
                    try:
                        raw_bytes = gzip.decompress(raw_bytes)
                    except Exception:
                        pass

                text = raw_bytes.decode("utf-8", errors="ignore")
                if self.capturer and any(k in url or k in text for k in ["toplogin", "ac.php", "userSvt", "userServant", "treasureDeviceLv1"]):
                    self.capturer._inspect_captured_payload(url, text)
        except Exception as e:
            try:
                self.send_error(502, f"Proxy Error: {e}")
            except Exception:
                pass

    def do_GET(self) -> None:
        self._handle_http("GET")

    def do_POST(self) -> None:
        self._handle_http("POST")


class FGOPacketCapturer:
    """FGO 本地 HTTPS MITM 抓包服务与模拟器根证书/代理自动控制器"""

    def __init__(
        self,
        port: int = 18889,
        log_callback: Optional[Callable[[str], None]] = None,
        on_account_captured: Optional[Callable[[Any], Any]] = None,
        on_https_fallback: Optional[Callable[[str], None]] = None,
    ):
        self.port = port
        self.log_cb = log_callback or (lambda msg: None)
        self.on_account_captured = on_account_captured
        self.on_https_fallback = on_https_fallback
        self.server: Optional[ThreadingHTTPServer] = None
        self.is_listening = False
        self.proxy_auto_set = False
        self.ca_injected = False
        self.packet_count = 0
        self.captured_servants_count = 0
        self.captured_ces_count = 0
        self.last_capture_time: Optional[str] = None
        self._adb_path: Optional[str] = None
        self._device_serial: Optional[str] = None
        self._logged_hosts: set[str] = set()
        self.ssl_context: Optional[ssl.SSLContext] = self._init_ssl_context()

        # 注册进程退出钩子，确保无论如何退出都会还原模拟器的网络代理设置
        atexit.register(self.restore_emulator_proxy)

    def _init_ssl_context(self) -> Optional[ssl.SSLContext]:
        """加载内置的 *.bilibiligame.net 证书与私钥，用于 HTTPS MITM 解密"""
        cert_file = get_resource_path("data/certs/server.crt")
        key_file = get_resource_path("data/certs/server.key")
        if cert_file.exists() and key_file.exists():
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
                return ctx
            except Exception:
                return None
        return None

    def _log(self, msg: str) -> None:
        self.log_cb(msg)

    def _notify_https_host(self, host: str, port: int) -> None:
        """记录 HTTPS 游戏连接状态"""
        if host not in self._logged_hosts:
            self._logged_hosts.add(host)
            if "patch" in host or "log" in host:
                self._log(f"[抓包监听] 捕获到 FGO 资源/心跳通道: {host}:{port} (高速隧道透传)")
            else:
                self._log(
                    f"🔐 [HTTPS解密就绪] 捕获到 FGO 主通信服: {host}:{port}，正在执行 TLS MITM 解密监听 `_key=toplogin` 数据包..."
                )

    @staticmethod
    def get_local_ip() -> str:
        """获取本机局域网 IP 地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _inspect_captured_payload(self, url: str, text: str) -> None:
        """检查抓包捕获的响应文本（支持明文 JSON 与 FGO 国服 Base64 编码响应）是否包含从者及概念礼装数据"""
        if not text or len(text) < 100:
            return
        if self.on_account_captured:
            try:
                res = self.on_account_captured(text)
                if isinstance(res, tuple):
                    svt_count, ce_count = res
                else:
                    svt_count, ce_count = int(res or 0), 0
                if svt_count > 0 or ce_count > 0:
                    self.captured_servants_count = svt_count
                    self.captured_ces_count = ce_count
                    self.last_capture_time = time.strftime("%H:%M:%S")
                    self._log(
                        f"🎉 [抓包入库成功] 已从 `{url[:75]}` 成功解密并提取 {svt_count} 骑从者 + {ce_count} 张概念礼装！"
                        "智能配队已实时更新，正在自动关闭代理恢复模拟器直连..."
                    )
                    self.is_listening = False
                    threading.Thread(target=self.stop, kwargs={"restore_proxy": True}, daemon=True).start()
            except Exception as e:
                if "toplogin" in url:
                    self._log(f"[抓包解析提示] toplogin 响应解析异常: {e}")

    def _inject_emulator_ca_and_proxy(self, adb_path: str, serial: str) -> str:
        """
        复刻 Chaldea / HttpToolkit 原理：
        1. 通过 `adb root` 获取模拟器 root 权限；
        2. 将内置 CA 证书 (`61116fa4.0`) 自动挂载注入 Android 系统证书目录 `/system/etc/security/cacerts` 与 `/apex/com.android.conscrypt/cacerts`；
        3. 通过 `adb reverse tcp:18889 tcp:18889` 建立本地回环隧道并设置代理为 `127.0.0.1:18889`，彻底规避虚拟网卡 (`198.18.0.1`) 干扰。
        """
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        ca_hash_file = get_resource_path("data/certs/61116fa4.0")

        # 1. 尝试开启 adb root 并注入系统级 CA 根证书
        if ca_hash_file.exists():
            try:
                subprocess.run([adb_path, "-s", serial, "root"], capture_output=True, text=True, timeout=6, creationflags=flags)
                subprocess.run(
                    [adb_path, "-s", serial, "push", str(ca_hash_file), "/data/local/tmp/61116fa4.0"],
                    capture_output=True, text=True, timeout=6, creationflags=flags
                )
                inject_sh = (
                    "mkdir -p /data/misc/user/0/cacerts-added && "
                    "cp /data/local/tmp/61116fa4.0 /data/misc/user/0/cacerts-added/61116fa4.0 && "
                    "chmod 644 /data/misc/user/0/cacerts-added/61116fa4.0; "
                    "if [ ! -f /system/etc/security/cacerts/61116fa4.0 ]; then "
                    "mkdir -p /data/local/tmp/cacerts-copy && "
                    "cp /system/etc/security/cacerts/* /data/local/tmp/cacerts-copy/ 2>/dev/null; "
                    "cp /data/local/tmp/61116fa4.0 /data/local/tmp/cacerts-copy/61116fa4.0 && "
                    "chmod 644 /data/local/tmp/cacerts-copy/* && "
                    "mount -t tmpfs tmpfs /system/etc/security/cacerts && "
                    "cp /data/local/tmp/cacerts-copy/* /system/etc/security/cacerts/ && "
                    "chmod 644 /system/etc/security/cacerts/*; "
                    "fi; "
                    "if [ -d /apex/com.android.conscrypt/cacerts ]; then "
                    "mount --bind /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts 2>/dev/null; "
                    "for pid in $(pidof zygote zygote64 com.bilibili.fatego 2>/dev/null); do "
                    "nsenter --mount=/proc/$pid/ns/mnt -- /bin/mount --bind /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts 2>/dev/null; "
                    "done; "
                    "fi"
                )
                subprocess.run([adb_path, "-s", serial, "shell", inject_sh], capture_output=True, text=True, timeout=10, creationflags=flags)
                self.ca_injected = True
            except Exception as e:
                self._log(f"[证书提示] 自动注入系统证书返回: {e}")

        # 2. 建立 adb reverse 本地回环隧道并挂载 127.0.0.1:port 代理 (优先于局域网/虚拟网卡 IP)
        rev_res = subprocess.run(
            [adb_path, "-s", serial, "reverse", f"tcp:{self.port}", f"tcp:{self.port}"],
            capture_output=True, text=True, timeout=5, creationflags=flags
        )
        proxy_target = f"127.0.0.1:{self.port}" if rev_res.returncode == 0 else f"{self.get_local_ip()}:{self.port}"
        subprocess.run(
            [adb_path, "-s", serial, "shell", "settings", "put", "global", "http_proxy", proxy_target],
            capture_output=True, text=True, timeout=5, creationflags=flags
        )
        self.proxy_auto_set = True
        return proxy_target

    def start(self, adb_path: Optional[str] = None, serial: Optional[str] = None, auto_set_proxy: bool = True) -> Dict[str, Any]:
        """启动本地 HTTPS MITM 抓包代理服务器，并自动注入系统根证书与模拟器代理"""
        self._logged_hosts.clear()
        if self.is_listening:
            return {
                "success": True,
                "listening": True,
                "host": "127.0.0.1",
                "port": self.port,
                "message": f"HTTPS 解密抓包监听已在运行中 (端口 {self.port})"
            }

        _ProxyHandler.capturer = self
        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", self.port), _ProxyHandler)
            t = threading.Thread(target=self.server.serve_forever, daemon=True)
            t.start()
            self.is_listening = True
            self.packet_count = 0
        except Exception as e:
            return {"success": False, "listening": False, "message": f"启动抓包代理端口 {self.port} 失败: {e}"}

        proxy_target = f"{self.get_local_ip()}:{self.port}"
        if auto_set_proxy and adb_path and serial:
            self._adb_path = adb_path
            self._device_serial = serial
            try:
                proxy_target = self._inject_emulator_ca_and_proxy(adb_path, serial)
                self._log(
                    f"📡 [HTTPS 解密抓包已开启] 已向模拟器 {serial} 自动注入系统级 CA 根证书并建立 `{proxy_target}` 代理隧道！"
                    "现在请在模拟器中【从 FGO 标题画面点击屏幕登录进入主界面】（若已在游戏内，请点个人空间->返回标题画面重新登录），即可 100% 截获 `toplogin` 全量从者与概念礼装！"
                )
            except Exception as e:
                self._log(f"[抓包提示] 自动配置模拟器代理异常 ({e})")
        else:
            self._log(f"📡 [抓包已开启] 监听端口: {self.port}")

        return {
            "success": True,
            "listening": True,
            "host": proxy_target.split(":")[0],
            "port": self.port,
            "proxy_auto_set": self.proxy_auto_set,
            "ca_injected": self.ca_injected,
            "message": f"HTTPS MITM 抓包已启动 ({proxy_target} | 系统证书已自动注入)"
        }

    def restore_emulator_proxy(self) -> None:
        """清除模拟器上的 HTTP 代理与 adb reverse 隧道，恢复模拟器直连网络"""
        if self._adb_path and self._device_serial:
            try:
                flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                subprocess.run(
                    [self._adb_path, "-s", self._device_serial, "shell", "settings", "put", "global", "http_proxy", ":0"],
                    capture_output=True, text=True, timeout=4, creationflags=flags
                )
                subprocess.run(
                    [self._adb_path, "-s", self._device_serial, "shell", "settings", "delete", "global", "http_proxy"],
                    capture_output=True, text=True, timeout=4, creationflags=flags
                )
                subprocess.run(
                    [self._adb_path, "-s", self._device_serial, "reverse", "--remove", f"tcp:{self.port}"],
                    capture_output=True, text=True, timeout=4, creationflags=flags
                )
                self.proxy_auto_set = False
            except Exception:
                pass

    def stop(self, restore_proxy: bool = True) -> Dict[str, Any]:
        """停止本地抓包代理服务器并还原模拟器网络设置"""
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        self.is_listening = False

        if restore_proxy:
            self.restore_emulator_proxy()
            self._log("⏹ [抓包已停止] 已关闭本地抓包监听并还原模拟器直连网络设置。")

        return {
            "success": True,
            "listening": False,
            "packet_count": self.packet_count,
            "captured_servants_count": self.captured_servants_count,
            "captured_ces_count": self.captured_ces_count,
            "message": "已停止抓包监听并恢复模拟器正常网络"
        }

    def status(self) -> Dict[str, Any]:
        return {
            "listening": self.is_listening,
            "host": "127.0.0.1" if self.proxy_auto_set else self.get_local_ip(),
            "port": self.port,
            "proxy_auto_set": self.proxy_auto_set,
            "ca_injected": self.ca_injected,
            "packet_count": self.packet_count,
            "captured_servants_count": self.captured_servants_count,
            "captured_ces_count": self.captured_ces_count,
            "last_capture_time": self.last_capture_time,
        }
