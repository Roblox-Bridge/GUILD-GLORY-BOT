"""TXT import, API failure handling, TLS verification, redaction, Discord registration. No network, fake data only."""
import io, os, re, ssl, glob, types, asyncio, json, stat, time
import pytest, httpx, requests, discord
import discord_control as dc
import dashboard_server as ds
import security_utils as su

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ───────────── TXT parsing ─────────────
def test_txt_empty_and_whitespace_files():
    assert dc.parse_accounts("") == ([], 0)
    assert dc.parse_accounts("\n\n   \n# only a comment\n") == ([], 0)

def test_txt_malformed_lines_counted_invalid():
    txt = "no-colon-here\n:pw\n123:\nabc:pw\n12 34:pw\n123:pw\twith\x00nul\n999:ok"
    accts, inv = dc.parse_accounts(txt)
    assert accts == [("999", "ok")] and inv == 6

def test_txt_duplicates_comments_and_colons_in_password():
    accts, inv = dc.parse_accounts("# header\n1:a\n1:different\n2:p:a:s:s\n  3 : spaced pw \r\n")
    assert accts == [("1", "a"), ("2", "p:a:s:s"), ("3", "spaced pw")] and inv == 0

def test_txt_limits_on_uid_and_password_length():
    assert dc.parse_accounts(f"{'1'*21}:pw")[0] == []                 # uid > 20 digits
    assert dc.parse_accounts(f"{'1'*20}:pw")[0] != []
    assert dc.parse_accounts("1:" + "p" * 129)[0] == []
    assert dc.parse_accounts("1:" + "p" * 128)[0] != []

def test_txt_bom_and_crlf_and_unicode():
    raw = ("\ufeff1:pw\r\n2:пароль\r\n").encode("utf-8")
    accts, inv = dc.parse_accounts(raw.decode("utf-8-sig"))
    assert accts == [("1", "pw"), ("2", "пароль")] and inv == 0

def test_txt_large_file_is_fast_and_bounded():
    big = "\n".join(f"{n}:password{n}" for n in range(1, 60000))          # ~1 MB, 60k lines
    assert len(big.encode()) <= dc.MAX_TXT_BYTES * 1.2
    t = time.time(); accts, inv = dc.parse_accounts(big)
    assert time.time() - t < 2 and len(accts) == dc.MAX_JOIN_ACCOUNTS + 1       # stops scanning once over the limit
    junk = "garbage line\n" * 100000
    assert dc.parse_accounts(junk) == ([], 100000)

class Resp:
    def __init__(s): s.sent = []; s.done = False
    def is_done(s): return s.done
    async def send_message(s, *a, **k):
        s.done = True
        if a: k["content"] = a[0]
        s.sent.append(k)
    async def defer(s, **k): s.done = True
    async def send_modal(s, m): s.done = True
class FU:
    def __init__(s): s.sent = []
    async def send(s, *a, **k): s.sent.append(k)
def inter(uid):
    return types.SimpleNamespace(user=types.SimpleNamespace(id=uid), response=Resp(), followup=FU(), data={}, command=None)

class FakeAtt:
    def __init__(s, name, data, size=None): s.filename = name; s._d = data; s.size = len(data) if size is None else size
    async def read(s): return s._d

async def autojoin(i, att, gid="55"):
    return await dc.cmd_autojoin.callback(i, att, gid)

@pytest.mark.asyncio
async def test_upload_wrong_type_size_encoding_and_missing_guild(monkeypatch):
    ran = []
    async def fake_run(i, accounts, invalid, gid): ran.append((accounts, invalid, gid))
    monkeypatch.setattr(dc, "run_join", fake_run)
    i = inter(111); await autojoin(i, FakeAtt("a.csv", b"1:a")); assert "Wrong file" in i.response.sent[0]["embed"].title
    i = inter(111); await autojoin(i, FakeAtt("a.txt", b"1:a", size=2_000_000)); assert "too large" in i.response.sent[0]["embed"].title
    i = inter(111); await autojoin(i, FakeAtt("a.txt", b"x" * 2_000_000, size=10)); assert "too large" in i.response.sent[0]["embed"].title  # lying size
    i = inter(111); await autojoin(i, FakeAtt("a.txt", b"\xff\xfe\xfa\x80bad")); assert "Could not read" in i.response.sent[0]["embed"].title
    i = inter(111); await autojoin(i, FakeAtt("a.txt", b"1:a"), gid=""); assert "Guild ID missing" in i.response.sent[0]["embed"].title
    i = inter(111); await autojoin(i, FakeAtt("a.txt", b""), gid="5"); assert "No valid" in i.response.sent[0]["embed"].title      # empty file
    i = inter(111); await autojoin(i, FakeAtt("a.txt", b"junk\nmore junk"), gid="5"); assert "No valid" in i.response.sent[0]["embed"].title
    assert ran == []
    i = inter(111); await autojoin(i, FakeAtt("A.TXT", b"\xef\xbb\xbf1:a\n2:b\nbad"), gid="5")
    assert ran == [([("1", "a"), ("2", "b")], 1, 5)]

