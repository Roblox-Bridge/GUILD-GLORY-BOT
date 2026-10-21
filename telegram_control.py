# -*- coding: utf-8 -*-
"""Professional Telegram control panel for the GUILD GLORY dashboard."""
import os, json, time, asyncio
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import dashboard_server as ds

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "your bot token").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", "your cheat id").split(",") if x.strip()}

def is_admin(update):
    user = update.effective_user
    return bool(user and str(user.id) in ADMIN_IDS)

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live Dashboard", callback_data="status"),
         InlineKeyboardButton("👥 Accounts", callback_data="accounts")],
        [InlineKeyboardButton("➕ Add UID Account", callback_data="help_add"),
         InlineKeyboardButton("🔑 Add Token", callback_data="help_token")],
        [InlineKeyboardButton("⏸ Pause / Resume All", callback_data="pauseall"),
         InlineKeyboardButton("📜 Live Logs", callback_data="logs")],
        [InlineKeyboardButton("🔄 Refresh Account", callback_data="help_refresh"),
         InlineKeyboardButton("♻️ Restart Account", callback_data="help_restart")],
        [InlineKeyboardButton("🗑 Delete Account", callback_data="help_delete"),
         InlineKeyboardButton("🏰 Guild Info", callback_data="help_guild")],
        [InlineKeyboardButton("🤝 Clan Join Request", callback_data="help_clan")],
        [InlineKeyboardButton("🚀 Auto Guild Join", callback_data="join_start")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton("❔ Help & Support", callback_data="help_panel")]
    ])

