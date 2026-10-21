# -*- coding: utf-8 -*-
"""Discord control panel for the GUILD GLORY dashboard.

Discord port of telegram_control.py. Feature parity:
  status / accounts / add / token / delete / pause / pauseall / refresh /
  restart / logs / clearlogs / clanjoin / guild + Auto Guild Join wizard
  + Settings and Help panels.

Telegram concept          -> Discord equivalent
  /command ARGS           -> slash command (options) or modal form
  inline keyboard         -> persistent button view (PanelView)
  multi-step text wizard  -> one modal (accounts + guild id) or /autojoin file:
  "Admin access only."    -> checked in: command tree, view, modal, AND action

Security model
  * Admins = user IDs in DISCORD_ADMIN_IDS.
  * Every admin action re-checks the caller (defence in depth), so a button,
    modal or slash command cannot be used to bypass the permission check.
  * Passwords / tokens are only ever collected through modals and all replies
    are ephemeral.
  * Auto Guild Join is admin-only unless DISCORD_PUBLIC_AUTOJOIN=true
    (Telegram allowed every user; see README_DISCORD.md for why this differs).
"""
import os, io, re, json, time, asyncio
import requests
import discord
from discord import app_commands
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import dashboard_server as ds
import security_utils as su
from security_utils import safe_error

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# Only numeric IDs are accepted; anything else is ignored (and reported at startup).
_raw_admin = [x.strip() for x in os.getenv("DISCORD_ADMIN_IDS", "").split(",") if x.strip()]
ADMIN_IDS = {x for x in _raw_admin if x.isdigit()}
INVALID_ADMIN_IDS = [x for x in _raw_admin if not x.isdigit()]
# Optional: server ID so slash commands appear instantly (global sync can take up to 1 hour).
DEV_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

ACCOUNTS_FILE = "accounts.json"
TOKEN_CACHE_FILE = "token_cache.json"

# Commands any user may run (when public autojoin is enabled; see PUBLIC_AUTOJOIN).
PUBLIC_COMMANDS = {"start", "autojoin"}

COLOR_MAIN = discord.Color.from_rgb(255, 140, 0)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.red()
COLOR_INFO = discord.Color.blurple()


# ───────────────────────── permissions ─────────────────────────
def is_admin(user) -> bool:
    return bool(user and str(user.id) in ADMIN_IDS)


def public_autojoin_enabled() -> bool:
    """Read at call time so the setting can be changed/tested without reimport."""
    return os.getenv("DISCORD_PUBLIC_AUTOJOIN", "").strip().lower() in {"1", "true", "yes", "on"}


def can_use_autojoin(user) -> bool:
    return is_admin(user) or public_autojoin_enabled()


async def deny(i: discord.Interaction, text="🔒 Admin access only."):
    try:
        if i.response.is_done():
            await i.followup.send(text, ephemeral=True)
        else:
            await i.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def ensure_admin(i: discord.Interaction) -> bool:
    """Called at the top of EVERY admin action (defence in depth)."""
    if is_admin(i.user):
        return True
    await deny(i)
    return False


# ───────────────────────── helpers ─────────────────────────
spawn = ds.spawn          # strong-referenced background tasks with redacted failure logging


def embed(title, desc="", color=COLOR_MAIN):
    return discord.Embed(title=title[:256], description=desc[:4000], color=color)


async def reply(i: discord.Interaction, content=None, *, emb=None, view=None, file=None):
    """Always an ephemeral reply (visible only to the invoking user)."""
    kwargs = {"ephemeral": True}
    if content is not None: kwargs["content"] = content
    if emb is not None: kwargs["embed"] = emb
    if view is not None: kwargs["view"] = view
    if file is not None: kwargs["file"] = file
    if i.response.is_done():
        return await i.followup.send(**kwargs)
    return await i.response.send_message(**kwargs)


