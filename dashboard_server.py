# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Professional Web Dashboard & Real-Time EXP Tracker
Embedded Async Web Server (aiohttp)
Optimized for ultra-smooth operation, zero memory leaks, and dynamic multi-account control.
"""

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import time
from typing import Dict, List, Any, Optional
from aiohttp import web
import requests
import httpx
import jwt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import ReqCLan_pb2
import encode_id_clan_pb2
import data_pb2
import output_pb2
from google.protobuf.json_format import MessageToDict
import security_utils as su
from security_utils import redact, safe_error

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Web_temp", "web.html")

OB = "OB55"
KEY = bytes([89,103,38,116,99,37,68,69,117,104,54,37,90,99,94,56])
IV  = bytes([54,111,121,90,68,114,50,50,69,51,121,99,104,106,77,37])

GAME_HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9)",
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/octet-stream",
    "X-Unity-Version": "2018.4.11f1",
    "X-GA": "v1 1",
    "ReleaseVersion": OB,
    "Expect": "100-continue"
}

REGION_SERVER_MAP = {
    "IND": ("https://client.ind.freefiremobile.com", "client.ind.freefiremobile.com"),
    "ME":  ("https://clientbp.ggblueshark.com", "clientbp.ggblueshark.com"),
    "VN":  ("https://clientbp.ggpolarbear.com", "clientbp.ggpolarbear.com"),
    "BD":  ("https://clientbp.ggwhitehawk.com", "clientbp.ggwhitehawk.com"),
    "PK":  ("https://clientbp.ggblueshark.com", "clientbp.ggblueshark.com"),
    "SG":  ("https://clientbp.ggpolarbear.com", "clientbp.ggpolarbear.com"),
    "BR":  ("https://client.us.freefiremobile.com", "client.us.freefiremobile.com"),
    "NA":  ("https://client.us.freefiremobile.com", "client.us.freefiremobile.com"),
    "ID":  ("https://clientbp.ggpolarbear.com", "clientbp.ggpolarbear.com"),
    "RU":  ("https://clientbp.ggpolarbear.com", "clientbp.ggpolarbear.com"),
    "TH":  ("https://clientbp.ggpolarbear.com", "clientbp.ggpolarbear.com"),
    "SAC": ("https://client.br.freefiremobile.com", "client.br.freefiremobile.com"),
    "TW":  ("https://client.tw.freefiremobile.com", "client.tw.freefiremobile.com"),
    "CIS": ("https://client.cis.freefiremobile.com", "client.cis.freefiremobile.com"),
}
DEFAULT_SERVER_URL = "https://clientbp.ggblueshark.com"
DEFAULT_HOST = "clientbp.ggblueshark.com"

def encrypt_payload(data):
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    return cipher.encrypt(pad(data, AES.block_size))

def get_server_info(region):
    val = REGION_SERVER_MAP.get(str(region).upper(), DEFAULT_SERVER_URL)
    if isinstance(val, tuple):
        return val
    return (val, DEFAULT_HOST)

def get_server_url(region):
    info = get_server_info(region)
    return info[0] if isinstance(info, tuple) else info

def fetch_single_clan(clan_id, region, token_cache):
    server_base, host = get_server_info(region)
    url = f"{server_base}/GetClanInfoByClanID"

    try:  
        my_data = encode_id_clan_pb2.MyData()  
        my_data.field1 = int(clan_id)  
        my_data.field2 = 1  
        payload = encrypt_payload(my_data.SerializeToString())  
    except Exception as e:
        return {"clan_id": clan_id, "error": f"Protobuf failed: {str(e)}"}

    response = None
    for uid, creds in token_cache.items():
        token = creds.get('token')
        if not token:
            continue

        headers = GAME_HEADERS.copy()
        headers["Authorization"] = f"Bearer {token}" if not token.startswith("Bearer ") else token
        headers["Host"] = host

        try:
            res = requests.post(url, headers=headers, data=payload, timeout=15.0)  # TLS verification ON (default)
            if res.status_code == 200:
                response = res
                break
            elif "expired" in res.text.lower() or res.status_code == 401:
                continue
            else:
                response = res
                break
        except Exception as ex:
            continue

    if not response or response.status_code != 200:
        return {"clan_id": clan_id, "error": "All tokens failed or expired"}

    full_response = {}
    resp = None
    try:
        resp = data_pb2.response()  
        resp.ParseFromString(response.content)  
        full_response = MessageToDict(resp, preserving_proto_field_name=True)
    except Exception as e:
        pass

    guild_xp = 0
    if resp:
        for field_desc, val in resp.ListFields():
            if field_desc.number == 36 and isinstance(val, int):
                guild_xp = val
                break

    if guild_xp == 0 and full_response:
        guild_xp = int(full_response.get("field_36") or full_response.get("xp") or 0)

    return {
        "captain_id": getattr(resp, "captain_id", 0) if resp else full_response.get("captain_id", 0),
        "clan_id": int(clan_id),
        "clan_level": getattr(resp, "rank", 4) if resp else full_response.get("rank", 4),
        "clan_name": getattr(resp, "special_code", None) if resp else (full_response.get("special_code") or f"Guild_{clan_id}"),
        "current_officer": full_response.get("current_officer", 0),
        "current_members": full_response.get("current_members", 1),                
        "max_members": full_response.get("max_members", 50),
        "Total guild xp": int(guild_xp),
        "welcome_message": getattr(resp, "welcome_message", "") if resp else full_response.get("welcome_message", ""),
        "region": region,
        "xp": int(guild_xp),
        "raw_response_bytes": response.content.hex()
    }

async def request_clan_async(jwt_token, clan_id, region):
    server_url = get_server_url(region)
    msg = ReqCLan_pb2.MyMessage()
    msg.field_1 = int(clan_id)
    payload = encrypt_payload(msg.SerializeToString())
    headers = GAME_HEADERS.copy()
    headers["Authorization"] = f"Bearer {jwt_token}" if not jwt_token.startswith("Bearer ") else jwt_token
    url = f"{server_url}/RequestJoinClan"
    # TLS certificate verification is ON (httpx default). A join request is not retried automatically.
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.post(url, headers=headers, content=payload)
        return r.status_code, r.text

EXP_TABLE: Dict[int, int] = {
    1: 0, 2: 48, 3: 202, 4: 544, 5: 1012, 6: 1844, 7: 2792, 8: 3800,
    9: 4870, 10: 6004, 11: 7192, 12: 8448, 13: 9760, 14: 11140, 15: 12566,
    16: 14060, 17: 15610, 18: 17224, 19: 18902, 20: 20632, 21: 22424, 22: 24278,
    23: 26192, 24: 28166, 25: 30200, 26: 32294, 27: 34448, 28: 37804, 29: 41274,
    30: 44870, 31: 48582, 32: 53394, 33: 58566, 34: 64096, 35: 69994, 36: 76460,
    37: 83506, 38: 91128, 39: 99322, 40: 108092, 41: 120144, 42: 133266, 43: 147472,
    44: 162760, 45: 179126, 46: 196572, 47: 215368, 48: 235516, 49: 257010, 50: 279860,
    51: 304056, 52: 348318, 53: 394982, 54: 444044, 55: 495508, 56: 549364, 57: 633756,
    58: 721744, 59: 813336, 60: 908522, 61: 1041438, 62: 1180352, 63: 1325266,
    64: 1476184, 65: 1634300, 66: 1840946, 67: 2056594, 68: 2281242, 69: 2514880,
    70: 2757530, 71: 3059506, 72: 3372284, 73: 3699456, 74: 4041030, 75: 4397002,
    76: 4829104, 77: 5282204, 78: 5756304, 79: 6251408, 80: 6776502, 81: 7381324,
    82: 8043154, 83: 8752982, 84: 9510808, 85: 10316338, 86: 11277190, 87: 12291748,
    88: 13360304, 89: 14482858, 90: 15659418, 91: 17026708, 92: 18453950, 93: 19941280,
    94: 21488570, 95: 23095858, 96: 24763138, 97: 26490428, 98: 28378704, 99: 30124996,
    100: 32032884
}

_SENSITIVE_ACCOUNT_KEYS = {"token", "access_token", "auth_token", "auth_password", "password", "login_payload_data"}


def public_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of an account dict without any credential-like field."""
    return {k: v for k, v in acc.items() if k not in _SENSITIVE_ACCOUNT_KEYS}


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = [public_account(a) for a in bot_state.accounts.values()]
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    uptime_sec = max(1, int(time.time() - bot_state.start_time))
    total_gained = bot_state.total_gained_exp
    exp_per_hour = int((total_gained / uptime_sec) * 3600)
    total_active_matches = sum(acc.get("active_matches", 0) for acc in accounts_data)

    for acc in accounts_data:
        uid_k = str(acc.get("uid", ""))
        acc["uptime_seconds"] = bot_state.get_account_uptime(uid_k)
        acc["is_paused"] = bot_state.is_paused(uid_k)

    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches_started,
        "total_active_matches": total_active_matches,
        "total_gained_exp": total_gained,
        "exp_per_hour": exp_per_hour,
        "accounts": accounts_data,
        "logs": bot_state.logs[-80:],
        "uptime": uptime_sec
    })