def fmt_uptime(seconds):
    seconds=int(seconds); d,seconds=divmod(seconds,86400); h,seconds=divmod(seconds,3600); m,seconds=divmod(seconds,60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m {seconds}s"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        user_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Auto Guild Join", callback_data="join_start")]])
        await update.effective_message.reply_text(
            "╭━━━ ⚡ *GUILD GLORY* ━━━╮\n"
            "│  Guild Join Assistant\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Upload your account TXT file and submit a Guild ID to start the join-request workflow.\n"
            "Only use accounts you own or are authorized to use.",
            reply_markup=user_keyboard, parse_mode="Markdown")
        return
    await update.effective_message.reply_text(
        "╭━━━ ⚡ *GUILD GLORY CONTROL* ━━━╮\n"
        "│  Premium Telegram Command Center\n"
        "│  Website controls • Live status • Logs\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Choose a module below or use /help.",
        reply_markup=home_keyboard(), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    await update.effective_message.reply_text(
        "*GUILD GLORY • COMMAND GUIDE*\n\n"
        "/status — live statistics\n/accounts — account list\n"
        "/add UID PASSWORD — add guest account\n/token TOKEN — add token account\n"
        "/delete UID — delete account\n/pause UID — pause/resume account\n"
        "/pauseall — pause/resume all\n/refresh UID — refresh profile\n"
        "/restart UID — restart account worker\n/logs — latest activity\n"
        "/clanjoin CLAN_ID — send clan join request\n"
        "/guild CLAN_ID REGION — fetch guild details\n\n"
        "Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS` in environment variables.",
        parse_mode="Markdown")

async def status_text():
    uptime=max(1,int(time.time()-ds.bot_state.start_time))
    s=ds.bot_state
    active=sum(1 for a in s.accounts.values() if not s.is_paused(str(a.get("uid",""))))
    return ("╭━━ 📊 *LIVE OVERVIEW* ━━╮\n"
            f"├ Accounts: *{len(s.accounts)}*\n"
            f"├ Active matches: *{sum(a.get('active_matches',0) for a in s.accounts.values())}*\n"
            f"├ Matches started: *{s.total_matches_started}*\n"
            f"├ Total matches: *{s.total_matches}*\n"
            f"├ EXP gained: *{s.total_gained_exp:,}*\n"
            f"├ EXP/hour: *{int(s.total_gained_exp/uptime*3600):,}*\n"
            f"├ Runtime: *{fmt_uptime(uptime)}*\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯")

async def accounts_text():
    accounts=list(ds.bot_state.accounts.values())
    if not accounts: return "👥 *ACCOUNTS*\n\nNo accounts are currently loaded."
    rows=["👥 *ACCOUNT CENTER*"]
    for i,a in enumerate(accounts[:40],1):
        uid=str(a.get("uid") or a.get("account_id") or "Unknown")
        paused=ds.bot_state.is_paused(uid)
        rows.append(f"\n{i}. `{uid}`\n   Level: {a.get('level','—')} • EXP: {int(a.get('gained_exp',0) or 0):,}\n   Status: {'⏸ Paused' if paused else '🟢 Running'} • Matches: {a.get('active_matches',0)}")
    if len(accounts)>40: rows.append(f"\n… and {len(accounts)-40} more.")
    return "\n".join(rows)

async def do_add(data, update):
    # Shared, validated implementation (also cancels any previous worker for the same account).
    return ds.add_account(data, "Telegram")

async def add_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if len(context.args)!=2: return await update.effective_message.reply_text("Usage: /add UID PASSWORD")
    try:
        label=await do_add({"uid":context.args[0],"password":context.args[1]},update)
        await update.effective_message.reply_text(f"✅ Account `{label}` added to rotation.",parse_mode="Markdown")
    except Exception as e: await update.effective_message.reply_text(f"❌ Could not add account: {e}")

async def token_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if not context.args: return await update.effective_message.reply_text("Usage: /token YOUR_TOKEN")
    try:
        label=await do_add({"token":" ".join(context.args)},update)
        await update.effective_message.reply_text(f"✅ `{label}` added to rotation.",parse_mode="Markdown")
    except Exception as e: await update.effective_message.reply_text(f"❌ Could not add token: {e}")

async def delete_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if not context.args: return await update.effective_message.reply_text("Usage: /delete UID")
    uid=context.args[0]
    # Reuse the same deletion handler as the website by making a small request object.
    class Req:
        async def json(self): return {"uid":uid}
    result=await ds.handle_delete_account(Req())
    try: payload=json.loads(result.text)
    except Exception: payload={}
    await update.effective_message.reply_text("✅ Account removed." if payload.get("status")=="ok" else f"⚠️ Delete result: {payload}")

async def pause_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if not context.args: return await update.effective_message.reply_text("Usage: /pause UID")
    uid=context.args[0]
    if uid not in ds.bot_state.accounts and uid not in ds.bot_state.account_workers:
        return await update.effective_message.reply_text("❌ Account not found in active state.")
    state=ds.bot_state.toggle_pause(uid)
    await update.effective_message.reply_text(f"{'⏸ Paused' if state else '▶️ Resumed'} `{uid}`",parse_mode="Markdown")

async def pauseall_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    state=ds.bot_state.toggle_pause_all()
    await update.effective_message.reply_text("⏸ All accounts paused." if state else "▶️ All accounts resumed.")

async def refresh_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if not context.args: return await update.effective_message.reply_text("Usage: /refresh UID")
    uid=context.args[0]; cb=ds.bot_state.refresh_callbacks.get("on_refresh_account")
    if not cb: return await update.effective_message.reply_text("⚠️ Refresh callback is not available.")
    asyncio.create_task(cb(uid)); await update.effective_message.reply_text(f"🔄 Refresh requested for `{uid}`",parse_mode="Markdown")

async def restart_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if not context.args: return await update.effective_message.reply_text("Usage: /restart UID")
    uid=context.args[0]
    cb=ds.bot_state.refresh_callbacks.get("on_restart_account") or ds.bot_state.refresh_callbacks.get("on_refresh_account")
    if not cb: return await update.effective_message.reply_text("⚠️ Restart callback is not available.")
    asyncio.create_task(cb(uid)); await update.effective_message.reply_text(f"♻️ Restart requested for `{uid}`",parse_mode="Markdown")

async def clearlogs_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    ds.bot_state.logs.clear()
    await update.effective_message.reply_text("🧹 Activity logs cleared.")

async def logs_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    entries=ds.bot_state.logs[-15:]
    if not entries: return await update.effective_message.reply_text("📜 No activity logs yet.")
    out=["📜 *LATEST ACTIVITY*"]
    for e in entries:
        if isinstance(e,dict): out.append(f"`{e.get('time','')}` {e.get('message','')}")
        else: out.append(str(e))
    msg="\n".join(out)
    await update.effective_message.reply_text(msg[:4000],parse_mode="Markdown")

async def clanjoin_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if not context.args: return await update.effective_message.reply_text("Usage: /clanjoin CLAN_ID")
    try:
        tokens=json.load(open("token_cache.json",encoding="utf-8"))
        successes=total=0
        for uid,creds in tokens.items():
            token=creds.get("token")
            if not token: continue
            total+=1
            try:
                code,_=await ds.request_clan_async(token,context.args[0],creds.get("region","IND"))
                successes+=int(code==200)
            except Exception: pass
        await update.effective_message.reply_text(f"🏰 Clan join requests sent: *{successes}/{total}*",parse_mode="Markdown")
    except Exception as e: await update.effective_message.reply_text(f"❌ Clan join failed: {e}")

async def guild_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    if len(context.args)<2: return await update.effective_message.reply_text("Usage: /guild CLAN_ID REGION")
    try:
        cache=json.load(open("token_cache.json",encoding="utf-8"))
        data=ds.fetch_single_clan(context.args[0],context.args[1].upper(),cache)
        if "error" in data: return await update.effective_message.reply_text(f"❌ {data['error']}")
        data.pop("raw_response_bytes",None)
        await update.effective_message.reply_text("🏰 *GUILD INTEL*\n```json\n"+json.dumps(data,indent=2,ensure_ascii=False)[:3500]+"\n```",parse_mode="Markdown")
    except Exception as e: await update.effective_message.reply_text(f"❌ Guild lookup failed: {e}")

# ---------- Telegram TXT Guild Join Wizard ----------
# Users upload a UTF-8 .txt file containing one UID:PASSWORD pair per line.
# Credentials are processed in memory and are not saved by this wizard.
JOIN_JWT_API_URL = "https://ff-jwt-gen-api.lovable.app/api/public/token"
JOIN_API_URL = "https://client.ind.freefiremobile.com/RequestJoinClan"
JOIN_KEY = b'Yg&tc%DEuh6%Zc^8'
JOIN_IV = b'6oyZDr22E3ychjM%'


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
    try:
        r = requests.get(JOIN_JWT_API_URL, params={"uid": uid, "password": password}, timeout=25)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, str): return data.strip() or None
        if isinstance(data, dict):
            token = data.get("token") or data.get("jwt") or data.get("access_token")
            if isinstance(token, dict): token = token.get("token") or token.get("jwt")
            if not token and isinstance(data.get("data"), dict):
                token = data["data"].get("token") or data["data"].get("jwt")
            return token if isinstance(token, str) and token.strip() else None
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


