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
