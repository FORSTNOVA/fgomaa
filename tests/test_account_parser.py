import json
import base64
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from src.parser.chaldea_parser import ChaldeaParser, ServantProfile
from src.strategy.account_versions import resolve_versions


def packet():
    return {"cache": {"replaced": {
        "userSvt": [{"id": "11", "svtId": "704000", "lv": "100", "treasureDeviceLv1": "2",
                     "skillLv1": "10", "skillLv2": "10", "skillLv3": "10",
                     "limitCount": "4", "imageLimitCount": "3", "adjustAtk": "110", "adjustHp": "120"},
                    {"id": "12", "svtId": "704000", "lv": "1", "treasureDeviceLv1": "1"}],
        "userSvtStorage": [{"id": "13", "svtId": "201300", "lv": "60", "treasureDeviceLv1": "5"}],
        "userSvtAppendPassiveSkill": [{"svtId": "704000", "unlockNums": [100, 101, 103, 104]},
                                       {"svtId": "201300", "unlockNums": [101]}],
        "userSvtAppendPassiveSkillLv": [
            {"userSvtId": "11", "appendPassiveSkillNums": [104, 101, 103], "appendPassiveSkillLvs": [8, 5, 7]},
            {"userSvtId": "12", "appendPassiveSkillNums": [101], "appendPassiveSkillLvs": [10]},
            {"userSvtId": "13", "appendPassiveSkillNums": [101], "appendPassiveSkillLvs": [6]}],
        "userQuest": [{"userId": "private", "questId": "123", "questPhase": "3", "clearNum": "1", "status": "0"}],
        "userEquip": [{"equipId": "20", "lv": "6"}],
    }}}


