import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from quota_resume.windows import official_engine, desktop
from quota_resume.readonly import Unavailable


@unittest.skipUnless(os.name=='nt', 'Windows paths')
class EngineTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.patch=patch.dict(os.environ, {'ProgramW6432':str(self.root/'Program Files'),
            'LOCALAPPDATA':str(self.root/'Local')});self.patch.start();self.addCleanup(self.patch.stop)

    def file(self,path):
        path.parent.mkdir(parents=True,exist_ok=True);path.touch();return path

    def test_bundled_and_extracted_official_locations_are_supported(self):
        for path in (self.root/'Program Files/WindowsApps/OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0/app/resources/codex.exe',
                     self.root/'Local/OpenAI/Codex/bin/12345678/codex.exe'):
            self.assertEqual(official_engine(self.file(path)),path.resolve())

    def test_arbitrary_resource_location_or_other_publisher_is_rejected(self):
        for path in (self.root/'app/resources/codex.exe',
                     self.root/'Program Files/WindowsApps/OpenAI.Codex_26.1111.2228.0_x64__otherpublisher/app/resources/codex.exe'):
            self.file(path)
            with self.assertRaises(Unavailable):official_engine(path)

    def check_nested(self, mode='normal'):
        base=self.root/'Program Files/WindowsApps/OpenAI.Codex_26.1117.3838.0_x64__2p2nqsd0c76g0/app'
        app=base/'ChatGPT.exe';exe=self.file(base/'resources/codex.exe')
        rows=[dict(pid=1,parent=0,path=str(app)),
              dict(pid=2,parent=2 if mode=='cycle' else 1,path=str(self.root/'foreign.exe') if mode=='foreign' else str(app)),
              dict(pid=3,parent=2,path=str(exe))]
        if mode=='ambiguous':rows.append(dict(pid=4,parent=1,path=str(exe)))
        identities={r['pid']:dict(pid=r['pid'],created=r['pid'],path=r['path'].lower()) for r in rows}
        holders=[(3,3)]+([(4,4)] if mode=='ambiguous' else [])
        with ExitStack() as stack:
            stack.enter_context(patch('quota_resume.windows.inventory',return_value=rows))
            stack.enter_context(patch('quota_resume.windows.identity',side_effect=lambda pid:identities[pid]))
            stack.enter_context(patch('quota_resume.windows.generation',return_value=self.root/'db'))
            stack.enter_context(patch('quota_resume.windows.resource_users',return_value=holders))
            return desktop(self.root)

    def test_nested_trusted_utility_parent_is_supported(self):
        pair=self.check_nested()
        self.assertEqual(pair['server']['pid'],3)
        self.assertEqual([a['pid'] for a in pair['ancestors']],[2])

    def test_foreign_intermediate_parent_is_rejected(self):
        with self.assertRaises(Unavailable):self.check_nested('foreign')

    def test_cyclic_parent_chain_is_rejected(self):
        with self.assertRaises(Unavailable):self.check_nested('cycle')

    def test_two_database_holding_engines_remain_ambiguous(self):
        with self.assertRaises(Unavailable):self.check_nested('ambiguous')

    def test_bundled_engine_of_different_package_is_rejected_even_if_it_holds_database(self):
        base=self.root/'Program Files/WindowsApps'
        app=base/'OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0/app/ChatGPT.exe'
        exe=self.file(base/'OpenAI.Codex_26.9999.0_x64__2p2nqsd0c76g0/app/resources/codex.exe')
        rows=[dict(pid=1,parent=0,path=str(app)),dict(pid=2,parent=1,path=str(exe))]
        with patch('quota_resume.windows.inventory',return_value=rows), \
             patch('quota_resume.windows.identity',return_value=dict(pid=1,created=1,path=str(app).lower())), \
             patch('quota_resume.windows.generation',return_value=self.root/'db'), \
             patch('quota_resume.windows.resource_users',return_value=[(2,2)]):
            with self.assertRaisesRegex(Unavailable,'desktop_engine_not_identified'):desktop(self.root)
