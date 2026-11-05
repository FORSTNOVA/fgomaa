import copy
import json
import os
from pathlib import Path
import ssl
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.request
from urllib.error import URLError

from src.parser.chaldea_parser import ChaldeaParser, CraftEssenceProfile, ServantProfile
from src.strategy.auto_team_builder import AutoTeamBuilder
from src.strategy.laplace import LaplaceError, LaplaceService, PublicData, recommendation_signature
from src.strategy.smart_engine import SmartEngine
from src.strategy.auto_party_assembler import AutoPartyAssembler
from src.strategy.battle_plans import TurnDirective, SkillAction, MasterSkillAction

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    parser = ChaldeaParser()
    parser.servants = {
        404200: ServantProfile(svt_id=404200, name="哈贝特洛特", lv=80, np_lv=5, skill_lv2=10),
        504500: ServantProfile(svt_id=504500, name="阿尔托莉雅·卡斯特", lv=100,
                              skill_lv1=10, skill_lv2=10, skill_lv3=10),
        2800100: ServantProfile(svt_id=2800100, name="奥伯龙", lv=90,
                               skill_lv1=10, skill_lv2=10, skill_lv3=10),
    }
    parser.craft_essences = {9401380: CraftEssenceProfile(ce_id=9401380, name="毒蛇一艺", lv=13,
        is_mlb=True, card_buff_color="Arts", card_buff_rate=40)}
    return parser, AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme")


class TestPublicDataDownload(unittest.TestCase):
    def test_tls_eof_retries_and_reports_the_failed_resource(self):
        eof = URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("urllib.request.urlopen", side_effect=eof) as request,
            patch("src.strategy.laplace.time.sleep"),
        ):
            data = PublicData(directory)
            with self.assertRaisesRegex(LaplaceError, "CN/servant/800100.*尝试 3 次"):
                data.get("CN", "servant", 800100)
            self.assertEqual(request.call_count, 3)
            self.assertFalse((Path(directory) / "CN").exists())

    def test_tls_certificate_failure_does_not_retry(self):
        failure = URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
        with patch("urllib.request.urlopen", side_effect=failure) as request:
            with self.assertRaisesRegex(LaplaceError, "CERTIFICATE_VERIFY_FAILED"):
                PublicData().download("CN/servant/800100")
            request.assert_called_once()