async def join_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["guild_join_state"] = "awaiting_accounts"
    context.user_data.pop("guild_join_accounts", None)
    await update.callback_query.edit_message_text(
        "╭━━━ 🚀 *AUTO GUILD JOIN* ━━━╮\n"
        "│ Step 1 of 2 • Add Accounts\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Send accounts in either way:\n"
        "• Upload a `.txt` file, or\n"
        "• Paste the list directly in this chat\n\n"
        "One account per line, using this format:\n"
        "`UID:PASSWORD`\n\n"
        "Example:\n`123456789:your_password`\n`987654321:your_password`\n\n"
        "• Maximum 100 unique accounts per run\n"
        "• Credentials are processed in memory and not saved by this wizard\n"
        "• Only use accounts you own or are authorized to use\n\n"
        "Send the TXT file or paste the account list now. Use /cancel to stop.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="home"), InlineKeyboardButton("✖ Cancel", callback_data="join_cancel")]])
    )


async def join_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("guild_join_state", None)
    context.user_data.pop("guild_join_accounts", None)
    if update.callback_query:
        await update.callback_query.edit_message_text("🛑 Guild join wizard cancelled.", reply_markup=home_keyboard() if is_admin(update) else InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Auto Guild Join", callback_data="join_start")]]))
    elif update.effective_message:
        await update.effective_message.reply_text("🛑 Guild join wizard cancelled.", reply_markup=home_keyboard())


async def join_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("guild_join_state") != "awaiting_accounts":
        return
    doc = update.effective_message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await update.effective_message.reply_text("⚠️ Please upload a `.txt` file.", parse_mode="Markdown")
        return
    if doc.file_size and doc.file_size > 1024 * 1024:
        await update.effective_message.reply_text("⚠️ File is too large. Maximum size is 1 MB.")
        return
    try:
        tg_file = await doc.get_file()
        payload = await tg_file.download_as_bytearray()
        content = bytes(payload).decode("utf-8-sig")
    except Exception:
        await update.effective_message.reply_text("❌ Could not read the file. Please send a UTF-8 TXT file.")
        return
    await accept_join_accounts(update, context, content, source="TXT file")


async def accept_join_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, content: str, source: str = "message"):
    accounts, invalid = [], 0
    seen = set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            invalid += 1
            continue
        uid, password = line.split(":", 1)
        uid, password = uid.strip(), password.strip()
        if not uid or not password:
            invalid += 1
            continue
        if uid in seen:
            continue
        seen.add(uid)
        accounts.append((uid, password))
    if not accounts:
        await update.effective_message.reply_text("❌ No valid `UID:PASSWORD` rows found. Check the format and try again.", parse_mode="Markdown")
        return
    if len(accounts) > 100:
        await update.effective_message.reply_text(f"⚠️ Found {len(accounts)} accounts. Maximum per run is 100; split the list and retry.")
        return
    context.user_data["guild_join_accounts"] = accounts
    context.user_data["guild_join_state"] = "awaiting_guild_id"
    await update.effective_message.reply_text(
        "╭━━━ ✅ *ACCOUNTS ACCEPTED* ━━━╮\n"
        f"├ Source: *{source}*\n├ Valid accounts: *{len(accounts)}*\n├ Invalid rows skipped: *{invalid}*\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Step 2 of 2 • Send the target *Guild ID* as a number.\nUse /cancel to stop.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data="join_cancel"), InlineKeyboardButton("🏠 Main Menu", callback_data="home")]])
    )


