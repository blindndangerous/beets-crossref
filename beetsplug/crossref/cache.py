"""SQLite cache of distilled API answers, keyed by (namespace, key).

A cached None is a real answer ("this service has no such album") and is
kept, so a miss is not asked again every run.  MISSING means nothing usable
is stored.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

MISSING = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""


class Cache:
    def __init__(self, path: Path, max_age_days: float = 30.0):
        """max_age_days 0 keeps answers forever; delete the file to start over."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age_days * 86400.0
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_SCHEMA)
        if self.max_age:
            self.conn.execute("DELETE FROM entries WHERE fetched_at < ?", (time.time() - self.max_age,))
            self.conn.commit()

    def get(self, namespace: str, key: str) -> Any:
        row = self.conn.execute(
            "SELECT fetched_at, payload FROM entries WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
        if row is None or (self.max_age and time.time() - row[0] > self.max_age):
            return MISSING
        return json.loads(row[1])

    def put(self, namespace: str, key: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?)",
            (namespace, key, time.time(), json.dumps(payload, separators=(",", ":"))),
        )
        self.conn.commit()

    def remember(self, namespace: str, key: str, fetch):
        """The cached answer, or fetch() stored and returned.

        fetch() returning MISSING means the request failed: nothing is
        stored, so the next run asks again.
        """
        cached = self.get(namespace, key)
        if cached is not MISSING:
            return cached
        value = fetch()
        if value is not MISSING:
            self.put(namespace, key, value)
        return value

    def close(self) -> None:
        self.conn.close()
