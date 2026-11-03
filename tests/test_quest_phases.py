import unittest
from unittest.mock import patch, MagicMock
from src.strategy.quest_catalog import quest_binding
from src.strategy.laplace import LaplaceService


class TestExplicitQuestPhase(unittest.TestCase):
    def setUp(self):
        self.entry={'id':93040401,'phase':3,'phases':[1,2,3],'name':'月球之巅'}
        binding=patch('src.strategy.quest_catalog.quest_bindings',return_value={'target':self.entry})
        binding.start();self.addCleanup(binding.stop)

    def test_explicit_phase_preserves_default_and_metadata(self):
        self.assertEqual(quest_binding('target@phase=1'),{**self.entry,'phase':1})
        self.assertEqual(quest_binding('target'),self.entry)
        self.assertEqual(self.entry['phase'],3)

    def test_unknown_or_unsupported_phase_is_rejected(self):
        for key in ['unknown@phase=1','target@phase=0','target@phase=4',
                    'target@phase=-1','target@phase=01','target@phase=1@phase=2']:
            with self.subTest(key=key):self.assertIsNone(quest_binding(key))
        self.entry.pop('phases')
        self.assertIsNone(quest_binding('target@phase=1'))

    def test_laplace_loads_the_explicit_phase_enemy_data(self):
        data=MagicMock()
        data.get.return_value={'id':93040401,'phase':1,'stages':[{'enemies':[{'hp':100}]}]}
        entry,quest,region=LaplaceService(data=data)._load_quest('target@phase=1',None)
        data.get.assert_called_once_with('CN','quest',93040401,1)
        self.assertEqual((entry['phase'],quest['phase'],region),(1,1,'CN'))


if __name__=='__main__':unittest.main()
