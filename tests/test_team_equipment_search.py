import threading
import unittest
from unittest.mock import patch

from src.parser.chaldea_parser import ChaldeaParser, ServantProfile
from src.strategy.auto_team_builder import AutoTeamBuilder, SERVANT_TACTICAL_DB
from src.strategy.laplace import LaplaceService


def account():
    parser = ChaldeaParser()
    parser.servants = {
        404200: ServantProfile(svt_id=404200, lv=80, np_lv=5, skill_lv2=10),
        504500: ServantProfile(svt_id=504500, lv=100, skill_lv1=10, skill_lv2=10, skill_lv3=10),
        2800100: ServantProfile(svt_id=2800100, lv=90, skill_lv1=10, skill_lv2=10, skill_lv3=10),
    }
    for cid in [9401380, 9406390, 9400340, 9400480, 9400290, 9400400]:
        ce = parser._build_ce_from_item(cid, {"is_mlb": True, "lv": 100})
        parser.craft_essences[cid] = ce
    return parser


class TeamEquipmentSearchTests(unittest.TestCase):
    def test_class_order_uses_laplace_constants_including_extra_classes(self):
        self.assertEqual(AutoTeamBuilder.calc_class_affinity("Berserker", "Berserker"), 1.5)
        self.assertEqual(AutoTeamBuilder.calc_class_affinity("Avenger", "Ruler"), 2)
        self.assertEqual(AutoTeamBuilder.calc_class_affinity("Saber", "Ruler"), 0.5)
        self.assertEqual(AutoTeamBuilder.calc_class_affinity("Alterego", "Foreigner"), 2)

    def test_low_np_counter_class_precedes_high_np_berserker(self):
        parser = account()
        parser.servants[704300] = ServantProfile(svt_id=704300, lv=120, np_lv=5,
                                               skill_lv1=10, skill_lv2=10, skill_lv3=10)
        parser.servants[104900] = ServantProfile(svt_id=104900, lv=80, np_lv=1)
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_training_lancer")
        self.assertEqual(rec.slot1_class, "Saber")
        self.assertEqual(rec.slot1_affinity, 2)
        self.assertEqual(rec.candidate_dps_ranking[0]["affinity"], 2)

    def test_exact_enemy_classes_override_the_display_profile(self):
        parser = account()
        parser.servants[501200] = ServantProfile(svt_id=501200, lv=80, np_lv=1)
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme", enemy_classes={"assassin": 9})
        self.assertEqual(rec.slot1_svt_id, 501200)
        self.assertNotIn(404200, [r["svt_id"] for r in rec.candidate_dps_ranking])

    def test_equipment_pool_keeps_charge_refund_damage_and_card_options(self):
        parser = account()
        builder = AutoTeamBuilder(parser)
        rows = builder.craft_essence_candidates(SERVANT_TACTICAL_DB[404200], "Arts")
        details = {row[2]["ce_id"]: row[2] for row in rows}
        self.assertTrue({9401380, 9406390, 9400340, 9400480, 9400290, 9400400} <= details.keys())
        self.assertEqual(details[9406390]["initial_np"], 60)
        self.assertEqual(details[9400480]["np_damage_up"], 80)
        self.assertLessEqual(len(rows), 9)
        self.assertEqual(len(rows), len(details))

    def test_unowned_and_storage_ces_are_never_candidates(self):
        parser = account()
        parser.storage_craft_essences[9400340] = parser.craft_essences.pop(9400340)
        builder = AutoTeamBuilder(parser)
        self.assertNotIn(9400340, [r[2]["ce_id"] for r in builder.craft_essence_candidates(
            SERVANT_TACTICAL_DB[404200], "Arts")])
        with self.assertRaisesRegex(ValueError, "背包"):
            builder.recommend_for_quest("daily_qp_extreme", ce_id=9400340)

    def test_explicit_ce_keeps_actual_level_and_limit_break(self):
        parser = account()
        parser.craft_essences[9400340] = parser._build_ce_from_item(9400340, {"lv": 20, "is_mlb": False})
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme", dps_id=404200, ce_id=9400340)
        self.assertEqual(rec.slot1_ce_detail["initial_np"], 80)
        self.assertEqual(rec.slot1_ce_detail["lv"], 20)
        self.assertFalse(rec.slot1_ce_detail["is_mlb"])

    def run_search(self, only_mirror=False, black_cup_passes=False):
        parser = account()
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme")
        service = LaplaceService()
        seen = []

        def simulate(candidate, *_):
            cid = candidate.slot1_ce_detail.get("ce_id")
            seen.append(cid)
            passed = cid == 9400340 if only_mirror else cid != 9400480 or black_cup_passes
            candidate.laplace = {"passed": passed, "message": "通过" if passed else "NP不足",
                                 "steps": [{"damage": [{"hp": 100, "damage": 2000 if cid == 9400480 else 1000}]}]}
            return passed

        with patch.object(service, "status", return_value={"available": True}), \
             patch.object(service, "_load_quest", return_value=({}, {}, "JP")), \
             patch("src.strategy.low_hp_planner.rotation_candidates", return_value=[]), \
             patch.object(service, "_validate_one", side_effect=simulate):
            self.assertTrue(service.validate(rec, parser))
        return rec, seen

    def test_same_dps_can_switch_to_kaleidoscope_after_card_ce_fails(self):
        rec, seen = self.run_search(only_mirror=True)
        self.assertEqual(rec.slot1_svt_id, 404200)
        self.assertEqual(rec.slot1_ce_detail["ce_id"], 9400340)
        self.assertGreater(len(seen), 1)
        self.assertTrue(any(not a["passed"] for a in rec.laplace["attempts"]))

    def test_failed_black_cup_probe_keeps_the_passed_plan(self):
        rec, seen = self.run_search()
        self.assertIn(9400480, seen)
        self.assertNotEqual(rec.slot1_ce_detail["ce_id"], 9400480)
        self.assertTrue(rec.laplace["passed"])
        self.assertFalse(rec.laplace["attempts"][-1]["passed"])

    def test_black_cup_is_selected_only_after_np_and_damage_proof(self):
        rec, _ = self.run_search(black_cup_passes=True)
        self.assertEqual(rec.slot1_ce_detail["ce_id"], 9400480)
        self.assertTrue(rec.laplace["passed"])

    def test_cancellation_during_black_cup_probe_does_not_adopt_an_old_pass(self):
        parser = account()
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme")
        service = LaplaceService()
        event = threading.Event()

        def simulate(candidate, *_):
            if candidate.slot1_ce_detail["ce_id"] == 9400480:
                event.set()
            candidate.laplace = {"passed": True}
            return True

        with patch.object(service, "status", return_value={"available": True}), \
             patch.object(service, "_load_quest", return_value=({}, {}, "JP")), \
             patch("src.strategy.low_hp_planner.rotation_candidates", return_value=[]), \
             patch.object(service, "_validate_one", side_effect=simulate):
            self.assertFalse(service.validate(rec, parser, event))
        self.assertEqual(rec.laplace["status"], "cancelled")

    def test_explicit_rotation_plan_is_not_discarded(self):
        parser = account()
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme")
        rec.battle_mode = "rotation"
        service = LaplaceService()

        def simulate(candidate, *_):
            candidate.laplace = {"passed": True}
            return True

        with patch.object(service, "status", return_value={"available": True}), \
             patch.object(service, "_load_quest", return_value=({}, {}, "JP")), \
             patch("src.strategy.low_hp_planner.rotation_candidates", return_value=[]), \
             patch.object(service, "_validate_one", side_effect=simulate) as worker:
            self.assertTrue(service.validate(rec, parser))
        self.assertEqual(worker.call_args.args[0].battle_mode, "rotation")

    def test_all_failed_trials_report_failure_instead_of_an_unchecked_original(self):
        parser = account()
        rec = AutoTeamBuilder(parser).recommend_for_quest("daily_qp_extreme")
        rec.slot1_svt_id = 704300  # 原推荐因准确关卡数据而被替换，未直接参与试算。
        service = LaplaceService()

        def simulate(candidate, *_):
            candidate.laplace = {"passed": False, "message": "第二波NP不足"}
            return False

        with patch.object(service, "status", return_value={"available": True}), \
             patch.object(service, "_load_quest", return_value=({}, {}, "JP")), \
             patch("src.strategy.low_hp_planner.rotation_candidates", return_value=[]), \
             patch.object(service, "_validate_one", side_effect=simulate):
            self.assertFalse(service.validate(rec, parser))
        self.assertEqual(rec.laplace["status"], "failed")
        self.assertIn("均未通过", rec.laplace["message"])
        self.assertFalse(rec.laplace["passed"])


if __name__ == "__main__":
    unittest.main()
