# GitHub OAuth App setup

standup-board authenticates every user with their GitHub identity. Whether you
self-host or deploy to the cloud, you create **one GitHub OAuth App** for your
deployment and give the server its Client ID and secret. Users then sign in with
a single command — `standup login` — which runs GitHub's **device flow**; there
is no token to copy and paste.

This is a one-time setup per deployment.

## Step 1 — Create the OAuth App

Go to <https://github.com/settings/developers> → **New OAuth App** (or create one
under an organization at `https://github.com/organizations/<org>/settings/applications`).

| Field                          | Value                                              |
| ------------------------------ | -------------------------------------------------- |
| Application name               | `standup-board` (anything you like)                |
| Homepage URL                   | your board's URL, e.g. `https://board.example.com` |
| **Authorization callback URL** | `<your board URL>/auth/callback`                   |

Then **check "Enable Device Flow."** This is required — `standup login` uses the
device flow, and GitHub rejects the device-code request if it is not enabled.

> The callback URL is only used by the optional web-page sign-in fallback. Pure
> device-flow login needs no reachable callback, so a LAN-only self-host can put
> any placeholder there — but the box must be filled in for GitHub to save the app.

Click **Register application**, then **Generate a new client secret**. You now
have a **Client ID** and a **Client secret** (the secret is shown once — copy it).

## Step 2 — Give the server its credentials

Set these on your deployment (Railway variables, docker-compose `.env`, or your
host's env):

- `GITHUB_CLIENT_ID` = the Client ID
- `GITHUB_CLIENT_SECRET` = the Client secret
- `SECRET_KEY` = a strong random string (signs client tokens; rotating it
  invalidates every issued token — the revocation lever)

Optional:

- `GITHUB_ALLOWED_USERS` = comma/space-separated GitHub usernames allowed to sign
  in. Empty/unset means anyone with a verified GitHub email may sign in.
- `OAUTH_REDIRECT_URL` = the callback URL from Step 1. Only needed if you use the
  web-page sign-in fallback; device-flow login does not need it.

> The startup guard fails fast if `SECRET_KEY`, `GITHUB_CLIENT_ID`, or
> `GITHUB_CLIENT_SECRET` is missing — set all three before the service starts, or
> it will crash-loop.

## Step 3 — Users log in

On each machine, run the client by path the first time (it self-installs the
`~/.local/bin/standup` symlink):

```
./client/standup login --url https://board.example.com
```

This prints a code and a URL (works headless — approve on any device), opens your
browser best-effort, and on approval writes `~/.config/standup/env` with your
`STANDUP_URL` and a signed `STANDUP_TOKEN`. Then, in each repo you want on the
board:

```
standup init
```

See the main [README](../README.md) for the full client and deployment walkthrough.

## Notes

- **Revoking access / rotating tokens:** change `SECRET_KEY`. That invalidates
  every issued client token at once; users re-run `standup login` afterward.
- **Per-user isolation:** anyone who signs in gets their own board keyed to their
  verified GitHub email — they never see another user's sessions.