class TestCompleteAccountParser(unittest.TestCase):
    def setUp(self):
        self.parser = ChaldeaParser()

    def test_sparse_numbered_levels_and_shared_unlocks(self):
        self.parser.parse_data(packet())
        servant = self.parser.servants[704000]
        self.assertEqual(servant.append_skills, [1, 5, 0, 7, 8])
        self.assertEqual(servant.get_append_np_charge(), 14)
        self.assertEqual(servant.card_id, 11)

    def test_copy_levels_do_not_leak_to_main_copy(self):
        self.parser.parse_data(packet())
        copies = {s.card_id: s for s in self.parser.servant_copies}
        self.assertEqual(copies[11].append_skill_lv2, 5)
        self.assertEqual(copies[12].append_skill_lv2, 10)
        self.assertEqual(copies[12].append_skill_lv4, 1)
        self.assertEqual(self.parser.servants[704000].append_skill_lv2, 5)

    def test_storage_is_parsed_but_not_available_for_immediate_party_selection(self):
        self.parser.parse_data(packet())
        self.assertNotIn(201300, self.parser.servants)
        self.assertEqual(self.parser.storage_servants[201300].append_skills, [0, 6, 0, 0, 0])
        self.assertTrue(self.parser.storage_servants[201300].in_storage)

    def test_storage_ce_cannot_replace_an_available_inventory_ce(self):
        data = packet()
        data["cache"]["replaced"]["userSvt"].append({"id": 21, "svtId": 9405300, "lv": 20, "limitCount": 0})
        data["cache"]["replaced"]["userSvtStorage"].append({"id": 22, "svtId": 9405300, "lv": 100, "limitCount": 4})
        self.parser.parse_data(data)
        self.assertEqual(self.parser.craft_essences[9405300].initial_np, 30)
        self.assertEqual(self.parser.storage_craft_essences[9405300].initial_np, 50)
        loaded = ChaldeaParser()
        loaded.parse_data(self.parser.export_data())
        self.assertEqual(loaded.export_data(), self.parser.export_data())

    def test_actual_fou_units_and_ascension_are_preserved(self):
        self.parser.parse_data(packet())
        servant = self.parser.servants[704000]
        self.assertEqual((servant.atk_fou, servant.hp_fou), (1100, 1200))
        self.assertEqual((servant.ascension, servant.costume_id), (4, 3))
        self.assertEqual(self.parser.mystic_codes, {20: 6})
        self.assertTrue(self.parser.mystic_codes_complete)

    def test_export_reload_preserves_all_simulation_fields_without_credentials(self):
        self.parser.parse_data(packet())
        exported = json.loads(json.dumps(self.parser.export_data()))
        self.assertNotIn("private", json.dumps(exported))
        loaded = ChaldeaParser()
        loaded.parse_data(exported)
        self.assertEqual(loaded.export_data(), self.parser.export_data())
        self.assertTrue(loaded.quest_records_complete)
        self.assertEqual(loaded.quest_records[123]["clearNum"], 1)

    def test_cache_updates_merge_rows_with_existing_snapshot(self):
        data = packet()
        data["cache"]["updated"] = {
            "userSvtAppendPassiveSkillLv": [{"userSvtId": "11", "appendPassiveSkillNums": [101], "appendPassiveSkillLvs": [9]}],
            "userQuest": [{"questId": "124", "questPhase": "2", "clearNum": "1"}],
        }
        self.parser.parse_data(data)
        self.assertEqual(self.parser.servants[704000].append_skill_lv2, 9)
        self.assertEqual(set(self.parser.quest_records), {123, 124})
        self.assertTrue(self.parser.quest_records_complete)

    def test_har_with_urlencoded_base64_gzip_body_retains_full_account_context(self):
        body = quote(base64.b64encode(gzip.compress(json.dumps(packet()).encode())).decode(), safe="")
        har = {"log": {"entries": [
            {"response": {"content": {"text": '{"sdk": true}'}}},
            {"response": {"content": {"encoding": "base64", "text": base64.b64encode(body.encode()).decode()}}},
        ]}}
        self.parser.parse_data(har)
        self.assertEqual(self.parser.servants[704000].append_skills, [1, 5, 0, 7, 8])
        self.assertEqual(self.parser.quest_records[123]["clearNum"], 1)
        self.assertEqual(self.parser.mystic_codes[20], 6)

    def test_updated_only_quest_table_is_not_a_complete_snapshot(self):
        data = packet()
        data["cache"]["updated"] = {"userQuest": data["cache"]["replaced"].pop("userQuest")}
        self.parser.parse_data(data)
        self.assertFalse(self.parser.quest_records_complete)
        self.assertIn(123, self.parser.quest_records)

    def test_malformed_append_pair_does_not_erase_previous_account(self):
        self.parser.parse_data(packet())
        previous = self.parser.export_data()
        bad = packet()
        bad["cache"]["replaced"]["userSvtAppendPassiveSkillLv"][0]["appendPassiveSkillLvs"] = [10]
        with self.assertRaises(ValueError):
            self.parser.parse_data(bad)
        self.assertEqual(self.parser.export_data(), previous)

    def test_new_account_does_not_inherit_old_quest_or_append_state(self):
        self.parser.parse_data(packet())
        self.parser.parse_data({"userServant": [{"svtId": 704000, "lv": 90, "npLv": 1}]})
        self.assertEqual(self.parser.quest_records, {})
        self.assertFalse(self.parser.quest_records_complete)
        self.assertEqual(self.parser.servants[704000].append_skills, [0] * 5)

    def test_legacy_inline_and_separate_svt_level_formats_remain_supported(self):
        for data in (
            {"userSvt": [{"svtId": 704000, "appendPassiveSkillLvs": [2, 10, 3]}]},
            {"userSvt": [{"svtId": 704000}], "userSvtAppendPassiveSkill": [{"svtId": 704000, "skillLvs": [2, 10, 3]}]},
            {"servants": {"704000": {"cur": {"skills": [10, 10], "appendSkills": [2, 10, 3]}}}},
            [{"svt_id": 704000, "append_skills": [2, 10, 3]}],
        ):
            with self.subTest(data=data):
                self.parser.parse_data(data)
                self.assertEqual(self.parser.servants[704000].append_skills, [2, 10, 3, 0, 0])

    def test_inline_numbered_levels_are_not_treated_as_dense_array(self):
        self.parser.parse_data({"userSvt": [{"svtId": 704000,
            "appendPassiveSkillNums": [104, 101], "appendPassiveSkillLvs": [4, 8]}]})
        self.assertEqual(self.parser.servants[704000].append_skills, [0, 8, 0, 0, 4])

    def test_chaldea_fou_counts_are_converted_to_stats(self):
        self.parser.parse_data({"servants": {"704000": {"cur": {
            "fouAtk3": 20, "fouHp3": 10, "fouAtk": 5, "fouHp5": 2}}}})
        self.assertEqual((self.parser.servants[704000].atk_fou, self.parser.servants[704000].hp_fou), (1100, 2200))

    def test_chaldea_native_np_level_name(self):
        self.parser.parse_data({"servants": {"704000": {"cur": {"npLv": 4}}}})
        self.assertEqual(self.parser.servants[704000].np_lv, 4)

    def test_server_saves_the_complete_normalized_account(self):
        from src.server.web_server import GlobalState
        self.parser.parse_data(packet())
        dummy = type("State", (), {"chaldea_parser": self.parser})()
        with tempfile.TemporaryDirectory() as folder, patch("src.server.web_server.get_root_dir", return_value=Path(folder)):
            GlobalState.save_current_servants(dummy)
            loaded = ChaldeaParser()
            loaded.parse_file(Path(folder) / "data/user_account.json")
        self.assertEqual(loaded.export_data(), self.parser.export_data())


def version_fixture():
    data = {"id": 704000, "skills": [{"id": i, "num": i, "name": f"skill{i}", "priority": 1} for i in range(1, 4)],
            "noblePhantasms": [{"id": 10, "num": 1, "name": "base NP", "priority": 101},
                               {"id": 20, "num": 1, "name": "upgraded NP", "priority": 102,
                                "condQuestId": 123, "condQuestPhase": 3}]}
    profile = ServantProfile(svt_id=704000, name="test", lv=90, ascension=4)
    return data, profile