@pytest.mark.asyncio
async def test_progress_and_final_report_never_contain_passwords(monkeypatch):
    monkeypatch.setattr(dc, "_join_generate_jwt", lambda u, p: "JWT_VALUE_XYZ")
    monkeypatch.setattr(dc, "_join_send", lambda j, g: types.SimpleNamespace(status_code=200))
    async def nosleep(*a): pass
    monkeypatch.setattr(dc.asyncio, "sleep", nosleep)
    edits = []; i = inter(111)
    async def edit(**k): edits.append(k)
    i.edit_original_response = edit
    await dc.run_join(i, [("1", "TOPSECRETPW1"), ("2", "TOPSECRETPW2"), ("3", "TOPSECRETPW3")], 0, 9)
    blob = ""
    for k in edits:
        if k.get("embed"): blob += json.dumps(k["embed"].to_dict())
        for a in k.get("attachments", []) or []: blob += a.fp.read().decode()
    assert "TOPSECRETPW" not in blob and "JWT_VALUE_XYZ" not in blob and "HTTP_200" in blob
    assert all("TOPSECRETPW" not in str(l) and "JWT_VALUE" not in str(l) for l in ds.bot_state.logs)

@pytest.mark.asyncio
async def test_report_dm_fallback_and_undeliverable(monkeypatch):
    monkeypatch.setattr(dc, "_join_generate_jwt", lambda u, p: None)
    async def nosleep(*a): pass
    monkeypatch.setattr(dc.asyncio, "sleep", nosleep)
    class Gone(discord.HTTPException):
        def __init__(s): s.status = 404; s.code = 50027; s.text = "Invalid Webhook Token"; s.response = None
    i = inter(111); dms = []
    async def edit(**k): raise Gone()
    async def dm(**k): dms.append(k)
    i.edit_original_response = edit; i.user.send = dm
    await dc.run_join(i, [("1", "pw")], 0, 9)
    assert len(dms) == 1 and "JWT_FAILED" in dms[0]["file"].fp.read().decode()           # delivered by DM
    async def dm_closed(**k): raise Gone()
    i = inter(111); i.edit_original_response = edit; i.user.send = dm_closed
    await dc.run_join(i, [("1", "pw")], 0, 9)                                             # must not raise
    assert any("could not be delivered" in l["message"] for l in ds.bot_state.logs)

# ───────────── API failures ─────────────
class R:
    def __init__(s, code=200, j=None): s.status_code = code; s._j = j
    def raise_for_status(s):
        if s.status_code >= 400: raise requests.HTTPError("boom https://x?password=SECRETPW")
    def json(s):
        if s._j is None: raise ValueError("no json")
        return s._j

def test_jwt_api_failure_modes(monkeypatch):
    monkeypatch.setattr(dc.time, "sleep", lambda s: None)
    seq = iter([R(503), R(200, {"token": "T"})]); calls = []
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: (calls.append(k), next(seq))[1])
    assert dc._join_generate_jwt("1", "p") == "T" and len(calls) == 2 and calls[0]["timeout"] == 25   # retried once on 5xx
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: R(404)); assert dc._join_generate_jwt("1", "p") is None   # 4xx: no retry
    n = []
    def timeout(*a, **k): n.append(1); raise requests.Timeout("t")
    monkeypatch.setattr(dc.requests, "get", timeout); assert dc._join_generate_jwt("1", "p") is None and len(n) == 2
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: R(200, None)); assert dc._join_generate_jwt("1", "p") is None  # bad JSON
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: R(200, [1])); assert dc._join_generate_jwt("1", "p") is None

def test_join_send_timeout_returns_none(monkeypatch):
    def boom(*a, **k): raise requests.Timeout("x")
    monkeypatch.setattr(dc.requests, "post", boom); assert dc._join_send("J", 5) is None

@pytest.mark.asyncio
async def test_request_clan_async_with_mock_transport(monkeypatch):
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, text="ok")
    real = httpx.AsyncClient
    monkeypatch.setattr(ds.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    code, _ = await ds.request_clan_async("JWT", "123", "IND")
    assert code == 200 and seen["url"].endswith("/RequestJoinClan") and seen["auth"] == "Bearer JWT"
    def failing(req): raise httpx.ConnectError("down")
    monkeypatch.setattr(ds.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(failing), **kw))
    with pytest.raises(httpx.ConnectError): await ds.request_clan_async("JWT", "123", "IND")

