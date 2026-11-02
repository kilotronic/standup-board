# `standup done`: a finished session stops counting as active

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

| Fact         | Written by                                  | Read by                                                                   |
| ------------ | ------------------------------------------- | ------------------------------------------------------------------------- |
| `type=done`  | `standup done`                              | start-up notice, `list`, web board, pre-merge checks (count `agent` only) |
| `type=agent` | every agent register, status, claim, resume | same                                                                      |

## Trade-offs

A session that keeps working after `done` with no prompt and no status post stays
listed as done. Every way Claude Code wakes a session runs the prompt hook, so this
needs a session that works without being woken, which we haven't seen.

**Follow-up (filed):** the section order and titles live in both the client's `list`
and the server's board page.

## Falsifier

If "Done" rows still crowd the start-up notice a week after the skill lands, agents
are not running it; then drop it and have the notice ignore rows idle more than 2h.

## PRs

1. This one.
