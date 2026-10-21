"""Restart lifecycle: real task stop/start logic of app.restart_account_worker, exercised with fake workers
(no network, no game server, no credentials)."""
import asyncio, types, pytest
import app
import dashboard_server as ds
import discord_control as dc

class Recorder:
    def __init__(self): self.started = []; self.invalidated = []

@pytest.fixture
def env(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(app, "load_accounts", lambda: [{"uid": "1001", "password": "pw1"},
                                                         {"uid": "1002", "password": "pw2"},
                                                         {"token": "TOKENTOKEN12345678901234567890"}])
    async def fake_guest(uid, pw):
        rec.started.append(("guest", uid, pw))
        try: await asyncio.sleep(3600)
        except asyncio.CancelledError: return
    async def fake_token(tok):
        rec.started.append(("token", tok))
        try: await asyncio.sleep(3600)
        except asyncio.CancelledError: return
    monkeypatch.setattr(app, "account_loop_guest", fake_guest)
    monkeypatch.setattr(app, "account_loop_token", fake_token)
    monkeypatch.setattr(app, "cache_invalidate", lambda k: rec.invalidated.append(k))
    app._restarting.clear()
    yield rec
    for t in list(ds.bot_state.account_workers.values()): t.cancel()
    ds.bot_state.account_workers.clear()

async def spawn_old(key, fn):
    t = asyncio.create_task(fn()); ds.bot_state.account_workers[key] = t; await asyncio.sleep(0); return t

async def forever():
    try: await asyncio.sleep(3600)
    except asyncio.CancelledError: return

@pytest.mark.asyncio
async def test_restart_replaces_worker_and_drops_cache(env):
    old = await spawn_old("1001", forever)
    r = await app.restart_account_worker("1001")
    assert r["ok"] and "logging in fresh" in r["message"]
    assert old.done()                                                    # old task really stopped
    new = ds.bot_state.account_workers["1001"]; assert new is not old and not new.done()
    assert env.started == [("guest", "1001", "pw1")] and env.invalidated == ["1001"]
    assert len([t for t in ds.bot_state.account_workers.values() if not t.done()]) == 1   # no duplicates

@pytest.mark.asyncio
async def test_restart_when_no_worker_running(env):
    r = await app.restart_account_worker("1002")
    assert r["ok"] and not ds.bot_state.account_workers["1002"].done()

@pytest.mark.asyncio
async def test_restart_resolves_in_game_id_and_token_accounts(env):
    ds.bot_state.game_to_auth_id["777"] = "1002"
    ds.bot_state.auth_to_game_id["1002"] = "777"
    r = await app.restart_account_worker("777")                          # in-game id -> login uid
    assert r["ok"] and ("guest", "1002", "pw2") in env.started
    r = await app.restart_account_worker("TOKENTOKEN")                    # token prefix key
    assert r["ok"] and ("token", "TOKENTOKEN12345678901234567890") in env.started
    assert "tok_TOKENTOKEN1234567890" in env.invalidated

@pytest.mark.asyncio
async def test_unknown_and_empty_account(env):
    assert (await app.restart_account_worker("9999"))["ok"] is False
    assert "not found" in (await app.restart_account_worker("9999"))["message"]
    assert (await app.restart_account_worker("  "))["ok"] is False
    assert env.started == []

@pytest.mark.asyncio
async def test_simultaneous_restarts_are_serialised(env):
    await spawn_old("1001", forever)
    a, b = await asyncio.gather(app.restart_account_worker("1001"), app.restart_account_worker("1001"))
    assert sorted([a["ok"], b["ok"]]) == [False, True]
    assert "already in progress" in (a if not a["ok"] else b)["message"]
    assert len(env.started) == 1                                         # only ONE new worker was started
    assert app._restarting == set()                                      # lock released

@pytest.mark.asyncio
async def test_worker_that_refuses_to_stop_is_not_duplicated(env, monkeypatch):
    monkeypatch.setattr(app, "RESTART_STOP_TIMEOUT", 0.2)
    async def stubborn():
        while True:
            try: await asyncio.sleep(3600)
            except asyncio.CancelledError: await asyncio.sleep(0.6)       # swallows the first cancel for a while
    old = await spawn_old("1001", stubborn)
    r = await app.restart_account_worker("1001")
    assert r["ok"] is False and "did not stop" in r["message"]
    assert env.started == [] and ds.bot_state.account_workers["1001"] is old      # old kept, nothing duplicated
    assert app._restarting == set()
    old.cancel()

@pytest.mark.asyncio
async def test_new_worker_crashing_immediately_is_reported(env, monkeypatch):
    async def dies(uid, pw): return
    monkeypatch.setattr(app, "account_loop_guest", dies)
    r = await app.restart_account_worker("1001")
    assert r["ok"] is False and "exited immediately" in r["message"]
    assert "1001" not in ds.bot_state.account_workers

@pytest.mark.asyncio
async def test_paused_account_stays_paused_note(env):
    ds.bot_state.paused_accounts.add("1001")
    r = await app.restart_account_worker("1001"); assert r["ok"] and "stay paused" in r["message"]

@pytest.mark.asyncio
async def test_callbacks_registered_by_main_are_these_functions():
    src = open(app.__file__, encoding="utf-8").read()
    assert 'refresh_callbacks["on_restart_account"] = restart_account_worker' in src
    assert 'refresh_callbacks["on_refresh_account"] = refresh_account_by_id' in src

@pytest.mark.asyncio
async def test_refresh_reports_real_outcome(monkeypatch):
    assert (await app.refresh_account_by_id("5"))["ok"] is False                      # no session yet
    ds.bot_state.account_credentials["5"] = {"account_id": 5}
    async def good(ad): return True
    async def bad(ad): return False
    monkeypatch.setattr(app, "refresh_account_profile", good); assert (await app.refresh_account_by_id("5"))["ok"]
    monkeypatch.setattr(app, "refresh_account_profile", bad);  assert not (await app.refresh_account_by_id("5"))["ok"]

# ---- Discord handler -> callback wiring ----
class Resp:
    def __init__(s): s.sent = []; s.done = False
    def is_done(s): return s.done
    async def send_message(s, *a, **k):
        s.done = True
        if a: k["content"] = a[0]
        s.sent.append(k)
    async def defer(s, **k): s.done = True
class FU:
    def __init__(s): s.sent = []
    async def send(s, *a, **k): s.sent.append(k)
def inter(uid):
    return types.SimpleNamespace(user=types.SimpleNamespace(id=uid), response=Resp(), followup=FU(), data={}, command=None)

@pytest.mark.asyncio
async def test_discord_restart_missing_callback_is_graceful():
    i = inter(111); await dc.act_restart(i, "1001")
    e = i.followup.sent[0]["embed"]; assert "Restart failed" in e.title and "not available" in e.description

@pytest.mark.asyncio
async def test_discord_restart_reports_true_result(env):
    ds.bot_state.refresh_callbacks["on_restart_account"] = app.restart_account_worker
    i = inter(111); await dc.act_restart(i, "1001")
    e = i.followup.sent[0]["embed"]; assert "Restarted" in e.title and "fresh" in e.description
    i = inter(111); await dc.act_restart(i, "424242")
    assert "Restart failed" in i.followup.sent[0]["embed"].title and "not found" in i.followup.sent[0]["embed"].description
    i = inter(111); await dc.act_restart(i, "bad uid!")
    assert "Invalid" in i.response.sent[0]["embed"].title
    i = inter(222); await dc.act_restart(i, "1001")                      # non-admin
    assert "Admin access only" in i.response.sent[0]["content"]

@pytest.mark.asyncio
async def test_discord_restart_callback_exception_and_timeout_redacted(monkeypatch):
    async def boom(uid): raise RuntimeError("Bearer abc.def.ghi password=hunter2")
    ds.bot_state.refresh_callbacks["on_restart_account"] = boom
    i = inter(111); await dc.act_restart(i, "1"); d = i.followup.sent[0]["embed"].description
    assert "hunter2" not in d and "abc.def" not in d and "failed" in i.followup.sent[0]["embed"].title.lower()
    assert all("hunter2" not in str(l) for l in ds.bot_state.logs)
    async def slow(uid): await asyncio.sleep(10)
    ds.bot_state.refresh_callbacks["on_restart_account"] = slow
    orig = ds.call_account_callback
    async def quick(name, uid, timeout): return await orig(name, uid, 0.05)
    monkeypatch.setattr(ds, "call_account_callback", quick)
    i = inter(111); await dc.act_restart(i, "1"); assert "Timed out" in i.followup.sent[0]["embed"].description
