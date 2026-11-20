"""Read-only Windows process/Restart Manager inventory.

The RM declarations and identity checks are adapted from codex-auto-resume-windows
(MIT, copyright 2026 songyb111-gachon); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import subprocess
from .readonly import Unavailable, generation

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ps_path():
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


class UniqueProcess(C.Structure):
    _fields_ = [("pid", W.DWORD), ("created", W.FILETIME)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", UniqueProcess), ("name", W.WCHAR * 256), ("service", W.WCHAR * 64),
                ("type", C.c_int), ("status", W.ULONG), ("session", W.DWORD), ("restartable", W.BOOL)]


def resource_users(*paths):
    if os.name != "nt" or not paths:
        raise Unavailable("windows_required")
    rm = C.WinDLL("rstrtmgr", use_last_error=True, winmode=0x800)
    rm.RmStartSession.argtypes = [C.POINTER(W.DWORD), W.DWORD, W.LPWSTR]
    rm.RmRegisterResources.argtypes = [W.DWORD, W.UINT, C.POINTER(W.LPCWSTR), W.UINT,
                                     C.POINTER(UniqueProcess), W.UINT, C.POINTER(W.LPCWSTR)]
    rm.RmGetList.argtypes = [W.DWORD, C.POINTER(W.UINT), C.POINTER(W.UINT),
                           C.POINTER(ProcessInfo), C.POINTER(W.DWORD)]
    rm.RmEndSession.argtypes = [W.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(rm, name).restype = W.DWORD
    session, key = W.DWORD(), C.create_unicode_buffer(33)
    if rm.RmStartSession(C.byref(session), 0, key):
        raise Unavailable("inventory_start_failed")
    try:
        files = (W.LPCWSTR * len(paths))(*(str(p) for p in paths))
        if rm.RmRegisterResources(session, len(paths), files, 0, None, 0, None):
            raise Unavailable("inventory_register_failed")
        needed, count, why = W.UINT(), W.UINT(), W.DWORD()
        result = rm.RmGetList(session, C.byref(needed), C.byref(count), None, C.byref(why))
        if result == 0:
            return []
        for _ in range(3):
            if result != 234 or not 0 < needed.value <= 1024:
                raise Unavailable("inventory_unavailable")
            array = (ProcessInfo * needed.value)()
            count.value = needed.value
            result = rm.RmGetList(session, C.byref(needed), C.byref(count), array, C.byref(why))
            if result == 0:
                return [(p.process.pid, (p.process.created.dwHighDateTime << 32) |
                         p.process.created.dwLowDateTime) for p in array[:count.value]]
        raise Unavailable("inventory_unstable")
    finally:
        rm.RmEndSession(session)


def identity(pid):
    if os.name != "nt":
        raise Unavailable("windows_required")
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
    k.CloseHandle.argtypes, k.CloseHandle.restype = [W.HANDLE], W.BOOL
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
    handle = k.OpenProcess(0x101000, False, pid)
    if not handle:
        raise Unavailable("process_unreadable")
    try:
        buf, length = C.create_unicode_buffer(32768), W.DWORD(32768)
        times = [W.FILETIME() for _ in range(4)]
        if (k.WaitForSingleObject(handle, 0) != 258
                or not k.QueryFullProcessImageNameW(handle, 0, buf, C.byref(length))
                or not k.GetProcessTimes(handle, *(C.byref(t) for t in times))):
            raise Unavailable("process_changed")
        return {"pid": pid, "path": buf.value.lower(),
                "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}
    finally:
        k.CloseHandle(handle)


def official_engine(path):
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    path = Path(path)
    pf = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")
    bundled = re.fullmatch(re.escape(str(Path(pf) / "WindowsApps")) +
        r"\\OpenAI\.Codex_[0-9.]+_(?:x64|arm64)__2p2nqsd0c76g0\\app\\resources\\codex\.exe",
        str(path.resolve()), re.I)
    extracted = (re.fullmatch(r"[a-f0-9]{8,64}", path.parent.name)
                 and path.resolve().parent.parent == root.resolve())
    if (not path.is_file() or path.is_symlink() or path.name.lower() != "codex.exe"
            or not (bundled or extracted)):
        raise Unavailable("official_engine_required")
    return path.resolve()


def inventory():
    script = """$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
