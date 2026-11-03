import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from src.parser.chaldea_parser import ChaldeaParser
from src.strategy.laplace import LaplaceError, require_supported_np_refunds


class InstantDeathRefundGuardTests(unittest.TestCase):
    def test_real_nitocris_loop_is_not_proven_by_a_single_probability_threshold(self):
        root = Path(__file__).resolve().parents[1]
        servant = json.loads((root / "data/laplace/public/CN/servant/501200.json").read_text(encoding="utf-8"))
        rec = SimpleNamespace(strategy=SimpleNamespace(turns={
            n: SimpleNamespace(attack_mode="np", np_servant=1) for n in range(1, 4)
        }))
        with self.assertRaisesRegex(LaplaceError, "零 NP 回收"):
            require_supported_np_refunds(rec, servant, 1, 501202)

    def test_single_finishing_np_is_allowed(self):
        rec = SimpleNamespace(strategy=SimpleNamespace(turns={
            3: SimpleNamespace(attack_mode="np", np_servant=1)
        }))
        require_supported_np_refunds(rec, {}, 1, 501202)

    def test_death_after_damage_does_not_erase_refund(self):
        rec = SimpleNamespace(strategy=SimpleNamespace(turns={
            n: SimpleNamespace(attack_mode="np", np_servant=1) for n in range(1, 4)
        }))
        servant = {"noblePhantasms": [{"id": 1, "functions": [
            {"funcType": "damageNp"}, {"funcType": "instantDeath"}
        ]}]}
        require_supported_np_refunds(rec, servant, 1, 1)

    def test_sea_flyer_has_no_np_gain_buff(self):
        ce = ChaldeaParser()._build_ce_from_item(9405300, {"lv": 15, "is_mlb": True})
        self.assertEqual(ce.np_gain_rate, 0)
        self.assertEqual((ce.initial_np, ce.card_buff_rate, ce.np_damage_up), (50, 8, 15))
