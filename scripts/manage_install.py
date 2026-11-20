from pathlib import Path
import argparse
import json
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from quota_resume.install import install, uninstall


def main():
    parser = argparse.ArgumentParser(description="安装/卸载简化额度续跑插件（普通用户，无模型请求）")
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--root", type=Path, default=Path.home()/".codex-quota-resume-lite")
    parser.add_argument("--enable", action="store_true", help="安装后开启真实恢复；旧插件必须已暂停")
    parser.add_argument("--purge", action="store_true", help="卸载时清除本工具配置")
    parser.add_argument("--no-register", action="store_true", help="隔离验证：不写Windows任务或Codex插件配置")
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args()
    if args.action == "install":
        result = install(SOURCE, args.root, register=not args.no_register, start=not args.no_start, enable=args.enable)
    else:
        result = uninstall(args.root, purge=args.purge, register=not args.no_register)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Installation stopped: " + type(exc).__name__ + ". Check installation paths and doctor.")
