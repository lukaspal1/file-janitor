"""
SQLite log of every action taken. Undo relies on this.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .config import DB_PATH
from .models import Action, MoveProposal, MoveResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    action          TEXT    NOT NULL,       -- move | trash
    src             TEXT    NOT NULL,
    dst             TEXT,                   -- null for trash
    original_name   TEXT    NOT NULL,
    size            INTEGER NOT NULL,
    rule_name       TEXT,
    success         INTEGER NOT NULL,
    error           TEXT,
    undone          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp);
CREATE INDEX IF NOT EXISTS idx_actions_src       ON actions(src);
CREATE INDEX IF NOT EXISTS idx_actions_undone    ON actions(undone);

CREATE TABLE IF NOT EXISTS ignored_files (
    path        TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS ignored_patterns (
    pattern     TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL,
    reason      TEXT
);
"""


class JanitorDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- logging ----

    def log_result(self, result: MoveResult, rule_name: Optional[str] = None) -> int:
        p = result.proposal
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO actions
                   (timestamp, action, src, dst, original_name, size, rule_name, success, error)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    result.timestamp.isoformat(timespec="seconds"),
                    p.action.value,
                    str(p.file.path),
                    str(result.actual_destination) if result.actual_destination else None,
                    p.file.name,
                    p.file.size,
                    rule_name or p.rule.name,
                    1 if result.success else 0,
                    result.error,
                ),
            )
            return cur.lastrowid

    # ---- undo ----

    def recent_actions(self, limit: int = 100, include_undone: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM actions"
        if not include_undone:
            q += " WHERE undone = 0"
        q += " ORDER BY id DESC LIMIT ?"
        return list(self._conn.execute(q, (limit,)))

    def mark_undone(self, action_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE actions SET undone = 1 WHERE id = ?", (action_id,))

    # ---- ignore lists ----

    def ignore_file(self, path: Path, reason: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO ignored_files (path, added_at, reason) VALUES (?,?,?)",
                (str(path), datetime.now().isoformat(timespec="seconds"), reason),
            )

    def ignore_pattern(self, pattern: str, reason: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO ignored_patterns (pattern, added_at, reason) VALUES (?,?,?)",
                (pattern, datetime.now().isoformat(timespec="seconds"), reason),
            )

    def ignored_files(self) -> set[str]:
        return {r["path"] for r in self._conn.execute("SELECT path FROM ignored_files")}

    def ignored_patterns(self) -> list[str]:
        return [r["pattern"] for r in self._conn.execute("SELECT pattern FROM ignored_patterns")]

    # ---- stats ----

    def stats(self) -> dict:
        row = self._conn.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE success=1 AND undone=0) AS moved,
                 COALESCE(SUM(size) FILTER (WHERE success=1 AND undone=0), 0) AS bytes,
                 COUNT(*) FILTER (WHERE action='trash' AND success=1 AND undone=0) AS trashed
               FROM actions"""
        ).fetchone()
        return {"moved": row["moved"], "bytes": row["bytes"], "trashed": row["trashed"]}