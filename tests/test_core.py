from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from quota_resume.cli import main
from quota_resume.core import Engine, NotStarted
from quota_resume.model import Failure, Quota, View, parse_quota
from quota_resume.simulation import SimAdapter, SimSubmission, demo, uid
from quota_resume.storage import Store


class CoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.sqlite"
        self.store = Store(self.path, 1000)
        self.addCleanup(self.store.close)
        self.adapter = SimAdapter()
        self.failure = Failure(uid(1), uid(2), 1100)
        self.adapter.items = [self.failure]
        self.adapter.views[uid(1)] = View(uid(2))
        self.adapter.current_quota = Quota(True)
        self.engine = Engine(self.store, self.adapter)

    def row(self):
        return self.store.rows()[0]

    def test_available_sends_original_thread_and_global_message(self):
        self.store.message("继续此任务\n保留已有修改")
        self.engine.tick(1100)
        self.assertEqual(self.adapter.sent[0]["thread_id"], uid(1))
        self.assertEqual(self.adapter.sent[0]["message"], "继续此任务\n保留已有修改")
        self.assertEqual(self.row()["state"], "queued")
        self.assertIsNotNone(self.row()["receipt_id"])

    def test_reset_timestamp_and_buffer_are_waited_for(self):
        self.adapter.current_quota = Quota(False, 1200, "quota")
        self.engine.tick(1100)
        self.assertEqual(self.row()["next_check"], 1230)
        self.adapter.current_quota = Quota(True)
        self.engine.tick(1229)
        self.assertEqual(self.adapter.sent, [])
        self.engine.tick(1230)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_past_reset_with_still_blocked_quota_does_not_send(self):
        self.adapter.current_quota = Quota(False, 1050, "quota")
        self.engine.tick(1100)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.row()["next_check"], 1160)

    def test_quota_exception_waits_and_discards_exception_text(self):
        def broken():
            raise RuntimeError("secret-api-key")
        self.adapter.quota = broken
        self.engine.tick(1100)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.row()["reason"], "quota_unknown")
        self.assertNotIn("secret-api-key", str(self.store.rows()))

    def test_billing_block_is_not_mistaken_for_a_reset(self):
        self.adapter.current_quota = Quota(False, reason="billing")
        self.engine.tick(1100)
        self.assertEqual(self.row()["reason"], "billing_blocked")
        self.assertEqual(self.adapter.sent, [])

    def test_user_continued_or_completed_task_is_cancelled(self):
        for index, view in enumerate((View(uid(99)), View(uid(2), status="completed"),
                                     View(uid(2), status="interrupted"), View(uid(2), queued_input=True))):
            with self.subTest(view=view):
                other = Store(self.path.parent / ("case-" + str(index)) / "test.sqlite", 1000)
                self.adapter.views[uid(1)] = view
                try:
                    Engine(other, self.adapter).tick(1100)
                    self.assertEqual(other.rows()[0]["state"], "cancelled")
                    self.assertEqual(self.adapter.sent, [])
                finally:
                    other.close()

    def test_unknown_or_unloaded_view_waits(self):
        self.adapter.views[uid(1)] = View(uid(2), loaded=False)
        self.engine.tick(1100)
        self.assertEqual(self.row()["reason"], "chat_not_loaded")
        self.adapter.views[uid(1)] = View(uid(2), fresh=None)
        self.engine.tick(1160)
        self.assertEqual(self.row()["reason"], "state_unknown")
        self.assertEqual(self.adapter.sent, [])

    def test_old_nonquota_and_nondesktop_failures_are_ignored(self):
        self.adapter.items = [replace(self.failure, at=999),
                              replace(self.failure, turn_id=uid(3), kind="network"),
                              replace(self.failure, turn_id=uid(4), origin="cli")]
        self.engine.tick(1100)
        self.assertEqual(self.store.rows(), [])

    def test_pause_then_resume_retains_waiting_work(self):
        self.store.pause(True)
        self.engine.tick(1100)
        self.assertEqual(self.adapter.sent, [])
        self.store.pause(False)
        self.engine.tick(1101)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_repeated_scan_does_not_repeat_submission(self):
        for now in (1100, 1200, 1300, 5000):
            self.engine.tick(now)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(len(self.store.rows()), 1)

    def test_unknown_receipt_is_not_retried_after_restart(self):
        self.adapter.uncertain = True
        self.engine.tick(1100)
        reopened = Store(self.path, 2000)
        try:
            reopened.recover_crash()
            Engine(reopened, self.adapter).tick(2000)
            self.assertEqual(reopened.rows()[0]["state"], "uncertain")
            self.assertEqual(len(self.adapter.sent), 1)
        finally:
            reopened.close()

    def test_newer_failure_of_same_chat_can_be_recovered(self):
        self.engine.tick(1100)
        self.adapter.items.append(Failure(uid(1), uid(3), 1200))
        self.adapter.views[uid(1)] = View(uid(3))
        self.engine.tick(1200)
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertNotEqual(self.adapter.sent[0]["marker"], self.adapter.sent[1]["marker"])

    def test_send_failure_after_launch_is_uncertain(self):
        def interrupted(*args):
            self.adapter.sent.append(args)
            raise TimeoutError("private details")
        self.adapter.start = interrupted
        self.engine.tick(1100)
        self.engine.tick(1200)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.row()["state"], "uncertain")

    def test_proven_not_started_retries_after_backoff_without_duplicate_launch(self):
        original = self.adapter.start
        def refused(*args):
            raise NotStarted()
        self.adapter.start = refused
        self.engine.tick(1100)
        self.assertEqual(self.row()["state"], "waiting")
        self.assertEqual(self.row()["reason"], "send_not_started")
        self.adapter.start = original
        self.engine.tick(1159)
        self.assertEqual(self.adapter.sent, [])
        self.engine.tick(1160)
        self.assertEqual(len(self.adapter.sent), 1)
        self.engine.tick(1200)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_pause_committed_after_claim_prevents_launch(self):
        claim = self.store.claim
        def pause_after_claim(key, now):
            result = claim(key, now)
            self.store.pause(True)
            return result
        self.store.claim = pause_after_claim
        self.engine.tick(1100)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.row()["state"], "waiting")

    def test_new_user_input_between_claim_and_launch_is_rechecked(self):
        claim = self.store.claim
        def user_after_claim(key, now):
            result = claim(key, now)
            self.adapter.views[uid(1)] = View(uid(2), queued_input=True)
            return result
        self.store.claim = user_after_claim
        self.engine.tick(1100)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.row()["state"], "cancelled")

    def test_receipt_wait_does_not_hold_pause_lock(self):
        class PausingReceipt:
            def receipt(inner):
                other = Store(self.path, 1100)
                try:
                    other.pause(True)
                finally:
                    other.close()
                return uid(100)
        self.adapter.start = lambda *args: PausingReceipt()
        self.engine.tick(1100)
        self.assertTrue(self.store.settings()["paused"])
        self.assertEqual(self.row()["state"], "queued")

    def test_abandoned_durable_claim_becomes_uncertain_not_waiting(self):
        self.store.admit(self.failure, 1100)
        self.assertTrue(self.store.claim(self.row()["id"], 1100))
        self.store.recover_crash()
        self.engine.tick(1200)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.row()["state"], "uncertain")

    def test_two_workers_have_only_one_claimant(self):
        barrier = threading.Barrier(2)
        errors = []
        def run():
            own = Store(self.path, 1100)
            try:
                barrier.wait(timeout=5)
                Engine(own, self.adapter).tick(1100)
            except Exception as exc:
                errors.append(exc)
            finally:
                own.close()
        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.adapter.sent), 1)

    def test_global_message_change_applies_to_unsent_work(self):
        self.adapter.current_quota = Quota(False, 1200)
        self.engine.tick(1100)
        self.store.message("新消息")
        self.adapter.current_quota = Quota(True)
        self.engine.tick(1230)
        self.assertEqual(self.adapter.sent[0]["message"], "新消息")

    def test_chat_becomes_loaded_then_can_submit(self):
        self.adapter.views[uid(1)] = View(uid(2), loaded=False)
        self.engine.tick(1100)
        self.adapter.views[uid(1)] = View(uid(2), loaded=True)
        self.engine.tick(1160)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_observer_failure_never_sends_previously_waiting_job(self):
        self.adapter.current_quota = Quota(False, 1200)
        self.engine.tick(1100)
        self.adapter.current_quota = Quota(True)
        def unreadable(since):
            raise OSError("observer offline")
        self.adapter.failures = unreadable
        with self.assertRaises(OSError):
            self.engine.tick(1230)
        self.assertEqual(self.adapter.sent, [])


