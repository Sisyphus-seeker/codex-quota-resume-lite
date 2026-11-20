"""Finite official helper: only usage reads and withdrawal of owned queue items.

Bounded JSON-lines design adapted from the MIT project credited in THIRD_PARTY_NOTICES.md.
No thread/start, thread/resume, turn/start, approval or authentication requests.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from .readonly import Unavailable
from .windows import NO_WINDOW, official_engine


def environment(home):
    env = os.environ.copy()
    env.update(CODEX_HOME=str(Path(home).resolve()), OTEL_SDK_DISABLED="true")
    return env


def arguments(exe):
    return [str(official_engine(exe)), "-c", "analytics.enabled=false", "-c", 'otel.exporter="none"',
            "-c", 'otel.trace_exporter="none"', "-c", 'otel.metrics_exporter="none"',
            "-c", "otel.log_user_prompt=false", "-c", 'chatgpt_base_url="https://chatgpt.com/backend-api/"']


class Protocol:
    METHODS = {"initialize", "account/rateLimits/read", "thread/queue/delete"}

    def __init__(self, exe, home):
        self.exe, self.home = exe, Path(home).resolve()
        self.process = None
        self.sequence = 0
        self.responses = queue.Queue(maxsize=64)
        self.write_lock = threading.Lock()

    def __enter__(self):
        try:
            self.process = subprocess.Popen(arguments(self.exe) + ["app-server", "--stdio"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1, cwd=self.home, env=environment(self.home),
                creationflags=NO_WINDOW, shell=False, close_fds=True)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            reply = self.call("initialize", {"clientInfo": {"name": "quota_resume_lite", "version": "0.1.0"},
                                             "capabilities": {"experimentalApi": True}})
            if (not isinstance(reply, dict) or not reply.get("codexHome")
                    or Path(reply["codexHome"]).resolve() != self.home or reply.get("platformOs") != "windows"):
                raise Unavailable("protocol_home_or_platform_mismatch")
            self._write({"method": "initialized"})
            return self
        except BaseException:
            self.__exit__()
            raise

    def _write(self, value):
        with self.write_lock:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            self.process.stdin.flush()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line or len(line) > 1024 * 1024:
                    return
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(value, dict):
                    continue
                if "id" in value and "method" in value:
                    self._write({"id": value["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
                elif "id" in value:
                    self.responses.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            return

    def call(self, method, params=None):
        if method not in self.METHODS:
            raise ValueError("Method forbidden")
        self.sequence += 1
        request = {"id": self.sequence, "method": method}
        if params is not None:
            request["params"] = params
        self._write(request)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                reply = self.responses.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise Unavailable("protocol_timeout") from None
            if reply.get("id") == self.sequence:
                if "error" in reply or "result" not in reply:
                    raise Unavailable("protocol_refused")
                return reply["result"]
        raise Unavailable("protocol_timeout")

    def __exit__(self, *unused):
        p = self.process
        if p is None:
            return
        if p.stdin:
            try:
                p.stdin.close()
            except OSError:
                pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()  # Only the process this object created and still holds.
            p.wait(timeout=5)
        if hasattr(self, "reader"):
            self.reader.join(timeout=2)
        if p.stdout:
            p.stdout.close()


class QueueSubmission:
    def __init__(self, process, thread):
        self.process, self.thread = process, thread

    def receipt(self):
        p = self.process
        try:
            # The helper response is bounded. Drain in a thread; a malicious/changed
            # binary cannot make the main worker wait forever or consume unlimited RAM.
            result = queue.Queue(maxsize=1)
            def read():
                try:
                    result.put(p.stdout.read(8193))
                except (OSError, ValueError):
                    result.put("")
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            output = result.get(timeout=45)
            if len(output) > 8192:
                return None
            p.wait(timeout=2)
            match = re.fullmatch(r"Queued message ([0-9a-f-]{36}) for thread " + re.escape(self.thread) + r"\.", output.strip())
            if p.returncode == 0 and match:
                from .model import identifier
                return identifier(match[1])
            return None
        except (queue.Empty, subprocess.TimeoutExpired, OSError, ValueError):
            return None
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)
            if p.stdout:
                p.stdout.close()