class TestLaplaceGates(unittest.TestCase):
    def setUp(self):
        self.parser, self.rec = fixture()
        self.ctrl = MagicMock()
        self.ctrl.cancel_event = None
        self.engine = SmartEngine(self.ctrl, chaldea_parser=self.parser, log_callback=lambda _: None)
        self.engine.current_recommendation = self.rec
        self.engine.active_strategy = self.rec.strategy

    def test_oberon_in_backline_does_not_block_castoria_skill_three(self):
        self.assertFalse(self.engine._is_forbidden_skill(2, 3, False))
        self.assertFalse(self.engine._is_forbidden_skill(3, 3, False))
        self.assertTrue(self.engine._is_forbidden_skill(1, 3, False))

    def test_oberon_guard_follows_the_actor_after_order_change(self):
        self.engine.party_slot_to_screen_slot.update({2: 4, 4: 2})
        self.assertTrue(self.engine._is_forbidden_skill(2, 3, False))
        self.assertFalse(self.engine._is_forbidden_skill(2, 3, True))
        self.assertFalse(self.engine._is_forbidden_skill(3, 3, False))

    def test_caster_skadi_is_not_mistaken_for_castoria(self):
        self.assertEqual(AutoPartyAssembler.resolve_support_servant_id("斯卡哈·斯卡蒂(Caster)"), 503900)

    @patch.object(LaplaceService, "validate", return_value=False)
    def test_daily_failure_stops_before_any_game_click(self, validate):
        with patch("src.strategy.daily_quest_policy.normal_ap", return_value=40), \
             patch("src.strategy.daily_quest_policy.discount_status", return_value=True):
            self.assertIsNone(self.engine._select_daily_quest("daily_qp_extreme", [], (1300, 300), 1))
        self.ctrl.click.assert_not_called()
        self.ctrl.click_and_verify.assert_not_called()
        validate.assert_called_once()

    @patch.object(LaplaceService, "validate", return_value=False)
    def test_weekly_failure_stops_before_navigation(self, validate):
        self.engine.navigate_to_free_quest = MagicMock()
        plan = SimpleNamespace(runs=1, war="冬木", spot="X-A", name="宅邸残迹", quest_id="fuyuki_x_a")
        self.assertFalse(self.engine.run_reactive_weekly_plan(plan))
        self.engine.navigate_to_free_quest.assert_not_called()
        self.ctrl.click.assert_not_called()

    def test_verified_combat_reads_np_after_skills_instead_of_stale_before_skills(self):
        self.rec.laplace = {"passed": True}
        self.engine._get_current_np_and_ready_skills = MagicMock(return_value=({1: 100}, {}))
        self.assertEqual(self.engine._combat_np_after_actions(1, False, 1, {1: 0}), 100)
        self.ctrl.cast_servant_skill.assert_not_called()

    def test_low_or_unknown_np_cannot_trigger_attack(self):
        self.rec.laplace = {"passed": True}
        for np in ({1: 80}, {}):
            self.engine._get_current_np_and_ready_skills = MagicMock(return_value=(np, {}))
            self.assertIsNone(self.engine._combat_np_after_actions(1, False, 1, {1: 100}))
        self.ctrl.select_attack_and_np.assert_not_called()

    def test_failed_wave_is_not_automatically_repaired_with_unvalidated_facecards(self):
        self.rec.laplace = {"passed": True}
        self.assertIsNone(self.engine._combat_np_after_actions(1, True, 2))
        self.ctrl.cast_servant_skill.assert_not_called()

    def test_failed_skill_stops_the_remaining_sequence(self):
        self.ctrl.cast_servant_skill.return_value = False
        turn = TurnDirective(skills=[SkillAction(servant=2, skill=1), SkillAction(servant=3, skill=1)])
        self.assertFalse(self.engine._execute_turn_actions(turn, 1))
        self.assertEqual(self.ctrl.cast_servant_skill.call_count, 1)

    def test_validated_skill_cannot_be_silently_skipped_by_runtime_guard(self):
        self.rec.laplace = {"passed": True}
        turn = TurnDirective(skills=[SkillAction(servant=1, skill=3)])
        self.assertFalse(self.engine._execute_turn_actions(turn, 1))
        self.ctrl.cast_servant_skill.assert_not_called()

    def test_failed_order_change_does_not_change_actor_mapping(self):
        self.ctrl.cast_master_skill.return_value = False
        turn = TurnDirective(master_skills=[MasterSkillAction(skill=3, swap=[2, 4])])
        self.assertFalse(self.engine._execute_turn_actions(turn, 3))
        self.assertEqual(self.engine.party_slot_to_screen_slot[2], 2)
        self.assertEqual(self.engine.party_slot_to_screen_slot[4], 4)

    def test_account_or_strategy_change_invalidates_passed_binding(self):
        before = recommendation_signature(self.rec, self.parser)
        self.parser.servants[404200].np_lv = 1
        self.assertNotEqual(before, recommendation_signature(self.rec, self.parser))
        before = recommendation_signature(self.rec, self.parser)
        self.rec.strategy.turns[1].skills.clear()
        self.assertNotEqual(before, recommendation_signature(self.rec, self.parser))

    def test_missing_rotation_support_replans_and_validates_same_quest(self):
        old = self.rec.model_copy(deep=True)
        old.battle_mode = 'rotation'
        self.engine.current_recommendation = old
        with patch.object(self.engine.team_builder, 'recommend_for_quest', return_value=self.rec), \
             patch.object(LaplaceService, 'validate', return_value=True) as validate:
            self.assertTrue(self.engine._replace_unavailable_rotation_support())
        self.assertIs(self.engine.current_recommendation,self.rec)
        self.assertFalse(validate.call_args.kwargs['allow_rotation'])

    def test_missing_support_does_not_adopt_failed_or_different_quest(self):
        old = self.rec.model_copy(deep=True)
        old.battle_mode = 'rotation'
        self.engine.current_recommendation = old
        with patch.object(LaplaceService, 'validate', return_value=False):
            self.assertFalse(self.engine._replace_unavailable_rotation_support())
        self.assertIs(self.engine.current_recommendation,old)
        other = self.rec.model_copy(deep=True)
        other.target_quest_id = 'fuyuki_x_a'
        with patch.object(self.engine.team_builder, 'recommend_for_quest', return_value=other), \
             patch.object(LaplaceService, 'validate') as validate:
            self.assertFalse(self.engine._replace_unavailable_rotation_support())
            validate.assert_not_called()