def fmt_uptime(seconds):
    seconds = int(seconds); d, seconds = divmod(seconds, 86400); h, seconds = divmod(seconds, 3600); m, seconds = divmod(seconds, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m {seconds}s"


def status_embed():
    s = ds.bot_state
    uptime = max(1, int(time.time() - s.start_time))
    e = embed("📊 LIVE OVERVIEW", color=COLOR_INFO)
    e.add_field(name="Accounts", value=f"**{len(s.accounts)}**")
    e.add_field(name="Active matches", value=f"**{sum(a.get('active_matches', 0) for a in s.accounts.values())}**")
    e.add_field(name="Matches started", value=f"**{s.total_matches_started}**")
    e.add_field(name="Total matches", value=f"**{s.total_matches}**")
    e.add_field(name="EXP gained", value=f"**{s.total_gained_exp:,}**")
    e.add_field(name="EXP / hour", value=f"**{int(s.total_gained_exp / uptime * 3600):,}**")
    e.add_field(name="Runtime", value=f"**{fmt_uptime(uptime)}**", inline=False)
    return e


ACCOUNTS_PER_PAGE = 10


def accounts_page_embed(page=0):
    """Telegram showed the first 40 accounts; Discord embeds are limited to 4096 chars,
    so the full list is paginated instead of truncated."""
    accounts = list(ds.bot_state.accounts.values())
    if not accounts:
        return embed("👥 ACCOUNTS", "No accounts are currently loaded."), 1
    pages = max(1, (len(accounts) + ACCOUNTS_PER_PAGE - 1) // ACCOUNTS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * ACCOUNTS_PER_PAGE
    rows = []
    for n, a in enumerate(accounts[start:start + ACCOUNTS_PER_PAGE], start + 1):
        uid = str(a.get("uid") or a.get("account_id") or "Unknown")
        paused = ds.bot_state.is_paused(uid)
        rows.append(f"**{n}.** `{uid}`\nLevel: {a.get('level', '—')} • EXP: {int(a.get('gained_exp', 0) or 0):,}\n"
                    f"{'⏸ Paused' if paused else '🟢 Running'} • Matches: {a.get('active_matches', 0)}")
    e = embed("👥 ACCOUNT CENTER", "\n\n".join(rows))
    e.set_footer(text=f"Page {page + 1}/{pages} • {len(accounts)} accounts")
    return e, pages


def accounts_embed(page=0):
    return accounts_page_embed(page)[0]


def logs_embed(count=15):
    entries = ds.bot_state.logs[-count:]
    if not entries:
        return embed("📜 LATEST ACTIVITY", "No activity logs yet.")
    lines = []
    for e in entries:
        lines.append(f"`{e.get('time', '')}` {e.get('message', '')}" if isinstance(e, dict) else str(e))
    return embed("📜 LATEST ACTIVITY", "\n".join(lines)[-3900:])


def settings_embed():
    return embed("⚙️ CONTROL PANEL SETTINGS",
        "• Admin access is controlled by `DISCORD_ADMIN_IDS`\n"
        "• Bot token must be stored in `DISCORD_BOT_TOKEN`\n"
        f"• Auto Guild Join public access: **{'ON' if public_autojoin_enabled() else 'OFF (admins only)'}** "
        "(`DISCORD_PUBLIC_AUTOJOIN`)\n"
        "• Auto Guild Join accepts a TXT upload or a pasted UID:PASSWORD list\n"
        f"• Maximum {MAX_JOIN_ACCOUNTS} accounts per run\n"
        "• Account credentials are not saved by the join wizard", COLOR_INFO)


def help_embed(admin=True):
    if not admin:
        return embed("❔ GUILD GLORY HELP", JOIN_HELP, COLOR_INFO)
    return embed("GUILD GLORY • COMMAND GUIDE",
        "`/start` — control panel\n`/status` — live statistics\n`/accounts` — account list\n"
        "`/add` — add guest account (private form)\n`/token` — add token account (private form)\n"
        "`/delete uid` — delete account\n`/pause uid` — pause/resume account\n"
        "`/pauseall` — pause/resume all\n`/refresh uid` — refresh profile\n"
        "`/restart uid` — restart account worker\n`/logs` — latest activity\n`/clearlogs` — clear logs\n"
        "`/clanjoin clan_id` — send clan join request\n`/guild clan_id region` — fetch guild details\n"
        "`/autojoin` — Auto Guild Join wizard\n\n"
        "For account safety, only submit accounts you own or are authorized to use.", COLOR_INFO)


# ───────────────────────── actions ─────────────────────────
async def do_add(data):
    """Delegates to dashboard_server.add_account: validates input, cancels any previous worker for the
    same UID/token (no duplicate workers), writes accounts.json atomically (0600) and starts the worker."""
    return ds.add_account(data, "Discord")


async def act_add(i, uid, password):
    if not await ensure_admin(i): return
    try:
        label = await do_add({"uid": uid, "password": password})
        await reply(i, emb=embed("✅ Account added", f"`{label}` added to rotation.", COLOR_OK))
    except ValueError as e:                       # validation message, safe to show
        await reply(i, emb=embed("❌ Could not add account", str(e), COLOR_ERR))
    except Exception as e:
        ds.bot_state.log(f"Discord add account failed: {safe_error(e)}", "error")
        await reply(i, emb=embed("❌ Could not add account", "Unexpected error. See the activity log.", COLOR_ERR))


async def act_token(i, token):
    if not await ensure_admin(i): return
    try:
        label = await do_add({"token": token})
        await reply(i, emb=embed("✅ Token added", f"`{label}` added to rotation.", COLOR_OK))
    except ValueError as e:
        await reply(i, emb=embed("❌ Could not add token", str(e), COLOR_ERR))
    except Exception as e:
        ds.bot_state.log(f"Discord add token failed: {safe_error(e)}", "error")
        await reply(i, emb=embed("❌ Could not add token", "Unexpected error. See the activity log.", COLOR_ERR))


async def act_delete(i, uid):
    if not await ensure_admin(i): return
    if not su.valid_ident(uid):
        return await reply(i, emb=embed("⚠️ Invalid UID", "Use the account's numeric UID / ID.", COLOR_ERR))
    if not i.response.is_done():
        await i.response.defer(ephemeral=True, thinking=True)

    class Req:  # reuse the website's delete handler, exactly like Telegram does
        async def json(self): return {"uid": uid}
    result = await ds.handle_delete_account(Req())
    try: payload = json.loads(result.text)
    except Exception: payload = {}
    if payload.get("status") == "ok":
        await reply(i, emb=embed("🗑 Deleted", f"Account `{uid}` removed.", COLOR_OK))
    else:
        await reply(i, emb=embed("⚠️ Delete failed", str(payload.get("error", "Account not found or could not be deleted."))[:300], COLOR_ERR))


async def act_pause(i, uid):
    if not await ensure_admin(i): return
    if not su.valid_ident(uid):
        return await reply(i, emb=embed("⚠️ Invalid UID", "Use the account's numeric UID / ID.", COLOR_ERR))
    if uid not in ds.bot_state.accounts and uid not in ds.bot_state.account_workers:
        return await reply(i, emb=embed("❌ Not found", "Account not found in active state.", COLOR_ERR))
    state = ds.bot_state.toggle_pause(uid)
    await reply(i, emb=embed("⏸ Paused" if state else "▶️ Resumed", f"`{uid}`", COLOR_INFO))


async def act_pauseall(i):
    if not await ensure_admin(i): return
    state = ds.bot_state.toggle_pause_all()
    await reply(i, emb=embed("⏯ Pause / Resume All", "⏸ All accounts paused." if state else "▶️ All accounts resumed.", COLOR_INFO))


async def act_refresh(i, uid):
    if not await ensure_admin(i): return
    if not su.valid_ident(uid):
        return await reply(i, emb=embed("⚠️ Invalid UID", "Use the account's numeric UID / ID.", COLOR_ERR))
    if not i.response.is_done():
        await i.response.defer(ephemeral=True, thinking=True)
    res = await ds.call_account_callback("on_refresh_account", uid, 45.0)
    await reply(i, emb=embed("🔄 Refreshed" if res["ok"] else "⚠️ Refresh failed", f"`{uid}`\n{res['message']}",
                             COLOR_OK if res["ok"] else COLOR_ERR))


async def act_restart(i, uid):
    """Real restart: stops the account's worker, drops its cached session and starts a fresh worker
    (app.restart_account_worker). Reports the true outcome; if the callback is missing it says so."""
    if not await ensure_admin(i): return
    if not su.valid_ident(uid):
        return await reply(i, emb=embed("⚠️ Invalid UID", "Use the account's numeric UID / ID.", COLOR_ERR))
    if not i.response.is_done():
        await i.response.defer(ephemeral=True, thinking=True)
    res = await ds.call_account_callback("on_restart_account", uid, 90.0)
    await reply(i, emb=embed("♻️ Restarted" if res["ok"] else "⚠️ Restart failed", f"`{uid}`\n{res['message']}",
                             COLOR_OK if res["ok"] else COLOR_ERR))


valid_clan_id = su.valid_clan_id
valid_region = su.valid_region


async def act_clanjoin(i, clan_id):
    if not await ensure_admin(i): return
    if not valid_clan_id(clan_id):
        return await reply(i, emb=embed("⚠️ Invalid Clan ID", "Clan ID must be a number.", COLOR_ERR))
    if not i.response.is_done():
        await i.response.defer(ephemeral=True, thinking=True)
    try:
        tokens = su.read_json(TOKEN_CACHE_FILE, {})
        successes = total = 0
        for uid, creds in tokens.items():
            token = creds.get("token")
            if not token: continue
            total += 1
            try:
                code, _ = await ds.request_clan_async(token, clan_id, creds.get("region", "IND"))
                successes += int(code == 200)
            except Exception:
                pass
        await reply(i, emb=embed("🏰 Clan join requests", f"Sent: **{successes}/{total}**", COLOR_OK))
    except Exception as e:
        await reply(i, emb=embed("❌ Clan join failed", safe_error(e), COLOR_ERR))


async def act_guild(i, clan_id, region):
    if not await ensure_admin(i): return
    if not valid_clan_id(clan_id):
        return await reply(i, emb=embed("⚠️ Invalid Clan ID", "Clan ID must be a number.", COLOR_ERR))
    if not valid_region(region):
        return await reply(i, emb=embed("⚠️ Invalid region", "Use a region code such as `IND`.", COLOR_ERR))
    if not i.response.is_done():
        await i.response.defer(ephemeral=True, thinking=True)
    try:
        cache = su.read_json(TOKEN_CACHE_FILE, {})
        data = await asyncio.to_thread(ds.fetch_single_clan, clan_id, region.upper(), cache)
        if "error" in data:
            return await reply(i, emb=embed("❌ Guild lookup failed", str(data["error"]), COLOR_ERR))
        data.pop("raw_response_bytes", None)
        body = json.dumps(data, indent=2, ensure_ascii=False)
        await reply(i, emb=embed("🏰 GUILD INTEL", "```json\n" + body[:3700] + "\n```", COLOR_INFO))
    except Exception as e:
        await reply(i, emb=embed("❌ Guild lookup failed", safe_error(e), COLOR_ERR))


async def act_clearlogs(i):
    if not await ensure_admin(i): return
    ds.bot_state.logs.clear()
    await reply(i, emb=embed("🧹 Logs cleared", "Activity logs cleared.", COLOR_OK))


# ───────────────────────── modals ─────────────────────────
class AdminModal(discord.ui.Modal):
    """Base class: the submit is rejected unless the submitter is an admin."""
    async def interaction_check(self, i: discord.Interaction) -> bool:
        if is_admin(i.user): return True
        await deny(i)
        return False

    async def on_error(self, i: discord.Interaction, error: Exception):
        print(f"[DISCORD] Modal error: {type(error).__name__}")
        try: await reply(i, emb=embed("❌ Error", "Something went wrong. Please try again.", COLOR_ERR))
        except Exception: pass


class AddModal(AdminModal, title="Add Guest Account"):
    uid = discord.ui.TextInput(label="UID", max_length=32)
    password = discord.ui.TextInput(label="Password", max_length=128)
    async def on_submit(self, i): await act_add(i, self.uid.value, self.password.value)


class TokenModal(AdminModal, title="Add Token Account"):
    token = discord.ui.TextInput(label="Token", style=discord.TextStyle.paragraph, max_length=4000)
    async def on_submit(self, i): await act_token(i, self.token.value)


class UidModal(AdminModal):
    uid = discord.ui.TextInput(label="Account UID", max_length=64)
    def __init__(self, title, action):
        super().__init__(title=title)
        self.action = action
    async def on_submit(self, i): await self.action(i, self.uid.value.strip())


class ClanJoinModal(AdminModal, title="Send Clan Join Request"):
    clan_id = discord.ui.TextInput(label="Clan ID", max_length=24)
    async def on_submit(self, i): await act_clanjoin(i, self.clan_id.value.strip())


class GuildModal(AdminModal, title="Fetch Guild Details"):
    clan_id = discord.ui.TextInput(label="Clan ID", max_length=24)
    region = discord.ui.TextInput(label="Region", default="IND", max_length=8)
    async def on_submit(self, i): await act_guild(i, self.clan_id.value.strip(), self.region.value.strip())


# ───────────────────────── auto guild join ─────────────────────────
# Constants and request-building are byte-for-byte the same as telegram_control.py
# (verified by tests/test_discord_control.py::test_join_api_parity).
JOIN_JWT_API_URL = "https://ff-jwt-gen-api.lovable.app/api/public/token"   # THIRD-PARTY: receives UID+password
JOIN_API_URL = "https://client.ind.freefiremobile.com/RequestJoinClan"
JOIN_KEY = b'Yg&tc%DEuh6%Zc^8'
JOIN_IV = b'6oyZDr22E3ychjM%'
MAX_JOIN_ACCOUNTS = 100
_join_running = set()                      # user IDs that currently have a run in progress
_join_slots = asyncio.Semaphore(2)         # at most 2 runs at once across all users


def _join_varint(n: int) -> bytes:
    out = bytearray()
    while n > 127:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _join_body(guild_id: int) -> bytes:
    raw = _join_varint((1 << 3) | 0) + _join_varint(guild_id)
    raw += _join_varint((2 << 3) | 0) + _join_varint(1)
    return AES.new(JOIN_KEY, AES.MODE_CBC, JOIN_IV).encrypt(pad(raw, AES.block_size))


def _join_generate_jwt(uid: str, password: str):
    """Unchanged third-party endpoint/method. Retries once on timeouts, connection errors and 5xx.
    Exceptions are swallowed on purpose: their text can contain the request URL (incl. the password)."""
    for attempt in range(2):
        try:
            r = requests.get(JOIN_JWT_API_URL, params={"uid": uid, "password": password}, timeout=25)
            if r.status_code >= 500 and attempt == 0:
                time.sleep(1.0); continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, str): return data.strip() or None
            if isinstance(data, dict):
                token = data.get("token") or data.get("jwt") or data.get("access_token")
                if isinstance(token, dict): token = token.get("token") or token.get("jwt")
                if not token and isinstance(data.get("data"), dict):
                    token = data["data"].get("token") or data["data"].get("jwt")
                return token if isinstance(token, str) and token.strip() else None
            return None
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 0:
                time.sleep(1.0); continue
            return None
        except (requests.RequestException, ValueError):
            return None
    return None


def _join_send(jwt: str, guild_id: int):
    headers = {
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*", "Authorization": f"Bearer {jwt}", "X-GA": "v1 1",
        "X-GA-SV": str(int(time.time())), "ReleaseVersion": "OB55",
        "Content-Type": "application/x-www-form-urlencoded", "X-Unity-Version": "2018.4.12f1",
    }
    try:
        return requests.post(JOIN_API_URL, headers=headers, data=_join_body(guild_id), timeout=25)
    except requests.RequestException:
        return None


MAX_TXT_BYTES = 1024 * 1024


def parse_accounts(content: str):
    """Same rules as telegram_control.accept_join_accounts (blank/# lines skipped, first ':' splits,
    duplicate UIDs ignored) PLUS validation: UID must be numeric (<=20 digits) and the password
    printable (<=128). Rows failing validation count as invalid. Never logs row contents."""
    accounts, invalid, seen = [], 0, set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if ":" not in line:
            invalid += 1; continue
        uid, password = line.split(":", 1)
        uid, password = uid.strip(), password.strip()
        if not su.valid_uid(uid) or not su.valid_password(password):
            invalid += 1; continue
        if uid in seen: continue
        seen.add(uid)
        accounts.append((uid, password))
        if len(accounts) > MAX_JOIN_ACCOUNTS: break       # already over the limit; stop scanning huge files
    return accounts, invalid


async def run_join(i: discord.Interaction, accounts, invalid, guild_id: int):
    """`i` must already be deferred (ephemeral)."""
    user_id = i.user.id
    if user_id in _join_running:
        return await reply(i, emb=embed("⏳ Already running", "You already have a join run in progress.", COLOR_ERR))
    _join_running.add(user_id)
    try:
        async with _join_slots:
            await _run_join_locked(i, accounts, invalid, guild_id)
    finally:
        _join_running.discard(user_id)


async def _run_join_locked(i, accounts, invalid, guild_id):
    total = len(accounts)
    jwt_ok = sent = success = jwt_failed = request_failed = 0
    results = []
    interaction_alive = True

    def progress_embed(index):
        filled = int(index / total * 10)
        latest = results[-1] if results else None
        e = embed("⚙️ GUILD JOIN PROCESSOR", color=COLOR_INFO)
        e.add_field(name="Guild ID", value=f"`{guild_id}`")
        e.add_field(name="Progress", value=f"**{index}/{total}** `{'█' * filled}{'░' * (10 - filled)}`")
        e.add_field(name="JWT generated", value=str(jwt_ok))
        e.add_field(name="Requests sent", value=str(sent))
        e.add_field(name="HTTP 200", value=str(success))
        e.add_field(name="JWT failed", value=str(jwt_failed))
        e.add_field(name="Network errors", value=str(request_failed))
        if latest:
            e.add_field(name="Latest", value=f"`{latest[0]}` → **{latest[1]}**" + (f" ({latest[2]})" if latest[2] is not None else ""), inline=False)
        return e

    try:
        await i.edit_original_response(content=None, embed=embed("⚙️ GUILD JOIN PROCESSOR",
            f"Guild ID: `{guild_id}`\nAccounts queued: **{total}** (invalid rows skipped: {invalid})\n⏳ Initializing…", COLOR_INFO))
    except discord.HTTPException:
        interaction_alive = False

    for index, (uid, password) in enumerate(accounts, 1):
        jwt = await asyncio.to_thread(_join_generate_jwt, uid, password)
        if not jwt:
            jwt_failed += 1; results.append((uid, "JWT_FAILED", None))
        else:
            jwt_ok += 1
            response = await asyncio.to_thread(_join_send, jwt, guild_id)
            if response is None:
                request_failed += 1; results.append((uid, "NETWORK_ERROR", None))
            else:
                sent += 1
                if response.status_code == 200:
                    success += 1; results.append((uid, "HTTP_200", 200))
                else:
                    results.append((uid, "HTTP_RESPONSE", response.status_code))
        if interaction_alive and (index == total or index % 2 == 0):
            try: await i.edit_original_response(embed=progress_embed(index))
            except discord.HTTPException: interaction_alive = False   # 15-minute interaction token expired
        await asyncio.sleep(0.35)

    detail = [f"{uid} — {status}" + (f" ({code})" if code is not None else "") for uid, status, code in results]
    final = embed("🏁 GUILD JOIN REPORT", color=COLOR_OK if success else COLOR_ERR)
    final.add_field(name="Guild ID", value=f"`{guild_id}`")
    final.add_field(name="Processed", value=f"**{total}/{total}**")
    final.add_field(name="JWT success", value=str(jwt_ok))
    final.add_field(name="Requests sent", value=str(sent))
    final.add_field(name="HTTP 200", value=str(success))
    final.add_field(name="JWT failed", value=str(jwt_failed))
    final.add_field(name="Network errors", value=str(request_failed))
    shown = "\n".join(detail[:20])[:950]
    final.add_field(name="Per-account results", value=f"```{shown}```" if shown else "—", inline=False)

    def make_report():  # full report as .txt; passwords are never included
        return discord.File(io.BytesIO("\n".join(detail).encode("utf-8")), filename=f"join_report_{guild_id}.txt")

    delivered = False
    if interaction_alive:
        try:
            await i.edit_original_response(embed=final, attachments=[make_report()])
            delivered = True
        except discord.HTTPException:
            pass
    if not delivered:
        try:
            await i.user.send(embed=final, file=make_report())
        except discord.HTTPException:
            ds.bot_state.log("Guild join finished but the report could not be delivered (interaction expired, DMs closed).", "warning")


class JoinModal(discord.ui.Modal, title="Auto Guild Join"):
    accounts = discord.ui.TextInput(label="Accounts (one UID:PASSWORD per line)",
                                    style=discord.TextStyle.paragraph, max_length=4000,
                                    placeholder="123456789:password\n987654321:password")
    guild_input = discord.ui.TextInput(label="Guild ID", max_length=20)

    def __init__(self, guild_default=""):
        super().__init__()
        if guild_default: self.guild_input.default = guild_default

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if can_use_autojoin(i.user): return True
        await deny(i, "🔒 Auto Guild Join is restricted to administrators.")
        return False

    async def on_error(self, i: discord.Interaction, error: Exception):
        print(f"[DISCORD] Modal error: {type(error).__name__}")
        try: await reply(i, emb=embed("❌ Error", "Something went wrong. Please try again.", COLOR_ERR))
        except Exception: pass

    async def on_submit(self, i: discord.Interaction):
        await start_join(i, self.accounts.value, self.guild_input.value.strip())


async def start_join(i: discord.Interaction, content: str, guild_raw: str):
    if not can_use_autojoin(i.user):
        return await deny(i, "🔒 Auto Guild Join is restricted to administrators.")
    if not guild_raw.isdigit() or len(guild_raw) > 20:
        return await reply(i, emb=embed("⚠️ Invalid Guild ID", "Guild ID must be a numeric ID.", COLOR_ERR))
    accounts, invalid = parse_accounts(content)
    if not accounts:
        return await reply(i, emb=embed("❌ No valid rows", "No valid `UID:PASSWORD` rows found. Check the format and try again.", COLOR_ERR))
    if len(accounts) > MAX_JOIN_ACCOUNTS:
        return await reply(i, emb=embed("⚠️ Too many accounts", f"More than {MAX_JOIN_ACCOUNTS} accounts. Maximum per run is {MAX_JOIN_ACCOUNTS}; split the list and retry.", COLOR_ERR))
    await i.response.defer(ephemeral=True, thinking=True)
    await run_join(i, accounts, invalid, int(guild_raw))


# ───────────────────────── views (buttons) ─────────────────────────
JOIN_HELP = ("Two ways to start:\n• `/autojoin` → paste accounts into the form\n"
             "• `/autojoin file:<accounts.txt> guild_id:<ID>` → upload a TXT file\n\n"
             "Format (one per line): `UID:PASSWORD`\n"
             f"• Max **{MAX_JOIN_ACCOUNTS}** unique accounts per run\n"
             "• Credentials are processed in memory and are not saved by this wizard\n"
             "• The JWT generation service receives each UID/password pair — only use accounts you own or are authorized to use")


class JoinOnlyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if can_use_autojoin(i.user): return True
        await deny(i, "🔒 Auto Guild Join is restricted to administrators.")
        return False

    @discord.ui.button(label="🚀 Auto Guild Join", style=discord.ButtonStyle.success, custom_id="gg:join_pub")
    async def join(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(JoinModal())

    @discord.ui.button(label="❔ Help", style=discord.ButtonStyle.secondary, custom_id="gg:help_pub")
    async def help(self, i: discord.Interaction, b: discord.ui.Button):
        await reply(i, emb=help_embed(admin=False))


class AccountsView(discord.ui.View):
    def __init__(self, page=0):
        super().__init__(timeout=300)
        self.page = page
        self._sync()

    def _sync(self):
        _, pages = accounts_page_embed(self.page)
        self.page = max(0, min(self.page, pages - 1))
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= pages - 1

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if is_admin(i.user): return True
        await deny(i)
        return False

    async def _show(self, i):
        self._sync()
        e, _ = accounts_page_embed(self.page)
        await i.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, i, b):
        self.page -= 1; await self._show(i)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, i, b):
        self.page += 1; await self._show(i)