async def join_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("guild_join_state")
    raw = (update.effective_message.text or "").strip()
    if state == "awaiting_accounts":
        # Accept a pasted multiline UID:PASSWORD list instead of requiring a file upload.
        if ":" not in raw:
            await update.effective_message.reply_text("⚠️ Send a TXT file or paste one `UID:PASSWORD` pair per line. Use /cancel to stop.", parse_mode="Markdown")
            return
        await accept_join_accounts(update, context, raw, source="pasted text")
        return
    if state != "awaiting_guild_id":
        return
    if not raw.isdigit() or len(raw) > 20:
        await update.effective_message.reply_text("⚠️ Guild ID must be a numeric ID. Please send it again, or /cancel.")
        return
    guild_id = int(raw)
    accounts = context.user_data.get("guild_join_accounts", [])
    if not accounts:
        context.user_data.pop("guild_join_state", None)
        await update.effective_message.reply_text("❌ Account file session expired. Tap the wizard button and upload again.")
        return
    context.user_data.pop("guild_join_state", None)
    context.user_data.pop("guild_join_accounts", None)
    total = len(accounts)
    progress = await update.effective_message.reply_text(
        "╭━━━ ⚙️ *GUILD JOIN PROCESSOR* ━━━╮\n"
        f"├ Guild ID: `{guild_id}`\n├ Accounts queued: *{total}*\n"
        "├ JWT generated: *0*\n├ Requests sent: *0*\n├ HTTP 200: *0*\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n⏳ Initializing…", parse_mode="Markdown")
    jwt_ok = sent = success = jwt_failed = request_failed = 0
    results = []
    for index, (uid, password) in enumerate(accounts, 1):
        jwt = await asyncio.to_thread(_join_generate_jwt, uid, password)
        if not jwt:
            jwt_failed += 1
            results.append((uid, "JWT_FAILED", None))
        else:
            jwt_ok += 1
            response = await asyncio.to_thread(_join_send, jwt, guild_id)
            if response is None:
                request_failed += 1
                results.append((uid, "NETWORK_ERROR", None))
            else:
                sent += 1
                if response.status_code == 200:
                    success += 1
                    results.append((uid, "HTTP_200", 200))
                else:
                    results.append((uid, "HTTP_RESPONSE", response.status_code))
        if index == total or index % 2 == 0:
            latest = results[-1]
            text = (
                "╭━━━ ⚙️ *GUILD JOIN PROCESSOR* ━━━╮\n"
                f"├ Guild ID: `{guild_id}`\n├ Progress: *{index}/{total}* `({'█' * int(index/total*10)}{'░' * (10-int(index/total*10))})`\n"
                f"├ JWT generated: *{jwt_ok}*\n├ Requests sent: *{sent}*\n├ HTTP 200: *{success}*\n"
                f"├ JWT failed: *{jwt_failed}*\n├ Network errors: *{request_failed}*\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"Latest UID: `{latest[0]}`\nLatest result: *{latest[1]}*"
                + (f" (HTTP {latest[2]})" if latest[2] is not None else "")
            )
            try: await progress.edit_text(text, parse_mode="Markdown")
            except Exception: pass
        await asyncio.sleep(0.35)
    # Keep the report in the Telegram chat; do not persist credentials or passwords.
    detail = [f"`{uid}` — {status}" + (f" ({code})" if code is not None else "") for uid, status, code in results]
    summary = (
        "╭━━━ 🏁 *GUILD JOIN REPORT* ━━━╮\n"
        f"├ Guild ID: `{guild_id}`\n├ Processed: *{total}/{total}*\n"
        f"├ JWT success: *{jwt_ok}*\n├ Requests sent: *{sent}*\n├ HTTP 200: *{success}*\n"
        f"├ JWT failed: *{jwt_failed}*\n├ Network errors: *{request_failed}*\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "*Per-account results*\n" + "\n".join(detail[:45])
    )
    if len(detail) > 45:
        summary += f"\n…and {len(detail)-45} more results."
    await progress.edit_text(summary[:4000], parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Run Again", callback_data="join_start"), InlineKeyboardButton("🏠 Main Menu", callback_data="home")]
    ]))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    key=q.data
    if key == "join_start":
        return await join_wizard_start(update, context)
    if key == "join_cancel":
        return await join_cancel(update, context)
    if key == "home":
        if not is_admin(update):
            return await q.edit_message_text("⚡ *GUILD GLORY*\nChoose an action:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Auto Guild Join", callback_data="join_start")]]), parse_mode="Markdown")
        return await q.edit_message_text("⚡ *GUILD GLORY CONTROL*\nSelect a module:",reply_markup=home_keyboard(),parse_mode="Markdown")
    if not is_admin(update): return await q.edit_message_text("🔒 Admin access only.")
    if key=="status": text=await status_text()
    elif key=="accounts": text=await accounts_text()
    elif key=="logs":
        entries=ds.bot_state.logs[-12:]
        text="📜 *LATEST ACTIVITY*\n"+"\n".join(f"`{e.get('time','')}` {e.get('message','')}" if isinstance(e,dict) else str(e) for e in entries) if entries else "📜 No logs yet."
    elif key=="pauseall":
        paused=ds.bot_state.toggle_pause_all()
        text="⏸ All accounts paused." if paused else "▶️ All accounts resumed."
    elif key=="settings":
        text=("⚙️ *CONTROL PANEL SETTINGS*\n\n"
              "• Admin access is controlled by TELEGRAM_ADMIN_IDS\n"
              "• Bot token must be stored in TELEGRAM_BOT_TOKEN\n"
              "• Guild Join accepts a TXT upload or pasted UID:PASSWORD list\n"
              "• Maximum 100 accounts per run\n"
              "• Account credentials are not saved by the join wizard")
    elif key=="help_panel":
        text=("❔ *GUILD GLORY HELP & SUPPORT*\n\n"
              "Use the inline buttons to open a module.\n"
              "Commands: /status, /accounts, /logs, /add, /token, /delete, /pause, /pauseall, /refresh, /restart, /clanjoin, /guild, /cancel\n\n"
              "For account safety, only submit accounts you own or are authorized to use.")
    else:
        helptext={
        "help_add":"Send: `/add UID PASSWORD`","help_token":"Send: `/token YOUR_TOKEN`",
        "help_refresh":"Send: `/refresh UID`","help_restart":"Send: `/restart UID`",
        "help_delete":"Send: `/delete UID`","help_guild":"Send: `/guild CLAN_ID REGION`",
        "help_clan":"Send: `/clanjoin CLAN_ID`"}
        text="🧭 *QUICK ACTION*\n"+helptext.get(key,"Use /help for commands.")
    await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu",callback_data="home")]]),parse_mode="Markdown")

