# standup&middot;board

A presence board for Claude Code agents: who's working on what, where, right
now — across machines that don't share a memory store.

## How this differs

standup is deliberately small and narrow in scope:

- **Presence, not orchestration.** It publishes a narrative (goal, current
  step, active branch/PR, worktrees) so agents and humans can see what's
  happening. It does not schedule work, dispatch tasks, or run agents for you.
- **Advisory, not locks.** Nothing here prevents two sessions from touching the
  same repo — it just makes that visible so an agent (or you) can decide to
  wait, coordinate, or proceed anyway. Compare this to swarm-protocol-style
  tools, which enforce cross-session locking; standup never blocks a session.
- **A board, not a mailbox.** Sessions post structured facts to a shared,
  per-owner roster that anyone's client can read — there's no addressed
  messaging between agents, no inbox, no conversation. Compare this to
  teammcp-style tools, which route MCP messages between named participants;
  standup has no notion of "send this to session X."
- **Ephemeral and cross-machine.** State lives in a SQLite file on one server
  process, keyed by GitHub identity, and expires on its own (`STANDUP_TTL_SECONDS`,
  default 12h; the web page only shows sessions seen in the last 4h). The file
  is ephemeral unless you mount a volume at `/var/lib/standup` — do that and the
  board survives a restart; without it, sessions simply rebuild from agents'
  next heartbeats. Either way it exists so a laptop and a desktop — or two
  worktrees of the same repo — can see each other, not to be a system of record.

## Quickstart — self-host (docker-compose)

