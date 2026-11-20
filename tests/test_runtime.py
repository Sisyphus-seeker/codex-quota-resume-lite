import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from quota_resume.runtime import heartbeat, request_stop
from quota_resume.storage import Store

ROOT=Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def test_worker_single_instance_and_clean_stop_without_live_access(self):
        with tempfile.TemporaryDirectory(prefix='quota-lite-worker-test-') as folder:
            state=Path(folder)/'config/state.sqlite'
            store=Store(state,time.time()); store.pause(True); store.close()
            # A deliberately absent home ensures this test cannot contact a real chat.
            argv=[sys.executable,str(ROOT/'scripts/quota_resume.py'),'--state',str(state),
                  '--codex-home',str(Path(folder)/'absent-home'),'run','--interval','5']
            process=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and not heartbeat(state)['running']:
                    time.sleep(.1)
                self.assertTrue(heartbeat(state)['running'])
                duplicate=subprocess.run(argv+['--once'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                self.assertNotEqual(duplicate.returncode,0)
                self.assertIsNone(heartbeat(state)['last_error'])
                request_stop(state)
                self.assertEqual(process.wait(timeout=10),0)
                self.assertFalse(heartbeat(state)['running'])
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)


if __name__=='__main__': unittest.main()
