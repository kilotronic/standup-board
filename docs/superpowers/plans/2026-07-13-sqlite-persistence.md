# SQLite Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `Roster` SQLite-authoritative so the live board survives a container restart when a volume is mounted at `/var/lib/standup`.

**Architecture:** A new `SessionStore` owns a single `sqlite3` connection (schema, CRUD, JSON serialization). `Roster` keeps its exact public API and all merge/TTL policy but delegates storage to `SessionStore` instead of an in-memory dict. Durability is purely a function of whether the DB file's directory is a mounted volume.

**Tech Stack:** Python 3.13, stdlib `sqlite3` + `json`, Flask, pytest.

## Global Constraints

- `requires-python >=3.13`; no new runtime dependencies (stdlib only).
- Deploy model is `gunicorn --workers 1` (single writer) — no locking needed.
- Coverage gate: `--cov-fail-under=95` over `standup_board` + `client`.
- Lint/format gate: `ruff check .` and `ruff format --check .` must pass.
- Per-owner isolation must hold: every query filters on `owner`.
- Preserve `Roster`'s public API and merge semantics byte-for-byte.
- Library default `db_path=":memory:"` (ephemeral, per-instance) so existing tests are unaffected.

---

### Task 1: `SessionStore` persistence unit

**Files:**

- Create: `standup_board/store.py`
- Test: `tests/test_store.py`

**Interfaces:**

- Consumes: `standup_board.roster.Session` (existing dataclass).
- Produces:
  - `SessionStore(db_path: str = ":memory:")`
  - `.upsert(session: Session) -> None`
  - `.get(owner: str, session_id: str) -> Session | None`
  - `.delete(owner: str, session_id: str) -> None`
  - `.list_owner(owner: str) -> list[Session]` — ordered `(repo, machine)`
  - `.prune_expired(owner: str, cutoff: float) -> None` — delete rows with `registered_at < cutoff`

- [ ] **Step 1: Write failing tests** in `tests/test_store.py`:

```python
from standup_board.roster import Session
from standup_board.store import SessionStore

ALICE = "alice@example.com"


def _sess(session_id="s1", **kw):
    base = dict(owner=ALICE, session_id=session_id, machine="mini", repo="pg",
                registered_at=100.0)
    base.update(kw)
    return Session(**base)


def test_upsert_then_get_round_trips_all_fields():
    store = SessionStore()
    store.upsert(_sess(active_pr={"url": "http://x/1", "number": 1},
                       worktrees=[{"path": "/a", "branch": "main", "pr": None}],
                       goal="g", current_step="c", active_branch="b",
                       last_prompt="p", narrative_updated_at=110.0))
    got = store.get(ALICE, "s1")
    assert got == _sess(active_pr={"url": "http://x/1", "number": 1},
                        worktrees=[{"path": "/a", "branch": "main", "pr": None}],
                        goal="g", current_step="c", active_branch="b",
                        last_prompt="p", narrative_updated_at=110.0)


def test_get_missing_returns_none():
    assert SessionStore().get(ALICE, "nope") is None


def test_upsert_replaces_existing_row():
    store = SessionStore()
    store.upsert(_sess(last_prompt="first"))
    store.upsert(_sess(last_prompt="second"))
    assert store.get(ALICE, "s1").last_prompt == "second"
    assert len(store.list_owner(ALICE)) == 1


def test_delete_removes_and_is_idempotent():
    store = SessionStore()
    store.upsert(_sess())
    store.delete(ALICE, "s1")
    store.delete(ALICE, "s1")  # must not raise
    assert store.get(ALICE, "s1") is None


def test_list_owner_sorted_by_repo_then_machine():
    store = SessionStore()
    store.upsert(_sess("a", repo="pg", machine="studio"))
    store.upsert(_sess("b", repo="pg", machine="mini"))
    store.upsert(_sess("c", repo="dot", machine="zed"))
    assert [(s.repo, s.machine) for s in store.list_owner(ALICE)] == [
        ("dot", "zed"), ("pg", "mini"), ("pg", "studio")]


def test_list_owner_scoped_to_owner():
    store = SessionStore()
    store.upsert(_sess("a", owner=ALICE))
    store.upsert(_sess("b", owner="bob@example.com"))
    assert [s.session_id for s in store.list_owner(ALICE)] == ["a"]


def test_prune_expired_deletes_below_cutoff_keeps_equal():
    store = SessionStore()
    store.upsert(_sess("old", registered_at=100.0))
    store.upsert(_sess("edge", registered_at=140.0))
    store.upsert(_sess("new", registered_at=200.0))
    store.prune_expired(ALICE, cutoff=140.0)
    assert sorted(s.session_id for s in store.list_owner(ALICE)) == ["edge", "new"]


def test_none_json_fields_round_trip_as_none():
    store = SessionStore()
    store.upsert(_sess(active_pr=None, worktrees=None))
    got = store.get(ALICE, "s1")
    assert got.active_pr is None and got.worktrees is None


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "s.db")
    SessionStore(path).upsert(_sess(goal="ship"))
    reopened = SessionStore(path)
    assert reopened.get(ALICE, "s1").goal == "ship"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'standup_board.store'`)