class QuotaTests(unittest.TestCase):
    def reply(self, primary, secondary=None):
        return {"rateLimits": {"primary": primary, "secondary": secondary}}

    def test_five_hour_and_weekly_reset_waits_for_later_window(self):
        result = parse_quota(self.reply({"usedPercent": 100, "resetsAt": 1200},
                                        {"usedPercent": 100, "resetsAt": 9000}))
        self.assertIs(result.available, False)
        self.assertEqual(result.reset_at, 9000)

    def test_multiple_buckets_and_missing_reset(self):
        result = parse_quota({"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 10}},
            "other": {"primary": {"usedPercent": 100}},
        }})
        self.assertIs(result.available, False)
        self.assertIsNone(result.reset_at)

    def test_malformed_or_absent_usage_does_not_authorize_sending(self):
        for value in (None, {}, self.reply({"usedPercent": True}),
                      self.reply({"usedPercent": float("nan")}),
                      self.reply({"usedPercent": -1}),
                      self.reply({"usedPercent": 10, "resetsAt": float("inf")})):
            with self.subTest(value=value):
                self.assertIsNone(parse_quota(value).available)

    def test_server_reported_limit_overrides_available_percent(self):
        value = self.reply({"usedPercent": 20})
        value["rateLimits"]["rateLimitReachedType"] = "rate_limit_reached"
        self.assertIs(parse_quota(value).available, False)

    def test_available_snapshot(self):
        self.assertIs(parse_quota(self.reply({"usedPercent": 99})).available, True)

    def test_billing_is_separate_from_temporarily_exhausted_quota(self):
        value = self.reply({"usedPercent": 0})
        value["rateLimits"]["rateLimitReachedType"] = "workspace_owner_credits_depleted"
        self.assertEqual(parse_quota(value).reason, "billing")


class EntryTests(unittest.TestCase):
    def test_demo_has_two_queued_tasks_one_cancelled_and_no_repeat(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / "demo.sqlite", 1000)
            try:
                result = demo(store)
                self.assertEqual(result["simulated_sends"], 2)
                self.assertEqual([r["state"] for r in store.rows()], ["queued", "queued", "cancelled"])
                self.assertEqual(result["frames"][-1]["simulated_sends"], 2)
            finally:
                store.close()

    def test_cli_status_does_not_expose_message_and_is_explicitly_simulation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.sqlite"
            store = Store(path, 1000)
            store.message("a-private-message")
            store.close()
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--state", str(path), "status"])
            self.assertNotIn("a-private-message", output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["live_sending"])

    def test_plugin_entrypoint_works_outside_project_directory(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "quota_resume.py"
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([sys.executable, str(script), "demo", "--uncertain"],
                                    cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["simulated_sends"], 2)
        self.assertEqual(output["frames"][-1]["tasks"][0]["state"], "uncertain")

    def test_invalid_global_message_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / "state.sqlite", 1000)
            try:
                for text in ("", "   ", "a\0b", "a" * 2001):
                    with self.assertRaises(ValueError):
                        store.message(text)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
