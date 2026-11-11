# `standup done`: a finished session stops counting as active

> **Superseded 2026-11-10** by the product call this note filed under _Known gaps_: `done`
> is deleted, and a row with no prompt, status post or subagent start or stop for 2 hours
> lists as `idle` (`Roster.list`, `IDLE_SECONDS`). What pushed it: on the partygame board,
> 36 rows counted as active co-workers though their work had landed, their helpers had
> handed back, or their scheduled run had stopped at a usage limit; none ran `done`. The
> trade-off it accepts: a session finished minutes ago counts until 2 hours pass. Older
> clients still post `done`, which lists as idle.

## The story

A session finishes its work (its PRs merged) but its terminal stays open. Today it
counts as an active co-worker until it closes or its row expires after 12h. On
2026-11-02, 2 of the 10 partygame rows said "Done…merged", one of them idle for 11h.
Every new session's start-up notice and every pre-merge check named them.

## Prior art

The board already sorts rows by `type` (`agent`, `runner`, `parked`), and only `agent`
counts as a co-worker. `done` is one more value of that existing field. There is no
new column.

## Rules

- `standup done [--step TEXT]` sets the row's type to `done` and its step (default
  "done").
- **Any activity makes it active again.** Every agent `register` (session start, any
  prompt, including system wakeups) and every `status`, `claim` or `resume` post sends
  `type=agent`. Runners pass `--type runner` explicitly and are never changed.
- **A subagent's start or stop clears it too** (server side). A subagent shares its
  parent's session id, so a `done` it runs lands on the parent. The parent becomes
  active again the moment that subagent stops. Only the main session is told to run
  `done`.

Rejected: refusing `done` while the row lists running subagents. The board hides
subagent entries after 30 minutes, so long subagents slip through. It also stranded a
main session that ran `done` with a background subagent still finishing (review
round 2).

## Scenarios

| Case                         | Result                            | Test                                                                                                         |
| ---------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Finished session runs done   | listed under done, not counted    | `test_done_marks_the_row_done_with_its_step`, `test_register_notice_does_not_count_done_sessions`            |
| Done session gets any prompt | active again                      | `test_real_prompt_makes_a_done_session_active_again`, `test_system_wakeup_makes_a_done_session_active_again` |
| Done session posts status    | active again                      | `test_status_makes_a_done_session_active_again`                                                              |
| A subagent runs done         | parent active again when it stops | `test_subagent_activity_makes_a_done_session_active_again`                                                   |
| CI runner registers          | stays a runner                    | `test_runner_register_keeps_its_type`                                                                        |

## Provenance

| Fact         | Written by                                                                                                       | Read by                                                                   |
| ------------ | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `type=done`  | `standup done`                                                                                                   | start-up notice, `list`, web board, pre-merge checks (count `agent` only) |
| `type=agent` | every agent register, status, claim, resume (client); a subagent start or stop on a `done` row (server, `_wake`) | same                                                                      |

## Trade-offs

A session that keeps working after `done` with no prompt and no status post stays
listed as done. Every way Claude Code wakes a session runs the prompt hook, so this
needs a session that works without being woken, which we haven't seen.

**Known gaps (filed after review round 3; the rule to stop after two fix rounds applies):**

- ~~A `done` session's `--as-subagent` rows stay type `agent` until its SessionEnd, so they
  still count.~~ Fixed 2026-11-10: a parent's `done` finishes its child rows (server,
  `register`), and a helper finishes its own with `done --as-subagent` when it hands back
  (`test_a_done_parent_marks_its_child_rows_done`,
  `test_done_as_subagent_marks_only_the_child_row_done`).
- A main session that runs `done` while a background subagent is finishing is woken
  when that subagent stops, and stays active until it ends.
- A stray or late SubagentStop wakes a `done` row even when no entry was removed.
- When a subagent runs `done`, its step and branch overwrite the parent's. `_wake`
  restores only the type.

The product call that removes all four: drop `done`, and have the start-up notice and
`list` set apart rows with no prompt for 2h or more. That needs no new state and no
subagent rule, but it misses a session that finished minutes ago.

**Follow-up (filed):** the section order and titles live in both the client's `list`
and the server's board page.

## Falsifier

If "Done" rows still crowd the start-up notice a week after the skill lands, agents
are not running it; then drop it and have the notice ignore rows idle more than 2h.

## PRs

1. This one.
