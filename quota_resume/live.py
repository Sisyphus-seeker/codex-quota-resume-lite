"""Live adapter. Construction, doctor and observation cannot send any message."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import time
from .core import NotStarted
from .model import identifier, parse_quota
from .observer import Observer
from .protocol import Protocol, QueueSubmission, arguments, environment
from .readonly import Unavailable
from . import windows


def old_plugin_blocks(profile=None):
    root = Path(profile or Path.home()) / ".codex-auto-resume"
    if not root.exists():
        return False
    try:
        data = json.loads((root / "config/settings.json").read_text(encoding="utf-8"))
        if data.get("observe_only") is True:
            return False
    except Exception:
        pass
    import sqlite3
    path = root / "config/state.sqlite"
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            row = db.execute("SELECT enabled,observe_only FROM settings").fetchone()
            return not (row and (row[0] == 0 or row[1] == 1))
    except Exception:
        return True


class LiveAdapter:
    def __init__(self, home, *, send_allowed=lambda: False):
        self.home = Path(home).resolve()
        self.send_allowed = send_allowed
        self.observer = Observer(self.home, self._loaded)
        self.pair = None
        self.last_quota = None
        self.expected_turns = {}

    def _pair(self):
        # Do not cache process inventory across a scheduling tick or a send.
        self.pair = windows.desktop(self.home)
        return self.pair

    def _loaded(self, thread):
        return windows.loaded(self.home, identifier(thread), self._pair())

    def failures(self, since):
        self._pair()  # The correct desktop must be running on this Codex home.
        return self.observer.failures(since)

    def inspect(self, thread):
        view = self.observer.inspect(thread)
        if view is not None:
            self.expected_turns[thread] = view.latest_turn
        return view

    def quota(self):
        pair = self._pair()
        with Protocol(pair["exe"], self.home) as helper:
            snapshot = parse_quota(helper.call("account/rateLimits/read"))
            self.last_quota = (time.monotonic(), snapshot)
            return snapshot

    def start(self, thread_id, message, marker):
        identifier(thread_id)
        if not self.send_allowed() or old_plugin_blocks():
            raise NotStarted("sending_disabled_or_old_plugin_active")
        if self.observer.marker_exists(thread_id, marker):
            raise NotStarted("marker_already_present")
        pair = self._pair()
        if windows.loaded(self.home, thread_id, pair) is not True:
            raise NotStarted("chat_not_loaded")
        text = message + "\n\n" + marker
        if not 0 < len(text) <= 8192 or "\0" in text:
            raise NotStarted("invalid_message")
        # The scheduler just queried quota, outside the launch lock. Never block
        # Pause behind a network round-trip; discard a snapshot older than 30s.
        if (self.last_quota is None or not 0 <= time.monotonic() - self.last_quota[0] <= 30
                or self.last_quota[1].available is not True):
            raise NotStarted("quota_not_available")
        if not self.send_allowed():
            raise NotStarted("sending_disabled")
        if windows.loaded(self.home, thread_id, pair) is not True:
            raise NotStarted("desktop_changed")
        # Re-read history/queued input after potentially slow process checks.
        view = self.observer.inspect(thread_id)
        if (view is None or not view.fresh or view.queued_input is not False or view.loaded is not True
                or view.latest_turn != self.expected_turns.get(thread_id) or view.status != "failed"
                or view.failure_kind != "usage_limit"):
            raise NotStarted("state_changed")
        try:
            process = subprocess.Popen(arguments(pair["exe"]) + ["queue", "--thread", thread_id, "--message=" + text],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="strict", cwd=self.home, env=environment(self.home),
                creationflags=windows.NO_WINDOW, shell=False, close_fds=True)
        except OSError:
            raise NotStarted("queue_not_started") from None
        return QueueSubmission(process, thread_id)

    def evidence(self, job):
        return self.observer.evidence(job)

    def withdraw(self, job, receipt):
        if not self.observer.owns_queue(job, receipt):
            return False
        pair = self._pair()
        with Protocol(pair["exe"], self.home) as helper:
            reply = helper.call("thread/queue/delete", {"threadId": job["thread_id"],
                                                       "queuedSubmissionId": receipt})
        return isinstance(reply, dict) and reply.get("deleted") is True

    def doctor(self, probe_usage=False):
        self.observer.validate()
        pair = self._pair()
        result = subprocess.run([str(pair["exe"]), "queue", "--help"], capture_output=True, encoding="utf-8",
                                timeout=15, creationflags=windows.NO_WINDOW, shell=False)
        flags = result.returncode == 0 and "--thread" in result.stdout and "--message" in result.stdout
        if not flags:
            raise Unavailable("queue_interface_changed")
        report = {"schema": "supported", "desktop": "identified", "queue_interface": "supported",
                  "old_plugin_blocks": old_plugin_blocks(), "loaded_check": "restart_manager",
                  "live_test": "not_performed"}
        if probe_usage:
            snapshot = self.quota()
            report["quota"] = {"available": snapshot.available, "reset_at": snapshot.reset_at,
                               "reason": snapshot.reason}
        return report