async def send_accounts(i):
    if not await ensure_admin(i): return
    e, pages = accounts_page_embed(0)
    await reply(i, emb=e, view=AccountsView(0) if pages > 1 else None)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        # The join button follows the autojoin policy; everything else is admin-only.
        cid = (i.data or {}).get("custom_id")
        if cid == "gg:join":
            if can_use_autojoin(i.user): return True
            await deny(i, "🔒 Auto Guild Join is restricted to administrators.")
            return False
        if is_admin(i.user): return True
        await deny(i)
        return False

    @discord.ui.button(label="📊 Live Dashboard", style=discord.ButtonStyle.primary, custom_id="gg:status", row=0)
    async def b_status(self, i, b): await reply(i, emb=status_embed())

    @discord.ui.button(label="👥 Accounts", style=discord.ButtonStyle.primary, custom_id="gg:accounts", row=0)
    async def b_accounts(self, i, b): await send_accounts(i)

    @discord.ui.button(label="➕ Add UID Account", style=discord.ButtonStyle.secondary, custom_id="gg:add", row=1)
    async def b_add(self, i, b): await i.response.send_modal(AddModal())

    @discord.ui.button(label="🔑 Add Token", style=discord.ButtonStyle.secondary, custom_id="gg:token", row=1)
    async def b_token(self, i, b): await i.response.send_modal(TokenModal())

    @discord.ui.button(label="⏸ Pause / Resume All", style=discord.ButtonStyle.secondary, custom_id="gg:pauseall", row=2)
    async def b_pauseall(self, i, b): await act_pauseall(i)

    @discord.ui.button(label="📜 Live Logs", style=discord.ButtonStyle.secondary, custom_id="gg:logs", row=2)
    async def b_logs(self, i, b): await reply(i, emb=logs_embed(12))

    @discord.ui.button(label="⚙️ Settings", style=discord.ButtonStyle.secondary, custom_id="gg:settings", row=2)
    async def b_settings(self, i, b): await reply(i, emb=settings_embed())

    @discord.ui.button(label="❔ Help", style=discord.ButtonStyle.secondary, custom_id="gg:help", row=2)
    async def b_help(self, i, b): await reply(i, emb=help_embed())

    @discord.ui.button(label="⏯ Pause Account", style=discord.ButtonStyle.secondary, custom_id="gg:pause", row=3)
    async def b_pause(self, i, b): await i.response.send_modal(UidModal("Pause / Resume Account", act_pause))

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, custom_id="gg:refresh", row=3)
    async def b_refresh(self, i, b): await i.response.send_modal(UidModal("Refresh Account", act_refresh))

    @discord.ui.button(label="♻️ Restart", style=discord.ButtonStyle.secondary, custom_id="gg:restart", row=3)
    async def b_restart(self, i, b): await i.response.send_modal(UidModal("Restart Account", act_restart))

    @discord.ui.button(label="🗑 Delete", style=discord.ButtonStyle.danger, custom_id="gg:delete", row=3)
    async def b_delete(self, i, b): await i.response.send_modal(UidModal("Delete Account", act_delete))

    @discord.ui.button(label="🏰 Guild Info", style=discord.ButtonStyle.secondary, custom_id="gg:guild", row=4)
    async def b_guild(self, i, b): await i.response.send_modal(GuildModal())

    @discord.ui.button(label="🤝 Clan Join", style=discord.ButtonStyle.secondary, custom_id="gg:clan", row=4)
    async def b_clan(self, i, b): await i.response.send_modal(ClanJoinModal())

    @discord.ui.button(label="🚀 Auto Guild Join", style=discord.ButtonStyle.success, custom_id="gg:join", row=4)
    async def b_join(self, i, b): await i.response.send_modal(JoinModal())


