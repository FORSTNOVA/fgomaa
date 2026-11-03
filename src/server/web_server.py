"""
FGO MAA Desktop Backend Server
提供轻量高效的 RESTful API 与静态文件服务，桥接前端客户端与核心业务模块。
"""

from __future__ import annotations
import sys
import json
import time
import io
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import List, Dict, Any, Optional

# 根目录加入 sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.core.path_helper import get_resource_path, get_root_dir
from src.core.device_manager import DeviceManager
from src.core.controller import FGOController
from src.parser.chaldea_parser import ChaldeaParser, ServantProfile
from src.parser.packet_capturer import FGOPacketCapturer
from src.solver.mission_solver import MasterMissionSolver, MissionRequirement
from src.strategy.battle_plans import BattlePlanManager
from src.strategy.auto_team_builder import AutoTeamBuilder, SERVANT_TACTICAL_DB
from src.strategy.laplace import LaplaceService
from src.strategy.screen_detector import ScreenDetector
from src.workflows.daily_runner import DailyRunner
from src.workflows.weekly_runner import WeeklyRunner


class GlobalState:
    """全局运行时状态与日志队列"""
    def __init__(self):
        self.device_mgr = DeviceManager()
        self.chaldea_parser = ChaldeaParser()
        self.solver = MasterMissionSolver()
        self.plan_mgr = BattlePlanManager()
        self.logs: List[Dict[str, str]] = []
        self.log_lock = threading.Lock()
        self.is_running = False
        self.task_lock = threading.Lock()
        self.quest_report = None
        self.cancel_event = threading.Event()
        self.last_heartbeat = time.time()
        self.latest_weekly_solution: Optional[Any] = None
        self._controller: Optional[FGOController] = None
        self.capturer = FGOPacketCapturer(
            port=18889,
            log_callback=lambda msg: self.add_log(msg, "info"),
            on_account_captured=self.handle_captured_account,
            on_https_fallback=self.handle_https_fallback,
        )

        # 优先加载用户导入并保存的账户 JSON，否则加载默认示例数据
        user_file = get_resource_path("data/user_account.json")
        sample_file = get_resource_path("tests/sample_chaldea.json")
        if user_file.exists():
            try:
                self.chaldea_parser.parse_file(user_file)
                if not self.chaldea_parser.servants and sample_file.exists():
                    self.chaldea_parser.parse_file(sample_file)
            except Exception:
                if sample_file.exists():
                    self.chaldea_parser.parse_file(sample_file)
        elif sample_file.exists():
            self.chaldea_parser.parse_file(sample_file)

    def handle_captured_account(self, raw_payload: Any) -> Tuple[int, int]:
        """当抓包代理捕获到 toplogin / userSvt 数据包时自动入库从者与概念礼装并持久化"""
        self.chaldea_parser.load_names()
        servants = self.chaldea_parser.parse_data(raw_payload)
        ces = self.chaldea_parser.craft_essences
        if not servants and not ces:
            return (0, 0)
        self.save_current_servants()
        return (len(servants), len(ces))

    def handle_https_fallback(self, host: str) -> None:
        """当捕获到 :443 HTTPS 加密游戏连接时，自动联动实机画面 OCR 扫描同步从者与概念礼装"""
        time.sleep(1.2)
        try:
            found_svts, found_ces = self.scan_screen_servants_and_ces()
            svt_total = len(self.chaldea_parser.servants)
            ce_total = len(self.chaldea_parser.craft_essences)
            self.capturer.captured_servants_count = svt_total
            self.capturer.captured_ces_count = ce_total
            self.capturer.last_capture_time = time.strftime("%H:%M:%S")
            self.add_log(
                f"√ [抓包&实机联动完成] 已捕获 {host}:443 通信并同步账户数据："
                f"当前已就绪 {svt_total} 骑从者 + {ce_total} 张概念礼装"
                + (f" (新识别: {', '.join(found_svts + found_ces)})" if (found_svts or found_ces) else "")
                + "！已将概念礼装纳入智能配队计算。",
                "success"
            )
        except Exception as e:
            self.add_log(f"[抓包联动提示] 实机辅助扫描跳过: {e}", "warn")

    def scan_screen_servants_and_ces(self) -> Tuple[List[str], List[str]]:
        """对当前实机画面执行 OCR，同时识别并入库从者与概念礼装"""
        ctrl = self.controller
        if ctrl is None:
            ctrl = FGOController(log_callback=self.add_log)
            self._controller = ctrl
        img = ctrl.screencap(force_fresh=True)
        ocr_res = ScreenDetector.run_ocr(img)
        all_text = " ".join(item[1] for item in ocr_res)

        aliases = {
            "杀狐": 604200, "高扬斯卡娅": 604200, "光之高扬斯卡娅": 604200,
            "c呆": 504500, "术呆": 504500, "阿尔托莉雅·caster": 504500, "阿尔托莉雅caster": 504500, "阿尔托莉雅·卡斯特": 504500,
            "奥伯龙": 2800100, "水cba": 901400, "斯卡蒂": 901400,
            "摩根": 704000, "狂娜": 703300, "阿周那": 703300,
            "伊吹童子": 704300, "哈贝特洛特": 404200, "达·芬奇": 403500, "达芬奇": 403500,
            "千子村正": 104900, "村正": 104900, "吉尔伽美什": 200200, "伊什塔尔": 202000,
            "妖精骑士兰斯洛特": 304800, "美露莘": 304800, "太空伊什塔尔": 1100900, "仇凛": 1100900,
            "库库尔坎": 2501100, "爱尔奎特": 2300500, "芦屋道满": 1001000, "暗狐": 2500900,
            "诸葛孔明": 501900, "孔明": 501900, "司马懿": 403200, "莱妮丝": 403200, "梅林": 500800, "阿瓦隆女士": 2800300,
        }
        found_svts: List[str] = []
        for kw, sid in aliases.items():
            if kw in all_text.lower():
                meta = SERVANT_TACTICAL_DB.get(sid)
                name = meta.name if meta else self.chaldea_parser._resolve_name(sid)
                if sid not in self.chaldea_parser.servants:
                    prof = ServantProfile(
                        svt_id=sid, name=name, lv=90, np_lv=2,
                        skill_lv1=10, skill_lv2=10, skill_lv3=10,
                        append_skill_lv1=0, append_skill_lv2=10, append_skill_lv3=0
                    )
                    self.chaldea_parser._upsert_servant(prof)
                if name not in found_svts:
                    found_svts.append(name)

        for sid_str, sname in self.chaldea_parser.id_to_name.items():
            clean_sname = sname.split("(")[0]
            if len(clean_sname) >= 2 and clean_sname in all_text:
                sid = int(sid_str)
                if sid not in self.chaldea_parser.servants:
                    prof = ServantProfile(
                        svt_id=sid, name=sname, lv=90, np_lv=2,
                        skill_lv1=10, skill_lv2=10, skill_lv3=10,
                        append_skill_lv1=0, append_skill_lv2=10, append_skill_lv3=0
                    )
                    self.chaldea_parser._upsert_servant(prof)
                if sname not in found_svts:
                    found_svts.append(sname)

        # 同时通过 OCR 识别画面中的概念礼装名称与词条 (初始NP / NP回收 / 色卡性能提升)
        found_ces = self.chaldea_parser.parse_ce_from_ocr_text(all_text)
        self.save_current_servants()
        return found_svts, found_ces

    def save_current_servants(self) -> None:
        """将当前内存中的从者列表与概念礼装列表一并持久化保存至 data/user_account.json"""
        if not any((self.chaldea_parser.servants, self.chaldea_parser.craft_essences,
                    self.chaldea_parser.storage_servants, self.chaldea_parser.storage_craft_essences)):
            return
        save_path = get_root_dir() / "data" / "user_account.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = save_path.with_suffix(".json.tmp")
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(self.chaldea_parser.export_data(), f, ensure_ascii=False, indent=2)
        temporary.replace(save_path)

    @property
    def controller(self) -> Optional[FGOController]:
        if self._controller is None:
            try:
                self._controller = FGOController(log_callback=self.add_log)
            except Exception:
                pass
        return self._controller

    def add_log(self, text: str, level: str = "info"):
        timestamp = time.strftime("%H:%M:%S")
        with self.log_lock:
            self.logs.append({
                "time": timestamp,
                "text": text,
                "level": level
            })
            if len(self.logs) > 300:
                self.logs.pop(0)

    def get_logs_since(self, index: int) -> List[Dict[str, str]]:
        with self.log_lock:
            return self.logs[index:]


