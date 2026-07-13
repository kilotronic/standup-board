"""SQLite persistence for the roster: pure storage, no merge/TTL policy.

One SQLite file backs the whole board. Under the deploy model
(gunicorn --workers 1) there is a single writer, so one connection is race-free
and no locking is needed. Durability is entirely a function of where the file
lives: a plain path inside the container is ephemeral; the same path on a
mounted volume survives restarts.
"""

import json
import sqlite3
from dataclasses import dataclass


@dataclass
class Session:
    """One live agent session registered on the board."""

    owner: str
    session_id: str
    machine: str
    repo: str
    active_branch: str | None = None
    last_prompt: str | None = None
    goal: str | None = None
    current_step: str | None = None
    active_pr: dict | None = None
    worktrees: list | None = None
    registered_at: float = 0.0
    narrative_updated_at: float = 0.0


_COLUMNS = (
    "owner",
    "session_id",
    "machine",
    "repo",
    "active_branch",
    "last_prompt",
    "goal",
    "current_step",
    "active_pr",
    "worktrees",
    "registered_at",
    "narrative_updated_at",
)
_JSON_FIELDS = ("active_pr", "worktrees")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  owner                TEXT NOT NULL,
  session_id           TEXT NOT NULL,
  machine              TEXT NOT NULL DEFAULT '',
  repo                 TEXT NOT NULL DEFAULT '',
  active_branch        TEXT,
  last_prompt          TEXT,
  goal                 TEXT,
  current_step         TEXT,
  active_pr            TEXT,
  worktrees            TEXT,
  registered_at        REAL NOT NULL DEFAULT 0,
  narrative_updated_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (owner, session_id)
);
"""


class SessionStore:
    """Owns the SQLite connection and the row<->Session mapping."""

    def __init__(self, db_path: str = ":memory:") -> None:
        # check_same_thread=False: safe under the single-writer deploy model and
        # tolerant of a threaded dev server; we never share the connection across
        # concurrent writers.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the connection. Idempotent: closing twice is a sqlite3 no-op."""
        self._conn.close()

    def __del__(self) -> None:
        # Finalizer so a dropped store (e.g. a short-lived Roster in tests)
        # releases its connection instead of leaking it to interpreter exit.
        # Guarded because __init__ may have failed before _conn was set.
        if getattr(self, "_conn", None) is not None:
            self._conn.close()

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        data = {c: row[c] for c in _COLUMNS}
        for field in _JSON_FIELDS:
            raw = data[field]
            data[field] = json.loads(raw) if raw is not None else None
        return Session(**data)

    def upsert(self, session: Session) -> None:
        values = []
        for col in _COLUMNS:
            value = getattr(session, col)
            if col in _JSON_FIELDS and value is not None:
                value = json.dumps(value)
            values.append(value)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        self._conn.execute(
            f"INSERT OR REPLACE INTO sessions ({', '.join(_COLUMNS)}) "
            f"VALUES ({placeholders})",
            values,
        )
        self._conn.commit()

    def get(self, owner: str, session_id: str) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE owner = ? AND session_id = ?",
            (owner, session_id),
        ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def delete(self, owner: str, session_id: str) -> None:
        self._conn.execute(
            "DELETE FROM sessions WHERE owner = ? AND session_id = ?",
            (owner, session_id),
        )
        self._conn.commit()

    def list_owner(self, owner: str) -> list[Session]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE owner = ? ORDER BY repo, machine",
            (owner,),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def prune_expired(self, owner: str, cutoff: float) -> None:
        self._conn.execute(
            "DELETE FROM sessions WHERE owner = ? AND registered_at < ?",
            (owner, cutoff),
        )
        self._conn.commit()