# ───────────────────────── client + slash commands ─────────────────────────
class GGTree(app_commands.CommandTree):
    async def interaction_check(self, i: discord.Interaction) -> bool:
        name = i.command.name if i.command else ""
        if name in PUBLIC_COMMANDS:
            return True                 # these two re-check policy themselves (can_use_autojoin)
        if is_admin(i.user):
            return True
        await deny(i)
        return False


class GGClient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())   # no privileged intents needed
        self.tree = GGTree(self)

    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(JoinOnlyView())
        try:
            if DEV_GUILD_ID.isdigit():
                g = discord.Object(id=int(DEV_GUILD_ID))
                self.tree.copy_global_to(guild=g)
                synced = await self.tree.sync(guild=g)
                print(f"[DISCORD] Synced {len(synced)} slash commands to guild {DEV_GUILD_ID} (instant).")
            else:
                synced = await self.tree.sync()
                print(f"[DISCORD] Synced {len(synced)} global slash commands (may take up to 1 hour to appear).")
        except discord.Forbidden:
            print("[DISCORD] Slash command sync forbidden: re-invite the bot with the 'applications.commands' scope.")
        except Exception as e:
            print(f"[DISCORD] Slash command sync failed: {safe_error(e)}")

    async def on_ready(self):
        print(f"[DISCORD] GUILD GLORY control bot started as {self.user}.")


