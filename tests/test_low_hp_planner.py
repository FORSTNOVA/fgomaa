import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from src.core.controller import FGOController
from src.parser.chaldea_parser import ChaldeaParser, ServantProfile, CraftEssenceProfile
from src.strategy.auto_team_builder import AutoTeamBuilder
from src.strategy.battle_plans import TeamStrategy, TurnDirective, BattlePlanManager
from src.strategy.low_hp_planner import rotation_candidates
from src.strategy.laplace import LaplaceService
from src.strategy.smart_engine import SmartEngine


def fixture():
    parser = ChaldeaParser()
    parser.servants = {sid: ServantProfile(svt_id=sid, name=name, lv=90, np_lv=1,
        skill_lv1=10, skill_lv2=10, skill_lv3=10) for sid, name in
        [(501200, "尼托克丽丝"), (2300500, "Archetype:Earth")]}
    base = AutoTeamBuilder(parser).recommend_for_quest("fuyuki_x_f")
    quest = {"stages": [{"enemies": [{"hp": 10000, "svt": {"className": "saber"}}]} for _ in range(3)]}
    return parser, base, quest


class TestLowHpCandidates(unittest.TestCase):
    def test_three_dps_have_separate_np_turns_and_explicit_ids(self):
        parser, base, quest = fixture()
        candidates = rotation_candidates(base, parser, quest)
        self.assertTrue(candidates)
        for rec in candidates:
            self.assertEqual(rec.battle_mode, "rotation")
            self.assertNotEqual(rec.party_servant_ids[1], rec.party_servant_ids[2])
            self.assertEqual({t.np_servant for t in rec.strategy.turns.values()}, {1, 2, 3})
            self.assertFalse(any(t.master_skills for t in rec.strategy.turns.values()))
        self.assertEqual(base.battle_mode, "loop")

    def test_high_hp_keeps_existing_loop_strategy(self):
        parser, base, quest = fixture()
        quest["stages"][2]["enemies"][0]["hp"] = 60001
        self.assertEqual(rotation_candidates(base, parser, quest), [])

    def test_higher_np_owned_attacker_is_reserved_for_harder_wave(self):
        parser, base, quest = fixture()
        parser.servants[2300500].np_lv = 5
        quest['stages'][2]['enemies'][0]['hp'] = 44000
        rec = rotation_candidates(base, parser, quest)[0]
        actor = rec.strategy.turns[3].np_servant
        self.assertEqual(rec.party_servant_ids[actor], 2300500)
        self.assertNotEqual(actor, 3)  # 助战按宝具 1 试算。

    def test_bounded_trials_try_different_owned_pairs(self):
        parser, base, quest = fixture()
        parser.servants[505300] = ServantProfile(svt_id=505300, lv=90,
            skill_lv1=10, skill_lv2=10, skill_lv3=10)
        candidates = rotation_candidates(base, parser, quest)
        pairs = {tuple(sorted((r.party_servant_ids[1], r.party_servant_ids[2]))) for r in candidates}
        self.assertEqual(len(pairs), 3)

    def test_unrecorded_ascension_charge_is_not_assumed(self):
        parser, base, quest = fixture()
        parser.servants[304800] = ServantProfile(svt_id=304800, lv=90,
            skill_lv1=10, skill_lv2=10, skill_lv3=10)
        for rec in rotation_candidates(base, parser, quest):
            self.assertNotIn(304800, rec.party_servant_ids.values())

    def test_low_hp_normal_seed_does_not_require_np_or_ce(self):
        parser, base, quest = fixture()
        parser.servants = {sid: ServantProfile(svt_id=sid, lv=90) for sid in (100100, 100900)}
        quest["stages"] = [{"enemies": [{"hp": 1, "svt": {"className": "lancer"}}]}]
        candidates = rotation_candidates(base, parser, quest)
        self.assertTrue(candidates)
        self.assertFalse(candidates[0].party_ce_details)
        self.assertFalse(candidates[0].strategy.turns[1].skills)
        self.assertEqual(len(candidates[0].strategy.turns), 1)

    def test_one_ce_is_never_allocated_to_two_actors(self):
        parser, base, quest = fixture()
        parser.servants = {sid: ServantProfile(svt_id=sid, lv=90) for sid in (100100, 100900)}
        parser.craft_essences = {9400001: CraftEssenceProfile(ce_id=9400001, initial_np=100)}
        self.assertEqual(rotation_candidates(base, parser, quest), [])

    def test_death_swap_dps_are_excluded(self):
        parser, base, quest = fixture()
        for sid in (201300, 504400):
            parser.servants[sid] = ServantProfile(svt_id=sid, lv=100, skill_lv1=10, skill_lv2=10, skill_lv3=10)
        for rec in rotation_candidates(base, parser, quest):
            self.assertFalse({201300, 504400} & set(rec.party_servant_ids.values()))


