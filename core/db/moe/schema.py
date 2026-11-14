from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import base


def open_db(path: str | Path) -> sqlite3.Connection | None:
    p = Path(path)
    if not p.is_file():
        return None
    return base.connect_readonly(p, timeout=15, check_same_thread=False)
