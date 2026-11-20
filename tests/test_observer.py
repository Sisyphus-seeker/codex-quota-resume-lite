import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from quota_resume.model import Quota
from quota_resume.observer import Observer, is_quota
from quota_resume.protocol import Protocol, QueueSubmission
from quota_resume.readonly import Unavailable
from quota_resume.simulation import uid
from quota_resume.live import LiveAdapter
from quota_resume.core import NotStarted


class Home:
    def __init__(self, root):
        self.root = Path(root)
        self.thread, self.turn = uid(1), uid(2)
        self.file = self.root/'sessions'/('rollout-'+self.thread+'.jsonl')
        self.file.parent.mkdir()
        self.write_meta()
        self.state = sqlite3.connect(self.root/'state_5.sqlite')
        self.history = sqlite3.connect(self.root/'thread_history_1.sqlite')
        self.queue = sqlite3.connect(self.root/'queue_1.sqlite')
        self.state.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,source TEXT,thread_source TEXT,archived INTEGER,history_mode TEXT)')
        self.state.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)', (self.thread,str(self.file),'vscode','user',0,'paginated'))
        self.history.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT,started_at REAL,completed_at REAL,rollout_ordinal INTEGER,first_user_item_id TEXT)')
        self.history.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?)',
            (self.thread,self.turn,'failed',json.dumps({'codexErrorInfo':'usageLimitExceeded'}),1000,1100,1,None))
        self.history.execute('CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_type TEXT,item_json TEXT)')
        self.history.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER)')
        self.history.execute('INSERT INTO thread_history_projection_state VALUES (?,?)',(self.thread,self.file.stat().st_size))
        self.queue.execute('CREATE TABLE queued_items(id TEXT,thread_id TEXT,payload_json TEXT)')
        self.commit()

    def write_meta(self):
        self.file.write_text(json.dumps({'type':'session_meta','payload':{'id':self.thread,'source':'vscode',
            'originator':'Codex Desktop','thread_source':'user'}})+'\n', encoding='utf-8')

    def commit(self):
        for db in (self.state,self.history,self.queue): db.commit()

    def close(self):
        for db in (self.state,self.history,self.queue): db.close()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Home(folder.name)
        self.addCleanup(self.home.close)
        self.observer = Observer(folder.name, lambda thread: True)

    def job(self):
        return {'thread_id':self.home.thread,'turn_id':self.home.turn,'marker':'[quota-lite:test]'}

    def enqueue(self, payload=None):
        payload = payload or {'UserInput':{'content':[{'type':'text','text':self.job()['marker']}]}}
        self.home.queue.execute('INSERT INTO queued_items VALUES (?,?,?)',(uid(3),self.home.thread,json.dumps(payload)))
        self.home.queue.commit()

    def test_latest_desktop_quota_failure_is_read_without_mutating_files(self):
        before = {p.name:p.read_bytes() for p in self.home.root.glob('*.sqlite')}
        failures = self.observer.failures(1000)
        self.assertEqual([(f.thread_id,f.turn_id) for f in failures],[(uid(1),uid(2))])
        self.assertTrue(self.observer.inspect(uid(1)).fresh)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.home.root.glob('*.sqlite')})

    def test_later_user_turn_means_old_failure_is_not_admitted(self):
        self.home.history.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?)',
            (uid(1),uid(10),'inProgress',None,1200,None,2,None))
        self.home.history.commit()
        self.assertEqual(self.observer.failures(1000),[])

    @unittest.skipUnless(os.name == 'nt', 'Windows extended drive path')
    def test_extended_windows_rollout_path_is_accepted(self):
        self.home.state.execute('UPDATE threads SET rollout_path=?', ('\\\\?\\'+str(self.home.file.resolve()),))
        self.home.state.commit()
        self.assertEqual(len(self.observer.failures(1000)),1)

    def test_rollout_outside_sessions_is_rejected(self):
        outside=self.home.root/'outside.jsonl'
        outside.write_bytes(self.home.file.read_bytes())
        self.home.state.execute('UPDATE threads SET rollout_path=?',(str(outside),))
        self.home.state.commit()
        self.assertEqual(self.observer.failures(1000),[])

    def test_projection_behind_rollout_blocks_admission(self):
        with self.home.file.open('a',encoding='utf-8') as stream: stream.write('{}\n')
        self.assertEqual(self.observer.failures(1000),[])
        self.assertFalse(self.observer.inspect(uid(1)).fresh)

    def test_unknown_new_database_generation_does_not_fall_back(self):
        sqlite3.connect(self.home.root/'state_6.sqlite').close()
        with self.assertRaises(Unavailable): self.observer.validate()

    def test_subagent_meta_is_rejected(self):
        meta = json.loads(self.home.file.read_text())
        meta['payload']['thread_source']='subagent'
        self.home.file.write_text(json.dumps(meta)+'\n',encoding='utf-8')
        self.assertEqual(self.observer.failures(1000),[])

    def test_codemoss_import_requires_verified_desktop_ownership(self):
        meta=json.loads(self.home.file.read_text())
        meta['payload'].update(originator='codemoss_intellij',thread_source=None)
        self.home.file.write_text(json.dumps(meta)+'\n',encoding='utf-8')
        self.home.state.execute('UPDATE threads SET thread_source=NULL')
        self.home.history.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?',
                                  (self.home.file.stat().st_size,))
        self.home.commit()
        for loaded in (False,None,True):
            self.observer.loaded=lambda _, value=loaded:value
            self.assertEqual(len(self.observer.failures(1000)),1 if loaded is True else 0)

    def test_false_marker_in_other_json_field_is_not_owned(self):
        self.enqueue({'UserInput':{'content':[], 'other':self.job()['marker']}})
        self.assertFalse(self.observer.owns_queue(self.job(),uid(3)))
        self.assertEqual(self.observer.evidence(self.job())['kind'],'unknown')

    def test_own_queue_is_excluded_from_foreign_input_and_can_be_proven(self):
        self.enqueue()
        evidence = self.observer.evidence(self.job())
        self.assertEqual(evidence['kind'],'queued')
        self.assertFalse(evidence['withdraw'])
        self.assertTrue(self.observer.owns_queue(self.job(),uid(3)))

    def test_foreign_queue_makes_own_queue_withdrawable(self):
        self.enqueue()
        self.home.queue.execute('INSERT INTO queued_items VALUES (?,?,?)',(uid(8),uid(1),'{}'))
        self.home.queue.commit()
        self.assertTrue(self.observer.evidence(self.job())['withdraw'])

    def test_transient_projection_or_unloaded_chat_does_not_withdraw_own_queue(self):
        self.enqueue()
        self.observer.loaded=lambda _:False
        self.assertFalse(self.observer.evidence(self.job())['withdraw'])
        with self.home.file.open('a',encoding='utf-8') as stream: stream.write('{}\n')
        self.assertFalse(self.observer.evidence(self.job())['withdraw'])

    def test_history_requires_marker_as_first_user_message_not_latest_turn_guess(self):
        self.home.history.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?)',
            (uid(1),uid(9),'inProgress',None,1200,None,2,uid(5)))
        self.home.history.execute('INSERT INTO thread_items VALUES (?,?,?,?,?)',
            (uid(1),uid(9),uid(5),'userMessage',json.dumps({'type':'userMessage','content':[{'type':'text','text':self.job()['marker']}]})))
        self.home.history.commit()
        self.assertEqual(self.observer.evidence(self.job())['kind'],'delivered')

    def test_current_split_queries_leaf_but_submits_to_root(self):
        leaf=uid(20)
        changed=self.home.file.with_stem(self.home.file.stem+'_'+leaf)
        self.home.file.rename(changed)
        self.home.state.execute('UPDATE threads SET rollout_path=?',(str(changed),))
        self.home.history.execute('UPDATE thread_turns SET thread_id=?',(leaf,))
        self.home.history.execute('UPDATE thread_history_projection_state SET thread_id=?',(leaf,))
        self.home.commit()
        self.assertEqual(self.observer.failures(1000)[0].thread_id,uid(1))
        self.assertEqual(self.observer.inspect(uid(1)).latest_turn,uid(2))

    def test_textual_limits_and_http_429_are_not_quota_codes(self):
        for error in ({'message':'quota exceeded'}, {'codexErrorInfo':'rateLimitExceeded'},
                      {'codexErrorInfo':{'httpStatusCode':429}}):
            self.assertFalse(is_quota(json.dumps(error)))