- [ ] **Step 3: Implement `standup_board/store.py`**

```python
"""SQLite persistence for the roster: pure storage, no merge/TTL policy.

One SQLite file backs the whole board. Under the deploy model
(gunicorn --workers 1) there is a single writer, so one connection is race-free
and no locking is needed. Durability is entirely a function of where the file
lives: a plain path inside the container is ephemeral; the same path on a
mounted volume survives restarts.
"""

import json
import sqlite3

from .roster import Session

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add standup_board/store.py tests/test_store.py
git commit -m "Add SessionStore: SQLite persistence for the roster"
```

---

### Task 2: Make `Roster` SQLite-authoritative

**Files:**

- Modify: `standup_board/roster.py`
- Test: `tests/test_roster.py` (add persistence tests; existing tests must still pass)

**Interfaces:**

- Consumes: `SessionStore` from Task 1.
- Produces: unchanged `Roster` public API, plus `Roster(ttl_seconds=..., db_path=":memory:")`.

- [ ] **Step 1: Add failing persistence tests** to `tests/test_roster.py`:

```python
def test_sessions_survive_reopening_same_db(tmp_path):
    path = str(tmp_path / "roster.db")
    r1 = Roster(db_path=path)
    r1.register(owner=ALICE, session_id="s1", machine="mini", repo="pg",
                goal="ship", worktrees=[{"path": "/a"}], now=100.0)
    del r1
    r2 = Roster(db_path=path)
    s = r2.get(ALICE, "s1")
    assert s.goal == "ship"
    assert s.worktrees == [{"path": "/a"}]
    assert s.machine == "mini"


def test_prune_removes_expired_rows_from_disk(tmp_path):
    path = str(tmp_path / "roster.db")
    r1 = Roster(ttl_seconds=60.0, db_path=path)
    r1.register(owner=ALICE, session_id="s1", machine="m", repo="r", now=100.0)
    assert r1.list(owner=ALICE, now=200.0) == []  # pruned on read
    del r1
    assert Roster(ttl_seconds=60.0, db_path=path).get(ALICE, "s1") is None
```

- [ ] **Step 2: Run to verify new tests fail**

Run: `uv run pytest tests/test_roster.py -q`
Expected: FAIL (`Roster.__init__() got an unexpected keyword argument 'db_path'`)

- [ ] **Step 3: Rewrite `standup_board/roster.py`** — keep `Session`, `DEFAULT_TTL_SECONDS`, and the merge-field tuples; swap the dict for a `SessionStore`. Replace the `Roster` class body:

```python
"""Roster of live agent sessions, partitioned per owner, backed by SQLite.

Each owner (a verified GitHub email) has its own isolated set of sessions:
every query filters on owner, so one user can never see or mutate another's.
Storage lives in a SessionStore; this class owns only the merge and TTL policy.
"""

import time
from dataclasses import dataclass

from .store import SessionStore

DEFAULT_TTL_SECONDS: float = 12 * 3600


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


class Roster:
    """Holds live sessions in SQLite, partitioned by owner then session_id."""

    def __init__(
        self, ttl_seconds: float = DEFAULT_TTL_SECONDS, db_path: str = ":memory:"
    ) -> None:
        self._store = SessionStore(db_path)
        self._ttl = ttl_seconds

    _MERGE_FIELDS = (
        "active_branch",
        "last_prompt",
        "goal",
        "current_step",
        "active_pr",
        "worktrees",
    )
    _NARRATIVE_FIELDS = ("goal", "current_step")

    def register(
        self,
        *,
        owner: str,
        session_id: str,
        machine: str | None = None,
        repo: str | None = None,
        now: float | None = None,
        **updates,
    ) -> Session:
        """Upsert a session for this owner, merging fields.

        Read-modify-write: only fields present in ``updates`` (a subset of
        ``_MERGE_FIELDS``) overwrite; everything else on an existing session is
        preserved. ``machine``/``repo`` overwrite only when supplied. The
        narrative timestamp advances only when goal/current_step are written.
        Always refreshes ``registered_at`` (liveness).
        """
        stamp = time.time() if now is None else now
        session = self._store.get(owner, session_id)
        if session is None:
            session = Session(
                owner=owner,
                session_id=session_id,
                machine=machine or "",
                repo=repo or "",
            )
        if machine is not None:
            session.machine = machine
        if repo is not None:
            session.repo = repo
        for key in self._MERGE_FIELDS:
            if key in updates:
                setattr(session, key, updates[key])
        if any(k in updates for k in self._NARRATIVE_FIELDS):
            session.narrative_updated_at = stamp
        session.registered_at = stamp
        self._store.upsert(session)
        return session

    def get(self, owner: str, session_id: str) -> Session | None:
        """Return one of this owner's sessions, or None. Does not prune."""
        return self._store.get(owner, session_id)

    def deregister(self, owner: str, session_id: str) -> None:
        """Remove one of this owner's sessions if present; idempotent.

        Scoped to ``owner`` so a session_id belonging to someone else is never
        touched, even if the ids happen to collide.
        """
        self._store.delete(owner, session_id)

    def list(
        self,
        *,
        owner: str,
        repo: str | None = None,
        now: float | None = None,
    ) -> list[Session]:
        """Return this owner's live sessions sorted by (repo, machine).

        Expired sessions are pruned on read (crash-safety: sessions registered
        before a restart age out naturally; this is not a heartbeat).
        """
        clock = time.time() if now is None else now
        self._store.prune_expired(owner, clock - self._ttl)
        live = self._store.list_owner(owner)
        return [s for s in live if repo is None or s.repo == repo]
```

- [ ] **Step 4: Run the full roster suite**

Run: `uv run pytest tests/test_roster.py -q`
Expected: PASS (all existing + 2 new)

- [ ] **Step 5: Commit**

```bash
git add standup_board/roster.py tests/test_roster.py
git commit -m "Make Roster SQLite-authoritative via SessionStore"
```

---

### Task 3: Wire `STANDUP_DB_PATH` into `create_app`

**Files:**

- Modify: `standup_board/app.py:91-93`
- Test: `tests/test_app.py`

**Interfaces:**

- Consumes: `Roster(ttl_seconds=..., db_path=...)`.
- Produces: `create_app()` reads `STANDUP_DB_PATH`; unset/empty ⇒ `:memory:`; a path ⇒ that file (parent dir created).

- [ ] **Step 1: Add a failing test** to `tests/test_app.py`:

```python
def test_create_app_persists_to_db_path(tmp_path, monkeypatch):
    db = tmp_path / "sub" / "board.db"  # parent dir does not exist yet
    monkeypatch.setenv("STANDUP_DB_PATH", str(db))
    build_app(roster=None)  # let create_app build the Roster from env
    assert db.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_app.py::test_create_app_persists_to_db_path -q`
Expected: FAIL (file not created — Roster still defaults to `:memory:`)

- [ ] **Step 3: Edit `standup_board/app.py`.** Add `import os` is already present. Replace lines 92-93:

```python
    ttl = float(os.environ.get("STANDUP_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    if roster is None:
        db_path = os.environ.get("STANDUP_DB_PATH", "") or ":memory:"
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        roster = Roster(ttl_seconds=ttl, db_path=db_path)
    app.config["ROSTER"] = roster
```

- [ ] **Step 4: Run to verify it passes + full suite**

Run: `uv run pytest tests/test_app.py -q && uv run pytest -q`
Expected: PASS, coverage ≥ 95%

- [ ] **Step 5: Commit**

```bash
git add standup_board/app.py tests/test_app.py
git commit -m "Wire STANDUP_DB_PATH env into create_app"
```

---

### Task 4: Container & docs wiring

**Files:**

- Modify: `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `railway-template.json`, `README.md`

**Interfaces:** none (deploy/docs only).

- [ ] **Step 1: `Dockerfile`** — after `RUN pip install`, add:

```dockerfile
# Presence state lives in a SQLite file. Mount a volume at /var/lib/standup to
# make the board survive restarts; without one it is ephemeral per container.
RUN mkdir -p /var/lib/standup
ENV STANDUP_DB_PATH=/var/lib/standup/standup.db
```

- [ ] **Step 2: `docker-compose.yml`** — add a `volumes:` mount to the service and a top-level named volume:

```yaml
volumes:
  - standup-data:/var/lib/standup
```

```yaml
volumes:
  standup-data:
```

- [ ] **Step 3: `.env.example`** — append:

```bash
# Optional: where the SQLite presence DB lives. The container sets this to
# /var/lib/standup/standup.db; mount a volume there to survive restarts.
# Leave unset when running from source for an in-memory (ephemeral) board.
# STANDUP_DB_PATH=
```

- [ ] **Step 4: `.gitignore`** — append:

```
*.db
*.db-wal
*.db-shm
```

- [ ] **Step 5: `railway-template.json`** — document `STANDUP_DB_PATH` and note a volume at `/var/lib/standup` is needed for durability (match the file's existing structure).

- [ ] **Step 6: `README.md`** — amend the "ephemeral and cross-machine" bullet and the compose/Railway sections: state lives in a SQLite file in the container, ephemeral unless a volume is mounted at `/var/lib/standup`; still TTL-pruned presence, not a system of record.

- [ ] **Step 7: Verify lint + full suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: PASS, coverage ≥ 95%

- [ ] **Step 8: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example .gitignore railway-template.json README.md
git commit -m "Wire /var/lib/standup SQLite volume into container and docs"
```

---

### Task 5: Real-app restart verification

**Files:** none (manual verification).

- [ ] **Step 1** Run the app from source against a temp DB, register a session via the API, restart the process, and confirm the session is still listed. Document the commands and output. (Behavior/UI changes need real-app verification, not just tests.)

## Self-Review

- **Spec coverage:** SessionStore (Task 1), SQLite-authoritative Roster + merge preservation (Task 2), STANDUP_DB_PATH wiring (Task 3), Dockerfile/compose/docs/`.gitignore`/`.env.example`/railway (Task 4), restart verification (Task 5). All spec sections covered.
- **Placeholder scan:** none — every code step shows complete code.
- **Type consistency:** `SessionStore` method names and `Roster(db_path=...)` signature are consistent across Tasks 1–3; `prune_expired` uses strict `< cutoff` matching the original `> ttl` boundary.
