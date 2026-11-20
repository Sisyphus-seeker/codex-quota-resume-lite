from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from .simulation import demo
from .storage import Store
from .errors import ActionError, describe, DIAGNOSTICS


def default_state():
    root = Path(__file__).resolve().parents[1]
    if root.name == "app" and (root.parent / ".quota-resume-lite.json").is_file():
        return root.parent / "config/state.sqlite"
    return root / ".local/state.sqlite"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Codex 额度恢复自动续接；未启用时只读观察")
    parser.add_argument("--state", type=Path, help="本工具独立 SQLite；默认安装目录/config/state.sqlite")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))))
    sub = parser.add_subparsers(dest="command", required=True)
    simulation = sub.add_parser("demo", help="临时目录和虚拟时钟演示，不等待真实额度")
    simulation.add_argument("--uncertain", action="store_true", help="模拟发送后收据丢失")
    sub.add_parser("status", help="状态只显示计数，不输出自定义消息正文")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("enable", help="明确开启所有新额度中断的真实自动续接；旧插件必须暂停")
    sub.add_parser("disable", help="关闭真实续接并暂停")
    doctor = sub.add_parser("doctor", help="只读检查，不发送消息")
    doctor.add_argument("--usage", action="store_true", help="通过官方引擎联网读取当前额度")
    observe = sub.add_parser("observe", help="一次只读扫描，不创建本工具状态或发送消息")
    run = sub.add_parser("run", help="独立后台 worker；默认观察，enable 后自动发送")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=int, default=30, choices=range(5, 301), metavar="5..300")
    sub.add_parser("start", help="启动已安装的 Windows 后台任务")
    sub.add_parser("stop", help="通知自己的 worker 正常退出")
    message = sub.add_parser("message", help="从 UTF-8 文件设置统一消息")
    message.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command in {"doctor", "observe"}:
        from .live import LiveAdapter
        adapter = LiveAdapter(args.codex_home)
        output = adapter.doctor(probe_usage=getattr(args, "usage", False))
        if args.command == "observe":
            found = adapter.failures(time.time() - 3600)
            output.update(recent_quota_failures=len(found), unsupported=adapter.observer.skipped)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    state = args.state or default_state()
    if state.resolve().is_relative_to(args.codex_home.resolve()):
        parser.error("状态文件不能放在 Codex home 内")
    if args.command in {"run", "start", "stop"}:
        from .runtime import run as worker, request_stop
        if args.command == "run":
            worker(state, args.codex_home, once=args.once, interval=args.interval)
        elif args.command == "stop":
            request_stop(state)
        else:
            from .install import task_action
            task_action(state.resolve().parent.parent, "start")
        return 0
    if args.command == "demo":
        if args.state is not None:
            parser.error("demo 使用一次性临时数据库，不接受 --state")
        with tempfile.TemporaryDirectory(prefix="quota-lite-demo-") as root:
            store = Store(Path(root) / "demo.sqlite", 1000)
            try:
                output = demo(store, args.uncertain)
            finally:
                store.close()
    else:
        store = Store(state, time.time())
        try:
            if args.command in ("pause", "resume"):
                store.pause(args.command == "pause")
            elif args.command == "message":
                if args.file.stat().st_size > 8192:
                    raise ValueError("消息文件过大")
                store.message(args.file.read_text(encoding="utf-8-sig"))
            elif args.command == "disable":
                store.disable_live()
                store.pause(True)
            elif args.command == "enable":
                from .live import LiveAdapter, old_plugin_blocks
                if old_plugin_blocks():
                    raise ActionError('old_plugin_active')
                LiveAdapter(args.codex_home).doctor()
                store.enable_live(time.time())
                installed = state.resolve().parent.parent
                if (installed / ".quota-resume-lite.json").is_file():
                    from .install import task_action
                    try:
                        task_action(installed, "start")
                    except Exception:
                        store.disable_live()
                        store.pause(True)
                        raise
            settings = store.settings()
            from .runtime import heartbeat
            worker_state = heartbeat(state)
            reason = worker_state.get('last_error_reason') or worker_state.get('last_error')
            output = {"mode": "LIVE_ENABLED" if settings["live_enabled"] else "OBSERVE_ONLY",
                      "live_sending": bool(settings["live_enabled"] and not settings["paused"]),
                      "paused": bool(settings["paused"]),
                      "message_configured": bool(settings["message"]),
                      "tasks": dict(Counter(row["state"] for row in store.rows())),
                      "health": ('stopped' if not worker_state.get('running') or not worker_state.get('recent')
                                 else 'error' if worker_state.get('last_error') else 'ok'),
                      "error_description": DIAGNOSTICS.get(reason),
                      "worker": worker_state}
        finally:
            store.close()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        # Error class only: protocol/SQLite exceptions may carry local paths.
        raise SystemExit(describe(exc))
