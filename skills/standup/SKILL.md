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

Run it from your active worktree so the
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

## When you finish, say so

A session left open after its work merged still counts as an active co-worker in every
new session's start-up notice and every rebase or auto-merge check. When you are done
and idle, run:

```
standup done --step 'Done: #123 merged'
```

The row stays listed, in a **done** section, but no longer counts. Any later prompt,
status post or subagent start or stop makes you active again. Only the main session
runs it: a subagent shares your session id.

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
derives `<parent>:<label>` from it. `standup list` then prints it indented under you (`↳`), with its
own branch and PR. A child row whose parent is not in the listing still prints at top
level rather than vanishing — hiding live work is the failure that matters. **Cleanup is
automatic**: your own `SessionEnd` deregister removes your child rows with you.

For a read-only subagent (an Explore, a review pass) the nested label is enough — skip
this.

## Claim a work item before starting it

Several sessions picking from one shared todo list will otherwise start the same
item minutes apart. Before starting an item, claim it:

```
standup claim '<item>'
```

Exit 0 means it is yours: your step now reads `CLAIMED: <item>`. Exit 1 means
another session holds it (the error names who) — pick another item. A claim
matches regardless of spacing and case, and when two sessions claim the same item
at once, the earlier claim wins and the loser's previous step is restored.
`standup list | grep CLAIMED` shows every claim. Update your step as you work;
a claim ends when your step changes or your session ends.

## Usage limits: parked work and `standup resume`

When a session stops on a usage limit, a `StopFailure` hook runs `standup park`. It posts
a **parked** row with the session's goal, step and branch, the stop message (which shows
the reset time), and what exists only on its machine: uncommitted files and unpushed
commits. A parked item stays claimed. When the original session posts a goal, a step or
a prompt again, its parked row is removed by itself.

The session-start notice lists parked work on the repo. To take one over:

```
standup resume            # list parked work on this repo
standup resume <id>       # take it (a prefix of the id is enough)
```

Exit 1 means another session took it first. On success your goal and step become the
parked ones, and it prints how to continue: `claude --resume <id>` on the same machine,
or fetching the branch on another. It warns when the work is not pushed. **Stop the
original session first if it is still open**; otherwise it continues by itself at its
reset. Park by hand before a known stop with `standup park --reason '<why>'`.

## Consult the board before a rebase or auto-merge

Before a rebase or arming auto-merge on a repo, run `standup list` first.
Concurrent rebases/merges on the same repo race each other, and an auto-merge
armed here can land on top of work another session is mid-flight. Advisory, not
blocking: if it lists other active sessions on this repo, name them in one line
and continue; the board is not a lock. Stop to ask only if your user's
instructions say to.

```
standup list          # board for the current repo
standup list --all    # every repo
```
