import sqlite3

import pytest

from standup_board.roster import Session
from standup_board.store import SessionStore

ALICE = "alice@example.com"


def _sess(session_id="s1", **kw):
    base = dict(
        owner=ALICE,
        session_id=session_id,
        machine="mini",
        repo="pg",
        registered_at=100.0,
    )
    base.update(kw)
    return Session(**base)


def test_upsert_then_get_round_trips_all_fields():
    store = SessionStore()
    store.upsert(
        _sess(
            active_pr={"url": "http://x/1", "number": 1},
            worktrees=[{"path": "/a", "branch": "main", "pr": None}],
            goal="g",
            current_step="c",
            active_branch="b",
            last_prompt="p",
            narrative_updated_at=110.0,
        )
    )
    got = store.get(ALICE, "s1")
    assert got == _sess(
        active_pr={"url": "http://x/1", "number": 1},
        worktrees=[{"path": "/a", "branch": "main", "pr": None}],
        goal="g",
        current_step="c",
        active_branch="b",
        last_prompt="p",
        narrative_updated_at=110.0,
    )


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
        ("dot", "zed"),
        ("pg", "mini"),
        ("pg", "studio"),
    ]


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


def test_close_releases_connection_and_is_idempotent():
    store = SessionStore()
    store.close()
    store.close()  # must not raise
    with pytest.raises(sqlite3.ProgrammingError):
        store.get(ALICE, "s1")  # connection is closed


def test_gc_does_not_warn_about_unclosed_db(recwarn):
    SessionStore()  # dropped immediately; finalizer must close it
    import gc

    gc.collect()
    assert not [w for w in recwarn.list if w.category is ResourceWarning]