client = GGClient()
tree = client.tree


@tree.command(name="start", description="Open the GUILD GLORY control panel")
async def cmd_start(i: discord.Interaction):
    if not is_admin(i.user):
        if not public_autojoin_enabled():
            return await deny(i)
        e = embed("⚡ GUILD GLORY • Guild Join Assistant",
                  "Submit your accounts and a Guild ID to start the join-request workflow.\n"
                  "Only use accounts you own or are authorized to use.")
        return await i.response.send_message(embed=e, view=JoinOnlyView(), ephemeral=True)
    e = embed("⚡ GUILD GLORY CONTROL", "Premium Discord Command Center\nWebsite controls • Live status • Logs\n\nChoose a module below.")
    await i.response.send_message(embed=e, view=PanelView(), ephemeral=True)


@tree.command(name="help", description="Command guide")
async def cmd_help(i: discord.Interaction):
    if not await ensure_admin(i): return
    await reply(i, emb=help_embed())


@tree.command(name="status", description="📊 Live statistics")
async def cmd_status(i: discord.Interaction):
    if not await ensure_admin(i): return
    await reply(i, emb=status_embed())


@tree.command(name="accounts", description="👥 Account list")
async def cmd_accounts(i: discord.Interaction): await send_accounts(i)


@tree.command(name="add", description="➕ Add guest account (UID + password in a private form)")
async def cmd_add(i: discord.Interaction):
    if not await ensure_admin(i): return
    await i.response.send_modal(AddModal())


