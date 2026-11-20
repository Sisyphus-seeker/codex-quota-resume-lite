"""Single worker lifecycle. Writes only quota-lite's own state and heartbeat."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from .core import Engine
from .live import LiveAdapter, old_plugin_blocks
from .storage import Store
from .errors import diagnostic_reason


@contextmanager
def worker_lock(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Lock must not be a link")
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()  # OS releases this worker's lock on normal exit and crash.


def atomic_json(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="heartbeat-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(state, home, *, once=False, interval=30):
    state, home = Path(state).resolve(), Path(home).resolve()
    # Never run a worker against Codex's own databases, even with an explicit flag.
    if state.is_relative_to(home):
        raise ValueError("State must be outside the Codex home")
    with worker_lock(state.parent / "worker.lock"):
        stop = state.parent / "stop.request"
        stop.unlink(missing_ok=True)
        store = Store(state, time.time())
        try:
            store.recover_crash()
            def allowed():
                data = store.settings()
                return bool(data["live_enabled"] and not data["paused"])
            adapter = LiveAdapter(home, send_allowed=allowed)
            engine = Engine(store, adapter, observe_only=lambda: not allowed())
            while not stop.exists():
                error = None
                error_reason = None
                try:
                    if allowed() and old_plugin_blocks():
                        # Withdrawal of our already-owned queued messages still runs.
                        engine.observe_only = lambda: True
                        try:
                            engine._reconcile()
                        finally:
                            engine.observe_only = lambda: not allowed()
                        error = "old_plugin_active"
                        error_reason = error
                    else:
                        engine.tick(time.time())
                except Exception as exc:
                    # Closed class names only; no paths, messages or credentials.
                    error = type(exc).__name__
                    error_reason = diagnostic_reason(exc)
                settings = store.settings()
                atomic_json(state.parent / "heartbeat.json", {
                    "pid": os.getpid(), "at": time.time(), "paused": bool(settings["paused"]),
                    "live_enabled": bool(settings["live_enabled"]), "last_error": error,
                    "last_error_reason": error_reason,
                    "observed_unsupported": adapter.observer.skipped,
                })
                if once:
                    break
                deadline = time.monotonic() + interval
                while not stop.exists() and time.monotonic() < deadline:
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        finally:
            # A clean stop also attempts to withdraw only our proven queue items.
            # Never cancel a turn that has already started.
            if stop.exists():
                engine.observe_only = lambda: True
                engine._reconcile()
            store.close()


def heartbeat(state):
    try:
        value = json.loads((Path(state).parent / "heartbeat.json").read_text(encoding="utf-8"))
        value["recent"] = 0 <= time.time() - value["at"] <= 90
        try:
            # A current heartbeat with an unlocked file is a stopped worker, not alive.
            with worker_lock(Path(state).parent / "worker.lock"):
                value["running"] = False
        except OSError:
            value["running"] = True
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {"running": False, "recent": False}


def request_stop(state):
    state = Path(state)
    state.parent.mkdir(parents=True, exist_ok=True)
    (state.parent / "stop.request").write_text("stop", encoding="ascii")