state = GlobalState()


class MAARequestHandler(BaseHTTPRequestHandler):
    """请求处理器"""

    def log_message(self, format: str, *args: Any):
        """覆盖默认实现，防止在 PyInstaller --noconsole 模式下因 sys.stderr 为 None 崩溃"""
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, content: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path

        # 0. 健康检查与心跳路由
        if path == "/api/ping":
            self._send_json({"status": "ok", "time": time.time()})
            return

        if path == "/api/heartbeat":
            state.last_heartbeat = time.time()
            self._send_json({"status": "alive", "time": state.last_heartbeat})
            return

        if path == "/api/stop":
            state.cancel_event.set()
            state.add_log("=== 用户已下发【终止运行】指令，正在停止所有自动化操作 ===", "warn")
            self._send_json({"success": True, "message": "已成功下发终止指令"})
            return

        if path == "/api/exit":
            self._send_json({"success": True, "message": "正在退出 FGO MAA 客户端..."})
            def delayed_shutdown():
                time.sleep(0.8)
                os._exit(0)
            threading.Thread(target=delayed_shutdown, daemon=True).start()
            return

        # 1. 静态前端页面路由
        if path == "/" or path == "/index.html":
            html_path = get_resource_path("web/index.html")
            if html_path.exists():
                self._send_bytes(html_path.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send_json({"error": f"web/index.html not found (looked at {html_path})"}, 404)
            return

        if path == "/api/quests/catalog":
            from src.workflows.quest_runner import quest_catalog
            from src.strategy.quest_catalog import catalog_sync
            self._send_json({"success": True, "quests": quest_catalog(state.chaldea_parser), "sync":catalog_sync().status()})
            return

        if path == "/api/quests/first-clear/catalog":
            from src.workflows.first_clear_runner import FirstClearOptions,FirstClearLedger,first_clear_catalog
            try:
                params=parse_qs(urlparse(self.path).query)
                serial=params.get('device_serial',[''])[0]
                wars=[int(v) for v in params.get('war_id',[]) if v]
                options=FirstClearOptions(war_ids=wars)
                ledger=FirstClearLedger(state.chaldea_parser,serial)
                rows=first_clear_catalog(state.chaldea_parser,options,ledger)
                self._send_json({'success':True,'quests':rows,'candidate_count':sum(r['supported'] for r in rows)})
            except (ValueError,TypeError) as exc:
                self._send_json({'success':False,'message':str(exc)},400)
            return

        if path == "/api/quests/sync/status":
            from src.strategy.quest_catalog import catalog_sync
            self._send_json({"success":True, **catalog_sync().status()})
            return

        if path == "/api/quests/status":
            self._send_json({"success": True, "report": state.quest_report})
            return

        # 2. 设备状态查询
        if path == "/api/devices":
            devs = state.device_mgr.get_connected_devices(auto_connect=True)
            res = [{
                "serial": d.serial,
                "model": d.model,
                "resolution": d.resolution,
                "state": d.state,
                "is_emulator": d.is_emulator
            } for d in devs]
            self._send_json({"success": True, "devices": res})
            return

        # 3. 实时截屏视讯流
        if path == "/api/screen":
            try:
                ctrl = state.controller
                if ctrl is None:
                    ctrl = FGOController(log_callback=state.add_log)
                    state._controller = ctrl
                img = ctrl.screencap()
                buf = io.BytesIO()
                # 压缩为 JPEG 提高实时传输效率
                img.convert("RGB").save(buf, format="JPEG", quality=75)
                self._send_bytes(buf.getvalue(), "image/jpeg")
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
            return

        # 4. 迦勒底从者与概念礼装库摘要
        if path == "/api/chaldea":
            total = len(state.chaldea_parser.servants)
            ce_total = len(state.chaldea_parser.craft_essences)
            max_skills = sum(1 for s in state.chaldea_parser.servants.values() if s.is_all_skills_max())
            max_append2 = sum(1 for s in state.chaldea_parser.servants.values() if s.has_max_append_np())
            supports = state.chaldea_parser.get_core_supports()

            support_list = []
            for nick, prof in supports.items():
                if prof:
                    support_list.append({
                        "nick": nick,
                        "name": prof.name,
                        "lv": prof.lv,
                        "np_lv": prof.np_lv,
                        "skills": prof.skills,
                        "append_np": prof.get_append_np_charge(),
                        "is_max": prof.is_all_skills_max(),
                        "has_max_append": prof.has_max_append_np()
                    })
                else:
                    support_list.append({"nick": nick, "name": "未持有", "owned": False})

            sorted_ces = sorted(
                state.chaldea_parser.craft_essences.values(),
                key=lambda c: (c.initial_np, c.np_gain_rate, c.card_buff_rate, c.np_damage_up, 1 if c.drop_bonus else 0, c.lv),
                reverse=True
            )
            ce_list = [
                {
                    "ce_id": ce.ce_id,
                    "name": ce.name,
                    "lv": ce.lv,
                    "is_mlb": ce.is_mlb,
                    "initial_np": ce.initial_np,
                    "np_gain_rate": ce.np_gain_rate,
                    "np_damage_up": ce.np_damage_up,
                    "card_buff_color": ce.card_buff_color,
                    "card_buff_rate": ce.card_buff_rate,
                    "effect_summary": ce.effect_summary(),
                }
                for ce in sorted_ces
            ]

            self._send_json({
                "success": True,
                "total": total,
                "ce_total": ce_total,
                "max_skills": max_skills,
                "max_append2": max_append2,
                "supports": support_list,
                "craft_essences": ce_list,
            })
            return

        # 5. 3T 战斗预设列表
        if path == "/api/teams":
            teams_res = []
            for tid, t in state.plan_mgr.teams.items():
                teams_res.append({
                    "id": t.id,
                    "name": t.name,
                    "match_servants": t.match_servants,
                    "required_min_np_charge": t.required_min_np_charge,
                    "turns_count": len(t.turns)
                })
            self._send_json({"success": True, "teams": teams_res})
            return

        # 5.5 智能自配队决策接口 (根据账户 JSON 与目标关卡敌人信息动态生成配队)
        if path == "/api/auto_team":
            query = parse_qs(url.query)
            quest = query.get("quest", ["daily_qp_extreme"])[0]
            builder = AutoTeamBuilder(state.chaldea_parser)
            rec = builder.recommend_for_quest(quest)
            LaplaceService().validate(rec, state.chaldea_parser)
            self._send_json({
                "success": True,
                "recommendation": rec.model_dump()
            })
            return

        if path == "/api/laplace/status":
            self._send_json({"success": True, **LaplaceService().status()})
            return

        # 5.6 抓包监听状态查询接口
        if path == "/api/capture/status":
            self._send_json({
                "success": True,
                **state.capturer.status()
            })
            return

        # 6. 增量日志流
        if path == "/api/logs":
            query = parse_qs(url.query)
            since = int(query.get("since", [0])[0])
            logs = state.get_logs_since(since)
            self._send_json({"success": True, "logs": logs, "is_running": state.is_running})
            return

        self._send_json({"error": "Not Found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            body = {}

        if path == "/api/quests/sync":
            if not isinstance(body, dict) or type(body.get("force", False)) is not bool:
                self._send_json({"success":False,"message":"刷新参数无效"},400)
                return
            from src.strategy.quest_catalog import catalog_sync
            with state.task_lock:
                if state.is_running:
                    self._send_json({"success":False,"message":"请等待自动作战结束后再刷新关卡库"},409)
                    return
                started = catalog_sync().start(force=body.get("force",False),log=lambda text:state.add_log(text,"info"))
            self._send_json({"success":True,"started":started, **catalog_sync().status()})
            return

        if path in ("/api/quests/preview", "/api/quests/run", "/api/quests/first-clear/run"):
            from src.workflows.quest_runner import QuestRunner, resolve_queue
            from src.workflows.first_clear_runner import FirstClearRunner,FirstClearOptions
            first_clear=path=="/api/quests/first-clear/run"
            try:
                if not isinstance(body, dict) or not isinstance(body.get("device_serial", ""), str):
                    raise ValueError("请求格式无效")
                if first_clear:
                    if set(body)-{'device_serial','options'} or not body.get('device_serial'):
                        raise ValueError('请选择目标设备并提交有效首通设置')
                    options=FirstClearOptions.model_validate(body.get('options',{}))
                    if not state.chaldea_parser.servants or not state.chaldea_parser.quest_records_complete:
                        raise ValueError('请先导入完整的账号与任务记录')
                else:
                    resolve_queue(body.get("queue"), state.chaldea_parser)
            except (ValueError, TypeError) as exc:
                self._send_json({"success": False, "message": str(exc)}, 400)
                return
            with state.task_lock:
                from src.strategy.quest_catalog import catalog_sync
                if state.is_running or catalog_sync().running:
                    self._send_json({"success": False, "message": "已有任务或关卡库刷新正在运行"}, 409)
                    return
                state.is_running = True
                state.cancel_event.clear()
            preview = path.endswith("/preview")
            state.quest_report = {"status": "starting", "preview_only": preview, "mode":"first_clear" if first_clear else "repeat", "stations": []}
            def worker():
                try:
                    # Freeze imported account data for this run; later imports apply to the next run.
                    import copy
                    runner_class=FirstClearRunner if first_clear else QuestRunner
                    runner = runner_class(copy.deepcopy(state.chaldea_parser), state.device_mgr,
                                         log_callback=lambda msg:state.add_log(msg, "info"))
                    def update(report):
                        state.quest_report = report
                    arguments={'device_serial':body.get('device_serial',''),'cancel_event':state.cancel_event,'on_progress':update}
                    if not first_clear:arguments['preview_only']=preview
                    report = runner.run(options if first_clear else body['queue'],**arguments)
                    state.add_log("自动作战" + ("模拟通过" if preview else "队列完成") if report.success
                                  else "自动作战已停止：" + report.error, "success" if report.success else "error")
                except Exception as exc:
                    state.quest_report = {"status": "failed", "error": str(exc), "stations": []}
                    state.add_log("自动作战异常：" + str(exc), "error")
                finally:
                    state.is_running = False
            threading.Thread(target=worker, daemon=True).start()
            self._send_json({"success": True, "message": "已开始自动首通" if first_clear else "已开始模拟" if preview else "已开始自动作战"})
            return

        # 0. 导入用户截取的账户 JSON / HAR / Base64 抓包文件并实时重算配队
        if path == "/api/chaldea/import":
            try:
                account_data = body.get("account_data", body)
                state.chaldea_parser.load_names()
                servants = state.chaldea_parser.parse_data(account_data)
                ces = state.chaldea_parser.craft_essences
                if not servants and not ces:
                    self._send_json({"success": False, "message": "未能在数据包中解析到有效从者或概念礼装数据，请检查文件格式 (支持 toplogin 原始响应 / .har 抓包归档 / userSvt / Chaldea servants & craft_essences 格式)"}, 400)
                    return

                state.save_current_servants()
                state.add_log(
                    f"√ 成功导入抓包/账户数据！共识别 {len(servants)} 名从者 + {len(ces)} 张概念礼装，已将概念礼装纳入智能自配队计算。",
                    "success"
                )
                builder = AutoTeamBuilder(state.chaldea_parser)
                rec = builder.recommend_for_quest("daily_qp_extreme")
                self._send_json({
                    "success": True,
                    "total": len(servants),
                    "ce_total": len(ces),
                    "message": f"成功导入 {len(servants)} 名从者 + {len(ces)} 张概念礼装！",
                    "recommendation": rec.model_dump()
                })
            except Exception as e:
                self._send_json({"success": False, "message": f"解析账户数据失败: {str(e)}"}, 400)
            return

        # 0.1 启动内置抓包监听与模拟器自动代理
        if path == "/api/capture/start":
            auto_proxy = body.get("auto_proxy", True)
            adb_path = str(state.device_mgr.adb_path) if state.device_mgr.adb_path else None
            devs = state.device_mgr.get_connected_devices(auto_connect=True)
            serial = devs[0].serial if devs else None
            res = state.capturer.start(adb_path=adb_path, serial=serial, auto_set_proxy=auto_proxy)
            self._send_json(res)
            return

        # 0.2 停止抓包监听并还原模拟器直连网络
        if path == "/api/capture/stop":
            res = state.capturer.stop(restore_proxy=True)
            self._send_json(res)
            return

        # 0.3 实机画面一键 OCR 扫描从者与概念礼装入库（无需重新登录抓包，在编队/灵基一览/概念礼装界面直接采集）
        if path == "/api/chaldea/scan_screen":
            try:
                found_names, found_ces = state.scan_screen_servants_and_ces()
                svt_total = len(state.chaldea_parser.servants)
                ce_total = len(state.chaldea_parser.craft_essences)
                combined = found_names + [f"礼装:{c}" for c in found_ces]
                msg = (
                    f"实机画面扫描完成！新识别并同步: {', '.join(combined)} (当前库内共 {svt_total} 骑从者 + {ce_total} 张概念礼装)"
                    if combined else f"当前画面已完成核验 (库内已就绪 {svt_total} 骑从者 + {ce_total} 张概念礼装)；若需扫描更多，请切换到【队伍编成】/【灵基一览】/【概念礼装】界面再点扫描。"
                )
                state.add_log(f"🔍 [实机从者&礼装采集] {msg}", "info")
                self._send_json({
                    "success": True,
                    "found": found_names,
                    "found_ces": found_ces,
                    "total": svt_total,
                    "ce_total": ce_total,
                    "message": msg
                })
            except Exception as e:
                self._send_json({"success": False, "message": f"实机扫描失败: {e}"}, 500)
            return

        # 1. 求解周常
        if path == "/api/solve_weekly":
            auto_scan = body.get("auto_scan", False)
            if auto_scan and state.is_running:
                self._send_json({"success": False, "message": "任务正在执行，不能同时操作实机扫描周常。"}, 400)
                return
            if auto_scan:
                ctrl = FGOController(log_callback=lambda msg: state.add_log(msg, "info"))
                from src.strategy.smart_engine import SmartEngine
                engine = SmartEngine(ctrl, log_callback=lambda msg: state.add_log(msg, "info"))
                res = engine.auto_scan_and_solve_weekly()
            else:
                texts = body.get("texts", [
                    "击败15个具有【地】属性的敌人",
                    "击败8个具有【魔性】特性的敌人",
                    "击败3个【从者】",
                    "击败6个具有【死灵】特性的敌人",
                    "获得15个战利品",
                    "完成任意关卡3次"
                ])
                try:
                    reqs = state.solver.parse_missions(texts)
                except (ValueError, TypeError) as exc:
                    state.latest_weekly_solution = None
                    self._send_json({"success": False, "message": str(exc)}, 400)
                    return
                res = state.solver.solve(reqs)

            if res.success:
                state.latest_weekly_solution = res
                plans_res = [{
                    "quest_id": p.quest_id,
                    "war": p.war,
                    "spot": p.spot,
                    "name": p.name,
                    "ap_per_run": p.ap_per_run,
                    "runs": p.runs,
                    "total_ap": p.total_ap
                } for p in res.plans]
                self._send_json({
                    "success": True,
                    "total_ap": res.total_ap,
                    "total_runs": res.total_runs,
                    "plans": plans_res,
                    "event_missions": getattr(res, "event_missions", []),
                    "completed_missions": getattr(res, "completed_missions", []),
                    "message": getattr(res, "message", "Success")
                })
            else:
                state.latest_weekly_solution = None
                self._send_json({"success": False, "message": res.message}, 400)
            return

        # 1. 停止当前所有自动化运行任务
        if path == "/api/stop":
            state.cancel_event.set()
            state.add_log("=== 用户已下发【终止运行】指令，正在停止所有自动化操作 ===", "warn")
            self._send_json({"success": True, "message": "已成功下发终止指令"})
            return

        # 2. 启动日常全流程 (异步线程)
        if path == "/api/run_daily":
            with state.task_lock:
                from src.strategy.quest_catalog import catalog_sync
                busy = state.is_running or catalog_sync().running
                if not busy:
                    state.is_running = True
                    state.cancel_event.clear()
            if busy:
                self._send_json({"success": False, "message": "已有任务正在运行中"}, 409)
                return

            def worker():
                state.add_log("=== 开始执行【一键日常】自动化任务 ===", "info")
                try:
                    # 每次任务启动前动态热重载最新策略模块，无需重启客户端即可 100% 生效最新代码
                    import importlib
                    import src.strategy.anchor_verifier
                    import src.strategy.ordeal_navigation
                    import src.strategy.chapter_navigation
                    import src.strategy.event_navigation
                    import src.strategy.screen_detector
                    import src.strategy.auto_team_builder
                    import src.strategy.servant_picker
                    import src.strategy.auto_party_assembler
                    import src.strategy.craft_essence_picker
                    import src.strategy.team_detector
                    import src.core.controller
                    import src.strategy.quest_navigation
                    import src.strategy.quest_battle
                    import src.strategy.smart_engine
                    import src.workflows.daily_runner
                    for mod in [
                        src.strategy.anchor_verifier,
                        src.strategy.ordeal_navigation,
                        src.strategy.chapter_navigation,
                        src.strategy.event_navigation,
                        src.strategy.screen_detector,
                        src.strategy.auto_team_builder,
                        src.strategy.servant_picker,
                        src.strategy.auto_party_assembler,
                        src.strategy.craft_essence_picker,
                        src.strategy.team_detector,
                        src.core.controller,
                        src.strategy.quest_navigation,
                        src.strategy.quest_battle,
                        src.strategy.smart_engine,
                        src.workflows.daily_runner,
                    ]:
                        try:
                            importlib.reload(mod)
                        except Exception:
                            pass
                    from src.workflows.daily_runner import DailyRunner as HotDailyRunner

                    runner = HotDailyRunner(
                        device_manager=state.device_mgr,
                        chaldea_parser=state.chaldea_parser,
                        dry_run=False
                    )
                    # 重定向日常日志
                    orig_log = runner.log
                    def custom_log(rep, msg):
                        orig_log(rep, msg)
                        state.add_log(msg, "info")
                    runner.log = custom_log

                    quest = body.get("quest", "daily_qp_extreme")
                    runs = int(body.get("runs", 3))
                    options = {
                        "enable_story_summon": body.get("enable_story_summon", True),
                        "enable_fp": body.get("enable_fp", True),
                        "enable_quest": body.get("enable_quest", True),
                        "enable_reward": body.get("enable_reward", True),
                    }
                    report = runner.run(
                        target_quest=quest,
                        repeat_runs=runs,
                        options=options,
                        cancel_event=state.cancel_event
                    )
                    if report.success:
                        state.add_log("√ 一键日常指定任务全部执行完成！", "success")
                    else:
                        state.add_log("× 一键日常已停止或遇到异常", "error")
                except Exception as e:
                    state.add_log(f"执行异常: {str(e)}", "error")
                finally:
                    state.is_running = False

            threading.Thread(target=worker, daemon=True).start()
            self._send_json({"success": True, "message": "已启动日常自动化任务"})
            return

        # 3. 启动周常全流程 (异步线程)
        if path == "/api/run_weekly":
            with state.task_lock:
                from src.strategy.quest_catalog import catalog_sync
                busy = state.is_running or catalog_sync().running
                if not busy:
                    state.is_running = True
                    state.cancel_event.clear()
            if busy:
                self._send_json({"success": False, "message": "已有任务正在运行中"}, 409)
                return

            def worker():
                state.add_log("=== 开始执行【智能周常】自动化闭环 ===", "info")
                try:
                    # 动态热重载最新周常与策略模块
                    import importlib
                    import src.strategy.anchor_verifier
                    import src.strategy.ordeal_navigation
                    import src.strategy.chapter_navigation
                    import src.strategy.event_navigation
                    import src.strategy.screen_detector
                    import src.strategy.auto_team_builder
                    import src.strategy.servant_picker
                    import src.strategy.auto_party_assembler
                    import src.strategy.craft_essence_picker
                    import src.strategy.team_detector
                    import src.core.controller
                    import src.solver.mission_solver
                    import src.strategy.weekly_missions
                    import src.strategy.quest_navigation
                    import src.strategy.quest_battle
                    import src.strategy.smart_engine
                    import src.workflows.weekly_runner
                    for mod in [
                        src.strategy.anchor_verifier,
                        src.strategy.ordeal_navigation,
                        src.strategy.chapter_navigation,
                        src.strategy.event_navigation,
                        src.strategy.screen_detector,
                        src.strategy.auto_team_builder,
                        src.strategy.servant_picker,
                        src.strategy.auto_party_assembler,
                        src.strategy.craft_essence_picker,
                        src.strategy.team_detector,
                        src.core.controller,
                        src.solver.mission_solver,
                        src.strategy.weekly_missions,
                        src.strategy.quest_navigation,
                        src.strategy.quest_battle,
                        src.strategy.smart_engine,
                        src.workflows.weekly_runner,
                    ]:
                        try:
                            importlib.reload(mod)
                        except Exception:
                            pass
                    from src.workflows.weekly_runner import WeeklyRunner as HotWeeklyRunner

                    runner = HotWeeklyRunner(
                        device_manager=state.device_mgr,
                        chaldea_parser=state.chaldea_parser,
                        dry_run=False
                    )
                    orig_log = runner.log
                    def custom_log(rep, msg):
                        orig_log(rep, msg)
                        state.add_log(msg, "info")
                    runner.log = custom_log

                    # 优先采用传入的 plans 或上一步求得的全局最优规划
                    plans = body.get("plans")

                    texts = body.get("texts")
                    report = runner.run(
                        custom_mission_texts=texts,
                        plans=plans,
                        cancel_event=state.cancel_event
                    )
                    if report.success:
                        state.add_log(f"√ 周常任务及领奖已复核！本次确认 {report.total_battles} 场，标称 AP 估算 {report.estimated_ap_spent}（实际消耗未测量）。", "success")
                    else:
                        state.add_log("× 周常执行异常或已被终止", "warn")
                except Exception as e:
                    state.add_log(f"执行异常: {str(e)}", "error")
                finally:
                    state.is_running = False

            threading.Thread(target=worker, daemon=True).start()
            self._send_json({"success": True, "message": "已启动周常自动化任务"})
            return

        self._send_json({"error": "Not Found"}, 404)


def start_server(port: int = 18888) -> ThreadingHTTPServer:
    """启动本地后端服务器"""
    server = ThreadingHTTPServer(("127.0.0.1", port), MAARequestHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    def refresh_catalog():
        from src.strategy.quest_catalog import catalog_sync
        while t.is_alive():
            with state.task_lock:
                if not state.is_running:
                    catalog_sync().start(log=lambda text:state.add_log(text,"info"))
            # The service applies its own 24h TTL and failed-request retry delay.
            for _ in range(60):
                if not t.is_alive():
                    return
                time.sleep(5)
    threading.Thread(target=refresh_catalog,daemon=True).start()
    return server


if __name__ == "__main__":
    port = 18888
    srv = start_server(port)
    print(f"MAA 后端服务已启动: http://127.0.0.1:{port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("服务退出")
