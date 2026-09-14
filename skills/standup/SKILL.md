---
name: standup
description: Use throughout any coding session to keep your presence current on the standup board — post a goal and step whenever work materially changes, and consult the board before a rebase or arming auto-merge on a shared repo. standup is an ongoing async check-in that coordinates Claude Code and Cursor agents across machines that don't share a memory store.
---

# Standup presence

standup is an ongoing async check-in that coordinates Claude Code and Cursor
agents across machines that don't share a memory store. Session start/end
hooks (Claude Code settings and/or Cursor `hooks.json`) keep your presence and
git facts (worktrees, PRs) current automatically. The _narrative_ — what you're
actually doing — is yours to post.

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
branch and PR are detected correctly. Session id comes from
`$CLAUDE_CODE_SESSION_ID` or `$CURSOR_CONVERSATION_ID` automatically; without
one (e.g. opencode, which sets neither) `standup status` falls back to the
agent process id (`$OPENCODE_PID`), so your row still lands on the board and
stays one row across tool calls.

Keep `--goal` stable across the session; update `--step` as you progress. The
goal is the destination, the step is your current position.

Agent sessions are `type=agent` (the default — no flag needed). The board and
`standup list` group sessions by type, and coordination surfaces (the SessionStart
co-worker warning, rebase/auto-merge checks) count agents only. Non-agent sessions
(e.g. CI runners posting `type=runner`) appear on the board for visibility but are
never treated as coordination peers.

## Subagent activity is automatic

Subagents you dispatch (Explore, general-purpose, fork, etc.) show up nested under this
session's row on the board while they run — `SubagentStart`/`SubagentStop` hooks post
and clear them for you. No action needed; this is just so "N subagents active" makes
sense when you see it.

## A subagent doing real work needs its own row, and you must give it one

**Never let a subagent run a bare `standup status`.** A subagent's shell inherits the
parent's `$CLAUDE_CODE_SESSION_ID`, so a bare `status` posts to the PARENT's row: it
overwrites your goal and step with its own, and several workers overwrite each other.
The label the hooks post above is all the board gets for free.

When you dispatch a subagent that holds its own worktree, branch or PRs, it is a
coordination peer in its own right — the thing the board exists to make visible. Tell it
in its prompt to post as a child row, with a label you choose:

```
standup status --as-subagent '<label>' --goal '<its goal>' --step '<where it is>'
```

One flag, and no session id to paste: the subagent inherits yours, so `--as-subagent`
derives `<parent>:<label>` from it. (The `update_status` MCP tool takes the same
`as_subagent` label.) `standup list` then prints it indented under you (`↳`), with its
own branch and PR. A child row whose parent is not in the listing still prints at top
level rather than vanishing — hiding live work is the failure that matters. **Cleanup is
automatic**: your own `SessionEnd` deregister removes your child rows with you.

For a read-only subagent (an Explore, a review pass) the nested label is enough — skip
this.

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
