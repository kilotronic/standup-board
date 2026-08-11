# Cursor support for standup (2026-08-11)

## Problem

Cursor loads Claude Code hooks from `~/.claude/settings.json` (third-party
hooks). When those hooks run `standup register`, Cursor's stdin has
`session_id` / `prompt` / `workspace_roots` but not Claude's `cwd`. The client
falls back to `os.getcwd()`, which is often `~/.claude`, so the board shows
`.claude on <machine>` instead of the real repo (e.g. `partygame`).

Separately, `standup status` and MCP `update_status` only read
`CLAUDE_CODE_SESSION_ID`, so Cursor agents (which set `CURSOR_CONVERSATION_ID`)
cannot post narrative without `--session-id`.

## Scope (option B)

1. Fix cwd + session-id detection for Cursor payloads and env vars.
2. Have `standup init` / `--global` also write native Cursor hooks.
3. Update skill + README so Cursor is a supported client.

Out of scope: Cursor MCP auto-wiring (option C), Cursor subagent hooks.

## Detection

Shared helpers used by `register`, `deregister`, `status`, and MCP
`update_status`:

**Session id** (first wins):

1. `--session-id` / explicit arg
2. hook `session_id`
3. hook `conversation_id`
4. `$CLAUDE_CODE_SESSION_ID`
5. `$CURSOR_CONVERSATION_ID`

**Project cwd** (first wins):

1. `--cwd` / explicit arg
2. hook `cwd`
3. first entry of hook `workspace_roots` (non-empty string)
4. `$CURSOR_PROJECT_DIR`
5. `$CLAUDE_PROJECT_DIR`
6. `os.getcwd()`

Then `_repo_name(cwd)` unchanged (origin remote basename, else toplevel
basename).

## Cursor hook wiring

`standup init` continues to write Claude hooks. It also merges native Cursor
hooks so presence works without relying on third-party Claude import.

| Scope            | Claude                                        | Cursor                           |
| ---------------- | --------------------------------------------- | -------------------------------- |
| `--global`       | `~/.claude/settings.json` + skill             | `~/.cursor/hooks.json`           |
| per-repo default | `.claude/settings.local.json` + skill         | `.cursor/hooks.json`             |
| `--shared`       | `.claude/settings.json` + skill + `.mcp.json` | `.cursor/hooks.json` (committed) |

Cursor events:

| Event                | Command                               |
| -------------------- | ------------------------------------- |
| `sessionStart`       | `$HOME/.local/bin/standup register`   |
| `beforeSubmitPrompt` | `$HOME/.local/bin/standup register`   |
| `sessionEnd`         | `$HOME/.local/bin/standup deregister` |

Format: `{ "version": 1, "hooks": { "<event>": [{ "command", "timeout": 5 }] } }`.
Merge is idempotent on exact `command` string. No matchers (Cursor
`sessionStart` does not use Claude's source matcher).

If both Claude third-party hooks and native Cursor hooks fire, register is an
idempotent upsert — duplicate fire is harmless.

## Docs / skill

Skill and README refer to Claude Code and Cursor. Mention that Cursor agents
use `$CURSOR_CONVERSATION_ID` automatically for `standup status`.

## Testing

- Unit tests for cwd/session-id resolution order (Cursor payload, env fallbacks).
- Register/status use Cursor env when Claude fields absent.
- Init global/local/shared write/merge Cursor hooks.json idempotently.
- Existing Claude hook tests remain green.
