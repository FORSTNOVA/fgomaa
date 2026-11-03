"""周常列表完整扫描及领奖核验；无法确认的状态一律保留为失败。"""

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from src.solver.mission_solver import MasterMissionSolver, SolverResult
from src.strategy.screen_detector import GameScreen, ScreenDetector


@dataclass
class WeeklySnapshot:
    success: bool = False
    message: str = "未完成周常扫描"
    requirements: list = field(default_factory=list)
    objectives_complete: bool = False
    all_claimed: bool = False


class WeeklyMissions:
    def __init__(self, engine):
        self.engine = engine
        self.ctrl = engine.ctrl
        self.log = engine.log

    def read(self):
        img = self.ctrl.wait_for_still(timeout=2.0, min_still_duration=0.2)
        if img.size != (1920, 1080):
            img = img.resize((1920, 1080))
        ocr = ScreenDetector.run_ocr(img)
        if self.summary(ocr) is None and self.weekly_active(img, ocr):
            hud = img.crop((1270, 280, 1410, 360))
            counter = ScreenDetector.run_ocr(hud.resize((420, 240)))
            ocr.extend(([[x / 3 + 1270, y / 3 + 280] for x, y in box], self.restore_counter_separator(hud, text), score)
                       for box, text, score in counter)
        return img, ocr

    @staticmethod
    def restore_counter_separator(hud, text):
        # 7/7 的斜杠会被 OCR 读成 1；必须有独立的细斜线像素证据，不能直接猜数字。
        if not re.fullmatch(r"\d{3,5}", text):
            return text
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (np.array(hud.convert("L")) > 205).astype(np.uint8))
        glyphs = sorted((i for i in range(1, count) if stats[i, 3] >= 12 and stats[i, 4] >= 20),
                        key=lambda i: stats[i, 0])
        if len(glyphs) != len(text):
            return text
        for index in range(1, len(glyphs) - 1):
            identity = glyphs[index]
            if text[index] != "1" or stats[identity, 3] < 20 or stats[identity, 2] > stats[identity, 3] * .45:
                continue
            ys, xs = np.where(labels == identity)
            slope, intercept = np.polyfit(ys, xs, 1)
            if -.4 < slope < -.1 and np.mean((xs - slope * ys - intercept) ** 2) < 1.4:
                return text[:index] + "/" + text[index + 1:]
        return text

    @staticmethod
    def weekly_active(img, ocr):
        pos = ScreenDetector.find_text_coordinate(ocr, "周常", region=(1000, 130, 1320, 270))
        if pos is None:
            return False
        x, y = pos
        arr = np.array(img.convert("RGB"))[y - 22:y + 22, x - 75:x + 75]
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        blue = (hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 135) & (hsv[:, :, 1] > 75) & (hsv[:, :, 2] > 160)
        return blue.mean() > 0.35

    @staticmethod
    def extract_cards(ocr):
        items = []
        for box, text, _ in ocr:
            x = sum(p[0] for p in box) / 4
            y = sum(p[1] for p in box) / 4
            is_state = text.replace(" ", "") in ("已领取", "已获得", "已获取", "领取完毕", "已达成", "已完成")
            if 900 <= x <= (1880 if is_state else 1640) and 370 <= y <= 1060:
                items.append((y, x, re.sub(r"\s+", "", text)))
        items.sort()
        cards = []
        card = None
        for y, x, text in items:
            if re.match(r"^(?:完成本周所有|击败|败\d|消灭|战胜|获得|累计|在[『「【])", text):
                card = {"description": text, "progress": None, "claimed": False, "complete": False, "y": y}
                cards.append(card)
                continue
            if card is None or y - card["y"] > 215:
                continue
            progress = re.search(r"(\d+)\s*/\s*(\d+)", text)
            if progress:
                card["progress"] = (int(progress[1]), int(progress[2]))
                continue
            if text in ("已领取", "已获得", "已获取", "领取完毕"):
                card["claimed"] = True
                card["complete"] = True
            elif text in ("已达成", "已完成"):
                card["complete"] = True
            elif y - card["y"] <= 70 and not any(t in text for t in ("进行度", "报酬", "剩余", "期间", "Mission", "ission")):
                if not re.fullmatch(r"[xX×]?\d+", text):
                    card["description"] += text
        return cards

    @staticmethod
    def summary(ocr):
        for box, text, _ in ocr:
            x = sum(p[0] for p in box) / 4
            y = sum(p[1] for p in box) / 4
            match = re.search(r"(\d+)\s*/\s*(\d+)", text)
            if match and 850 <= x <= 1450 and 280 <= y <= 360:
                done, total = int(match[1]), int(match[2])
                if (total == 0 or 2 <= total <= 30) and 0 <= done <= total:
                    return done, total
        return None

    @staticmethod
    def signature(ocr):
        return tuple((WeeklyMissions.card_key(c["description"]), c["progress"], c["claimed"])
                     for c in WeeklyMissions.extract_cards(ocr))

    @staticmethod
    def card_key(description):
        if description.startswith("完成本周所有"):
            return "完成本周所有"
        # 左侧语音字幕偶尔遮住“击”字；只修正已知前缀和标点差异，
        # 保留数量及全部条件，避免把两条不同要求的任务合并。
        description = re.sub(r"^败(?=\d)", "击败", description)
        # 游戏固定的召唤敌人排除说明在字幕遮挡时常丢字，不属于目标条件。
        description = re.sub(r"[（(](?:部分首领|战斗中被召唤).*$", "", description)
        key = re.sub(r"[^\w]", "", description)
        return re.sub(r"持有1([天地人星兽])之力", r"持有\1之力", key)

    def enter(self):
        screen, details = self.engine.get_current_screen()
        if screen != GameScreen.MASTER_MISSIONS:
            if not self.engine.navigate_to_main_menu() or self.engine.is_cancelled():
                return False
            self.ctrl.click(810, 890, delay_after=1.5)
            for _ in range(5):
                if self.engine.is_cancelled():
                    return False
                screen, details = self.engine.get_current_screen()
                if screen == GameScreen.MASTER_MISSIONS:
                    break
            else:
                return False
        pos = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "周常", region=(1000, 130, 1320, 270))
        # 上次可能停在尚未提交的领取确认；先取消，重新扫描后再领取。
        pending = details.get("ocr_results", [])
        if ScreenDetector.find_text_coordinate(pending, "领取确认", region=(500, 0, 1400, 160)):
            cancel = ScreenDetector.find_text_coordinate(pending, "取消", region=(400, 850, 900, 1030))
            if cancel is None or self.engine.is_cancelled():
                return False
            self.ctrl.click(*cancel, delay_after=0.6)
            screen, details = self.engine.get_current_screen()
            if screen != GameScreen.MASTER_MISSIONS:
                return False
            pos = ScreenDetector.find_text_coordinate(details.get("ocr_results", []), "周常", region=(1000, 130, 1320, 270))
        if pos is None or self.engine.is_cancelled():
            return False
        self.ctrl.click(*pos, delay_after=0.8)
        img, ocr = self.read()
        return self.weekly_active(img, ocr) and ScreenDetector.find_text_coordinate(ocr, "全部", region=(1400, 280, 1650, 360)) is not None

    def scan(self, leave=True):
        if self.engine.is_cancelled() or not self.enter():
            return WeeklySnapshot(message="无法确认周常标签及全部任务视图，停止扫描。")
        # 先回到列表顶部，避免从上次停留的中间位置开始漏扫。
        previous = None
        same = 0
        for _ in range(20):
            if self.engine.is_cancelled():
                return WeeklySnapshot(message="周常扫描已取消")
            img, ocr = self.read()
            if not self.weekly_active(img, ocr):
                return WeeklySnapshot(message="扫描期间离开周常界面")
            if self.summary(ocr) == (0, 0):
                empty_text = any("没有正在进行中的御主任务" in text.replace(" ", "") for _, text, _ in ocr)
                reason = "，且提示没有正在进行中的御主任务" if empty_text else ""
                self.log(f"[周常停止] 当前周常任务总数为 0（0/0）{reason}；不能确认任务已完成。")
                if leave and not self.engine.navigate_to_main_menu():
                    return WeeklySnapshot(message="当前周常显示 0/0，且未能确认返回大厅，停止执行。")
                return WeeklySnapshot(message=f"当前周常任务总数为 0（0/0）{reason}；未执行刷本和领奖，请待游戏提供周常任务后重试。")
            signature = self.signature(ocr)
            same = same + 1 if signature and signature == previous else 0
            if same >= 2:
                break
            previous = signature
            self.ctrl.swipe(1200, 450, 1200, 900, duration_ms=350)
        else:
            return WeeklySnapshot(message="未能确认周常列表顶部")

        expected = None
        seen = {}
        previous = None
        same = 0
        reward_zero = False
        summary_done = None
        for _ in range(20):
            if self.engine.is_cancelled():
                return WeeklySnapshot(message="周常扫描已取消")
            img, ocr = self.read()
            if not self.weekly_active(img, ocr):
                return WeeklySnapshot(message="扫描期间周常标签失效")
            counts = self.summary(ocr)
            if counts is None or (expected is not None and expected != counts[1]):
                return WeeklySnapshot(message="无法确认任务总数，或扫描期间任务列表发生变化")
            summary_done, expected = counts
            for box, text, _ in ocr:
                if max(p[0] for p in box) < 850 and min(p[1] for p in box) > 850:
                    reward_zero |= bool(re.fullmatch(r"剩余[:：]?0", text.replace(" ", "")))
            cards = self.extract_cards(ocr)
            for card in cards:
                # 边缘卡片的标题可能被裁切；等重叠扫描读到完整进度再登记。
                if card["progress"] is None and not card["complete"]:
                    continue
                if card["complete"] and card["progress"] and card["progress"][0] != card["progress"][1]:
                    # 字幕可能把 20/20 的首位遮成 0/20；等另一位置给出一致证据。
                    continue
                key = self.card_key(card["description"])
                old = seen.get(key)
                if old and old["progress"] and card["progress"] and old["progress"] != card["progress"]:
                    return WeeklySnapshot(message="同一任务的进度在扫描中冲突，需重新扫描")
                if old is None or (old["progress"] is None and card["progress"] is not None):
                    seen[key] = card
                elif old:
                    old["complete"] |= card["complete"]
                    old["claimed"] |= card["claimed"]
            if len(seen) == expected and all(c["progress"] is not None or c["complete"] for c in seen.values()):
                break
            signature = self.signature(ocr)
            same = same + 1 if signature and signature == previous else 0
            if same >= 2:
                break
            previous = signature
            # 保留重叠区域，让底部缺失进度的任务在下一帧完整出现。
            self.ctrl.swipe(1200, 800, 1200, 620, duration_ms=500)
        if expected is None or len(seen) != expected:
            return WeeklySnapshot(message=f"周常识别不完整：检测到 {len(seen)}/{expected or '?'} 条，停止执行。")

        solver = MasterMissionSolver()
        requirements = []
        meta = []
        for card in seen.values():
            description = card["description"]
            is_meta = "完成本周所有" in description
            quota = expected - 1 if is_meta else None
            if not is_meta:
                number = re.search(r"(\d+)(?:个|次|份)", description)
                quota = int(number[1]) if number else None
            progress = card["progress"]
            if progress is None and card["complete"] and quota:
                progress = (quota, quota)
            if progress is None or not 0 <= progress[0] <= progress[1] or progress[1] <= 0 or (quota and progress[1] != quota):
                return WeeklySnapshot(message=f"任务进度不明确或冲突：{description}")
            if card["complete"] and progress[0] != progress[1]:
                return WeeklySnapshot(message=f"完成标记与进度冲突：{description}")
            if is_meta:
                meta.append(progress)
                continue
            req = solver.parse_chinese_mission(description, progress_cur=progress[0], progress_total=progress[1])
            if req is None:
                return WeeklySnapshot(message=f"无法解析任务，不能忽略：{description}")
            requirements.append(req)
        if len(meta) != 1 or len(requirements) != expected - 1:
            return WeeklySnapshot(message="汇总任务和具体任务数量不一致")
        complete = all(r.count == 0 for r in requirements)
        claimed = complete and meta[0][0] == meta[0][1] and (
            all(c["claimed"] for c in seen.values()) or (summary_done == expected and reward_zero))
        snapshot = WeeklySnapshot(True, "周常任务及进度已完整核验", requirements, complete, claimed)
        if leave and not self.engine.navigate_to_main_menu():
            return WeeklySnapshot(message="任务已扫描，但未能确认返回大厅，停止执行。")
        return snapshot

    def solve(self):
        snapshot = self.scan()
        if not snapshot.success:
            return SolverResult(success=False, message=snapshot.message)
        result = MasterMissionSolver().solve(snapshot.requirements)
        result.requirements = snapshot.requirements
        result.missions_verified = True
        result.all_completed = snapshot.objectives_complete
        result.rewards_claimed = snapshot.all_claimed
        return result

    @staticmethod
    def claim_confirmation_button(img, ocr):
        header = ScreenDetector.find_text_coordinate(ocr, "领取确认", region=(500, 0, 1400, 160))
        prompt = ScreenDetector.find_text_coordinate(ocr, "是否领取全部报酬", region=(500, 750, 1400, 880))
        if header is None or prompt is None:
            return None
        button = ScreenDetector.find_text_coordinate(ocr, "领取", region=(1000, 880, 1500, 1000))
        if button is not None:
            return button
        # 领取二字在全屏 OCR 中会漏读，只放大已确认弹窗的按钮区域。
        words = ScreenDetector.run_ocr(img.crop((1000, 880, 1500, 1000)).resize((1000, 240)))
        button = ScreenDetector.find_text_coordinate(words, "领取")
        return (int(button[0] / 2 + 1000), int(button[1] / 2 + 880)) if button else None

    def claim(self):
        for _ in range(3):  # 常规任务领奖、汇总任务解锁后领奖、最终复核。
            snapshot = self.scan(leave=False)
            if not snapshot.success or not snapshot.objectives_complete:
                self.log("[周常领奖失败] " + (snapshot.message if not snapshot.success else "仍有未完成任务"))
                return False
            if snapshot.all_claimed:
                return self.engine.navigate_to_main_menu()
            img, ocr = self.read()
            if not self.weekly_active(img, ocr):
                return False
            button = ScreenDetector.find_text_coordinate(ocr, "统一领取", region=(1650, 270, 1910, 370))
            if button is None or self.engine.is_cancelled():
                self.log("[周常领奖失败] 未确认领取入口或已取消，不能判定奖励已领取。")
                return False
            self.ctrl.click(*button, delay_after=0.8)
            for _ in range(5):
                if self.engine.is_cancelled():
                    return False
                img, ocr = self.read()
                confirm = self.claim_confirmation_button(img, ocr)
                if confirm is not None:
                    self.log("[周常领奖] 已确认领取弹窗，点击【领取】并等待到账核验。")
                    self.ctrl.click(*confirm, delay_after=0.8)
                    continue
                if self.weekly_active(img, ocr):
                    break
                close = (ScreenDetector.find_text_coordinate(ocr, "关闭", region=(500, 600, 1400, 1000))
                         or ScreenDetector.find_text_coordinate(ocr, "决定", region=(500, 600, 1400, 1000)))
                if close:
                    self.ctrl.click(*close, delay_after=0.6)
        self.log("[周常领奖失败] 未获得全部奖励已领取的证据，停止流程。")
        return False
