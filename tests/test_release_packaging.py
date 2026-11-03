import tempfile
import unittest
from pathlib import Path
import zipfile

from scripts.package_release import inventory,package


class TestPublicRelease(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.app=self.root/'app';self.app.mkdir()
        for relative in ['FGO-MAA.exe','src/main.py','web/index.html','config/settings.yaml',
                         'data/servant_names.json','runtime/laplace/sources/source.txt']:
            target=self.app/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('safe')

    def test_personal_state_keys_and_duplicate_assets_never_enter_public_zip(self):
        private=['data/user_account.json','data/certs/server.key','.app_profile/account.json',
                 '_internal/data/user_account.json','_internal/runtime/private.log','src/__pycache__/a.pyc']
        for relative in private:
            target=self.app/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('private')
        archive=self.root/'release.zip';package(self.app,archive,'test')
        with zipfile.ZipFile(archive) as data:
            self.assertFalse(any('FGO-MAA/'+p in data.namelist() for p in private))
            self.assertIn('FGO-MAA/runtime/laplace/sources/source.txt',data.namelist())
            self.assertIn('FGO-MAA/THIRD_PARTY_NOTICES.md',data.namelist())
        self.assertTrue(archive.with_name('release.zip.sha256').exists())

    def test_embedded_private_key_in_text_aborts_packaging(self):
        (self.app/'src/main.py').write_text('-----BEGIN '+'PRIVATE KEY-----\nnot-a-real-key')
        with self.assertRaises(ValueError):inventory(self.app)

    def test_nested_archive_cannot_smuggle_account_or_keys(self):
        path=self.app/'runtime/laplace/sources/source.zip'
        with zipfile.ZipFile(path,'w') as data:data.writestr('data/user_account.json','private')
        with self.assertRaises(ValueError):inventory(self.app)

    def test_incomplete_runtime_is_rejected(self):
        with self.assertRaises(ValueError):inventory(self.root/'missing')

    def test_existing_release_is_not_overwritten(self):
        target=self.root/'release.zip';target.write_bytes(b'original')
        with self.assertRaises(FileExistsError):package(self.app,target,'test')
        self.assertEqual(target.read_bytes(),b'original')


if __name__=='__main__':unittest.main()
