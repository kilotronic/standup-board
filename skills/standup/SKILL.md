---
name: standup
description: Use throughout any coding session to keep your presence current on the standup board — post a goal and step whenever work materially changes, and consult the board before a rebase or arming auto-merge on a shared repo. standup is an ongoing async check-in that coordinates Claude Code agents across machines that don't share a memory store.
---

# Standup presence

standup is an ongoing async check-in that coordinates Claude Code agents
across machines that don't share a memory store. The `SessionStart`/`SessionEnd`
hooks keep your presence and git facts (worktrees, PRs) current automatically.
The _narrative_ — what you're actually doing — is yours to post.

It's fail-safe and silent: it never blocks you, and an unreachable board just
no-ops. Don't post a goal that merely restates the last prompt — the board
already shows that separately.

## Post your narrative when work materially changes

When work materially changes (starting a task, switching worktree/branch, the
focus shifting), post a real goal and current step:

```
standup status --goal '<the session goal>' --step '<what you're doing now>'
```

(or the `update_status` MCP tool). Run it from your active worktree so the
branch and PR are detected correctly.

Keep `--goal` stable across the session; update `--step` as you progress. The
goal is the destination, the step is your current position.

## Consult the board before a rebase or auto-merge

Before `/rebase-arm-automerge` or arming auto-merge on a repo, run
`standup list` first. Concurrent rebases/merges on the same repo race each
other, and an auto-merge armed here can land on top of work another session is
mid-flight. Advisory, not blocking: if it lists other active sessions on this
repo, surface them and ask before proceeding — especially if a listed session's
task overlaps. With no other sessions (or no board), continue.

```
standup list          # board for the current repo
standup list --all    # every repo
```

The `list_sessions` MCP tool does the same — use it to ask "who else is on this
repo right now?" mid-session.