class Resp2:
    def __init__(s): s.sent = []; s.done = False
@pytest.mark.asyncio
async def test_clanjoin_counts_failures_when_api_down(monkeypatch):
    json.dump({"1": {"token": "t1"}, "2": {"token": "t2"}, "3": {}}, open("token_cache.json", "w"))
    async def flaky(tok, cid, reg):
        if tok == "t1": raise httpx.ConnectError("down")
        return 500, ""
    monkeypatch.setattr(ds, "request_clan_async", flaky)
    i = inter(111); await dc.act_clanjoin(i, "77")
    assert "0/2" in i.followup.sent[0]["embed"].description

def test_fetch_single_clan_api_down_and_tls(monkeypatch):
    calls = []
    def post(url, **kw): calls.append(kw); raise requests.ConnectionError("down")
    monkeypatch.setattr(ds.requests, "post", post)
    out = ds.fetch_single_clan("123", "IND", {"1": {"token": "t"}})
    assert out["error"] == "All tokens failed or expired"
    assert calls and "verify" not in calls[0] and calls[0]["timeout"] > 0
    assert ds.fetch_single_clan("123", "IND", {}) ["error"]

@pytest.mark.asyncio
async def test_guild_command_api_failure_is_graceful(monkeypatch):
    json.dump({"1": {"token": "t"}}, open("token_cache.json", "w"))
    monkeypatch.setattr(ds, "fetch_single_clan", lambda *a: {"error": "All tokens failed or expired"})
    i = inter(111); await dc.act_guild(i, "5", "IND"); assert "Guild lookup failed" in i.followup.sent[0]["embed"].title
    def raises(*a): raise requests.ConnectionError("https://h?token=LEAKME123456")
    monkeypatch.setattr(ds, "fetch_single_clan", raises)
    i = inter(111); await dc.act_guild(i, "5", "IND"); assert "LEAKME" not in i.followup.sent[0]["embed"].description

# ───────────── TLS verification restored ─────────────
def test_no_tls_verification_disabled_anywhere():
    bad = []
    for f in glob.glob(os.path.join(ROOT, "*.py")):
        src = open(f, encoding="utf-8").read()
        for pat in (r"verify\s*=\s*False", r"disable_warnings", r"CERT_NONE", r"check_hostname\s*=\s*False", r"_create_unverified_context"):
            if re.search(pat, src): bad.append((os.path.basename(f), pat))
    assert bad == []

def test_app_http_client_verifies_certificates():
    import app
    ctx = app.client._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert app.client.timeout.connect and app.client.timeout.read          # explicit timeouts

@pytest.mark.asyncio
async def test_version_config_retries_then_gives_up(monkeypatch):
    import app
    async def ver(): return "1.0"
    monkeypatch.setattr(app, "get_playstore_version", ver)
    async def nosleep(*a): pass
    monkeypatch.setattr(app.asyncio, "sleep", nosleep)
    n = []
    class C:
        async def get(s, url):
            n.append(1)
            if len(n) < 3: raise httpx.ConnectTimeout("t")
            return httpx.Response(200, json={"server_url": "https://s/", "remote_version": "r", "latest_release_version": "OB99"},
                                  request=httpx.Request("GET", url))
    monkeypatch.setattr(app, "client", C()); assert await app.version_config() == ("OB99", "r", "https://s/") and len(n) == 3
    n.clear()
    class D:
        async def get(s, url): n.append(1); raise httpx.ConnectError("down")
    monkeypatch.setattr(app, "client", D()); assert await app.version_config() is None and len(n) == 3

# ───────────── redaction / files ─────────────
def test_redaction_and_bot_log():
    cases = {"GET https://h/inspect?token=ABCDEF123456&x=1": "ABCDEF123456", "Authorization: Bearer abc.DEF-ghi_123": "abc.DEF",
             "password=hunter2": "hunter2", "client_secret: 'zzz999'": "zzz999",
             "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef123456": "eyJhbGci", "A" * 60: "A" * 60}
    for text, secret in cases.items():
        assert secret not in su.redact(text), text
    ds.bot_state.log("login token=SECRETTOKEN123 ok"); assert "SECRETTOKEN123" not in ds.bot_state.logs[-1]["message"]
    assert "uid 8167969689 level 5" == su.redact("uid 8167969689 level 5")
    assert "SECRETPW" not in su.safe_error(requests.HTTPError("https://x?password=SECRETPW"))

