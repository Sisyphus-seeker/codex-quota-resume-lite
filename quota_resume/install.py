"""Per-user installation. No old plugin, account or Codex database is changed."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from .runtime import worker_lock, request_stop
from .storage import Store
from .windows import ps_path, NO_WINDOW

MARKER = ".quota-resume-lite.json"
PRODUCT = "quota-resume-lite"


def owned(root):
    root = Path(root)
    try:
        value = json.loads((root / MARKER).read_text(encoding="utf-8"))
        return (not root.is_symlink() and not os.path.isjunction(root)
                and value == {"product": PRODUCT, "root": str(root.resolve())})
    except (OSError, ValueError):
        return False


def guarded_tree(root, target):
    root, target = Path(root).resolve(), Path(target)
    if (not owned(root) or target.is_symlink() or os.path.isjunction(target)
            or target.resolve() == root or not target.resolve().is_relative_to(root)):
        raise ValueError("Refusing unowned tree")
    for directory, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink() or os.path.isjunction(item) or not item.resolve().is_relative_to(root):
                raise ValueError("Refusing linked installation files")
    return target


def task_action(root, action):
    root = Path(root).resolve()
    if not owned(root) or action not in {"install", "start", "remove"}:
        raise ValueError("Invalid installed task")
    script = root / "app/scripts/windows_task.ps1"
    env = os.environ.copy()
    env["PSModulePath"] = str(ps_path().parent / "Modules")
    subprocess.run([str(ps_path()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                    str(script), "-Action", action, "-InstallRoot", str(root)],
                   check=True, timeout=30, creationflags=NO_WINDOW, shell=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def verified_payload(source):
    source = Path(source).resolve()
    sums = source.parent / "SHA256SUMS.json"
    if not sums.is_file():
        raise ValueError("Install from a built release; checksum manifest missing")
    expected = json.loads(sums.read_text(encoding="utf-8"))
    if not isinstance(expected, dict) or not expected:
        raise ValueError("Invalid manifest")
    actual = {p.relative_to(source.parent).as_posix() for p in source.parent.rglob('*') if p.is_file()
              and p != sums and '__pycache__' not in p.parts}
    if actual != set(expected):
        raise ValueError("Release contains missing or unlisted files")
    for name, digest in expected.items():
        path = source.parent / name
        if (not path.resolve().is_relative_to(source.parent) or path.is_symlink()
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("Release integrity failed")
    for required in ("app/quota_resume/core.py", "app/scripts/quota_resume.py", "runtime/python.exe",
                     "runtime/pythonw.exe", "app/.codex-plugin/plugin.json", "app/scripts/windows_task.ps1"):
        if required not in expected:
            raise ValueError("Release incomplete")
    return source.parent / "runtime"


def install(source, root, *, register=True, start=True, enable=False):
    if Path(root).is_symlink() or os.path.isjunction(root):
        raise ValueError("Installation root must not be linked")
    source, root = Path(source).resolve(), Path(root).resolve()
    runtime = verified_payload(source)
    if (root == Path(root.anchor) or root == Path.home().resolve() or root.is_relative_to(source)
            or source.is_relative_to(root) or root.is_symlink() or os.path.isjunction(root)):
        raise ValueError("Invalid installation root")
    if root.exists() and any(root.iterdir()) and not owned(root):
        raise ValueError("Existing folder is not owned by this product")
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text(json.dumps({"product": PRODUCT, "root": str(root)}), encoding="utf-8")
    state = root / "config/state.sqlite"
    if (root / "config").is_symlink() or os.path.isjunction(root / "config"):
        raise ValueError("Config must not be linked")
    # Pause and request a clean exit before replacing any executable code.
    store = Store(state, time.time())
    store.pause(True)
    store.close()
    request_stop(state)
    deadline = time.monotonic() + 40
    while True:
        try:
            lock = worker_lock(state.parent / "worker.lock")
            lock.__enter__()
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise ValueError("Worker has not stopped; no code replaced")
            time.sleep(0.5)
    try:
        for name, origin in (("app", source), ("runtime", runtime)):
            target = root / name
            stage = root / (name + ".new")
            if stage.exists():
                shutil.rmtree(guarded_tree(root, stage))
            shutil.copytree(origin, stage, ignore=shutil.ignore_patterns("__pycache__", ".local", "dist", "tests"))
            # Preserve a previous complete tree for interrupted installs. Never overwrite it.
            previous = root / (name + ".previous")
            if previous.exists():
                shutil.rmtree(guarded_tree(root, previous))
            if target.exists():
                guarded_tree(root, target).rename(previous)
            stage.rename(target)
        (root / "config/codex-home.txt").write_text(
            str(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))).resolve()), encoding="utf-8")
        for name in ("Status.cmd", "Enable.cmd", "Pause.cmd", "Uninstall.cmd"):
            shutil.copy2(source.parent / name, root / name)
        store = Store(state, time.time())
        try:
            if enable:
                from .live import old_plugin_blocks, LiveAdapter
                if old_plugin_blocks():
                    raise ValueError("Pause old auto-resume before enabling this product")
                LiveAdapter(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))).doctor()
                store.enable_live(time.time())
        finally:
            store.close()
        if register:
            exe = windows_engine()
            for args in (["plugin", "marketplace", "add", str(root/"app")],
                         ["plugin", "add", "quota-resume-lite@quota-resume-lite-local"]):
                subprocess.run([str(exe)] + args, check=True, timeout=30,
                               creationflags=NO_WINDOW, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            task_action(root, "install")
    finally:
        lock.__exit__(None, None, None)
    if register and start:
        task_action(root, "start")
    return {"installed": str(root), "enabled": enable, "startup_registered": register,
            "started": bool(register and start)}


def windows_engine():
    from .windows import desktop
    return desktop(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))))["exe"]


def uninstall(root, *, purge=False, register=True):
    root = Path(root).resolve()
    if not owned(root):
        raise ValueError("Refusing unowned installation")
    if Path(sys.executable).resolve().is_relative_to(root):
        raise ValueError("Run Uninstall.cmd; the running interpreter cannot remove itself")
    state = root / "config/state.sqlite"
    store = Store(state, time.time())
    store.pause(True)
    store.close()
    request_stop(state)
    deadline = time.monotonic() + 40
    while True:
        try:
            lock = worker_lock(state.parent / "worker.lock")
            lock.__enter__()
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise ValueError("Worker still running; nothing removed")
            time.sleep(0.5)
    try:
        if register:
            task_action(root, "remove")
            # Remove only if the marketplace still points to this installation.
            exe = windows_engine()
            response = subprocess.run([str(exe), "plugin", "marketplace", "list", "--json"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", timeout=30,
                creationflags=NO_WINDOW, shell=False, check=True)
            listing = json.loads(response.stdout)
            rows = listing if isinstance(listing, list) else listing.get("marketplaces", [])
            matching = [r for r in rows if r.get("name") == "quota-resume-lite-local"]
            # Config formats vary by Codex version; ambiguity leaves registrations intact.
            if len(matching) == 1 and Path(matching[0].get("root", "")).resolve() == (root/"app").resolve():
                for args in (["plugin", "remove", "quota-resume-lite@quota-resume-lite-local"],
                             ["plugin", "marketplace", "remove", "quota-resume-lite-local"]):
                    subprocess.run([str(exe)]+args, check=True, timeout=30, creationflags=NO_WINDOW,
                                   shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif matching:
                raise ValueError("Cannot prove plugin registration ownership; program files retained")
        for name in ("app", "runtime", "app.previous", "runtime.previous", "app.new", "runtime.new"):
            target = root/name
            if target.exists():
                shutil.rmtree(guarded_tree(root, target))
    finally:
        lock.__exit__(None, None, None)
    if purge and (root/"config").exists():
        shutil.rmtree(guarded_tree(root, root/"config"))
    return {"uninstalled": True, "settings_retained": not purge}
