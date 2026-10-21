import json, time, pytest
from aiohttp.test_utils import TestClient, TestServer
import dashboard_server as ds

PW = "correct-horse-battery"

async def make_client():
    c = TestClient(TestServer(ds.create_app(PW)))
    await c.start_server()
    return c

async def login(c, pw=PW):
    return await c.post("/login", data={"password": pw}, allow_redirects=False)

async def authed():
    c = await make_client()
    r = await login(c); assert r.status == 302
    page = await c.get("/")
    html = await page.text()
    csrf = html.split('name="csrf-token" content="')[1].split('"')[0]
    return c, csrf, html

@pytest.mark.asyncio
async def test_everything_requires_login():
    c = await make_client()
    try:
        r = await c.get("/", allow_redirects=False); assert r.status == 302 and r.headers["Location"] == "/login"
        for path in ("/api/stats", "/api/logs", "/api/config", "/api/guildinfo?guild_id=1"):
            r = await c.get(path); assert r.status == 401, path
        for path in ("/api/account/add", "/api/account/delete", "/api/account/restart", "/api/account/refresh",
                     "/api/account/pause", "/api/account/pause_all", "/api/logs/clear", "/api/clanjoin", "/logout"):
            r = await c.post(path, json={"uid": "1"}); assert r.status == 401, path
        assert (await c.get("/healthz")).status == 200 and (await (await c.get("/healthz")).json()) == {"ok": True}
        assert (await c.get("/login")).status == 200
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_login_cookie_flags_and_headers():
    c = await make_client()
    try:
        r = await login(c)
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "gg_session=" in cookie
        page = await c.get("/")
        h = page.headers
        assert "frame-ancestors 'none'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "DENY" and h["Cache-Control"] == "no-store" and h["Referrer-Policy"] == "no-referrer"
        body = await page.text()
        assert "XEROX_MODS" not in body                       # hard-coded key gone from the page
        nonce = h["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        assert f'<script nonce="{nonce}">' in body
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_secure_flag_behind_https_proxy(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "1")
    c = await make_client()
    try:
        r = await c.post("/login", data={"password": PW}, headers={"X-Forwarded-Proto": "https"}, allow_redirects=False)
        assert "Secure" in r.headers["Set-Cookie"] and "Strict-Transport-Security" in r.headers
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_wrong_password_and_rate_limit(monkeypatch):
    async def fast(_): return None
    monkeypatch.setattr(ds.asyncio, "sleep", fast)
    c = await make_client()
    try:
        for _ in range(ds.LOGIN_MAX_FAILS):
            r = await login(c, "nope"); assert r.status == 401
        assert "gg_session" not in r.headers.get("Set-Cookie", "")
        r = await login(c, PW); assert r.status == 429                  # locked out even with the right password
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_csrf_and_origin_enforced():
    c, csrf, _ = await authed()
    try:
        r = await c.post("/api/logs/clear", json={}); assert r.status == 403           # no CSRF header
        r = await c.post("/api/logs/clear", json={}, headers={"X-CSRF-Token": "x" * 43}); assert r.status == 403
        r = await c.post("/api/logs/clear", json={}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
        assert r.status == 403
        r = await c.post("/api/logs/clear", json={}, headers={"X-CSRF-Token": csrf}); assert r.status == 200
        r = await c.post("/api/logs/clear", json={}, headers={"X-CSRF-Token": csrf, "Origin": f"http://{c.server.host}:{c.server.port}"})
        assert r.status == 200
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_logout_invalidates_session_and_expiry():
    c, csrf, _ = await authed()
    try:
        assert (await c.get("/api/stats")).status == 200
        sid = next(iter(c.server.app["sessions"]))
        c.server.app["sessions"][sid]["exp"] = time.time() - 1
        assert (await c.get("/api/stats")).status == 401                 # expired
        await login(c); csrf2 = (await (await c.get("/")).text()).split('name="csrf-token" content="')[1].split('"')[0]
        r = await c.post("/logout", headers={"X-CSRF-Token": csrf2}); assert r.status == 200
        assert (await c.get("/api/stats")).status == 401
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_stats_never_contain_secrets():
    ds.bot_state.accounts["1"] = {"uid": "1", "nickname": "n", "token": "SECRET_JWT_VALUE", "auth_password": "pw",
                                  "access_token": "AT", "gained_exp": 5}
    c, _, _ = await authed()
    try:
        r = await c.get("/api/stats"); txt = await r.text()
        assert r.status == 200 and "SECRET_JWT_VALUE" not in txt and "auth_password" not in txt and '"token"' not in txt
        assert json.loads(txt)["accounts"][0]["nickname"] == "n"
        assert "token" in ds.bot_state.accounts["1"]                      # live state not mutated
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_logs_are_escaped_and_redacted():
    ds.bot_state.log("<script>alert(1)</script> Authorization: Bearer abc.def.ghi token=SUPERSECRET", "error")
    c, _, _ = await authed()
    try:
        txt = await (await c.get("/api/logs")).text()
        assert "<script>" not in txt and "&lt;script&gt;" in txt
        assert "abc.def.ghi" not in txt and "SUPERSECRET" not in txt
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_input_validation_and_generic_errors():
    c, csrf, _ = await authed(); H = {"X-CSRF-Token": csrf}
    try:
        for bad in ({"uid": "abc", "password": "x"}, {"uid": "123", "password": ""}, {"uid": "1" * 30, "password": "x"},
                    {"token": "short"}, {"token": "bad token with spaces!!"}, {}):
            r = await c.post("/api/account/add", json=bad, headers=H); assert r.status == 400, bad
        r = await c.post("/api/account/add", data="not json", headers=H); assert r.status == 400
        r = await c.post("/api/account/add", json=[1, 2], headers=H); assert r.status == 400
        r = await c.post("/api/account/delete", json={"uid": "../../etc/passwd"}, headers=H); assert r.status == 400
        r = await c.post("/api/account/pause", json={"uid": "a b"}, headers=H); assert r.status == 400
        r = await c.post("/api/clanjoin", json={"clan_id": "12x"}, headers=H); assert r.status == 400
        r = await c.post("/api/account/add", data="x" * 70000, headers=H); assert r.status == 413   # body size cap
        r = await c.post("/api/account/add", json={"uid": "123", "password": "pw"}, headers=H); assert r.status == 200
        assert json.load(open("accounts.json")) == [{"uid": "123", "password": "pw"}]
        import os, stat
        assert stat.S_IMODE(os.stat("accounts.json").st_mode) == 0o600           # credentials file is private
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_guildinfo_api_key_from_env_only(monkeypatch):
    c = await make_client()
    try:
        assert (await c.get("/api/guildinfo?guild_id=1&key=XEROX_MODS")).status == 401      # old hard-coded key is dead
        monkeypatch.setenv("GUILDINFO_API_KEY", "k" * 20)
        assert (await c.get("/api/guildinfo?guild_id=1", headers={"X-API-Key": "wrong" * 5})).status == 401
        assert (await c.get("/api/guildinfo?guild_id=abc", headers={"X-API-Key": "k" * 20})).status == 400  # auth ok, input bad
        assert (await c.get("/api/guildinfo?guild_id=1", headers={"X-API-Key": "k" * 20})).status == 503    # auth ok, no tokens
        monkeypatch.setenv("GUILDINFO_API_KEY", "short")
        assert (await c.get("/api/guildinfo?guild_id=1", headers={"X-API-Key": "short"})).status == 401     # too-weak key refused
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_restart_endpoint_callbacks():
    c, csrf, _ = await authed(); H = {"X-CSRF-Token": csrf}
    try:
        r = await c.post("/api/account/restart", json={"uid": "5"}, headers=H)
        assert r.status == 503 and "not available" in (await r.json())["error"]          # missing callback handled
        async def good(uid): return {"ok": True, "message": f"restarted {uid}"}
        ds.bot_state.refresh_callbacks["on_restart_account"] = good
        r = await c.post("/api/account/restart", json={"uid": "5"}, headers=H)
        assert r.status == 200 and (await r.json())["message"] == "restarted 5"
        async def bad(uid): return {"ok": False, "message": "nope"}
        ds.bot_state.refresh_callbacks["on_restart_account"] = bad
        r = await c.post("/api/account/restart", json={"uid": "5"}, headers=H); assert r.status == 409
        async def boom(uid): raise RuntimeError("token=SECRET123 exploded")
        ds.bot_state.refresh_callbacks["on_restart_account"] = boom
        r = await c.post("/api/account/restart", json={"uid": "5"}, headers=H)
        assert "SECRET123" not in await r.text() and r.status == 409
        r = await c.post("/api/account/restart", json={}, headers=H); assert r.status == 400
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_config_autojoin_is_opt_in(monkeypatch):
    c, _, html = await authed()
    try:
        assert (await (await c.get("/api/config")).json())["auto_join"] is False
        monkeypatch.setenv("DASHBOARD_AUTO_JOIN", "true")
        assert (await (await c.get("/api/config")).json())["auto_join"] is True
    finally:
        await c.close()

def test_bind_address_defaults_and_fail_closed(monkeypatch, capsys):
    host, port, pw, gen = ds.resolve_dashboard_config()
    assert host == "127.0.0.1" and port == 20333 and gen and len(pw) >= 12          # loopback + generated password
    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    host, _, _, _ = ds.resolve_dashboard_config(); assert host == "127.0.0.1"          # remote bind refused w/o password
    assert "needs DASHBOARD_PASSWORD" in capsys.readouterr().out
    monkeypatch.setenv("DASHBOARD_PASSWORD", "short"); assert ds.resolve_dashboard_config()[0] == "127.0.0.1"
    monkeypatch.setenv("DASHBOARD_PASSWORD", "a-long-enough-password")
    host, _, pw, gen = ds.resolve_dashboard_config(); assert host == "0.0.0.0" and pw == "a-long-enough-password" and not gen
    with pytest.raises(ValueError): ds.create_app("")

@pytest.mark.asyncio
async def test_real_server_binds_loopback_only():
    runner = await ds.start_web_dashboard(port=0)
    try:
        socks = runner.addresses
        assert all(a[0] == "127.0.0.1" for a in socks), socks
    finally:
        await runner.cleanup()

def test_platform_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "8080"); assert ds.resolve_dashboard_config()[1] == 8080
    monkeypatch.setenv("DASHBOARD_PORT", "9000"); assert ds.resolve_dashboard_config()[1] == 9000