@tree.command(name="token", description="🔑 Add token account (private form)")
async def cmd_token(i: discord.Interaction):
    if not await ensure_admin(i): return
    await i.response.send_modal(TokenModal())


@tree.command(name="delete", description="🗑️ Delete account")
@app_commands.describe(uid="Account UID")
async def cmd_delete(i: discord.Interaction, uid: str): await act_delete(i, uid.strip())


@tree.command(name="pause", description="⏯️ Pause or resume an account")
@app_commands.describe(uid="Account UID")
async def cmd_pause(i: discord.Interaction, uid: str): await act_pause(i, uid.strip())


@tree.command(name="pauseall", description="🔄 Pause or resume all accounts")
async def cmd_pauseall(i: discord.Interaction): await act_pauseall(i)


@tree.command(name="refresh", description="🔃 Refresh account profile")
@app_commands.describe(uid="Account UID")
async def cmd_refresh(i: discord.Interaction, uid: str): await act_refresh(i, uid.strip())


@tree.command(name="restart", description="🚀 Restart account worker")
@app_commands.describe(uid="Account UID")
async def cmd_restart(i: discord.Interaction, uid: str): await act_restart(i, uid.strip())


@tree.command(name="logs", description="📋 Latest activity logs")
async def cmd_logs(i: discord.Interaction):
    if not await ensure_admin(i): return
    await reply(i, emb=logs_embed(15))


