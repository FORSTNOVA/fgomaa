import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from src.server.battle_assets import atlas_url, cached_image, visual_metadata


BASE = 'https://static.atlasacademy.io/'


class TestBattleAssets(unittest.TestCase):
    def test_rejects_external_hosts_redirect_targets_and_non_images(self):
        for url in ['http://static.atlasacademy.io/JP/Faces/a.png',
                    'https://static.atlasacademy.io.evil.test/JP/a.png',
                    'https://static.atlasacademy.io@127.0.0.1/JP/a.png',
                    BASE+'JP/../private.png', BASE+'JP/a.png?target=private',
                    BASE+'JP/a.json', 'file:///private.png', None]:
            with self.subTest(url=url), patch('src.server.battle_assets.requests.get') as get:
                self.assertEqual(atlas_url(url), '')
                with self.assertRaises(ValueError):cached_image(url)
                get.assert_not_called()

    def test_images_are_validated_cached_and_not_downloaded_again(self):
        out=io.BytesIO();Image.new('RGBA',(4,4)).save(out,format='PNG')
        response=MagicMock(status_code=200);response.iter_content.return_value=[out.getvalue()]
        response.__enter__.return_value=response
        with tempfile.TemporaryDirectory() as folder, patch('src.server.battle_assets.requests.get',return_value=response) as get:
            self.assertEqual(cached_image(BASE+'JP/Faces/a.png',folder),out.getvalue())
            self.assertEqual(cached_image(BASE+'JP/Faces/a.png',folder),out.getvalue())
            self.assertEqual(get.call_count,1)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_bad_content_and_redirects_do_not_enter_cache(self):
        for status,content in [(302,b''),(200,b'<html>failure</html>')]:
            response=MagicMock(status_code=status);response.iter_content.return_value=[content]
            response.__enter__.return_value=response
            with tempfile.TemporaryDirectory() as folder, patch('src.server.battle_assets.requests.get',return_value=response):
                with self.assertRaises((ValueError,OSError)):cached_image(BASE+'JP/Faces/a.png',folder)
                self.assertEqual(list(Path(folder).glob('*')),[])

    def test_visuals_follow_frozen_slots_ascension_costume_and_skill_versions(self):
        svt={'id':1,'className':'saber','extraAssets':{
            'faces':{'ascension':{'1':BASE+'CN/Faces/1.png','3':BASE+'CN/Faces/3.png'},
                     'costume':{'9':BASE+'CN/Faces/9.png'}},
            'commands':{'ascension':{'1':BASE+'CN/Servants/Commands/1.png'}}},
            'noblePhantasms':[{'name':'蓝卡宝具','card':'1'},
                             {'name':'同名变色宝具','card':'1'}, {'name':'同名变色宝具','card':'2'}],
            'skills':[{'id':10,'icon':BASE+'CN/SkillIcons/old.png'},
                      {'id':11,'icon':BASE+'CN/SkillIcons/new.png'}]}
        snapshot={'payload':{'request':{'team':[
            {'svt_id':1,'party_slot':1,'lv':80,'ascension':4,'skill_ids':[11]},
            {'svt_id':1,'party_slot':3,'lv':90,'costume_id':9,'is_friend':True,'skill_ids':[10]}],
            'mystic_code_id':20},'data':{'servants':[svt],
            'quest':{'stages':[{'wave':1,'enemies':[{'svt':{'id':2,'face':BASE+'JP/Enemys/2.png'}}]}]},
            'mystic_codes':[{'id':20,'skills':[{'icon':BASE+'CN/SkillIcons/master.png'}]}]}},
            'metadata':{'quest_name':'测试关卡'}}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'sessions').mkdir();file=root/'sessions'/('a'*32+'.json')
            file.write_text(json.dumps(snapshot),encoding='utf-8');before=file.read_bytes()
            result=visual_metadata(SimpleNamespace(root=root),'a'*32)
            self.assertEqual(result['players']['1']['face'],BASE+'CN/Faces/3.png')
            self.assertEqual(result['players']['3']['face'],BASE+'CN/Faces/9.png')
            self.assertTrue(result['players']['3']['support'])
            self.assertEqual(result['players']['1']['skills']['1'],BASE+'CN/SkillIcons/new.png')
            self.assertEqual(result['players']['1']['np_types'], {'蓝卡宝具':1})
            self.assertEqual(result['enemies']['1']['1']['face'],BASE+'JP/Enemys/2.png')
            self.assertEqual(result['master_skills']['1'],BASE+'CN/SkillIcons/master.png')
            self.assertEqual(file.read_bytes(),before)
            with self.assertRaises(ValueError):visual_metadata(SimpleNamespace(root=root),'../private')


if __name__=='__main__':unittest.main()
