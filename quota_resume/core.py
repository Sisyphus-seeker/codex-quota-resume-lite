"""Clock-driven scheduler; all Codex IO belongs to the injected adapter."""
from __future__ import annotations

from typing import Protocol
from .model import Failure, Quota, View
from .storage import Store


class NotStarted(Exception):
    """Adapter can prove no external process/request was started."""


class Submission(Protocol):
    def receipt(self) -> str | None:
        """Return an exact queue receipt ID, or None if delivery is uncertain.

        A receipt proves acceptance into a queue, NOT that the task has resumed.
        """


class Adapter(Protocol):
    def failures(self, since: float) -> list[Failure]: ...
    def inspect(self, thread_id: str) -> View | None: ...
    def quota(self) -> Quota: ...
    def start(self, thread_id: str, message: str, marker: str) -> Submission:
        """Only launch here; wait for a receipt in Submission.receipt outside the lock."""


def problem(job, view):
    if view is None or view.fresh is not True:
        return "wait", "state_unknown"
    if (view.latest_turn != job["turn_id"] or view.status != "failed"
            or view.failure_kind != "usage_limit"):
        return "cancel", "task_changed"
    if view.queued_input is True:
        return "cancel", "user_input_queued"
    if view.queued_input is not False:
        return "wait", "input_unknown"
    if view.loaded is not True:
        return "wait", "chat_not_loaded"
    return None


class Engine:
    def __init__(self, store: Store, adapter: Adapter, *, observe_only=lambda: False):
        self.store, self.adapter = store, adapter
        self.observe_only = observe_only

    def tick(self, now: float):
        self._reconcile()
        if self.store.settings()["paused"]:
            return
        # Read failures for all desktop conversations; no per-chat enrollment.
        # An adapter read failure is allowed to propagate to its runner: no send.
        for failure in self.adapter.failures(self.store.settings()["active_since"]):
            self.store.admit(failure, now)
        for row in self.store.rows("waiting"):
            self._consider(row, now)

    def _inspect(self, row):
        try:
            return problem(row, self.adapter.inspect(row["thread_id"]))
        except Exception:
            return "wait", "state_unknown"

    def _consider(self, row, now):
        issue = self._inspect(row)
        if issue:
            action, reason = issue
            if action == "cancel":
                self.store.invalidate(row["id"], reason)
            else:
                self.store.wait(row["id"], reason, now + 60)
            return
        if row["next_check"] > now:
            return
        try:
            quota = self.adapter.quota()
        except Exception:
            quota = Quota(None)
        if quota.available is not True:
            next_at = max(now + 60, quota.reset_at + 30) if quota.reset_at else now + 60
            reason = "quota_blocked" if quota.available is False else "quota_unknown"
            if quota.reason == "billing":
                reason = "billing_blocked"
            self.store.wait(row["id"], reason, next_at)
            return
        if self.observe_only():
            self.store.wait(row["id"], "observe_only", now + 30)
            return
        if self.store.claim(row["id"], now):
            self._launch(row, now)

    def _launch(self, row, now):
        try:
            with self.store.launch_guard(row["id"]) as allowed:
                if not allowed or self.observe_only():
                    handle, state, reason = None, "waiting", "paused_before_launch"
                else:
                    # Re-read user activity after claiming, immediately before launch.
                    issue = self._inspect(row)
                    if issue:
                        action, why = issue
                        handle, state, reason = None, ("cancelled" if action == "cancel" else "waiting"), why
                    else:
                        message = self.store.settings()["message"]
                        handle = self.adapter.start(row["thread_id"], message, row["marker"])
                        state, reason = "queued", "accepted"
            if handle is None:
                if state == "waiting":
                    self.store.release_unsent(row["id"])
                    self.store.wait(row["id"], reason, now + 60)
                else:
                    self.store.finish(row["id"], state, reason)
                return
            # A pause can now commit without waiting for a transport response.
            receipt = handle.receipt()
            if not isinstance(receipt, str) or not receipt:
                self.store.finish(row["id"], "uncertain", "receipt_unknown")
            else:
                self.store.finish(row["id"], "queued", "accepted", receipt)
        except NotStarted:
            # The adapter guarantees no external request started. A transient
            # desktop/quota change must not permanently discard this recovery.
            self.store.release_unsent(row["id"])
            self.store.wait(row["id"], "send_not_started", now + 60)
        except Exception:
            # Including an exception AFTER launch. Exception text may contain secrets.
            self.store.finish(row["id"], "uncertain", "send_unknown")

    def _reconcile(self):
        if not hasattr(self.adapter, "evidence"):
            return
        for row in self.store.rows():
            if row["state"] not in {"queued", "uncertain"}:
                continue
            try:
                evidence = self.adapter.evidence(row)
                kind = evidence.get("kind")
                if kind == "delivered":
                    self.store.reconcile(row["id"], "delivered", "marker_in_history")
                elif kind == "queued":
                    receipt = evidence["queue_id"]
                    settings = self.store.settings()
                    withdraw = (settings["paused"] or self.observe_only()
                                or evidence.get("withdraw") is not False)
                    if withdraw:
                        # Adapter re-proves ownership before deleting exactly its own item.
                        deleted = self.adapter.withdraw(row, receipt)
                        self.store.reconcile(row["id"], "cancelled" if deleted else "uncertain",
                                             "queue_withdrawn" if deleted else "withdraw_unconfirmed", receipt)
                    else:
                        self.store.reconcile(row["id"], "queued", "accepted", receipt)
                elif kind == "absent":
                    self.store.reconcile(row["id"], "uncertain", "delivery_unconfirmed")
            except Exception:
                # Unknown observation is not permission to resend or delete foreign input.
                continue