@tree.command(name="clearlogs", description="🧹 Clear activity logs")
async def cmd_clearlogs(i: discord.Interaction): await act_clearlogs(i)


@tree.command(name="clanjoin", description="⚔️ Send clan join request from all cached accounts")
@app_commands.describe(clan_id="Clan ID")
async def cmd_clanjoin(i: discord.Interaction, clan_id: str): await act_clanjoin(i, clan_id.strip())


@tree.command(name="guild", description="🏰 Fetch guild details")
@app_commands.describe(clan_id="Clan ID", region="Region, e.g. IND")
async def cmd_guild(i: discord.Interaction, clan_id: str, region: str = "IND"):
    await act_guild(i, clan_id.strip(), region.strip())


@tree.command(name="autojoin", description="🚀 Auto Guild Join — paste accounts or upload a .txt file")
@app_commands.describe(file="Optional .txt file, one UID:PASSWORD per line",
                       guild_id="Target Guild ID (required with a file)")
async def cmd_autojoin(i: discord.Interaction, file: discord.Attachment = None, guild_id: str = ""):
    if not can_use_autojoin(i.user):
        return await deny(i, "🔒 Auto Guild Join is restricted to administrators.")
    if file is None:
        return await i.response.send_modal(JoinModal(guild_default=guild_id.strip()))
    if not file.filename.lower().endswith(".txt"):
        return await reply(i, emb=embed("⚠️ Wrong file", "Please upload a `.txt` file.", COLOR_ERR))
    if file.size > MAX_TXT_BYTES:
        return await reply(i, emb=embed("⚠️ File too large", "Maximum size is 1 MB.", COLOR_ERR))
    if not guild_id.strip():
        return await reply(i, emb=embed("⚠️ Guild ID missing", "Provide `guild_id` together with the file.", COLOR_ERR))
    try:
        raw = await file.read()
        if len(raw) > MAX_TXT_BYTES:                 # don't trust the reported size alone
            return await reply(i, emb=embed("⚠️ File too large", "Maximum size is 1 MB.", COLOR_ERR))
        content = raw.decode("utf-8-sig")
    except Exception:
        return await reply(i, emb=embed("❌ Could not read file", "Please send a UTF-8 TXT file.", COLOR_ERR))
    await start_join(i, content, guild_id.strip())


