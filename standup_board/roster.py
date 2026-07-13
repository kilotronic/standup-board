"""In-memory roster of live agent sessions, partitioned per owner.

Each owner (a verified GitHub email) has its own isolated set of sessions:
list/register/deregister only ever touch the calling owner's sessions, so one
user can never see or mutate another's by construction.
"""

import time
from dataclasses import dataclass

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
    """Holds live sessions in memory, partitioned by owner then session_id."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        # owner -> {session_id -> Session}. The outer key isolates owners.
        self._by_owner: dict[str, dict[str, Session]] = {}
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

        Only the optional fields present in ``updates`` (a subset of
        ``_MERGE_FIELDS``) overwrite; everything else on an existing session is
        preserved. ``machine``/``repo`` overwrite only when supplied. The
        narrative timestamp advances only when goal/current_step are written.
        Always refreshes ``registered_at`` (liveness).
        """
        stamp = time.time() if now is None else now
        sessions = self._by_owner.setdefault(owner, {})
        session = sessions.get(session_id)
        if session is None:
            session = Session(
                owner=owner,
                session_id=session_id,
                machine=machine or "",
                repo=repo or "",
            )
            sessions[session_id] = session
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
        return session

    def get(self, owner: str, session_id: str) -> Session | None:
        """Return one of this owner's sessions, or None. Does not prune."""
        return self._by_owner.get(owner, {}).get(session_id)

    def deregister(self, owner: str, session_id: str) -> None:
        """Remove one of this owner's sessions if present; idempotent.

        Scoped to ``owner`` so a session_id belonging to someone else is never
        touched, even if the ids happen to collide.
        """
        sessions = self._by_owner.get(owner)
        if sessions is not None:
            sessions.pop(session_id, None)

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
        sessions = self._by_owner.get(owner)
        if not sessions:
            return []
        clock = time.time() if now is None else now
        expired = [
            sid for sid, s in sessions.items() if clock - s.registered_at > self._ttl
        ]
        for sid in expired:
            del sessions[sid]
        live = [s for s in sessions.values() if repo is None or s.repo == repo]
        return sorted(live, key=lambda s: (s.repo, s.machine))
