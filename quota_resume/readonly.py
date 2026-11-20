"""Read-only database selection; unknown generations never fall back to old files."""
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3


class Unavailable(RuntimeError):
    pass


def generation(home, kind):
    home = Path(home).resolve()
    matches = []
    for path in home.glob(kind + "_*.sqlite"):
        match = re.fullmatch(re.escape(kind) + r"_(\d+)\.sqlite", path.name)
        if match:
            matches.append((int(match[1]), path))
    if not matches:
        raise Unavailable("database_missing")
    path = max(matches, key=lambda x: x[0])[1]
    if path.is_symlink() or path.resolve().parent != home or not path.is_file():
        raise Unavailable("database_path_invalid")
    return path


@contextmanager
def connect(home, kind, schema):
    path = generation(home, kind)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        for table, required in schema.items():
            # Table names are program constants, never values from files or user input.
            if not re.fullmatch(r"[a-z_]+", table):
                raise ValueError("Invalid schema constant")
            columns = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
            if not set(required) <= columns:
                raise Unavailable("unsupported_schema")
        yield db
    finally:
        db.close()
