# GUILD GLORY — Discord control + secured dashboard

Everything below is about what was **actually tested** (see "Tests") versus what could **not** be tested here
(live Discord, live game servers, real hosting platforms).

## 1. Quick start (computer / VPS)
1. Python 3.10+ . `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. `cp .env.example .env` and fill at least `DISCORD_BOT_TOKEN` and `DISCORD_ADMIN_IDS`.
3. `python app.py`
4. Open Discord, run `/start`.
5. Dashboard: `http://127.0.0.1:20333` — log in with `DASHBOARD_PASSWORD` (or the one-time password printed in the console).

`python -m pytest tests` runs the test-suite (mocked, no real accounts).

## 2. Discord Developer Portal
- Bot tab: Reset Token -> `DISCORD_BOT_TOKEN`. **No privileged intents** are needed.
- OAuth2 -> URL Generator: scopes `bot` + `applications.commands`; permissions Send Messages, Embed Links, Attach Files.
- Discord app: Settings -> Advanced -> Developer Mode, long-press your profile -> Copy User ID -> `DISCORD_ADMIN_IDS`.
- Set `DISCORD_GUILD_ID` (your server) so slash commands appear instantly; otherwise allow up to 1 hour.

## 3. Running from an iPhone (hosted)
The bot cannot run *on* an iPhone; run it on a hosting service and control it from Discord and Safari.
1. Pick a service that runs a **long-lived Python worker** (Railway, Render "worker"/web service, Fly.io, a VPS). Free "sleeping" plans will stop the bot.
   Services that only run web requests or that block outbound connections to arbitrary hosts (e.g. PythonAnywhere free tier) will not work.