@tree.error
async def on_tree_error(i: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[DISCORD] Command error: {safe_error(error)}")
    try:
        await reply(i, emb=embed("❌ Error", "Something went wrong. Check the console log.", COLOR_ERR))
    except Exception:
        pass


# ───────────────────────── entry point ─────────────────────────
async def start_discord_bot():
    if not BOT_TOKEN:
        print("[DISCORD] Disabled: set DISCORD_BOT_TOKEN environment variable.")
        return
    if INVALID_ADMIN_IDS:
        print(f"[DISCORD] Ignoring {len(INVALID_ADMIN_IDS)} non-numeric value(s) in DISCORD_ADMIN_IDS.")
    if not ADMIN_IDS:
        print("[DISCORD] Disabled: set DISCORD_ADMIN_IDS to your numeric Discord user ID.")
        return
    try:
        await client.start(BOT_TOKEN)
    except asyncio.CancelledError:
        raise
    except discord.LoginFailure:
        print("[DISCORD] Login failed: DISCORD_BOT_TOKEN is invalid.")
    except discord.PrivilegedIntentsRequired:
        print("[DISCORD] Privileged intents required (should not happen: none are requested).")
    except Exception as e:
        print(f"[DISCORD] Bot stopped: {safe_error(e)}")
    finally:
        if not client.is_closed():
            await client.close()
