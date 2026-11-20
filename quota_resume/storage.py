from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path

DEFAULT_MESSAGE = "请检查当前进度，继续完成尚未完成的任务，避免重复已经完成的操作。"
WAITING = "waiting"
APPLICATION_ID = 0x51524C54


class Store:
    def __init__(self, path: Path, now: float):
        self.path = Path(path)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("State must not be a link")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.transaction():
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            application = self.db.execute("PRAGMA application_id").fetchone()[0]
            if application not in (0, APPLICATION_ID) or version not in (0, 1, 2):
                raise ValueError("Unsupported local state schema")
            if version == 0:
                tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if tables:
                    raise ValueError("Refusing an unrelated database")
                self.db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                                "paused INTEGER NOT NULL, message TEXT NOT NULL, active_since REAL NOT NULL)")
                self.db.execute("INSERT INTO settings VALUES (1,0,?,?)", (DEFAULT_MESSAGE, now))
                self.db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, "
                                "turn_id TEXT NOT NULL, failed_at REAL NOT NULL, state TEXT NOT NULL, "
                                "reason TEXT NOT NULL, next_check REAL NOT NULL, marker TEXT NOT NULL, "
                                "receipt_id TEXT, UNIQUE(thread_id,turn_id))")
                self.db.execute("PRAGMA user_version=1")
                version = 1
            if version == 1:
                columns = {r[1] for r in self.db.execute("PRAGMA table_info(settings)")}
                if columns != {"id", "paused", "message", "active_since"}:
                    raise ValueError("Not a quota-lite database")
                self.db.execute("ALTER TABLE settings ADD COLUMN live_enabled INTEGER NOT NULL DEFAULT 0")
                self.db.execute("PRAGMA user_version=2")
            self.db.execute("PRAGMA application_id=" + str(APPLICATION_ID))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def settings(self):
        return dict(self.db.execute("SELECT * FROM settings WHERE id=1").fetchone())

    def pause(self, value: bool):
        if type(value) is not bool:
            raise ValueError("Pause must be a boolean")
        with self.transaction():
            self.db.execute("UPDATE settings SET paused=? WHERE id=1", (int(value),))

    def enable_live(self, now):
        with self.transaction():
            # First activation starts a new window; do not adopt observation-era failures.
            if not self.settings()["live_enabled"]:
                self.db.execute("UPDATE jobs SET state='cancelled',reason='before_activation' WHERE state='waiting'")
                self.db.execute("UPDATE settings SET active_since=? WHERE id=1", (now,))
            self.db.execute("UPDATE settings SET live_enabled=1,paused=0 WHERE id=1")

    def disable_live(self):
        with self.transaction():
            self.db.execute("UPDATE settings SET live_enabled=0 WHERE id=1")

    def message(self, text: str):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or "\0" in text:
            raise ValueError("Message must contain 1–2000 characters and no NUL")
        with self.transaction():
            self.db.execute("UPDATE settings SET message=? WHERE id=1", (text,))

    def admit(self, failure, now):
        from hashlib import sha256
        if failure.kind != "usage_limit" or failure.origin != "desktop":
            return False
        if not self.settings()["active_since"] <= failure.at <= now:
            return False
        token = sha256((failure.thread_id + ":" + failure.turn_id).encode()).hexdigest()
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(thread_id,turn_id,failed_at,state,reason,next_check,marker) "
                "VALUES (?,?,?,'waiting','detected',?,?)",
                (failure.thread_id, failure.turn_id, failure.at, now, "[quota-lite:" + token + "]"))
            return cursor.rowcount == 1

    def rows(self, state=None):
        query = "SELECT * FROM jobs"
        values = ()
        if state is not None:
            query += " WHERE state=?"
            values = (state,)
        return [dict(row) for row in self.db.execute(query + " ORDER BY failed_at,id", values)]

    def get(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    def wait(self, key, reason, next_check):
        with self.transaction():
            self.db.execute("UPDATE jobs SET reason=?,next_check=? WHERE id=? AND state='waiting'",
                            (reason, next_check, key))

    def finish(self, key, state, reason, receipt=None):
        with self.transaction():
            # A concurrent cancellation or a different dispatcher must not be overwritten.
            self.db.execute("UPDATE jobs SET state=?,reason=?,receipt_id=? "
                            "WHERE id=? AND state='dispatching'", (state, reason, receipt, key))

    def invalidate(self, key, reason):
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='cancelled',reason=? WHERE id=? AND state='waiting'",
                            (reason, key))

    def claim(self, key, now):
        with self.transaction():
            if self.settings()["paused"]:
                return False
            return self.db.execute("UPDATE jobs SET state='dispatching',reason='reserved' "
                                   "WHERE id=? AND state='waiting' AND next_check<=?",
                                   (key, now)).rowcount == 1

    @contextmanager
    def launch_guard(self, key):
        """Pause/cancel serialize with launch, NOT with the subsequent receipt wait."""
        with self.transaction():
            row = self.get(key)
            yield bool(row and row["state"] == "dispatching" and not self.settings()["paused"])

    def release_unsent(self, key):
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='waiting',reason='paused_before_launch' "
                            "WHERE id=? AND state='dispatching'", (key,))

    def recover_crash(self):
        """Call once at exclusive worker startup, never per tick or from a status client.

        A future live worker must first acquire its single-instance lock. A durable
        reservation abandoned by that worker might have been sent; never retry it.
        """
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='uncertain',reason='worker_interrupted' "
                            "WHERE state='dispatching'")

    def reconcile(self, key, state, reason, receipt=None):
        if state not in {"queued", "delivered", "uncertain", "cancelled"}:
            raise ValueError("Invalid reconciliation")
        with self.transaction():
            self.db.execute("UPDATE jobs SET state=?,reason=?,receipt_id=coalesce(?,receipt_id) "
                            "WHERE id=? AND state IN ('queued','uncertain')",
                            (state, reason, receipt, key))