2. Create a project from your uploaded copy of this folder (GitHub private repo or the service's upload). **Never upload `.env`.**
3. Start command: `python app.py` (a `Procfile` with `worker: python app.py` is included). Build: `pip install -r requirements.txt`.
4. In the service's **Variables / Environment** screen (works in Safari) add:
   `DISCORD_BOT_TOKEN`, `DISCORD_ADMIN_IDS`, `DISCORD_GUILD_ID`, and for the dashboard:
   `DASHBOARD_HOST=0.0.0.0`, `DASHBOARD_PASSWORD=<12+ characters>`, `DASHBOARD_TRUST_PROXY=true`
   (most hosts provide `PORT`, which is used automatically).
5. Persistence: add a volume (e.g. mounted at `/data`) and set `GG_DATA_DIR=/data`, otherwise accounts are lost on redeploy.
6. Open the public URL in Safari -> login page -> add to Home Screen if you like. Use `/start` in the Discord app for the control panel.
If you do not need the web dashboard, leave `DASHBOARD_HOST` unset: it then listens on 127.0.0.1 only and is unreachable from outside.
*(No hosting service was available in this environment, so the hosting steps are untested.)*

## 4. Environment variables
See `.env.example` (every variable is documented there). Required: `DISCORD_BOT_TOKEN`, `DISCORD_ADMIN_IDS`.

## 5. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Console: `Disabled: set DISCORD_ADMIN_IDS` | Admin IDs missing or not numeric. |
| `Login failed: DISCORD_BOT_TOKEN is invalid` | Reset the token in the Portal and update the variable. |
| Slash commands missing | Re-invite with `applications.commands` scope; set `DISCORD_GUILD_ID`; wait up to 1h for global sync. |
| `Slash command sync forbidden` | Same: scope missing. |
| "Admin access only" | Your Discord user ID is not in `DISCORD_ADMIN_IDS`. |
| Dashboard falls back to 127.0.0.1 | `DASHBOARD_HOST` is public but `DASHBOARD_PASSWORD` is shorter than 12 characters / unset. |
| Dashboard login works but page logs you out | Behind HTTPS proxy set `DASHBOARD_TRUST_PROXY=true`; make sure cookies are allowed. |
| 429 on login | 5 wrong passwords: wait 5 minutes. |
| Logins/requests fail after the TLS change | Certificate verification is now ON. If a game host really has an invalid certificate the request fails; a private CA can be supplied with `SSL_CERT_FILE`. Verification is intentionally not switchable off. |
| Accounts vanish after redeploy | Host disk is ephemeral: use a volume + `GG_DATA_DIR`. |
| Restart says "not found" | The ID is not in `accounts.json` (use the login UID or in-game ID of an added account). |

## 6. Files changed and why
| File | Change |
|---|---|
| `dashboard_server.py` | Login + sessions (HttpOnly, SameSite=Strict, Secure on HTTPS), CSRF token + Origin check, login rate-limit, security headers/CSP, 64 KB body cap, input validation, loopback bind by default, password only from env (random one-time if unset & local), **API key `XEROX_MODS` removed** (now `GUILDINFO_API_KEY` from env, header preferred), **tokens/passwords no longer returned by `/api/stats`**, log output HTML-escaped (stored XSS), generic error messages, `verify=False` and TLS-warning suppression removed, atomic 0600 file writes, shared `add_account`, awaited refresh/restart callbacks with real results, secrets redacted in `bot_state.log`. |
| `app.py` | Real restart (`restart_account_worker`) + checked refresh (`refresh_account_by_id`) registered as callbacks; `verify=False` removed (2 places); explicit timeouts; retry (3x, backoff) for the idempotent version lookup; token no longer pasted into a URL; log lines redacted; raw tracebacks removed; dashboard bound to 127.0.0.1; `GG_DATA_DIR`; `.env` loading; SIGINT/SIGTERM graceful shutdown (workers, bots, dashboard, HTTP client); token cache chmod 0600. |
| `discord_control.py` | Restart/refresh return the true outcome (timeouts, missing callback, errors all handled), UID/clan/region validation, TXT row validation (numeric UID, password length/printable, 1 MB hard cap on bytes read, early stop over 100 accounts), JWT call retries once on transient errors, errors redacted, `applications.commands` sync hint, add-account goes through the shared validated path. |
| `telegram_control.py` | `do_add` now uses the shared validated `add_account` (fixes duplicate-worker bug). Nothing else changed. |
| `security_utils.py` (new) | redaction, safe error text, atomic private JSON writes, validators. |
| `Web_temp/web.html` | CSRF header on all requests, 401 -> login, hard-coded key removed, Logout button, **"Restart Bot" button now really resumes** (it previously called restart with no UID = no-op), clan-join-on-every-page-load is now opt-in (`DASHBOARD_AUTO_JOIN`). |
| `requirements.txt`, `.env.example`, `Procfile`, `.gitignore`, `SETUP.txt`, `README_DISCORD.md` | Setup/deployment. |
| `accounts.json`, `token_cache.json`, `devices.json`, `guild_exp_cache.json` | Shipped empty. `devices.json` previously held 22 account-ID-keyed device profiles; they are regenerated automatically on demand. |
| `tests/` (new) | 69 tests, see below. |
Unchanged: all protobuf modules, `message_ids.py`, `decode.py`, `encode-id-clan.py`, game/API endpoints, keys and payload code.

## 7. Tests actually executed (isolated venv built from requirements.txt, Python 3.12)
- `python -m py_compile` on every `.py`: passed.
- `pytest tests`: **69 passed, 0 failed** — dashboard auth/CSRF/cookies/headers/rate-limit/session expiry/logout, secrets absent from stats/logs, XSS escaping, validation, body limit, API-key rules, loopback bind & fail-closed config, restart lifecycle (replace worker, no duplicates, simultaneous restarts serialised, stubborn worker not duplicated, crash reported, ID mapping, token accounts, callback missing/exception/timeout), TXT parsing (empty, malformed, duplicates, BOM/CRLF/unicode, limits, 60k-line file, oversize/lying size/wrong type/bad encoding), no passwords in progress/report/logs, DM fallback, API failure paths (mocked 5xx/timeout/ConnectError/bad JSON), TLS (static scan for disabled verification + live httpx client uses CERT_REQUIRED), Discord command payload validity, no duplicate commands, setup_hook sync (global/guild/Forbidden), shutdown closes the client, permission enforcement on commands/buttons/modals/actions.
- Mutation check: 9 deliberate regressions (CSRF off, auth off, token leak, verify=False, no cancel on restart, no restart lock, no log escaping, wide bind, modal check off) were each caught by the tests.
- Real end-to-end run of `python app.py` (temp data dir, no accounts): dashboard on 127.0.0.1 only, `/` 302->login, `/api/*` 401 without session, wrong password 401, login sets HttpOnly+SameSite=Strict cookie, POST without CSRF 403, restart of unknown account returns a truthful error, SIGTERM exits cleanly in ~1 s.

## 8. NOT tested / unresolved
- **Live Discord**: login, real slash-command sync, buttons/modals in a real server (sandbox cannot reach Discord).
- **Live game servers and the real restart against them**: restart logic was tested with fake workers; a real logged-in session was never created (no credentials, by design). With certificate verification now ON, confirm game endpoints still connect; the code sends `Host: clientbp.ppmainecoonghj.com` to the server URL it received, so a mismatch there could surface as a TLS/HTTP failure.
- Hosting platforms (Railway/Render/etc.): instructions are generic and untested.
- **Third-party JWT service** `ff-jwt-gen-api.lovable.app` receives every UID+password pair, as a GET with the password in the query string (unchanged: replacing it would mean inventing an endpoint). It may log them. Only use accounts you own; consider setting `DISCORD_PUBLIC_AUTOJOIN=false` (default).
- `accounts.json` and `token_cache.json` still hold passwords/tokens in plain text (now chmod 0600 when written by this code). Protect the host and volume.
- Garena's public client constants (e.g. `client_secret` in the guest-token request, AES key/IV) remain in code: they are part of the reverse-engineered client protocol, not your secrets.
- Telegram bot: only `do_add` was touched; its other messages were not re-audited.
- Automating game accounts may violate the game's terms of service and can get accounts banned. No anti-ban or detection-evasion code was added; pre-existing timing-jitter options in `app.py` were left as they were.
