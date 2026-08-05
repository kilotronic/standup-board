"""Roster of live agent sessions, partitioned per owner, backed by SQLite.

Each owner (a verified GitHub email) has its own isolated set of sessions:
every query filters on owner, so one user can never see or mutate another's.
Storage lives in a SessionStore; this class owns only the merge and TTL policy.
"""

import time

from .store import Session, SessionStore

# Re-exported so existing importers keep using ``roster.Session``.
__all__ = ["DEFAULT_TTL_SECONDS", "Roster", "Session"]

DEFAULT_TTL_SECONDS: float = 12 * 3600
SUBAGENT_STALE_SECONDS: float = 30 * 60


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
        type: str | None = None,
        now: float | None = None,
        **updates,
    ) -> Session:
        """Upsert a session for this owner, merging fields.

        Read-modify-write: only fields present in ``updates`` (a subset of
        ``_MERGE_FIELDS``) overwrite; everything else on an existing session is
        preserved. ``machine``/``repo``/``type`` overwrite only when supplied. The
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
        for key in self._MERGE_FIELDS:
            if key in updates:
                setattr(session, key, updates[key])
        if any(k in updates for k in self._NARRATIVE_FIELDS):
            session.narrative_updated_at = stamp
        session.registered_at = stamp
        self._store.upsert(session)
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
        before a restart age out naturally; this is not a heartbeat). Stale
        subagent entries (no matching stop within SUBAGENT_STALE_SECONDS) are
        filtered out of the returned objects only — never deleted from storage,
        so a late stop still finds and removes the real row if it ever arrives.
        """
        clock = time.time() if now is None else now
        self._store.prune_expired(owner, clock - self._ttl)
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
