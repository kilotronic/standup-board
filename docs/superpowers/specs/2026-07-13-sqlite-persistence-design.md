# SQLite-backed Roster for restart survival

**Date:** 2026-07-13
**Status:** Approved (design)

## Goal

Let the live board survive a container restart, redeploy, or crash instead of
blanking until every agent next heartbeats. Nothing else about the app's
behavior changes: same HTTP/MCP API, same per-owner isolation, same TTL and
board-freshness semantics.

This is a restart-survival goal, explicitly **not** multi-worker scaling and
**not** a system of record. The board remains TTL-pruned presence.

## Approach

Make the `Roster` **SQLite-authoritative**: drop the in-memory dict and back
every operation with a single SQLite file. Under the deploy model
(`gunicorn --workers 1`, sync worker: one process, one thread, one request at a
time) there is a single writer, so no locking or connection-pooling is needed
and one `sqlite3` connection is race-free.

Durability is entirely a function of _where the file lives_:

- The container always writes to a real file at `/var/lib/standup/standup.db`
  (FHS application-state dir).
- With **no volume**, that file is ephemeral — lost on redeploy, so the board
  simply rebuilds from agents' next heartbeats (today's effective behavior).
- Mount a **volume** at `/var/lib/standup` and the file — and therefore the
  board — survives restarts. That mount is the only knob that changes
  durability.

## Units

Two focused units with a clean policy/persistence split.

### `standup_board/store.py` — `SessionStore` (new)

Pure persistence. Owns the `sqlite3` connection, schema, and the row↔`Session`
mapping. No merge/TTL policy lives here.

Public methods:

- `upsert(session: Session) -> None`
- `get(owner: str, session_id: str) -> Session | None`
- `delete(owner: str, session_id: str) -> None`
- `list_owner(owner: str) -> list[Session]` — ordered `(repo, machine)`
- `prune_expired(owner: str, cutoff: float) -> None` — delete rows with
  `registered_at <= cutoff`

Serialization: `active_pr` (dict) and `worktrees` (list) are stored as JSON
text columns and round-tripped with `json.dumps`/`json.loads`; `None` stays
SQL `NULL`. All other fields map to native columns.

Schema:

```sql
CREATE TABLE IF NOT EXISTS sessions (
  owner                TEXT NOT NULL,
  session_id           TEXT NOT NULL,
  machine              TEXT NOT NULL DEFAULT '',
  repo                 TEXT NOT NULL DEFAULT '',
  active_branch        TEXT,
  last_prompt          TEXT,
  goal                 TEXT,
  current_step         TEXT,
  active_pr            TEXT,   -- JSON
  worktrees            TEXT,   -- JSON
  registered_at        REAL NOT NULL DEFAULT 0,
  narrative_updated_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (owner, session_id)
);
```

The `(owner, session_id)` primary key gives per-owner isolation by construction
(every query filters on `owner`) and makes upsert a natural
`INSERT ... ON CONFLICT(owner, session_id) DO UPDATE`. The PK's `owner` prefix
indexes the per-owner list/prune queries.

Connection setup, run once at construction:

- `PRAGMA journal_mode=WAL` — durable, and tolerant if a read ever overlaps a
  write.
- `PRAGMA synchronous=NORMAL` — sane durability/latency balance for WAL.
- `row_factory = sqlite3.Row` for name-based column access.

A connection or schema failure at construction **raises** — consistent with the
app's existing fail-loud startup (the `SECRET_KEY` check). A broken/unwritable
volume should surface immediately, not silently. Per-request write errors
propagate (500) rather than being swallowed (no silent failures).

### `standup_board/roster.py` — `Roster` (changed)

Keeps its **exact** public API (`register`, `get`, `deregister`, `list`) and all
policy. Removes the `_by_owner` dict; holds a `SessionStore` instead.

- `register(...)` stays a **read-modify-write** to preserve the current merge
  semantics byte-for-byte: `store.get()` the existing row (or build a fresh
  `Session`), apply the identical `_MERGE_FIELDS`/`_NARRATIVE_FIELDS` logic and
  `registered_at` refresh in Python, then `store.upsert()` the full row. Using
  the store's UPSERT directly would wipe unspecified fields; the read-modify-write
  keeps "only provided fields overwrite, the rest are preserved."
- `get(...)` → `store.get(...)`.
- `deregister(...)` → `store.delete(...)` (still idempotent, still owner-scoped).
- `list(...)` → `store.prune_expired(owner, now - ttl)` then `store.list_owner(owner)`,
  filtered by `repo` when given. Prune-on-read behavior is unchanged; it now also
  removes expired rows from disk so the DB does not grow unbounded.

Constructor: `Roster(ttl_seconds: float = DEFAULT_TTL_SECONDS, db_path: str = ":memory:")`.
Default `:memory:` preserves today's ephemeral, per-instance-isolated behavior,
so bare `Roster()` and the existing test suite need no changes.

## Config & wiring

The `STANDUP_DB_PATH` env var is the single knob. `create_app` reads it; unset or
empty ⇒ `:memory:` (current behavior for from-source / LAN runs), a path ⇒ that
file (parent dir created defensively with `os.makedirs(..., exist_ok=True)`).

- **`Dockerfile`:** `RUN mkdir -p /var/lib/standup` and
  `ENV STANDUP_DB_PATH=/var/lib/standup/standup.db`. Every container therefore
  persists to that file by default — ephemeral with no volume, durable with one.
- **`docker-compose.yml`:** a named volume mounted at `/var/lib/standup`, so a
  self-hosted `docker compose up` survives restarts out of the box.
- **`railway.json` / `railway-template.json` / `README.md`:** document attaching
  a Railway volume at `/var/lib/standup`; note that without it the board rebuilds
  from heartbeats after a redeploy.
- **`.env.example`:** add a commented `STANDUP_DB_PATH` with the default and a
  one-line explanation.
- **`.gitignore`:** ignore local `*.db`, `*.db-wal`, `*.db-shm`.

## README framing

The README's "state lives in memory on one server process" becomes "state lives
in a SQLite file inside the container — ephemeral unless you mount a volume at
`/var/lib/standup`." The spirit is unchanged: TTL-pruned presence with a 4h
board window, not history, not a system of record. Update the relevant
"ephemeral and cross-machine" bullet and the compose/Railway sections.

## Testing

- **Existing tests unchanged.** `test_roster.py` (bare `Roster()`),
  `test_app.py`, and the rest keep passing because `:memory:` reproduces the old
  ephemeral semantics per instance.
- **New `test_store.py`** for `SessionStore` in isolation: upsert/get/delete,
  `list_owner` ordering, `prune_expired`, JSON round-trip of `active_pr` and
  `worktrees`, and `NULL` handling for absent optional fields.
- **New roster/persistence tests:**
  - _Restart round-trip:_ register sessions on a temp-file DB, drop the `Roster`,
    open a new `Roster` on the same `db_path`, assert the sessions are present and
    fully intact.
  - _Merge preservation across the store:_ a second `register` with a subset of
    fields leaves the others intact (guards the read-modify-write).
  - _On-disk pruning:_ an expired session is gone from disk after `list()`.
  - _`create_app` wiring:_ `STANDUP_DB_PATH` set ⇒ durable file used; unset ⇒
    `:memory:`.

Use `tmp_path` fixtures for file-backed DBs so nothing leaks between tests.

## Non-goals

- No multi-worker or multi-replica support; still `--workers 1`, single writer.
- No external database or network dependency.
- No historical/audit retention; TTL pruning and the 4h board window are
  unchanged. This is durable presence, not a system of record.