@(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe' OR Name='codex.exe'" |
ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId; parent=[int]$_.ParentProcessId; path=$_.ExecutablePath} }) |
ConvertTo-Json -Compress
"""
    env = os.environ.copy()
    env["PSModulePath"] = str(ps_path().parent / "Modules")
    result = subprocess.run([str(ps_path()), "-NoProfile", "-NonInteractive", "-Command", script],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            encoding="utf-8-sig", timeout=15, check=True,
                            creationflags=NO_WINDOW, shell=False, env=env)
    rows = json.loads(result.stdout or "[]")
    return rows if isinstance(rows, list) else [rows]


def desktop(home):
    rows = inventory()
    pf = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")
    pattern = re.compile(re.escape(str(Path(pf) / "WindowsApps")) +
                         r"\\OpenAI\.Codex_[0-9.]+_(?:x64|arm64)__2p2nqsd0c76g0\\app\\ChatGPT\.exe", re.I)
    apps = [r for r in rows if isinstance(r.get("path"), str) and pattern.fullmatch(r["path"])]
    ids = {r["pid"] for r in apps}
    mains = [r for r in apps if r["parent"] not in ids]
    if len(mains) != 1:
        raise Unavailable("desktop_not_running_or_ambiguous")
    main = identity(mains[0]["pid"])
    if main["path"] != mains[0]["path"].lower():
        raise Unavailable("desktop_changed")
    databases = [generation(home, kind) for kind in ("queue", "state")]
    holders = resource_users(*databases)
    app_rows = {r['pid']: r for r in apps if r['path'].lower() == main['path']}
    servers = []
    for row in rows:
        if not isinstance(row.get("path"), str) or Path(row['path']).name.lower() != 'codex.exe':
            continue
        try:
            exe = official_engine(row["path"])
            # A bundled engine must belong to this desktop's exact package.
            if (exe.parent.name.lower() == "resources"
                    and exe.parent.parent != Path(main["path"]).parent):
                continue
        except Unavailable:
            continue
        # Electron may launch the engine through a utility process. Every
        # intermediate parent must be the same trusted desktop executable.
        parent, ancestors, seen = row.get('parent'), [], set()
        while parent != main['pid'] and parent in app_rows and parent not in seen and len(seen) < 8:
            seen.add(parent)
            ancestor = identity(parent)
            if ancestor['path'] != main['path'] or ancestor['created'] < main['created']:
                break
            ancestors.append(ancestor)
            parent = app_rows[parent]['parent']
        if parent != main['pid']:
            continue
        current = identity(row["pid"])
        if ((current["pid"], current["created"]) in holders and current["path"] == str(exe).lower()
                and current["created"] >= max([main['created']] + [a['created'] for a in ancestors])):
            servers.append((current, exe, ancestors))
    if len(servers) != 1:
        raise Unavailable("desktop_engine_not_identified")
    server, exe, ancestors = servers[0]
    return {"main": main, "server": server, "exe": exe, "ancestors": ancestors}


def loaded(home, thread_id, pair):
    path = Path(home) / "thread-writer-locks" / (thread_id + ".lock")
    if not path.is_file() or path.is_symlink() or path.resolve().parent != (Path(home) / "thread-writer-locks").resolve():
        return False
    server, main = pair["server"], pair["main"]
    if identity(main["pid"]) != main or identity(server["pid"]) != server:
        return None
    if any(identity(a['pid']) != a for a in pair.get('ancestors', [])):
        return None
    users = resource_users(path)
    if not users:
        return False
    if users != [(server["pid"], server["created"])]:
        return None
    return (identity(server["pid"]) == server and identity(main["pid"]) == main
            and all(identity(a['pid']) == a for a in pair.get('ancestors', [])))
