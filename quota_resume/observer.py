"""Bounded reads of Codex metadata. No database, rollout or queue is modified."""
from __future__ import annotations
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
from .model import Failure, View, identifier, number
from .readonly import connect, Unavailable

STATE = {"threads": {"id", "rollout_path", "source", "thread_source", "archived", "history_mode"}}
HISTORY = {
    "thread_turns": {"thread_id", "turn_id", "status", "error_json", "started_at", "completed_at",
                     "rollout_ordinal", "first_user_item_id"},
    "thread_items": {"thread_id", "turn_id", "item_id", "item_type", "item_json"},
    "thread_history_projection_state": {"thread_id", "next_rollout_byte_offset"},
}
QUEUE = {"queued_items": {"id", "thread_id", "payload_json"}}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def resolved_local(path):
    """Compare ordinary and extended Windows drive paths after resolving links."""
    path = Path(path).resolve()
    value = str(path)
    if os.name == "nt" and re.match(r"^\\\\\?\\[a-zA-Z]:\\", value):
        return Path(value[4:])
    return path


def decode(text, limit=1024 * 1024):
    if not isinstance(text, str) or len(text) > limit:
        return None
    try:
        return json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        return None


def is_quota(raw):
    value = decode(raw)
    value = value.get("codexErrorInfo") if isinstance(value, dict) else None
    if value == "usageLimitExceeded":
        return True
    if isinstance(value, dict):
        return (any(value.get(k) == "usageLimitExceeded" for k in ("type", "kind", "tag", "variant", "code"))
                or list(value) == ["usageLimitExceeded"])
    return False


def has_text(value, marker, history=False):
    if not isinstance(value, dict):
        return False
    if history:
        if value.get("type") != "userMessage":
            return False
    else:
        if set(value) != {"UserInput"} or not isinstance(value["UserInput"], dict):
            return False
        value = value["UserInput"]
    content = value.get("content")
    return isinstance(content, list) and any(isinstance(v, dict) and v.get("type") == "text"
             and isinstance(v.get("text"), str) and marker in v["text"] for v in content)