def calculate_level_progress(level: int, current_exp: int) -> Dict[str, Any]:
    level = max(1, level)
    next_level = min(100, level + 1)
    base_exp = EXP_TABLE.get(level, 0)
    target_exp = EXP_TABLE.get(next_level, base_exp + 50000)
    
    needed_for_level = max(1, target_exp - base_exp)
    earned_in_level = max(0, current_exp - base_exp)
    remaining_exp = max(0, target_exp - current_exp)
    progress_pct = min(100.0, max(0.0, (earned_in_level / needed_for_level) * 100.0))

    return {
        "next_level": next_level,
        "base_exp": base_exp,
        "target_exp": target_exp,
        "needed_for_level": needed_for_level,
        "earned_in_level": earned_in_level,
        "remaining_exp": remaining_exp,
        "progress_pct": round(progress_pct, 1)
    }

class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_matches_started = 0      # 🔥 NEW
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.account_token_map: Dict[str, str] = {}  # uid -> token or token_prefix -> uid
        self.auth_to_game_id: Dict[str, str] = {}   # guest login uid -> in-game account id
        self.game_to_auth_id: Dict[str, str] = {}   # in-game account id -> guest login uid
        self.paused_accounts: set = set()
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.active_writers: Dict[str, set] = {}

    def register_writer(self, uid: str, writer):
        uid_str = str(uid)
        if uid_str not in self.active_writers:
            self.active_writers[uid_str] = set()
        self.active_writers[uid_str].add(writer)

    def unregister_writer(self, uid: str, writer):
        uid_str = str(uid)
        if uid_str in self.active_writers:
            self.active_writers[uid_str].discard(writer)
            if not self.active_writers[uid_str]:
                self.active_writers.pop(uid_str, None)

    def close_writers_for_account(self, uid: str):
        uid_str = str(uid)
        candidates = {uid_str}
        if uid_str in self.auth_to_game_id:
            candidates.add(str(self.auth_to_game_id[uid_str]))
        if uid_str in self.game_to_auth_id:
            candidates.add(str(self.game_to_auth_id[uid_str]))
        if uid_str in self.account_token_map:
            mapped = self.account_token_map[uid_str]
            candidates.add(str(mapped))
            candidates.add(str(mapped)[:16])

        for c in list(candidates):
            writers = list(self.active_writers.get(c, []))
            for w in writers:
                try:
                    if hasattr(w, "close"):
                        if hasattr(w, "is_closing"):
                            if not w.is_closing():
                                w.close()
                        else:
                            w.close()
                except Exception:
                    pass
            self.active_writers.pop(c, None)

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": redact(message),
            "uid": str(uid) if uid else None
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int,
                         likes: int = 0, token: Optional[str] = None, auth_uid: Optional[str] = None):
        uid_str = str(uid)
        auth_uid_str = str(auth_uid) if auth_uid else self.game_to_auth_id.get(uid_str, "")
        if auth_uid_str:
            self.auth_to_game_id[auth_uid_str] = uid_str
            self.game_to_auth_id[uid_str] = auth_uid_str
            self.account_token_map[auth_uid_str] = uid_str
            self.account_token_map[uid_str] = auth_uid_str
        if token:
            self.account_token_map[uid_str] = token
            self.account_token_map[token[:16]] = uid_str
            if auth_uid_str:
                self.account_token_map[auth_uid_str] = token

        prog = calculate_level_progress(level or 1, exp)

        lvl_val = level or 1
        acc_mode = "BR" if lvl_val < 3 else "LONE_WOLF"
        acc_mode_label = "Battle Royale (Lvl < 3)" if lvl_val < 3 else "Lone Wolf (Lvl 3+)"

        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "auth_uid": auth_uid_str or "",
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": lvl_val,
                "next_level": prog["next_level"],
                "mode": acc_mode,
                "mode_label": acc_mode_label,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "remaining_exp": prog["remaining_exp"],
                "target_exp": prog["target_exp"],
                "needed_for_level": prog["needed_for_level"],
                "earned_in_level": prog["earned_in_level"],
                "progress_pct": prog["progress_pct"],
                "likes": likes or 0,
                "status": "PAUSED" if self.is_paused(uid_str) else "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "token": token or "",
                "start_time": time.time(),
                "is_paused": self.is_paused(uid_str),
                "paused_at": time.time() if self.is_paused(uid_str) else None,
                "total_pause_duration": 0.0,
                "last_updated": time.strftime("%H:%M:%S")
            }
        else:
            acc = self.accounts[uid_str]
            if auth_uid_str:
                acc["auth_uid"] = auth_uid_str
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            if token:
                acc["token"] = token
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["next_level"] = prog["next_level"]
            acc["remaining_exp"] = prog["remaining_exp"]
            acc["target_exp"] = prog["target_exp"]
            acc["needed_for_level"] = prog["needed_for_level"]
            acc["earned_in_level"] = prog["earned_in_level"]
            acc["progress_pct"] = prog["progress_pct"]
            acc["likes"] = likes
            if not acc.get("is_paused"):
                acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def get_account_uptime(self, uid_str: str) -> int:
        acc = self.accounts.get(uid_str)
        if not acc:
            mapped = self.game_to_auth_id.get(uid_str) or self.auth_to_game_id.get(uid_str)
            if mapped and mapped in self.accounts:
                acc = self.accounts[mapped]
        if not acc:
            return 0
        start_t = acc.get("start_time", time.time())
        total_pause = acc.get("total_pause_duration", 0.0)
        if acc.get("is_paused") and acc.get("paused_at"):
            return max(0, int(acc["paused_at"] - start_t - total_pause))
        return max(0, int(time.time() - start_t - total_pause))

    def is_paused(self, uid: str) -> bool:
        uid_str = str(uid)
        if uid_str in self.paused_accounts:
            return True
        game_id = self.auth_to_game_id.get(uid_str)
        if game_id and game_id in self.paused_accounts:
            return True
        auth_uid = self.game_to_auth_id.get(uid_str)
        if auth_uid and auth_uid in self.paused_accounts:
            return True
        acc = self.accounts.get(uid_str) or (self.accounts.get(game_id) if game_id else None)
        if acc and acc.get("is_paused"):
            return True
        return False

    def toggle_pause(self, uid: str) -> bool:
        uid_str = str(uid)
        candidates = {uid_str}
        if uid_str in self.auth_to_game_id:
            candidates.add(self.auth_to_game_id[uid_str])
        if uid_str in self.game_to_auth_id:
            candidates.add(self.game_to_auth_id[uid_str])

        target_acc = None
        target_key = uid_str
        for c in candidates:
            if c in self.accounts:
                target_acc = self.accounts[c]
                target_key = c
                break

        is_now_paused = not self.is_paused(uid_str)
        if is_now_paused:
            for c in candidates:
                self.paused_accounts.add(c)
                self.close_writers_for_account(c)
            if target_acc:
                target_acc["is_paused"] = True
                target_acc["paused_at"] = time.time()
                target_acc["status"] = "PAUSED"
            nick = target_acc.get("nickname", target_key) if target_acc else target_key
            self.log(f"⏸ UID {target_key} ({nick}) matchmaking PAUSED (TCP socket disconnected).", "warning", target_key)
            if "on_pause_toggle" in self.refresh_callbacks:
                try:
                    asyncio.create_task(self.refresh_callbacks["on_pause_toggle"](target_key, True))
                except Exception:
                    pass
        else:
            for c in candidates:
                self.paused_accounts.discard(c)
            if target_acc:
                target_acc["is_paused"] = False
                if target_acc.get("paused_at"):
                    pause_dur = time.time() - target_acc["paused_at"]
                    target_acc["total_pause_duration"] = target_acc.get("total_pause_duration", 0.0) + pause_dur
                    target_acc["paused_at"] = None
                target_acc["status"] = "ONLINE"
            nick = target_acc.get("nickname", target_key) if target_acc else target_key
            self.log(f"▶ UID {target_key} ({nick}) matchmaking RESUMED.", "success", target_key)
            if "on_pause_toggle" in self.refresh_callbacks:
                try:
                    asyncio.create_task(self.refresh_callbacks["on_pause_toggle"](target_key, False))
                except Exception:
                    pass

        return is_now_paused

    def toggle_pause_all(self) -> bool:
        any_active = any(not self.is_paused(k) for k in self.accounts.keys())
        for k in list(self.accounts.keys()):
            current_paused = self.is_paused(k)
            if any_active and not current_paused:
                self.toggle_pause(k)
            elif not any_active and current_paused:
                self.toggle_pause(k)
        return any_active

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            old_level = acc.get("level", 1)
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            
            current_lvl = acc["level"]
            acc["mode"] = "BR" if current_lvl < 3 else "LONE_WOLF"
            acc["mode_label"] = "Battle Royale (Lvl < 3)" if current_lvl < 3 else "Lone Wolf (Lvl 3+)"

            prog = calculate_level_progress(acc["level"], current_exp)
            acc["next_level"] = prog["next_level"]
            acc["remaining_exp"] = prog["remaining_exp"]
            acc["target_exp"] = prog["target_exp"]
            acc["needed_for_level"] = prog["needed_for_level"]
            acc["earned_in_level"] = prog["earned_in_level"]
            acc["progress_pct"] = prog["progress_pct"]
            acc["last_updated"] = time.strftime("%H:%M:%S")

            # Check for Level 2 -> 3 Mode Transition
            if old_level < 3 and current_lvl >= 3:
                self.log(
                    f"🎉 LEVEL UP! UID {uid_str} ({acc['nickname']}) reached Level {current_lvl}! Switching from Battle Royale to Lone Wolf mode!",
                    "success",
                    uid_str
                )

            diff = current_exp - old_exp
            if diff > 0:
                self.log(
                    f"★ UID {uid_str} ({acc['nickname']}) gained +{diff:,} EXP | Level {acc['level']} [{acc['mode']}] ({prog['progress_pct']}% - {prog['remaining_exp']:,} EXP to Lvl {prog['next_level']})",
                    "success",
                    uid_str
                )
            self.recalc_totals()

    def get_account_level(self, uid: str) -> int:
        uid_str = str(uid)
        acc = self.accounts.get(uid_str)
        if not acc:
            mapped = self.game_to_auth_id.get(uid_str) or self.auth_to_game_id.get(uid_str)
            if mapped and mapped in self.accounts:
                acc = self.accounts[mapped]
        if acc:
            return int(acc.get("level", 1) or 1)
        return 1

    def get_account_mode(self, uid: str) -> str:
        lvl = self.get_account_level(uid)
        return "BR" if lvl < 3 else "LONE_WOLF"

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")


    def increment_match_started(self):
        """Track total matches STARTED (not completed)"""
        self.total_matches_started += 1

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.log(f"⚔ Match #{self.accounts[uid_str]['matches_played']} finished for {self.accounts[uid_str]['nickname']} ({uid_str})", "info", uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()

FALLBACK_INDEX_HTML = """<!DOCTYPE html>
<html>
<head><title>TEAM RBC Bot Dashboard</title></head>
<body style="background:#0a0a12;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
<h1>TEAM RBC BOT RUNNING</h1>
<p>Dashboard interface loaded successfully.</p>
</body>
</html>"""

async def handle_index(request: web.Request) -> web.Response:
    content = FALLBACK_INDEX_HTML
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    return web.Response(text=content, content_type="text/html", charset="utf-8")

# ==================== CLAN JOIN API ENDPOINT (AIOHTTP) ====================
_clan_join_lock = asyncio.Lock()


async def handle_api_clan_join(request: web.Request) -> web.Response:
    data = await _json_body(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    clan_id = str(data.get("clan_id", "")).strip()
    if not su.valid_clan_id(clan_id):
        return web.json_response({"error": "Clan ID must be a number"}, status=400)
    if _clan_join_lock.locked():
        return web.json_response({"error": "A clan join run is already in progress"}, status=429)
    async with _clan_join_lock:
        try:
            token_cache = su.read_json("token_cache.json", {})
            if not isinstance(token_cache, dict):
                token_cache = {}
            success_count = 0
            total_accounts = 0
            for uid, creds in token_cache.items():
                jwt_token = creds.get("token") if isinstance(creds, dict) else None
                region = (creds.get("region", "IND") if isinstance(creds, dict) else "IND")
                if not jwt_token:
                    continue
                total_accounts += 1
                try:
                    status_code, _ = await request_clan_async(jwt_token, clan_id, region)
                    if status_code == 200:
                        success_count += 1
                except Exception:
                    pass
            return web.json_response({
                "message": f"Clan join requests sent successfully for {success_count} / {total_accounts} accounts!"
            }, status=200)
        except Exception as e:
            bot_state.log(f"Clan join failed: {safe_error(e)}", "error")
            return web.json_response({"error": "Clan join failed"}, status=500)

# ==================== GUILD INFO API ENDPOINT (AIOHTTP) ====================
GUILD_CACHE_FILE = "guild_exp_cache.json"

def load_guild_cache():
    if os.path.exists(GUILD_CACHE_FILE):
        try:
            with open(GUILD_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_guild_cache(cache_data):
    try:
        with open(GUILD_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=4)
    except Exception:
        pass

def get_persisted_baseline_xp(clan_id, current_xp):
    cache = load_guild_cache()
    clan_key = str(clan_id)
    
    if clan_key not in cache or "old_xp" not in cache[clan_key]:
        cache[clan_key] = {
            "old_xp": current_xp,
            "live_xp": current_xp,
            "gained_exp": 0
        }
        save_guild_cache(cache)
        return current_xp
    
    return int(cache[clan_key]["old_xp"])

def _api_key_ok(request: web.Request) -> bool:
    """Optional machine access to /api/guildinfo. The key lives ONLY in env GUILDINFO_API_KEY
    (no default). Prefer the X-API-Key header; ?key= is accepted for backwards compatibility
    but ends up in access logs and browser history."""
    expected = os.getenv("GUILDINFO_API_KEY", "")
    if len(expected) < 16:
        return False
    supplied = request.headers.get("X-API-Key") or request.query.get("key") or ""
    return hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),
                               hashlib.sha256(expected.encode()).digest())


async def handle_api_guild_info(request: web.Request) -> web.Response:
    if request.get("session") is None and not _api_key_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    clan_id = (request.query.get('guild_id') or request.query.get('clan_uid') or request.query.get('clan_id') or "").strip()
    region = (request.query.get('region') or 'IND').strip().upper()
    if not clan_id:
        return web.json_response({"error": "guild_id is required"}, status=400)
    if not su.valid_clan_id(clan_id) or not su.valid_region(region):
        return web.json_response({"error": "Invalid guild_id or region"}, status=400)
    try:
        token_cache = su.read_json("token_cache.json", {})
        if not token_cache:
            return web.json_response({"error": "No cached account tokens available"}, status=503)

        clan_data = await asyncio.to_thread(fetch_single_clan, clan_id, region, token_cache)
        if "error" in clan_data:
            return web.json_response({"error": str(clan_data["error"])[:200]}, status=502)
        clan_data.pop("raw_response_bytes", None)

        current_xp = int(clan_data.get("Total guild xp") or clan_data.get("xp") or 0)
        baseline_xp = get_persisted_baseline_xp(clan_id, current_xp)
        gained_exp = current_xp - baseline_xp

        cache = load_guild_cache()
        cache[str(clan_id)] = {"old_xp": baseline_xp, "live_xp": current_xp, "gained_exp": gained_exp}
        save_guild_cache(cache)

        clan_data["old_xp"] = baseline_xp
        clan_data["gained_exp"] = gained_exp

        api_response_body = {
            "DEVLOVER": "XEROX_MODS",
            "TELEGRAM": "SEXTYMODS",
            "TIME AND DATE": time.strftime("%I:%M %p, %A, %B %d, %Y"),
            "clan_basic_info": clan_data
        }
        return web.json_response({
            "status": "success",
            "requested_region": region,
            "queried_clan_id": clan_id,
            "api_response": api_response_body
        }, status=200)
    except Exception as e:
        bot_state.log(f"Guild info failed: {safe_error(e)}", "error")
        return web.json_response({"error": "Guild lookup failed"}, status=500)

_bg_tasks = set()


def spawn(coro, what="task"):
    """create_task that keeps a strong reference and logs (redacted) failures."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception():
            bot_state.log(f"Background {what} failed: {safe_error(t.exception())}", "error")
    task.add_done_callback(_done)
    return task


def _cancel_worker_for_uid(uid):
    w = bot_state.account_workers.pop(uid, None)
    if w is not None:
        try: w.cancel()
        except Exception: pass


def _cancel_worker_for_token(token):
    tok_key = token[:16]
    for k in list(bot_state.account_workers.keys()):
        if k == tok_key or k.startswith(tok_key[:10]) or tok_key.startswith(k[:10]):
            try: bot_state.account_workers[k].cancel()
            except Exception: pass
            bot_state.account_workers.pop(k, None)


def add_account(data: Dict[str, Any], source: str = "dashboard") -> str:
    """Validate + persist a new account and start its worker. Raises ValueError on bad input.
    Synchronous read-modify-write on the single event loop, so no interleaving is possible."""
    existing = su.read_json("accounts.json", [])
    if not isinstance(existing, list):
        existing = []
    if "uid" in data:
        uid = str(data["uid"]).strip()
        pwd = str(data.get("password", "")).strip()
        if not su.valid_uid(uid):
            raise ValueError("UID must be numeric (max 20 digits).")
        if not su.valid_password(pwd):
            raise ValueError("Password is required (max 128 printable characters).")
        _cancel_worker_for_uid(uid)
        existing = [a for a in existing if str(a.get("uid", "")) != uid]
        existing.append({"uid": uid, "password": pwd})
        label = uid
        payload = {"uid": uid, "password": pwd}
    elif "token" in data:
        token = str(data["token"]).strip()
        if not su.valid_token(token):
            raise ValueError("Token has an invalid format.")
        _cancel_worker_for_token(token)
        existing = [a for a in existing if a.get("token", "") != token]
        existing.append({"token": token})
        label = f"Token {token[:4]}..."
        payload = {"token": token}
    else:
        raise ValueError("Invalid payload.")
    su.atomic_write_json("accounts.json", existing)
    bot_state.log(f"New account added via {source}: {label}", "success")
    cb = bot_state.refresh_callbacks.get("on_account_added")
    if cb:
        spawn(cb(payload), "account start")
    return label


async def _json_body(request: web.Request):
    try:
        data = await request.json()
    except web.HTTPException:
        raise                      # e.g. 413 body too large
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def handle_add_account(request: web.Request) -> web.Response:
    data = await _json_body(request)
    if data is None:
        return web.json_response({"status": "error", "error": "Invalid JSON body"}, status=400)
    try:
        add_account(data, "dashboard")
        return web.json_response({"status": "ok"})
    except ValueError as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)
    except Exception as e:
        bot_state.log(f"Add account failed: {safe_error(e)}", "error")
        return web.json_response({"status": "error", "error": "Could not add account"}, status=500)

async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        req_uid = str(data.get("uid", "")).strip()
        req_auth_uid = str(data.get("auth_uid", "")).strip()
        if not req_uid and not req_auth_uid:
            return web.json_response({"status": "error", "error": "UID is required"}, status=400)
        if (req_uid and not su.valid_ident(req_uid)) or (req_auth_uid and not su.valid_ident(req_auth_uid)):
            return web.json_response({"status": "error", "error": "Invalid UID"}, status=400)

        # Collect ALL possible candidate identifiers for this account
        candidate_ids = set()
        if req_uid:
            candidate_ids.add(req_uid)
        if req_auth_uid:
            candidate_ids.add(req_auth_uid)

        # Check game_to_auth and auth_to_game mappings
        for cid in list(candidate_ids):
            if cid in bot_state.game_to_auth_id:
                candidate_ids.add(str(bot_state.game_to_auth_id[cid]))
            if cid in bot_state.auth_to_game_id:
                candidate_ids.add(str(bot_state.auth_to_game_id[cid]))

        # Inspect bot_state.accounts
        target_tokens = set()
        for cid in list(candidate_ids):
            acc_info = bot_state.accounts.get(cid, {})
            if acc_info:
                if acc_info.get("auth_uid"):
                    candidate_ids.add(str(acc_info["auth_uid"]))
                if acc_info.get("uid"):
                    candidate_ids.add(str(acc_info["uid"]))
                t = acc_info.get("token") or acc_info.get("access_token")
                if t:
                    target_tokens.add(str(t))

        # Inspect bot_state.account_credentials
        for cid in list(candidate_ids):
            creds = bot_state.account_credentials.get(cid, {})
            if creds:
                if creds.get("auth_uid"):
                    candidate_ids.add(str(creds["auth_uid"]))
                if creds.get("account_id"):
                    candidate_ids.add(str(creds["account_id"]))
                t = creds.get("token") or creds.get("access_token") or creds.get("auth_token")
                if t:
                    target_tokens.add(str(t))

        # Also clean token_cache.json if entries match
        token_cache_file = "token_cache.json"
        if os.path.exists(token_cache_file):
            try:
                with open(token_cache_file, "r", encoding="utf-8") as f:
                    tcache = json.load(f)
                dirty_cache = False
                for k, v in list(tcache.items()):
                    k_str = str(k)
                    v_acc_id = str(v.get("account_id", ""))
                    v_auth_uid = str(v.get("auth_uid", ""))
                    if k_str in candidate_ids or v_acc_id in candidate_ids or v_auth_uid in candidate_ids:
                        candidate_ids.add(k_str)
                        if v_acc_id:
                            candidate_ids.add(v_acc_id)
                        if v_auth_uid:
                            candidate_ids.add(v_auth_uid)
                        del tcache[k]
                        dirty_cache = True
                if dirty_cache:
                    su.atomic_write_json(token_cache_file, tcache)
            except Exception:
                pass

        # Remove from accounts.json
        accounts_file = "accounts.json"
        if os.path.exists(accounts_file):
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                new_existing = []
                for acc in existing:
                    acc_uid = str(acc.get("uid", "")).strip()
                    acc_tok = str(acc.get("token", "")).strip()
                    is_match = False
                    if acc_uid and acc_uid in candidate_ids:
                        is_match = True
                    if acc_tok and (acc_tok in candidate_ids or acc_tok in target_tokens):
                        is_match = True
                    for tok in target_tokens:
                        if acc_tok and (acc_tok.startswith(tok[:16]) or tok.startswith(acc_tok[:16])):
                            is_match = True
                    if not is_match:
                        new_existing.append(acc)

                su.atomic_write_json(accounts_file, new_existing)
            except Exception:
                pass

        # Remove matching devices from devices.json
        devices_file = "devices.json"
        if os.path.exists(devices_file):
            try:
                with open(devices_file, "r", encoding="utf-8") as f:
                    devices_data = json.load(f)
                dirty_devices = False
                for dev_k in list(devices_data.keys()):
                    dev_k_str = str(dev_k)
                    if dev_k_str in candidate_ids:
                        del devices_data[dev_k]
                        dirty_devices = True
                    else:
                        for tok in target_tokens:
                            if dev_k_str == tok[:16] or tok.startswith(dev_k_str):
                                del devices_data[dev_k]
                                dirty_devices = True
                                break
                if dirty_devices:
                    su.atomic_write_json(devices_file, devices_data, indent=4, private=False)
            except Exception:
                pass

        # Remove from in-memory bot_state.accounts & credentials
        for cid in candidate_ids:
            bot_state.accounts.pop(cid, None)
            bot_state.account_credentials.pop(cid, None)
            bot_state.auth_to_game_id.pop(cid, None)
            bot_state.game_to_auth_id.pop(cid, None)
            bot_state.account_token_map.pop(cid, None)

        # Cancel matching worker tasks
        cancelled_keys = []
        for k, worker in list(bot_state.account_workers.items()):
            k_str = str(k)
            should_cancel = False
            if k_str in candidate_ids:
                should_cancel = True
            for tok in target_tokens:
                if k_str == tok[:16] or tok.startswith(k_str[:10]):
                    should_cancel = True
            if should_cancel:
                try:
                    worker.cancel()
                except Exception:
                    pass
                cancelled_keys.append(k)

        for k in cancelled_keys:
            bot_state.account_workers.pop(k, None)

        for cid in candidate_ids:
            bot_state.close_writers_for_account(cid)

        # Trigger on_account_deleted callback in Main.py if registered
        if "on_account_deleted" in bot_state.refresh_callbacks:
            try:
                asyncio.create_task(bot_state.refresh_callbacks["on_account_deleted"](list(candidate_ids)))
            except Exception:
                pass

        target_repr = req_uid or req_auth_uid
        bot_state.log(f"Account {target_repr} completely deleted from system and stopped.", "warning", target_repr)
        bot_state.recalc_totals()
        return web.json_response({"status": "ok", "deleted": list(candidate_ids)})
    except Exception as e:
        bot_state.log(f"Delete failed: {safe_error(e)}", "error")
        return web.json_response({"status": "error", "error": "Could not delete account"}, status=500)


async def call_account_callback(name, uid, timeout):
    """Await an account callback that returns {"ok": bool, "message": str}. Never raises."""
    cb = bot_state.refresh_callbacks.get(name)
    if not cb:
        return {"ok": False, "message": "This action is not available (callback not registered)."}
    try:
        res = await asyncio.wait_for(cb(uid), timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "Timed out waiting for the action to finish."}
    except Exception as e:
        bot_state.log(f"{name} failed: {safe_error(e)}", "error")
        return {"ok": False, "message": "The action failed. See the activity log."}
    if isinstance(res, dict) and "ok" in res:
        return res
    return {"ok": True, "message": "Done."}


async def _uid_action(request: web.Request, callback_name: str, timeout: float) -> web.Response:
    data = await _json_body(request)
    uid = str((data or {}).get("uid", "")).strip()
    if not uid:
        return web.json_response({"status": "error", "error": "UID is required"}, status=400)
    if not su.valid_ident(uid):
        return web.json_response({"status": "error", "error": "Invalid UID"}, status=400)
    res = await call_account_callback(callback_name, uid, timeout)
    code = 200 if res["ok"] else (503 if "not available" in res["message"] else 409)
    return web.json_response({"status": "ok" if res["ok"] else "error",
                              ("message" if res["ok"] else "error"): res["message"]}, status=code)


async def handle_refresh_account(request: web.Request) -> web.Response:
    return await _uid_action(request, "on_refresh_account", 45.0)

async def handle_restart_account(request: web.Request) -> web.Response:
    return await _uid_action(request, "on_restart_account", 90.0)

async def handle_clear_logs(request: web.Request) -> web.Response:
    bot_state.logs.clear()
    return web.json_response({"status": "ok"})

async def handle_toggle_pause(request: web.Request) -> web.Response:
    data = await _json_body(request)
    uid = str((data or {}).get("uid", "")).strip()
    if not uid:
        return web.json_response({"status": "error", "error": "UID is required"}, status=400)
    if not su.valid_ident(uid):
        return web.json_response({"status": "error", "error": "Invalid UID"}, status=400)
    is_paused = bot_state.toggle_pause(uid)
    return web.json_response({"status": "ok", "is_paused": is_paused})

async def handle_toggle_pause_all(request: web.Request) -> web.Response:
    paused_state = bot_state.toggle_pause_all()
    return web.json_response({"status": "ok", "all_paused": paused_state})

async def handle_api_logs(request: web.Request) -> web.Response:
    formatted_logs = []
    for entry in bot_state.logs[-80:]:
        if isinstance(entry, dict):
            t = html.escape(str(entry.get("time", "")))
            msg = html.escape(redact(entry.get("message", "")))     # escaped: the page renders this as HTML
            level = entry.get("level", "info")
            color = "#34d399" if level == "success" else ("#f87171" if level in ("warning", "error") else "#38bdf8")
            formatted_logs.append(f'<span style="color:{color}">[{t}] {msg}</span>')
        else:
            formatted_logs.append(html.escape(redact(entry)))
    return web.json_response({"logs": formatted_logs}, status=200)



# ==================== DASHBOARD SECURITY ====================
# * Binds to 127.0.0.1 unless DASHBOARD_HOST says otherwise.
# * Every route except /login and /healthz needs a logged-in session.
# * Sessions: random 256-bit id in an HttpOnly + SameSite=Strict (+Secure on HTTPS) cookie, 12h lifetime.
# * CSRF: every state-changing request needs the per-session X-CSRF-Token header AND a same-origin Origin.
# * Login is rate limited (5 failures / 5 min / client) and compares passwords in constant time.
# * The password comes from DASHBOARD_PASSWORD (never hard-coded). If it is unset and the dashboard is
#   loopback-only, a random one is generated and printed ONCE to the console.
SESSION_COOKIE = "gg_session"
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300
MIN_REMOTE_PASSWORD_LEN = 12
_TRUE = {"1", "true", "yes", "on"}

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashboard login</title>
<style nonce="{nonce}">body{{background:#0a0a12;color:#fff;font-family:-apple-system,system-ui,sans-serif;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}}form{{background:#111827;padding:28px;border-radius:14px;
width:min(92vw,340px)}}h1{{font-size:20px;margin:0 0 16px}}input,button{{width:100%;box-sizing:border-box;padding:14px;font-size:16px;
border-radius:10px;border:1px solid #334155;margin-top:10px}}input{{background:#0f172a;color:#fff}}button{{background:#38bdf8;color:#04111c;
border:0;font-weight:700}}.e{{color:#f87171;margin-top:10px;font-size:14px}}</style></head><body>
<form method="post" action="/login"><h1>GUILD GLORY Dashboard</h1>
<input type="password" name="password" placeholder="Dashboard password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button>{error}</form></body></html>"""


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _trust_proxy() -> bool:
    return os.getenv("DASHBOARD_TRUST_PROXY", "").strip().lower() in _TRUE


def _client_ip(request: web.Request) -> str:
    if _trust_proxy():
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return request.remote or "?"


def _is_https(request: web.Request) -> bool:
    if os.getenv("DASHBOARD_COOKIE_SECURE", "").strip().lower() in _TRUE:
        return True
    if request.secure:
        return True
    return _trust_proxy() and request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _origin_ok(request: web.Request) -> bool:
    """Reject cross-site form/XHR posts. A missing Origin is only tolerated for non-browser clients
    that also present a valid CSRF header (checked separately)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    host = request.headers.get("X-Forwarded-Host") if _trust_proxy() else None
    host = host or request.host
    m = re.match(r"^https?://([^/]+)$", origin)
    return bool(m) and m.group(1).lower() == (host or "").lower()


def _get_session(request: web.Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    sessions = request.app["sessions"]
    sess = sessions.get(sid)
    if not sess:
        return None
    if sess["exp"] < time.time():
        sessions.pop(sid, None)
        return None
    sess["sid"] = sid
    return sess


def _json_error(msg, status):
    return web.json_response({"error": msg}, status=status)


@web.middleware
async def security_headers_mw(request: web.Request, handler):
    request["nonce"] = secrets.token_urlsafe(16)
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        resp = ex
    n = request["nonce"]
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{n}'; script-src-attr 'unsafe-inline'; "
        f"style-src 'nonce-{n}'; style-src-attr 'unsafe-inline'; "
        "img-src 'self' data: https://raw.githubusercontent.com https://via.placeholder.com; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if _is_https(request):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


@web.middleware
async def auth_mw(request: web.Request, handler):
    path = request.path
    if path in ("/login", "/healthz"):
        return await handler(request)
    sess = _get_session(request)
    request["session"] = sess
    if sess is None:
        if path == "/api/guildinfo":          # may still authenticate with GUILDINFO_API_KEY inside the handler
            return await handler(request)
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return _json_error("Authentication required", 401)
        raise web.HTTPFound("/login")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _origin_ok(request):
            return _json_error("Cross-site request blocked", 403)
        supplied = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(_digest(supplied), _digest(sess["csrf"])):
            return _json_error("CSRF check failed", 403)
    return await handler(request)


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})          # no data; lets hosting platforms probe the app


async def handle_login_page(request: web.Request) -> web.Response:
    if _get_session(request):
        raise web.HTTPFound("/")
    return web.Response(text=LOGIN_HTML.format(nonce=request["nonce"], error=""), content_type="text/html")


async def handle_login_post(request: web.Request) -> web.Response:
    app = request.app
    ip = _client_ip(request)
    now = time.time()
    fails = [t for t in app["login_fails"].get(ip, []) if now - t < LOGIN_WINDOW]
    app["login_fails"][ip] = fails
    if len(fails) >= LOGIN_MAX_FAILS:
        return web.Response(status=429, text=LOGIN_HTML.format(nonce=request["nonce"],
                            error='<div class="e">Too many attempts. Try again in a few minutes.</div>'), content_type="text/html")
    if not _origin_ok(request):
        return _json_error("Cross-site request blocked", 403)
    try:
        form = await request.post()
        supplied = str(form.get("password", ""))[:256]
    except Exception:
        supplied = ""
    if not hmac.compare_digest(_digest(supplied), app["pw_digest"]):
        fails.append(now)
        await asyncio.sleep(0.5)                    # slow down guessing
        return web.Response(status=401, text=LOGIN_HTML.format(nonce=request["nonce"],
                            error='<div class="e">Incorrect password.</div>'), content_type="text/html")
    app["login_fails"].pop(ip, None)
    sessions = app["sessions"]
    for k in [k for k, v in sessions.items() if v["exp"] < now]:       # purge expired
        sessions.pop(k, None)
    sid = secrets.token_urlsafe(32)                  # fresh id on every login (no fixation)
    sessions[sid] = {"csrf": secrets.token_urlsafe(32), "exp": now + SESSION_TTL}
    resp = web.HTTPFound("/")
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="Strict", secure=_is_https(request),
                    max_age=SESSION_TTL, path="/")
    bot_state.log("Dashboard login succeeded.", "info")
    raise resp


async def handle_logout(request: web.Request) -> web.Response:
    sess = request.get("session")
    if sess:
        request.app["sessions"].pop(sess.get("sid"), None)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def handle_index(request: web.Request) -> web.Response:
    content = FALLBACK_INDEX_HTML
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    n = request["nonce"]
    csrf = html.escape(request["session"]["csrf"], quote=True)
    content = content.replace("<script>", f'<script nonce="{n}">').replace("<style>", f'<style nonce="{n}">')
    content = content.replace("</head>", f'<meta name="csrf-token" content="{csrf}"></head>', 1)
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_config(request: web.Request) -> web.Response:
    return web.json_response({
        "default_clan_id": os.getenv("DASHBOARD_DEFAULT_CLAN_ID", "3055517625"),
        "default_region": os.getenv("DASHBOARD_DEFAULT_REGION", "IND"),
        # The page used to fire clan-join requests from ALL accounts on every load. Now opt-in.
        "auto_join": os.getenv("DASHBOARD_AUTO_JOIN", "").strip().lower() in _TRUE,
    })


def create_app(password: str) -> web.Application:
    if not password:
        raise ValueError("A dashboard password is required.")
    app = web.Application(middlewares=[security_headers_mw, auth_mw], client_max_size=64 * 1024)
    app["pw_digest"] = _digest(password)
    app["sessions"] = {}
    app["login_fails"] = {}
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/login", handle_login_post)
    app.router.add_post("/logout", handle_logout)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/config", handle_config)
    app.router.add_post("/api/clanjoin", handle_api_clan_join)
    app.router.add_get("/api/guildinfo", handle_api_guild_info)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_get("/api/logs", handle_api_logs)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/pause", handle_toggle_pause)
    app.router.add_post("/api/account/pause_all", handle_toggle_pause_all)
    app.router.add_post("/api/logs/clear", handle_clear_logs)
    return app


class _DashboardRunner(web.AppRunner):
    """AppRunner that remembers where it is listening (plain AppRunner uses __slots__)."""
    gg_host = "127.0.0.1"
    gg_port = 0


def resolve_dashboard_config(host=None, port=None):
    """Return (host, port, password, generated). Fails closed: no password => loopback only."""
    host = (os.getenv("DASHBOARD_HOST") or host or "127.0.0.1").strip()
    try:
        port = int(os.getenv("DASHBOARD_PORT") or os.getenv("PORT") or (20333 if port is None else port))   # PORT: set by most hosts
    except ValueError:
        port = 20333
    password = os.getenv("DASHBOARD_PASSWORD", "")
    generated = False
    if not _is_loopback(host):
        if len(password) < MIN_REMOTE_PASSWORD_LEN:
            print(f"[DASHBOARD] DASHBOARD_HOST={host} needs DASHBOARD_PASSWORD with at least "
                  f"{MIN_REMOTE_PASSWORD_LEN} characters. Falling back to 127.0.0.1 (local only).")
            host = "127.0.0.1"
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    return host, port, password, generated


async def start_web_dashboard(host: str = "127.0.0.1", port: int = 20333):
    host, port, password, generated = resolve_dashboard_config(host, port)
    app = create_app(password)
    runner = _DashboardRunner(app, access_log=None)    # no access log: URLs/queries can carry keys
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[DASHBOARD] Listening on {host}:{port} (login required).")
    if generated:
        print(f"[DASHBOARD] No DASHBOARD_PASSWORD set. One-time password for this run: {password}")
    runner.gg_host, runner.gg_port = host, port
    return runner