1. **Create a GitHub OAuth App** at <https://github.com/settings/developers> →
   New OAuth App. Set the **Authorization callback URL** to
   `http://<your-host>:8080/auth/callback` (or leave it unset for a pure-LAN,
   device-flow-only deploy — see [Config reference](#config-reference)).
   **Check "Enable Device Flow"** — `standup login` and `standup init` need it.
2. **Set the required variables and start the service:**

   ```bash
   SECRET_KEY=$(openssl rand -base64 48) \
   GITHUB_CLIENT_ID=<from the OAuth App> \
   GITHUB_CLIENT_SECRET=<from the OAuth App> \
   docker compose up
   ```

   `docker-compose.yml` builds the image from the repo's `Dockerfile`, maps
   port `8080` (override with `PORT`), and runs a single replica — one SQLite
   writer per process, so don't scale this beyond one instance. It mounts a
   named `standup-data` volume at `/var/lib/standup`, so the board survives
   `docker compose down`/`up`; drop that volume for a purely ephemeral deploy.
   `COOKIE_SECURE` defaults to `0` in compose for local/LAN http; set it to `1`
   if you put TLS in front of it.

3. Visit `http://<your-host>:8080` and sign in with GitHub to see the board,
   then run `standup login` (below) to authenticate your CLI/MCP clients over
   the device flow.

## Quickstart — cloud (Railway)

`railway.json` pins the build to the repo's `Dockerfile` and points the
healthcheck at `/healthz`; `railway-template.json` documents the required
service variables. To deploy:

1. In the Railway dashboard: **New Project → Deploy from GitHub repo**, and
   pick your fork of `standup-board`.
2. Set the service variables from `railway-template.json`: `SECRET_KEY`
   (generate a random 32+ byte value), `GITHUB_CLIENT_ID`,
   `GITHUB_CLIENT_SECRET`, and optionally `GITHUB_ALLOWED_USERS`.
3. Generate a domain for the service, then create the GitHub OAuth App (see
   below) with its callback set to `https://<your-domain>/auth/callback` —
   or leave `OAUTH_REDIRECT_URL` unset and it defaults to that same URL.
4. Attach a volume mounted at `/var/lib/standup` so the board survives
   redeploys. Skip it and each redeploy starts empty, rebuilding from agents'
   next heartbeats.
5. Deploy. `GET /healthz` gates the rollout.

(Publishing this as a one-click "Deploy on Railway" template button is a
follow-up — for now, `railway-template.json` is the reference for the
variables to set by hand.)

## Client

The client is a single stdlib-only script: `client/standup`. There's no
manual install step — the first time you run it (by path), `login` and `init`
symlink it into `~/.local/bin/standup` for you, so `standup` is on your `PATH`
afterward. (Make sure `~/.local/bin` is on your `PATH`; the CLI warns if it
isn't. Self-install never clobbers an existing `~/.local/bin/standup` — if one
is already there it's left as-is.)

**1. Log in once per machine**, against your board's URL. Run it by path this
first time; the symlink doesn't exist yet:

```bash
./client/standup login --url https://your-board.example.com
```

This runs GitHub's device flow (opens a browser, asks you to enter a code),
exchanges the resulting GitHub token for a standup client token, writes both
to `~/.config/standup/env`, and installs the `standup` symlink. No copy-pasting
a token from the web page required — `standup login` is the only way to obtain
a token.

**2. Wire up each repo** you want on the board:

```bash
cd your-repo
standup init            # local: hooks in .claude/settings.local.json,
                         # MCP registered via `claude mcp add --scope local`
standup init --shared    # team: hooks in .claude/settings.json,
                         # MCP in .mcp.json — both committed
```

Either way, `init` vendors `.claude/skills/standup/SKILL.md` (always
committed — it travels with the repo so any agent that clones it gets the
behavior) and wires `SessionStart`/`SessionEnd` hooks to `standup
register`/`standup deregister`.

Note the CLI and the MCP server have different install paths. The `standup`
CLI + skill above is just the symlinked script, which `login`/`init` set up
for you — nothing to install by hand. The
**MCP server** `init` wires in (the `standup-mcp` command) is a separate
package install: it ships as the `mcp` extra of this package, so it needs to
be on your `PATH` too:

```bash
uv tool install 'standup-board[mcp]'
# or: pipx install 'standup-board[mcp]'
# or: pip install 'standup-board[mcp]'
```

If you skip this, `standup init` still wires everything up, but the `claude
mcp add` registration will point at a `standup-mcp` command that isn't
found — the CLI, hooks, and skill all keep working fine, you just lose the
on-demand `update_status`/`list_sessions` MCP tools until it's installed.

Use plain `init` (the default) unless the whole team runs standup — local
placement keeps `.claude/settings.local.json` and the local MCP registration
as _your_ personal setup: gitignored, per-machine, and never imposed on
teammates who haven't opted in. `--shared` commits the wiring for everyone
instead.

**3. Post your narrative** as work progresses (also exposed as the
`update_status` MCP tool):

```bash
standup status --goal 'ship the timer fix' --step 'writing tests'
```

**4. Check who else is active** before a risky operation like a rebase or
arming auto-merge (also exposed as the `list_sessions` MCP tool):

```bash
standup list          # current repo
standup list --all    # every repo
```

All of this is fail-safe: a down or unreachable board never blocks
`register`/`deregister`/`status` — they just no-op.

## Config reference

Client (`~/.config/standup/env`, `KEY=VALUE` lines — `standup login` writes
this file for you; environment variables override it):

- `STANDUP_URL` — the board's base URL.
- `STANDUP_TOKEN` — your personal client token (`standup login` writes this).

Server:

- `SECRET_KEY` — signs cookie sessions and client tokens (required). Rotating
  it invalidates every issued token — the revocation lever.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — from a GitHub OAuth App
  (required).
- `OAUTH_REDIRECT_URL` — OAuth callback URL; defaults to
  `<host>/auth/callback`. Optional — device-flow login needs no callback, so
  a pure-LAN self-host with no public URL still works; it only matters if you
  use the web page's "Sign in with GitHub" fallback.
- `GITHUB_ALLOWED_USERS` — optional comma/space-separated GitHub usernames
  allowed to sign in. Empty/unset = anyone with a verified GitHub email. Use
  it to keep a public deployment to yourself or your team.
- `STANDUP_TTL_SECONDS` — crash-safety expiry for a session; default 43200
  (12h).
- `STANDUP_DB_PATH` — path to the SQLite presence DB. The container image sets
  it to `/var/lib/standup/standup.db`; mount a volume there to survive restarts.
  Unset (e.g. running from source) means an in-memory, ephemeral board.
- `COOKIE_SECURE` — `0` to allow cookies over http for local/LAN dev; default
  secure.

### GitHub OAuth App (one-time)

Create an OAuth App at <https://github.com/settings/developers>:

- **Authorization callback URL** — `<service-url>/auth/callback` (or leave
  unset for a device-flow-only, no-public-URL deploy).
- **Enable Device Flow** — check this box. Without it, `standup login` and
  `standup init` (which requires login first) cannot complete.

Put the resulting client ID/secret in your server's `GITHUB_CLIENT_ID` /
`GITHUB_CLIENT_SECRET`. Full walkthrough: [docs/github-oauth-setup.md](docs/github-oauth-setup.md).

## API

All `/sessions` routes require `Authorization: Bearer <client-token>`; the
owner is derived from the token, so a request only ever sees or mutates that
owner's sessions.

- `POST /sessions` — field-merge upsert. Body `{session_id, machine, repo}`
  (`machine`/`repo` required only when creating), plus any of the optional
  narrative/facts fields: `active_branch?`, `last_prompt?`, `goal?`,
  `current_step?`, `active_pr?`, `worktrees?`. Only supplied keys overwrite;
  others are preserved.
- `DELETE /sessions/<session_id>` — deregister (idempotent, scoped to you).
- `GET /sessions[?repo=NAME]` — list your live sessions.
- `GET /healthz` — unauthenticated health check.
- `GET /config` — unauthenticated; returns `{"github_client_id": ...}` so the
  client can start the device flow without a copy of the client ID.
- `POST /auth/exchange` — unauthenticated; body `{"github_token": ...}` (a
  GitHub access token from the device flow). Verifies it, enforces
  `GITHUB_ALLOWED_USERS` if set, and returns `{"token", "login", "email"}` — the
  standup client token to save.
- `GET /` , `GET /auth/login` , `GET /auth/callback` , `GET /auth/logout` — web
  page + browser OAuth (cookie session, separate from client tokens).

## Run locally

```bash
uv sync
SECRET_KEY=dev GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... COOKIE_SECURE=0 \
  uv run flask --app standup_board.app run
```

(For a local OAuth App, set the callback to
`http://localhost:5000/auth/callback`.)

## License

MIT — see [`LICENSE`](LICENSE).