class TestRotatingExecution(unittest.TestCase):
    def setUp(self):
        self.ctrl = MagicMock(cancel_event=None)
        self.engine = SmartEngine(self.ctrl, log_callback=lambda _: None)
        self.engine.current_recommendation = SimpleNamespace(laplace={"passed": True})
        self.engine.active_strategy = TeamStrategy(id="rotation", name="rotation", match_servants=[],
            turns={1: TurnDirective(np_servant=2), 2: TurnDirective(attack_mode="facecards", np_servant=None)})

    def test_np_gate_reads_designated_second_actor(self):
        self.engine._get_current_np_and_ready_skills = MagicMock(return_value=({1: 0, 2: 100}, {}))
        self.assertEqual(self.engine._combat_np_after_actions(1, False, 1), 100)
        self.engine._get_current_np_and_ready_skills.return_value = ({1: 100, 2: 99}, {})
        self.assertIsNone(self.engine._combat_np_after_actions(1, False, 1))

    def test_normal_mode_skips_np_gate_but_cannot_repair_failed_wave(self):
        self.engine._get_current_np_and_ready_skills = MagicMock()
        self.assertEqual(self.engine._combat_np_after_actions(2, False, 2), 0)
        self.engine._get_current_np_and_ready_skills.assert_not_called()
        self.assertIsNone(self.engine._combat_np_after_actions(2, True, 3))

    def test_missing_validated_turn_stops_instead_of_using_slot_one(self):
        self.assertIsNone(self.engine._combat_np_after_actions(3, False, 3))
        self.engine.active_strategy = None
        self.assertIsNone(self.engine._combat_np_after_actions(1, False, 1))

    def test_normal_instructions_never_request_a_np(self):
        lines = BattlePlanManager().format_turn_instructions(self.engine.active_strategy, 2)
        self.assertIn("三张普通", lines[-1])
        self.assertNotIn("释放", lines[-1])


class TestControllerSelection(unittest.TestCase):
    def setUp(self):
        self.ctrl = object.__new__(FGOController)
        self.ctrl.cancel_event = None
        self.ctrl.log = MagicMock()
        self.ctrl.click = MagicMock()
        self.ctrl.screencap = MagicMock(return_value=Image.new("RGB", (1920, 1080)))
        self.ctrl._safe_sleep = MagicMock()

    def select(self, slot):
        with patch("src.core.controller.AnchorVerifier.is_card_select_back_button_present", return_value=True), \
             patch("src.core.controller.AnchorVerifier.detect_command_card_colors", return_value=["Arts"] * 5):
            return self.ctrl.select_attack_and_np(slot)

    def test_normal_mode_clicks_three_cards_without_any_np(self):
        self.assertTrue(self.select(None))
        positions = [c.args[:2] for c in self.ctrl.click.call_args_list]
        self.assertEqual(positions, [(1725, 900), (225, 750), (562, 750), (975, 750)])

    def test_rotation_clicks_only_designated_np_plus_two_cards(self):
        for slot, x in ((2, 960), (3, 1220)):
            self.ctrl.click.reset_mock()
            self.assertTrue(self.select(slot))
            self.assertEqual([c.args[:2] for c in self.ctrl.click.call_args_list],
                             [(1725, 900), (x, 320), (225, 750), (562, 750)])

    def test_cancel_does_not_click(self):
        self.ctrl.cancel_event = MagicMock()
        self.ctrl.cancel_event.is_set.return_value = True
        self.assertFalse(self.select(None))
        self.ctrl.click.assert_not_called()


@unittest.skipUnless(LaplaceService().status()["available"], "requires packaged Laplace runtime")
class TestRealLowHpLaplace(unittest.TestCase):
    def test_real_public_quest_clears_with_three_separate_np_users(self):
        parser, rec, _ = fixture()
        self.assertTrue(LaplaceService().validate(rec, parser), rec.laplace)
        self.assertEqual(rec.battle_mode, "rotation")
        self.assertEqual({s["np_servant"] for s in rec.laplace["steps"]}, {1, 2, 3})
        self.assertTrue(all(s["cleared"] for s in rec.laplace["steps"]))
        self.assertTrue(all(s["np_before_attack"] >= 100 for s in rec.laplace["steps"]))

    def normal_payload(self):
        # Public combat data, deliberately synthetic HP. This exercises the
        # actual kernel and every triple; it is not a claim about a live quest.
        from test_laplace_integration import fixture as loop_fixture
        parser, rec = loop_fixture()
        rec.battle_mode = "rotation"
        rec.party_servant_ids = {1: 404200, 2: 504500, 3: 504500}
        rec.party_ce_details = {}
        rec.strategy = TeamStrategy(id="normal_fixture", name="normal", match_servants=[],
            turns={i: TurnDirective() for i in range(1, 4)})
        service = LaplaceService()
        payload, _ = service._prepare(rec, parser, None)
        payload = copy.deepcopy(payload)
        for stage in payload["data"]["quest"]["stages"]:
            stage["enemies"] = stage["enemies"][:1]
            stage["enemies"][0]["hp"] = 1
        return service, payload

    def test_real_kernel_proves_every_triple_and_replays_three_normal_turns(self):
        service, payload = self.normal_payload()
        result = service._run_worker(payload)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["validation"]["success"], result)
        self.assertFalse(result["validation"]["battle_errors"])
        steps = result["validation"]["steps"]
        self.assertEqual(len(steps), 3)
        for step in steps:
            self.assertEqual(step["attack_mode"], "facecards")
            self.assertTrue(step["facecard_proof"]["all_triples"])
            self.assertEqual(step["facecard_proof"]["tested"], 2730)
            self.assertTrue(step["cleared"])

    def test_counterexample_hand_cannot_be_accepted_as_normal_clear(self):
        service, payload = self.normal_payload()
        payload["data"]["quest"]["stages"][0]["enemies"][0]["hp"] = 5000
        # One strong attacker cannot justify cards dealt only to weak allies.
        for row in payload["request"]["team"][1:]:
            row["lv"] = 1
            row["fou"] = 0
        result = service._run_worker(payload)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["validation"]["success"])
        self.assertEqual(result["validation"]["steps"][0]["attack_mode"], "np")
        attempt = result["facecard_probe"]["steps"][0]["facecard_attempt"]
        self.assertFalse(attempt["all_triples"])
        self.assertLess(attempt["tested"], 2730)


if __name__ == "__main__":
    unittest.main()