class ProtocolTests(unittest.TestCase):
    def test_reverse_requests_are_rejected_without_servicing_tools(self):
        helper=Protocol('unused','.')
        class Process:
            stdin=io.StringIO()
            stdout=io.StringIO(json.dumps({'id':2,'method':'item/commandExecution/requestApproval'})+'\n')
        helper.process=Process()
        helper._read()
        reply=json.loads(helper.process.stdin.getvalue())
        self.assertEqual(reply['error']['code'],-32601)

    def test_thread_start_and_authentication_methods_are_forbidden(self):
        helper=Protocol('unused','.')
        for name in ('thread/start','thread/resume','turn/start','account/login/start'):
            with self.assertRaises(ValueError): helper.call(name)

    def test_readonly_adapter_refuses_any_real_send_before_external_action(self):
        with patch('quota_resume.live.old_plugin_blocks', return_value=False):
            adapter=LiveAdapter('.')
            with self.assertRaises(NotStarted): adapter.start(uid(1),'continue','marker')

    def test_old_plugin_conflict_refuses_send(self):
        with patch('quota_resume.live.old_plugin_blocks', return_value=True):
            adapter=LiveAdapter('.',send_allowed=lambda:True)
            with self.assertRaises(NotStarted): adapter.start(uid(1),'continue','marker')


if __name__=='__main__': unittest.main()