async def start_telegram_bot():
    if not BOT_TOKEN:
        print("[TELEGRAM] Disabled: set TELEGRAM_BOT_TOKEN environment variable.")
        return
    if not ADMIN_IDS:
        print("[TELEGRAM] Disabled: set TELEGRAM_ADMIN_IDS to your numeric Telegram user ID.")
        return
    app=Application.builder().token(BOT_TOKEN).build()
    for command,func in [
        ("start",start),("help",help_cmd),("status",lambda u,c: None),
        ("accounts",lambda u,c: None),("add",add_cmd),("token",token_cmd),
        ("delete",delete_cmd),("pause",pause_cmd),("pauseall",pauseall_cmd),
        ("refresh",refresh_cmd),("restart",restart_cmd),("logs",logs_cmd),("clearlogs",clearlogs_cmd),
        ("clanjoin",clanjoin_cmd),("guild",guild_cmd)]:
        if command=="status": app.add_handler(CommandHandler(command, status_cmd))
        elif command=="accounts": app.add_handler(CommandHandler(command, accounts_cmd))
        else: app.add_handler(CommandHandler(command,func))
    app.add_handler(CommandHandler("cancel", join_cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, join_file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, join_text_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    print("[TELEGRAM] GUILD GLORY control bot started.")

async def status_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    await update.effective_message.reply_text(await status_text(),parse_mode="Markdown")

async def accounts_cmd(update, context):
    if not is_admin(update): return await update.effective_message.reply_text("🔒 Admin access only.")
    await update.effective_message.reply_text((await accounts_text())[:4000],parse_mode="Markdown")