class TestLaplaceProtocol(unittest.TestCase):
    def setUp(self):
        self.parser, self.rec = fixture()
        (ROOT / "scratch").mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "scratch")
        self.addCleanup(self.tmp.cleanup)
        self.service = LaplaceService(jobs=Path(self.tmp.name) / "jobs")
        LaplaceService._cache.clear()

    def test_missing_runtime_fails_without_fallback(self):
        self.service.runtime = Path(self.tmp.name) / "missing"
        with patch.object(self.service, "_run_worker") as worker:
            self.assertFalse(self.service.validate(self.rec, self.parser))
            self.assertFalse(self.rec.laplace["passed"])
            worker.assert_not_called()

    def test_cancelled_calculation_never_runs_worker(self):
        event = threading.Event(); event.set()
        with patch.object(self.service, "status", return_value={"available": True}), patch.object(self.service, "_run_worker") as worker:
            self.assertFalse(self.service.validate(self.rec, self.parser, event))
            self.assertEqual(self.rec.laplace["status"], "cancelled")
            worker.assert_not_called()

    def test_unknown_target_is_not_replaced_with_qp_quest(self):
        self.rec.target_quest_id = "unknown_quest"
        with self.assertRaises(LaplaceError):
            self.service._prepare(self.rec, self.parser, None)

    def test_no_swap_master_action_keeps_explicit_outfit_and_account_level(self):
        self.rec.strategy.turns[3].master_skills = [MasterSkillAction(skill=1)]
        self.rec.strategy.turns[3].skills = [s for s in self.rec.strategy.turns[3].skills if s.servant != 4]
        self.parser.mystic_codes_complete = True
        self.parser.mystic_codes = {20: 6}
        payload, metadata = self.service._prepare(self.rec, self.parser, None)
        self.assertEqual(payload["request"]["mystic_code_id"], 20)
        self.assertEqual(metadata["required_mystic_code_level"], 6)
        self.assertNotIn(4, metadata["party_servant_ids"])
        self.rec.strategy.mystic_code_id = None
        with self.assertRaisesRegex(LaplaceError, "未指定魔术礼装"):
            self.service._prepare(self.rec, self.parser, None)

    def test_expanded_search_tries_earlier_swap_and_owned_backline_support(self):
        self.parser.servants[501900] = ServantProfile(svt_id=501900, lv=90)
        self.rec.laplace = {"steps": [{"np_before_attack": 100}, {"np_before_attack": 97}]}
        variants = list(self.service._expanded_candidates(self.rec, self.parser, {}, [self.rec]))
        early = next(r for r in variants if r.direction_name == "奥伯龙第2波换人")
        self.assertEqual(early.strategy.turns[2].master_skills[0].swap, [2, 4])
        self.assertEqual([s.skill for s in early.strategy.turns[2].skills if s.servant == 4], [1, 2])
        self.assertIn(3, [s.skill for s in early.strategy.turns[3].skills if s.servant == 4])
        self.assertTrue(any(r.slot4_backline_support == "诸葛孔明" for r in variants))
        self.assertFalse(self.rec.strategy.turns[2].master_skills)

    def test_unknown_target_does_not_try_a_different_quest_as_fallback(self):
        self.rec.target_quest_id = "unknown_quest"
        self.rec.candidate_dps_ranking = [{"svt_id": 404200}, {"svt_id": 704300}]
        with patch.object(self.service, "_run_worker") as worker:
            self.assertFalse(self.service.validate(self.rec, self.parser))
            worker.assert_not_called()

    def test_explicit_dps_must_be_owned_and_eligible(self):
        with self.assertRaises(ValueError):
            AutoTeamBuilder(self.parser).recommend_for_quest("daily_qp_extreme", dps_id=999999)

    def test_lower_ranked_loop_is_used_only_after_validation(self):
        self.parser.servants[704300] = ServantProfile(svt_id=704300, lv=90, skill_lv1=10, skill_lv2=10, skill_lv3=10)
        rec = AutoTeamBuilder(self.parser).recommend_for_quest(quest_id="septem_massilia")
        def validate(candidate, *_):
            passed = candidate.slot1_svt_id == 404200
            candidate.laplace = {"passed": passed, "message": "ok" if passed else "NP不足"}
            return passed
        with patch("src.strategy.low_hp_planner.rotation_candidates", return_value=[]), \
             patch.object(self.service, "_load_quest", return_value=({}, {}, "JP")), \
             patch.object(self.service, "_validate_one", side_effect=validate):
            self.assertTrue(self.service.validate(rec, self.parser))
        self.assertEqual(rec.slot1_svt_id, 404200)
        self.assertEqual([a['passed'] for a in rec.laplace['attempts']], [False, True])

    def test_theoretical_ce_cannot_be_accepted_as_owned(self):
        self.rec.slot1_ce_detail["is_owned"] = False
        with self.assertRaises(LaplaceError):
            self.service._prepare(self.rec, self.parser, None)

    def test_stale_worker_output_is_rejected(self):
        def fake_process(command, **kwargs):
            job = Path(kwargs["env"]["FGOMAA_LAPLACE_JOB"])
            (job.parent / "result.json").write_text(json.dumps({"job_id": "old-job", "input_digest": "old-digest"}))
            return SimpleNamespace(poll=lambda: 0, returncode=0)
        with patch("src.strategy.laplace.subprocess.Popen", side_effect=fake_process):
            with self.assertRaisesRegex(LaplaceError, "不一致"):
                self.service._run_worker({"request": {}})

    def test_worker_timeout_terminates_its_process(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch("src.strategy.laplace.subprocess.Popen", return_value=process), \
             patch("src.strategy.laplace.time.monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(LaplaceError, "超时"):
                self.service._run_worker({"request": {}})
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_worker_cancellation_terminates_its_process(self):
        process = MagicMock()
        process.poll.return_value = None
        event = threading.Event(); event.set()
        with patch("src.strategy.laplace.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(LaplaceError, "取消"):
                self.service._run_worker({"request": {}}, event)
        process.terminate.assert_called_once()


@unittest.skipUnless((ROOT / "runtime/laplace/manifest.json").exists(), "packaged Laplace runtime not built")
class TestRealLaplace(unittest.TestCase):
    def setUp(self):
        self.parser, self.rec = fixture()
        (ROOT / "scratch").mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "scratch")
        self.addCleanup(self.tmp.cleanup)
        self.service = LaplaceService(jobs=Path(self.tmp.name) / "jobs")
        LaplaceService._cache.clear()

    def test_real_engine_validates_and_rewrites_cooldown_and_np_schedule_without_sdk(self):
        with patch.dict(os.environ, {"PATH": os.environ.get("SystemRoot", "C:/Windows") + "/System32"}):
            self.assertTrue(self.service.validate(self.rec, self.parser), self.rec.laplace)
        self.assertEqual(self.rec.waves, [3, 3, 1])
        self.assertEqual([s["np_before_attack"] for s in self.rec.laplace["steps"]], [100, 174.41, 176.32])
        self.assertEqual([s["damage"][0]["damage"] for s in self.rec.laplace["steps"]], [108941, 122559, 251853])
        t1 = [(s.servant, s.skill) for s in self.rec.strategy.turns[1].skills]
        t2 = [(s.servant, s.skill) for s in self.rec.strategy.turns[2].skills]
        t3 = [(s.servant, s.skill) for s in self.rec.strategy.turns[3].skills]
        self.assertIn((3, 2), t1)
        self.assertEqual(t2, [(1, 2)])
        self.assertNotIn((1, 2), t3)

    def test_real_engine_rejects_enemy_hp_above_damage(self):
        original = self.service.data.get
        def high_hp(region, kind, identity, phase=None):
            value = copy.deepcopy(original(region, kind, identity, phase))
            if kind == "quest" and region == "JP":
                for stage in value["stages"]:
                    for enemy in stage["enemies"]:
                        enemy["hp"] = 100000000
            return value
        with patch.object(self.service.data, "get", side_effect=high_hp):
            self.assertFalse(self.service.validate(self.rec, self.parser))
        self.assertEqual(self.rec.laplace["status"], "failed")
        self.assertIn("伤害不足", self.rec.laplace["message"])

    def test_result_cache_uses_current_inputs(self):
        with patch.object(self.service, "_run_worker", wraps=self.service._run_worker) as worker:
            self.assertTrue(self.service.validate(self.rec, self.parser))
            _, fresh = fixture()
            self.assertTrue(self.service.validate(fresh, self.parser))
            self.assertEqual(worker.call_count, 1)
            self.parser.servants[404200].np_lv = 4
            fresh = AutoTeamBuilder(self.parser).recommend_for_quest("daily_qp_extreme")
            self.service.validate(fresh, self.parser)
            self.assertEqual(worker.call_count, 2)

    def test_account_change_during_calculation_cannot_be_stamped_as_current(self):
        original = self.service._run_worker
        def change_account(*args, **kwargs):
            result = original(*args, **kwargs)
            self.parser.servants[404200].np_lv = 1
            return result
        with patch.object(self.service, "_run_worker", side_effect=change_account):
            self.assertFalse(self.service.validate(self.rec, self.parser))
        self.assertIn("已变化", self.rec.laplace["message"])

    def test_missing_public_skill_is_reported_without_network_in_worker(self):
        payload, _ = self.service._prepare(self.rec, self.parser, None)
        payload["data"]["base_skills"] = []
        result = self.service._run_worker(payload)
        self.assertFalse(result["success"])
        self.assertTrue(result["missing_data"])
        self.assertTrue(all(url.startswith("https://api.atlasacademy.io/nice/JP/skill/") for url in result["missing_data"]))

    def test_service_resolves_missing_public_skill_and_restarts_with_new_input(self):
        original = self.service._prepare
        def without_cached_skills(*args, **kwargs):
            payload, metadata = original(*args, **kwargs)
            payload["data"]["base_skills"] = []
            return payload, metadata
        with patch.object(self.service, "_prepare", side_effect=without_cached_skills), \
             patch.object(self.service, "_run_worker", wraps=self.service._run_worker) as worker:
            self.assertTrue(self.service.validate(self.rec, self.parser), self.rec.laplace)
            self.assertEqual(worker.call_count, 2)

    def test_real_client_api_returns_laplace_result_and_executable_plan(self):
        from http.server import ThreadingHTTPServer
        from src.server.web_server import MAARequestHandler, state
        with patch.object(state, "chaldea_parser", self.parser):
            server = ThreadingHTTPServer(("127.0.0.1", 0), MAARequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                address = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(address + "/api/laplace/status", timeout=5) as response:
                    self.assertTrue(json.load(response)["available"])
                with urllib.request.urlopen(address + "/api/auto_team?quest=daily_qp_extreme", timeout=30) as response:
                    result = json.load(response)["recommendation"]
                self.assertTrue(result["laplace"]["passed"])
                self.assertEqual(result["waves"], [3, 3, 1])
                self.assertEqual(result["strategy"]["turns"]["2"]["skills"], [{"servant": 1, "skill": 2, "target": 1}])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
