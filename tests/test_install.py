import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
from quota_resume.install import install, uninstall, verified_payload, guarded_tree, task_action
from quota_resume.storage import Store
from quota_resume.runtime import heartbeat, request_stop

ARCHIVE=Path(__file__).resolve().parents[1]/'dist/QuotaResumeLite-v0.1.2-win-x64.zip'


@unittest.skipUnless(ARCHIVE.is_file(), 'Build release ZIP first')
class InstallTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(prefix='quota-lite-install-test-')
        self.addCleanup(folder.cleanup)
        self.base=Path(folder.name)
        self.payload=self.base/'payload'
        with zipfile.ZipFile(ARCHIVE) as archive: archive.extractall(self.payload)
        self.root=self.base/'isolated-install'

    def test_install_update_uninstall_preserves_settings_and_other_files(self):
        install(self.payload/'app',self.root,register=False)
        state=self.root/'config/state.sqlite'
        store=Store(state,time.time())
        self.assertTrue(store.settings()['paused'])
        self.assertFalse(store.settings()['live_enabled'])
        store.message('configured test message');store.close()
        (self.root/'keep.txt').write_text('unrelated')
        install(self.payload/'app',self.root,register=False)
        store=Store(state,time.time());self.assertEqual(store.settings()['message'],'configured test message');store.close()
        uninstall(self.root,register=False)
        self.assertTrue(state.is_file());self.assertTrue((self.root/'keep.txt').is_file())
        self.assertFalse((self.root/'runtime').exists());self.assertFalse((self.root/'app').exists())
        uninstall(self.root,register=False,purge=True)
        self.assertFalse(state.exists());self.assertTrue((self.root/'keep.txt').is_file())

    def test_unknown_directory_and_outside_delete_are_refused(self):
        self.root.mkdir();(self.root/'keep.txt').write_text('x')
        with self.assertRaises(ValueError): install(self.payload/'app',self.root,register=False)
        with self.assertRaises(ValueError): guarded_tree(self.root,self.base)

    def test_altered_or_extra_release_file_is_rejected(self):
        target=self.payload/'app/quota_resume/core.py'; original=target.read_bytes()
        target.write_bytes(original+b'\n#changed')
        with self.assertRaises(ValueError): verified_payload(self.payload/'app')
        target.write_bytes(original)
        (self.payload/'app/extra.py').write_text('x')
        with self.assertRaises(ValueError): verified_payload(self.payload/'app')


if __name__=='__main__': unittest.main()
