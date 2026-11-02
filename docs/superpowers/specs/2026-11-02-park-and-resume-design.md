# Park and resume: hand off work stopped by a usage limit

## The story

A session on lucy hits its Claude usage limit mid-task. Today it sits until the limit resets
(up to 5 hours, or days for a weekly limit). Partygame's rule today is "a usage limit goes
to the owner; after it resets, resume each lane". Meanwhile another machine on another
account may be idle, and nothing on the board says the work is stuck or where it is.

With this change the stuck session posts a **parked** row by itself. Any new session on
the repo is told at start that parked work is waiting, and `standup resume` takes it in one
step and prints how to continue.

## Prior art

- Hapimatic (hapimatic.com) moves a whole session between accounts. It does that by running
  every agent under its own server. Too big for us, and Linux only.
- Claude Code's `--teleport` moves cloud sessions only, and only within one account.
- `standup claim`: we reuse its approach, a row's step as the claim, and add one
  server-side atomic "take" for the handoff.

## Mechanism

- **Park.** A Claude Code `StopFailure` hook with matcher `rate_limit` runs `standup park`.
  An agent or a person can also run it by hand (`--reason`). The hook ignores subagent
  failures (input carries `agent_id`). It posts a _second_ row, `parked:<session id>`, of
  type `parked`. The row copies the live row's goal, step and branch. Its `handoff` holds
  the reason (the stop message, which shows the reset time), the original session id, the
  worktree path, and git state: uncommitted files, unpushed commits, whether an upstream
  exists. The live row's lifecycle is unchanged.
- **Kept a week.** Parked rows expire after `STANDUP_PARKED_TTL_SECONDS` (default 7 days),
  not the 12h session TTL, and the web board does not hide them after 4h.
- **Unpark on return.** When the original session posts a goal, a step or a real prompt,
  the server deletes its parked twin. Heartbeats and system wakeups don't count.
- **Resume.** `standup resume` lists the repo's parked rows. `standup resume <id>` calls
  `POST /sessions/<id>/take`, which deletes the row and returns it. Only the first taker
  gets it; anyone else gets a 404. The taker posts the row's goal and step as its own, so a
  `CLAIMED:` step carries over. It then prints how to continue: on the same machine,
  `claude --resume <id>` in that worktree; on another machine, fetch the branch, but only
  if nothing exists solely on the parked machine.
- **Discovery.** At session start, the co-worker notice also lists parked work on the repo.

## Invariants

1. A parked row never counts as an active agent, so the rebase and auto-merge checks are
   unchanged.
2. A parked item stays claimed: `standup claim` still sees its `CLAIMED:` step.
3. Only one session can take a parked row.
4. A parked row never outlives its session's return.

## Scenarios

| Case                                                     | Result                                                                       | Test                                                                                |
| -------------------------------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Limit hits on the main session                           | parked row with reason and git state                                         | `test_park_from_stop_failure_hook_posts_parked_twin`                                |
| Limit hits in a subagent                                 | nothing posted                                                               | `test_park_ignores_subagent_failures`                                               |
| Two sessions resume at once                              | one gets the row, the other gets 404                                         | `test_take_is_single_winner`                                                        |
| Original session continues after reset                   | twin deleted on its next goal, step or prompt                                | `test_narrative_post_unparks_twin`; heartbeat keeps it: `test_heartbeat_keeps_twin` |
| Original session quits while parked                      | twin survives SessionEnd                                                     | `test_deregister_keeps_parked_twin`                                                 |
| Parked 2 days                                            | still listed; gone after 7                                                   | `test_parked_rows_use_their_own_ttl`                                                |
| Uncommitted work, other machine resumes                  | told the work exists only on lucy                                            | `test_resume_warns_when_work_is_only_on_the_parked_machine`                         |
| Session never reported its branch (hook runs in the hub) | git state marked unknown; resume says "resume it there"                      | `test_park_without_a_reported_branch_marks_the_work_unknown`                        |
| Taken while the original row still shows `CLAIMED:`      | the claim moves to the taker                                                 | `test_take_hands_the_claim_to_the_taker`                                            |
| `resume <id>` from a plain terminal                      | refused, row kept (nothing would carry its claim)                            | `test_resume_refuses_without_a_session_to_carry_the_claim`                          |
| Board fails after the take                               | the hint still prints, plus the status line to post                          | `test_resume_still_prints_the_hint_when_the_status_post_fails`                      |
| Original restarts with `claude --resume`                 | its own parked row is left out of its notice; it unparks on its first prompt | `test_register_notice_skips_this_sessions_own_parked_row`                           |
| Old client, new server                                   | parked type shows as its own section; no breakage                            | none (additive fields)                                                              |
| Board down                                               | park and resume say so and exit 0                                            | `test_park_is_silent_when_board_down`                                               |

**Known gap:** at the reset time, Claude Code's "continue automatically" option restarts
the original session even after someone else took the work. `resume` says to stop it
first. Detecting it automatically needs a record that the work was taken (new state);
deferred until it bites.

**Follow-up (filed, not built):** the `parked:` id prefix lives in both the client and the
server. A server-side `POST /sessions/<id>/park` that copies the live row would keep it
in one place and save the hook a round trip.

## Provenance

| Fact                  | Written by                             | Stored                      | Read by                                           |
| --------------------- | -------------------------------------- | --------------------------- | ------------------------------------------------- |
| parked row            | `park` (hook or by hand)               | board SQLite, `parked:<id>` | `list`, session-start notice, web board, `resume` |
| git state             | `park`, from git at park time          | `handoff`                   | `resume`'s instructions                           |
| reason and reset time | the hook's stop message, or `--reason` | `handoff.reason`            | everyone                                          |

## Trade-offs

- **No automatic push at the limit.** A hook can't run the repo's gates, and partygame
  never pushes ungated work. `resume` tells the taker what exists only on the parked
  machine.
- One new nullable column (`handoff`) and one new endpoint. Build size: one stdlib
  client script, no new dependency.

## Falsifier

If, after two weeks of use, parked work still waits for its own session's reset
because no one runs `resume` (fewer than one resume a week), delete the feature.

## PRs

1. This one: server (column, TTLs, take, unpark) + client (`park`, `resume`, hook,
   notice) + docs.