class TestAccountVersions(unittest.TestCase):
    def test_missing_or_incomplete_quest_does_not_select_upgraded_np(self):
        data, profile = version_fixture()
        for records in ({}, {123: {"clearNum": 0, "questPhase": 2}}, {123: {"clearNum": 1, "questPhase": 2}}):
            self.assertEqual(resolve_versions(data, profile, records)[0]["np_id"], 10)

    def test_completed_task_selects_upgraded_np_and_skill(self):
        data, profile = version_fixture()
        data["skills"].append({"id": 4, "num": 2, "name": "upgraded skill", "priority": 2,
                               "condQuestId": 123, "condQuestPhase": 3})
        resolved, _ = resolve_versions(data, profile, {123: {"clearNum": 1, "questPhase": 3}})
        self.assertEqual(resolved["np_id"], 20)
        self.assertEqual(resolved["skill_ids"], [1, 4, 3])

    def test_costume_gates_use_the_matching_servant_metadata(self):
        data, profile = version_fixture()
        data["skills"].append({"id": 4, "num": 3, "name": "form skill", "priority": 2, "skillSvts": [
            {"svtId": 704000, "num": 3, "priority": 2, "releaseConditions": [
                {"condType": "equipWithTargetCostume", "condTargetId": 704000, "condNum": 13}]}]})
        self.assertEqual(resolve_versions(data, profile, {})[0]["skill_ids"][2], 3)
        profile.costume_id = 13
        self.assertEqual(resolve_versions(data, profile, {})[0]["skill_ids"][2], 4)

    def test_unknown_release_condition_does_not_authorize_version(self):
        data, profile = version_fixture()
        data["noblePhantasms"][1].pop("condQuestId")
        data["noblePhantasms"][1]["releaseConditions"] = [{"condType": "unknown", "condTargetId": 1}]
        self.assertEqual(resolve_versions(data, profile, {})[0]["np_id"], 10)

    def test_locked_ascension_skill_is_unavailable(self):
        data, profile = version_fixture()
        data["skills"][2]["condLimitCount"] = 3
        profile.ascension = 1
        self.assertEqual(resolve_versions(data, profile, {})[0]["skill_ids"], [1, 2, 0])

    def test_explicit_version_is_checked_against_the_right_servant(self):
        data, profile = version_fixture()
        profile.np_id = 20
        self.assertEqual(resolve_versions(data, profile, {})[0]["np_id"], 20)
        profile.np_id = 999
        with self.assertRaises(ValueError):
            resolve_versions(data, profile, {})


class TestAccountSimulationInput(unittest.TestCase):
    def setUp(self):
        from tests.test_laplace_integration import fixture
        from src.strategy.laplace import LaplaceService
        self.parser, self.rec = fixture()
        self.service = LaplaceService()

    def test_worker_receives_five_appends_actual_fou_and_exact_versions(self):
        servant = self.parser.servants[404200]
        servant.append_skill_lv2 = 10
        servant.append_skill_lv4 = 8
        servant.append_skill_lv5 = 7
        servant.atk_fou, servant.hp_fou = 1100, 1200
        payload, meta = self.service._prepare(self.rec, self.parser, None)
        row = payload["request"]["team"][0]
        self.assertEqual(payload["request"]["protocol"], 4)
        self.assertEqual(row["append_levels"], [0, 10, 0, 8, 7])
        self.assertEqual((row["atk_fou"], row["hp_fou"]), (1100, 1200))
        self.assertEqual(row["skill_ids"], meta["party_versions"][1]["skill_ids"])
        self.assertGreater(row["np_id"], 0)

    def test_account_quest_and_mystic_code_changes_invalidate_binding(self):
        from src.strategy.laplace import recommendation_signature
        before = recommendation_signature(self.rec, self.parser)
        self.parser.quest_records[123] = {"questId": 123, "clearNum": 1, "questPhase": 3}
        after = recommendation_signature(self.rec, self.parser)
        self.assertNotEqual(before, after)
        self.parser.mystic_codes[20] = 5
        self.assertNotEqual(after, recommendation_signature(self.rec, self.parser))

    def test_owned_mystic_code_uses_its_actual_level(self):
        self.parser.mystic_codes_complete = True
        self.parser.mystic_codes = {20: 6}
        payload, _ = self.service._prepare(self.rec, self.parser, None)
        self.assertEqual(payload["request"]["mystic_code_lv"], 6)

    def test_confirmed_missing_mystic_code_cannot_be_assumed_owned(self):
        from src.strategy.laplace import LaplaceError
        self.parser.mystic_codes_complete = True
        with self.assertRaisesRegex(LaplaceError, "未持有"):
            self.service._prepare(self.rec, self.parser, None)


if __name__ == "__main__":
    unittest.main()
