"""Absolute-path entrypoint for the packaged skill."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_resume.cli import main
from quota_resume.errors import describe

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(describe(exc))
