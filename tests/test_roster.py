from standup_board.roster import Roster

ALICE = "alice@example.com"
BOB = "bob@example.com"


def test_register_adds_a_session():
    roster = Roster()
    session = roster.register(
        owner=ALICE, session_id="s1", machine="mini", repo="partygame", now=100.0
    )
    assert session.owner == ALICE
    assert session.session_id == "s1"
    assert session.machine == "mini"
    assert session.repo == "partygame"
    assert session.registered_at == 100.0


def test_register_same_id_upserts():
    roster = Roster()
    roster.register(
        owner=ALICE, session_id="s1", machine="mini", repo="partygame", now=100.0
    )
    updated = roster.register(
        owner=ALICE,
        session_id="s1",
        machine="mini",
        repo="partygame",
        last_prompt="timer fix",
        now=200.0,
    )
    assert updated.last_prompt == "timer fix"
    assert updated.registered_at == 200.0
    assert len(roster.list(owner=ALICE, now=200.0)) == 1


def test_deregister_removes_session():
    roster = Roster()
    roster.register(owner=ALICE, session_id="s1", machine="mini", repo="partygame")
    roster.deregister(ALICE, "s1")
    assert roster.list(owner=ALICE) == []


def test_deregister_missing_is_noop():
    roster = Roster()
    roster.deregister(ALICE, "nope")  # must not raise
    assert roster.list(owner=ALICE) == []


def test_list_returns_sorted_sessions():
    roster = Roster()
    roster.register(
        owner=ALICE, session_id="s2", machine="studio", repo="partygame", now=100.0
    )
    roster.register(
        owner=ALICE, session_id="s1", machine="mini", repo="partygame", now=100.0
    )
    listed = roster.list(owner=ALICE, now=100.0)
    assert [s.machine for s in listed] == ["mini", "studio"]


def test_list_filters_by_repo():
    roster = Roster()
    roster.register(
        owner=ALICE, session_id="s1", machine="mini", repo="partygame", now=100.0
    )
    roster.register(
        owner=ALICE, session_id="s2", machine="mini", repo="dotfiles", now=100.0
    )
    listed = roster.list(owner=ALICE, repo="partygame", now=100.0)
    assert [s.session_id for s in listed] == ["s1"]


def test_list_prunes_expired_entries():
    roster = Roster(ttl_seconds=60.0)
    roster.register(
        owner=ALICE, session_id="s1", machine="mini", repo="partygame", now=100.0
    )
    # 100s later, past the 60s TTL
    listed = roster.list(owner=ALICE, now=200.0)
    assert listed == []


# --- per-owner isolation ---


def test_list_only_returns_own_sessions():
    roster = Roster()
    roster.register(owner=ALICE, session_id="a1", machine="mini", repo="shared")
    roster.register(owner=BOB, session_id="b1", machine="studio", repo="shared")
    assert [s.session_id for s in roster.list(owner=ALICE)] == ["a1"]
    assert [s.session_id for s in roster.list(owner=BOB)] == ["b1"]


def test_owner_cannot_deregister_another_owners_session():
    roster = Roster()
    roster.register(owner=ALICE, session_id="a1", machine="mini", repo="shared")
    # Bob tries to delete Alice's session by id — must not touch it.
    roster.deregister(BOB, "a1")
    assert [s.session_id for s in roster.list(owner=ALICE)] == ["a1"]


def test_same_session_id_isolated_across_owners():
    # Two owners can independently use the same session_id without collision.
    roster = Roster()
    roster.register(owner=ALICE, session_id="dup", machine="mini", repo="a", now=1.0)
    roster.register(owner=BOB, session_id="dup", machine="studio", repo="b", now=1.0)
    assert roster.list(owner=ALICE, now=1.0)[0].repo == "a"
    assert roster.list(owner=BOB, now=1.0)[0].repo == "b"


def test_list_for_unknown_owner_is_empty():
    roster = Roster()
    assert roster.list(owner="nobody@example.com") == []


def test_register_merges_without_clobbering():
    roster = Roster()
    # facts writer posts worktrees + last_prompt
    roster.register(
        owner=ALICE,
        session_id="s1",
        machine="mini",
        repo="pg",
        last_prompt="do the thing",
        worktrees=[{"path": "/a", "branch": "main", "pr": None}],
        now=100.0,
    )
    # narrative writer posts goal/current_step/active_branch — must NOT wipe facts
    s = roster.register(
        owner=ALICE,
        session_id="s1",
        machine="mini",
        repo="pg",
        goal="ship the timer",
        current_step="writing tests",
        active_branch="feat/timer",
        now=110.0,
    )
    assert s.goal == "ship the timer"
    assert s.current_step == "writing tests"
    assert s.active_branch == "feat/timer"
    assert s.last_prompt == "do the thing"  # preserved
    assert s.worktrees == [{"path": "/a", "branch": "main", "pr": None}]  # preserved
    # facts writer posts again without narrative — must NOT wipe goal/step
    s2 = roster.register(
        owner=ALICE,
        session_id="s1",
        machine="mini",
        repo="pg",
        last_prompt="next prompt",
        now=120.0,
    )
    assert s2.goal == "ship the timer"  # preserved
    assert s2.current_step == "writing tests"  # preserved
    assert s2.last_prompt == "next prompt"  # updated


def test_narrative_timestamp_only_set_on_narrative_writes():
    roster = Roster()
    roster.register(
        owner=ALICE, session_id="s1", machine="m", repo="r", last_prompt="p", now=100.0
    )
    s = roster.get(ALICE, "s1")
    assert s.narrative_updated_at == 0.0  # facts-only write
    roster.register(
        owner=ALICE, session_id="s1", machine="m", repo="r", goal="g", now=150.0
    )
    assert roster.get(ALICE, "s1").narrative_updated_at == 150.0


def test_register_update_preserves_machine_and_repo():
    roster = Roster()
    roster.register(owner=ALICE, session_id="s1", machine="mini", repo="pg", now=1.0)
    s = roster.register(owner=ALICE, session_id="s1", goal="g", now=2.0)
    assert s.machine == "mini" and s.repo == "pg"  # preserved when omitted