def test_atomic_private_write_and_validators(tmp_path):
    p = str(tmp_path / "a.json"); su.atomic_write_json(p, {"a": 1})
    assert json.load(open(p)) == {"a": 1} and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".tmp_")]
    assert su.valid_uid("123") and not su.valid_uid("12a") and not su.valid_uid("")
    assert su.valid_ident("a-b_c.1") and not su.valid_ident("a b") and not su.valid_ident("../x")
    assert su.valid_token("abcdefgh.ijkl-mnop") and not su.valid_token("short") and not su.valid_token("has space token")
    assert su.valid_clan_id("123") and not su.valid_clan_id("1" * 21) and not su.valid_clan_id("1.5")

def test_shipped_data_files_hold_no_secrets():
    for name, empty in (("accounts.json", []), ("token_cache.json", {}), ("devices.json", {}), ("guild_exp_cache.json", {})):
        assert json.load(open(os.path.join(ROOT, name))) == empty, name
    for f in glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "Web_temp", "*")):
        assert "XEROX_MODS" not in open(f, encoding="utf-8").read().replace('"DEVLOVER": "XEROX_MODS"', ""), f
    assert not os.path.exists(os.path.join(ROOT, ".env"))

# ───────────── Discord registration / startup / shutdown ─────────────
def test_command_payloads_are_valid_for_discord():
    names = []
    for c in dc.tree.get_commands():
        d = c.to_dict(dc.tree)
        assert re.fullmatch(r"[\w-]{1,32}", d["name"]) and d["name"] == d["name"].lower()
        assert 1 <= len(d["description"]) <= 100, d["name"]
        for o in d.get("options", []): assert 1 <= len(o["description"]) <= 100 and re.fullmatch(r"[\w-]{1,32}", o["name"])
        names.append(d["name"])
    assert len(names) == len(set(names))                                  # no duplicate registrations
    assert len(names) <= 100

@pytest.mark.asyncio
async def test_setup_hook_syncs_global_or_guild_and_survives_failure(monkeypatch, capsys):
    calls = []
    async def fake_sync(self, *, guild=None): calls.append(guild); return [1] * 16
    monkeypatch.setattr(dc.GGTree, "sync", fake_sync)
    c = dc.GGClient()
    monkeypatch.setattr(dc, "DEV_GUILD_ID", ""); await c.setup_hook(); assert calls == [None]
    monkeypatch.setattr(dc, "DEV_GUILD_ID", "123456789012345678"); await c.setup_hook()
    assert calls[1].id == 123456789012345678
    async def forbidden(self, *, guild=None):
        raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "Missing Access")
    monkeypatch.setattr(dc.GGTree, "sync", forbidden); await c.setup_hook()
    assert "applications.commands" in capsys.readouterr().out
    await c.close()

@pytest.mark.asyncio
async def test_start_bot_shutdown_closes_client(monkeypatch):
    monkeypatch.setattr(dc, "BOT_TOKEN", "t.o.k"); monkeypatch.setattr(dc, "ADMIN_IDS", {"1"})
    closed = []
    async def fake_start(tok): await asyncio.sleep(3600)
    async def fake_close(): closed.append(1)
    monkeypatch.setattr(dc.client, "start", fake_start); monkeypatch.setattr(dc.client, "close", fake_close)
    monkeypatch.setattr(dc.client, "is_closed", lambda: False)
    t = asyncio.create_task(dc.start_discord_bot()); await asyncio.sleep(0.05); t.cancel()
    with pytest.raises(asyncio.CancelledError): await t
    assert closed == [1]

@pytest.mark.asyncio
async def test_missing_admin_ids_refuses_to_start(monkeypatch, capsys):
    monkeypatch.setattr(dc, "BOT_TOKEN", "t.o.k"); monkeypatch.setattr(dc, "ADMIN_IDS", set())
    started = []
    async def fake_start(tok): started.append(1)
    monkeypatch.setattr(dc.client, "start", fake_start)
    await dc.start_discord_bot(); assert started == [] and "DISCORD_ADMIN_IDS" in capsys.readouterr().out

@pytest.mark.asyncio
async def test_add_command_validation_messages_via_discord(monkeypatch):
    i = inter(111); await dc.act_add(i, "notnumeric", "pw"); assert "numeric" in i.response.sent[0]["embed"].description
    i = inter(111); await dc.act_token(i, "x y"); assert "invalid format" in i.response.sent[0]["embed"].description
    i = inter(111); await dc.act_add(i, "123", "pw"); assert "added" in i.response.sent[0]["embed"].title.lower()
    assert json.load(open("accounts.json")) == [{"uid": "123", "password": "pw"}]
