from pathlib import Path
import unittest
from unittest.mock import MagicMock
from PIL import Image

from src.strategy.auto_party_assembler import AutoPartyAssembler
from src.strategy.screen_detector import ScreenDetector
from src.strategy.servant_picker import ServantPicker
from tests import test_party_assembly_safety as party_tests


class TestServantLevelGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).parent/'fixtures'
        with Image.open(folder/'duplicate_sieg_picker.png') as image:
            cls.picker_image=image.copy()
        with Image.open(folder/'sieg70_party.png') as image:
            cls.party_image=image.copy()
        cls.picker_ocr=ScreenDetector.run_ocr(cls.picker_image)
        cls.party_ocr=ScreenDetector.run_ocr(cls.party_image)

    @classmethod
    def tearDownClass(cls):
        cls.picker_image.close()
        cls.party_image.close()

    def test_real_level80_copy_is_chosen_instead_of_higher_scoring_level70(self):
        picker=ServantPicker(MagicMock(cancel_event=None),AutoPartyAssembler())
        matched,score,point=picker.match_target_at_level(self.picker_image,self.picker_ocr,503800,80)
        self.assertTrue(matched)
        self.assertGreater(score,.95)
        self.assertTrue(700<point[0]<960 and 600<point[1]<850)

    def test_real_visible_level70_sieg_still_matches_when_explicitly_requested(self):
        picker=ServantPicker(MagicMock(cancel_event=None),AutoPartyAssembler())
        matched,score,point=picker.match_target_at_level(self.picker_image,self.picker_ocr,503800,70)
        self.assertTrue(matched)
        self.assertGreater(score,.95)
        self.assertTrue(1270<point[0]<1535 and 900<point[1]<1065)

    def test_actual_party_level_is_independent_of_face_identity(self):
        self.assertTrue(AutoPartyAssembler.verify_slot_level(self.party_image,1,70,self.party_ocr))
        self.assertFalse(AutoPartyAssembler.verify_slot_level(self.party_image,1,80,self.party_ocr))
        self.assertTrue(AutoPartyAssembler.verify_slot_level(self.party_image,2,100,self.party_ocr))
        self.assertFalse(AutoPartyAssembler.verify_slot_level(self.party_image,1,80,[]))

    def test_unknown_or_wrong_level_blocks_final_party(self):
        party_tests.TestPartyAssemblySafety.setUpClass()
        fixture=party_tests.TestPartyAssemblySafety()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        checks=0
        def verify(*args):
            nonlocal checks
            checks+=1
            return checks<=3
        fixture.assembler.verify_slot_level.side_effect=verify
        self.assertFalse(fixture.assemble())
        fixture.ctrl.click.assert_not_called()

    def test_rotated_fullscreen_90_uses_upright_roi_readings(self):
        with Image.open(Path(__file__).parent/'fixtures/sieg80_party_ocr_rotation.png') as image:
            img=image.convert('RGB')
        self.addCleanup(img.close)
        ocr=ScreenDetector.run_ocr(img)
        self.assertTrue(AutoPartyAssembler.verify_slot_level(img,4,90,ocr))
        self.assertFalse(AutoPartyAssembler.verify_slot_level(img,4,80,ocr))

    def test_picker_rotated_90_is_reread_regardless_of_suffix_order(self):
        with Image.open(Path(__file__).parent/'fixtures/ibuki_picker_rotated_level.png') as image:
            img=image.convert('RGB')
        self.addCleanup(img.close)
        ocr=ScreenDetector.run_ocr(img)
        picker=ServantPicker(MagicMock(cancel_event=None),AutoPartyAssembler())
        matched,score,point=picker.match_target_at_level(img,ocr,704300,90)
        self.assertTrue(matched)
        self.assertGreater(score,.95)
        self.assertTrue(985<point[0]<1255 and 300<point[1]<550)
