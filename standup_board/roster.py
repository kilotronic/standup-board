"""Roster of live agent sessions, partitioned per owner, backed by SQLite.

Each owner (a verified GitHub email) has its own isolated set of sessions:
every query filters on owner, so one user can never see or mutate another's.
Storage lives in a SessionStore; this class owns only the merge and TTL policy.
"""

import time

from .store import Session, SessionStore

# Re-exported so existing importers keep using ``roster.Session``.
__all__ = ["DEFAULT_PARKED_TTL_SECONDS", "DEFAULT_TTL_SECONDS", "Roster", "Session"]

DEFAULT_TTL_SECONDS: float = 12 * 3600
# A parked row waits for a usage limit to reset, which can be a weekly one.
DEFAULT_PARKED_TTL_SECONDS: float = 7 * 86400
PARKED_PREFIX = "parked:"
SUBAGENT_STALE_SECONDS: float = 30 * 60


def _wake(session: Session) -> None:
    """A session whose subagents start or stop is working, so not `done`.

    A subagent shares its parent's session id, so a `standup done` it runs
    lands on the parent; this undoes it when that subagent stops.
    """
    if session.type == "done":
        session.type = "agent"


class Roster:
    """Holds live sessions in SQLite, partitioned by owner then session_id."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        db_path: str = ":memory:",
        parked_ttl_seconds: float = DEFAULT_PARKED_TTL_SECONDS,
    ) -> None:
        self._store = SessionStore(db_path)
        self._ttl = ttl_seconds
        self._parked_ttl = parked_ttl_seconds

    _MERGE_FIELDS = (
        "active_branch",
        "last_prompt",
        "goal",
        "current_step",
        "active_pr",
        "worktrees",
        "handoff",
    )
    _NARRATIVE_FIELDS = ("goal", "current_step")
    # A session posting any of these is working again, so it is no longer parked.
    _RETURN_FIELDS = ("goal", "current_step", "last_prompt")

    def register(
        self,
        *,
        owner: str,
        session_id: str,
        machine: str | None = None,
        repo: str | None = None,
        type: str | None = None,
        parent_session_id: str | None = None,
        now: float | None = None,
        **updates,
    ) -> Session:
        """Upsert a session for this owner, merging fields.

        Read-modify-write: only fields present in ``updates`` (a subset of
        ``_MERGE_FIELDS``) overwrite; everything else on an existing session is
        preserved. ``machine``/``repo``/``type``/``parent_session_id``
        overwrite only when supplied — a subagent posts ``status`` repeatedly
        and only the first call need carry its parent. The
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
        if type is not None:
            session.type = type
        if parent_session_id is not None:
            session.parent_session_id = parent_session_id
        for key in self._MERGE_FIELDS:
            if key in updates:
                setattr(session, key, updates[key])
        if any(k in updates for k in self._NARRATIVE_FIELDS):
            session.narrative_updated_at = stamp
        session.registered_at = stamp
        self._store.upsert(session)
        if type == "done":
            # A child row has no hook of its own: a finished parent finishes it.
            for child in self._store.list_owner(owner):
                if child.parent_session_id == session_id and child.type == "agent":
                    child.type = "done"
                    self._store.upsert(child)
        if any(updates.get(k) for k in self._RETURN_FIELDS):
            self._store.delete(owner, PARKED_PREFIX + session_id)
        return session

    def take(self, owner: str, session_id: str) -> Session | None:
        """Remove and return one parked row; None if absent or not parked.

        Single-writer SQLite makes the get-then-delete atomic, so of two
        sessions resuming the same work only the first gets it.
        """
        session = self._store.get(owner, session_id)
        if session is None or session.type != "parked":
            return None
        self._store.delete(owner, session_id)
        # The original's live row still shows the claim with an older stamp,
        # which would make it the holder; hand the claim to the taker.
        original = self._store.get(owner, session_id.removeprefix(PARKED_PREFIX))
        claim = session.current_step or ""
        if (
            claim.startswith("CLAIMED:")
            and original is not None
            and original.current_step == claim
        ):
            original.current_step = "handed off: resumed by another session"
            self._store.upsert(original)
        return session

    def start_subagent(
        self,
        *,
        owner: str,
        session_id: str,
        agent_id: str,
        label: str,
        now: float | None = None,
    ) -> Session:
        """Add (or replace) one active-subagent entry on this session.

        Auto-creates a minimal session row if none exists yet — same fail-safe
        stance as register(): a subagent starting just before SessionStart
        completes is a race to tolerate, not an error.
        """
        stamp = time.time() if now is None else now
        session = self._store.get(owner, session_id)
        if session is None:
            session = Session(owner=owner, session_id=session_id, machine="", repo="")
        existing = [a for a in (session.subagents or []) if a["agent_id"] != agent_id]
        existing.append({"agent_id": agent_id, "label": label, "started_at": stamp})
        session.subagents = existing
        _wake(session)
        self._store.upsert(session)
        return session

    def stop_subagent(
        self, owner: str, session_id: str, agent_id: str
    ) -> Session | None:
        """Remove one active-subagent entry; no-op if the session or entry is gone."""
        session = self._store.get(owner, session_id)
        if session is None:
            return None
        session.subagents = [
            a for a in (session.subagents or []) if a["agent_id"] != agent_id
        ]
        _wake(session)
        self._store.upsert(session)
        return session

    def get(self, owner: str, session_id: str) -> Session | None:
        """Return one of this owner's sessions, or None. Does not prune."""
        return self._store.get(owner, session_id)

    def deregister(self, owner: str, session_id: str) -> None:
        """Remove one of this owner's sessions if present; idempotent.

        Scoped to ``owner`` so a session_id belonging to someone else is never
        touched, even if the ids happen to collide.

        Cascades to this session's SUBAGENT rows: the parent's SessionEnd is
        the only cleanup they can get, because a subagent has no hook of its
        own and does not choose the id it posts under. Without the cascade a
        finished worker's row outlives its session until the TTL.
        """
        self._store.delete(owner, session_id)
        self._store.delete_children(owner, session_id)

    def list(
        self,
        *,
        owner: str,
        repo: str | None = None,
        now: float | None = None,
    ) -> list[Session]:
        """Return this owner's live sessions sorted by (repo, machine).

        Expired sessions are pruned on read (crash-safety: sessions registered
        before a restart age out naturally; this is not a heartbeat). Stale
        subagent entries (no matching stop within SUBAGENT_STALE_SECONDS) are
        filtered out of the returned objects only — never deleted from storage,
        so a late stop still finds and removes the real row if it ever arrives.
        """
        clock = time.time() if now is None else now
        self._store.prune_expired(owner, clock - self._ttl, clock - self._parked_ttl)
        live = self._store.list_owner(owner)
        result = [s for s in live if repo is None or s.repo == repo]
        for s in result:
            if s.subagents:
                s.subagents = [
                    a
                    for a in s.subagents
                    if clock - a["started_at"] <= SUBAGENT_STALE_SECONDS
                ]
        return result