class Observer:
    def __init__(self, home, loaded=lambda thread: None):
        self.home = Path(home).resolve()
        self.loaded = loaded
        self.skipped = 0

    def _open(self, stack):
        return tuple(stack.enter_context(connect(self.home, kind, schema)) for kind, schema in
                     (("state", STATE), ("thread_history", HISTORY), ("queue", QUEUE)))

    def _owner(self, state, root):
        identifier(root)
        row = state.execute("SELECT * FROM threads WHERE id=?", (root,)).fetchone()
        if (row is None or row["source"] != "vscode" or row["archived"] != 0
                or row["history_mode"] != "paginated" or row["thread_source"] not in {None, "user", "chatgpt_handoff"}):
            raise Unavailable("not_desktop_conversation")
        path = Path(row["rollout_path"])
        sessions = resolved_local(self.home / "sessions")
        if path.is_symlink() or not resolved_local(path).is_relative_to(sessions) or path.suffix != ".jsonl":
            raise Unavailable("rollout_path_invalid")
        match = re.fullmatch(r".*" + re.escape(root) + r"(?:_(" + UUID + r"))?", path.stem)
        if not match:
            raise Unavailable("unsupported_split")
        leaf = match[1] or root
        if leaf != root and state.execute("SELECT 1 FROM threads WHERE id=?", (leaf,)).fetchone():
            raise Unavailable("ambiguous_split")
        with path.open("rb") as stream:
            first = stream.readline(256 * 1024 + 1)
        if len(first) > 256 * 1024 or not first.endswith(b"\n"):
            raise Unavailable("session_meta_invalid")
        data = decode(first.decode("utf-8"), 256 * 1024)
        meta = data.get("payload") if isinstance(data, dict) and data.get("type") == "session_meta" else None
        if (not isinstance(meta, dict) or meta.get("id") != root
                or meta.get("thread_source") != row["thread_source"]):
            raise Unavailable("session_meta_mismatch")
        origin, source = meta.get("originator"), meta.get("source")
        if not ((origin in {"Codex Desktop", "codex_work_desktop"} and source == "vscode")
                or (origin == "codex-tui" and source in {"cli", "vscode"})
                or (origin == "codemoss_intellij" and source == "vscode" and self.loaded(root) is True)):
            raise Unavailable("not_desktop_session")
        return path, leaf

    def _current(self, state, history, root):
        path, leaf = self._owner(state, root)
        row = history.execute("SELECT * FROM thread_turns WHERE thread_id=? "
                              "ORDER BY rollout_ordinal DESC LIMIT 1", (leaf,)).fetchone()
        projection = history.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state "
                                     "WHERE thread_id=?", (leaf,)).fetchone()
        fresh = bool(projection and type(projection[0]) is int and projection[0] == path.stat().st_size)
        return row, fresh, leaf

    def validate(self):
        with ExitStack() as stack:
            self._open(stack)
        return True

    def failures(self, since):
        found = []
        self.skipped = 0
        with ExitStack() as stack:
            state, history, _ = self._open(stack)
            roots = state.execute("SELECT id FROM threads WHERE source='vscode' AND archived=0 "
                                  "AND history_mode='paginated'").fetchall()
            for (root,) in roots:
                try:
                    row, fresh, _ = self._current(state, history, root)
                    if row is None or not fresh or row["status"] != "failed" or not is_quota(row["error_json"]):
                        continue
                    start, end = row["started_at"], row["completed_at"]
                    if (not number(start) or not number(end) or not 0 < start <= end
                            or type(row["rollout_ordinal"]) is not int or row["rollout_ordinal"] < 0):
                        continue
                    if end >= since:
                        found.append(Failure(root, row["turn_id"], end))
                except (Unavailable, ValueError, OSError, UnicodeError):
                    self.skipped += 1
        return sorted(found, key=lambda x: (x.at, x.thread_id))

    def inspect(self, root, marker=None):
        with ExitStack() as stack:
            state, history, queue = self._open(stack)
            try:
                row, fresh, _ = self._current(state, history, root)
            except Unavailable as exc:
                if str(exc) == "not_desktop_conversation":
                    return View("", status="gone", loaded=False)
                raise
            if row is None:
                return None
            sql, params = "SELECT count(*) FROM queued_items WHERE thread_id=?", [root]
            if marker:
                sql += " AND instr(payload_json,?)=0"
                params.append(marker)
            count = queue.execute(sql, params).fetchone()[0]
            return View(row["turn_id"], row["status"], self.loaded(root), fresh, bool(count),
                        "usage_limit" if is_quota(row["error_json"]) else "other")

    def evidence(self, job):
        root, marker = job["thread_id"], job["marker"]
        with ExitStack() as stack:
            state, history, queue = self._open(stack)
            # Root, current split, and the failed turn's split. Never search all chat contents.
            _, _, leaf = self._current(state, history, root)
            ids = {root, leaf}
            ids.update(r[0] for r in history.execute("SELECT thread_id FROM thread_turns WHERE turn_id=?",
                                                   (job["turn_id"],)))
            hits = []
            for tid in ids:
                hits.extend(history.execute(
                    "SELECT i.item_json,t.first_user_item_id=i.item_id AS starts_turn "
                    "FROM thread_items i JOIN thread_turns t ON t.thread_id=i.thread_id AND t.turn_id=i.turn_id "
                    "WHERE i.thread_id=? AND i.item_type='userMessage' AND instr(i.item_json,?)>0",
                    (tid, marker)).fetchall())
            matching = [r for r in hits if has_text(decode(r["item_json"]), marker, history=True)]
            if matching:
                return {"kind": "delivered" if len(matching) == 1 and matching[0]["starts_turn"] == 1 else "unknown"}
            rows = queue.execute("SELECT id,payload_json FROM queued_items WHERE thread_id=? "
                                 "AND instr(payload_json,?)>0", (root, marker)).fetchall()
            owned = [r for r in rows if has_text(decode(r["payload_json"]), marker)]
            if len(rows) == len(owned) == 1:
                receipt = identifier(owned[0]["id"])
            elif rows:
                return {"kind": "unknown"}
            else:
                return {"kind": "absent"}
        view = self.inspect(root, marker)
        from .core import problem
        issue = problem(job, view)
        return {"kind": "queued", "queue_id": receipt,
                "withdraw": bool(issue and issue[0] == "cancel")}

    def owns_queue(self, job, receipt):
        identifier(receipt)
        with connect(self.home, "queue", QUEUE) as db:
            row = db.execute("SELECT payload_json FROM queued_items WHERE thread_id=? AND id=?",
                             (job["thread_id"], receipt)).fetchone()
            return bool(row and has_text(decode(row[0]), job["marker"]))

    def marker_exists(self, root, marker):
        with ExitStack() as stack:
            state, history, queue = self._open(stack)
            _, _, leaf = self._current(state, history, root)
            return bool(queue.execute("SELECT 1 FROM queued_items WHERE thread_id=? AND instr(payload_json,?)>0",
                                     (root, marker)).fetchone() or
                        history.execute("SELECT 1 FROM thread_items WHERE thread_id IN (?,?) "
                                        "AND instr(item_json,?)>0", (root, leaf, marker)).fetchone())
