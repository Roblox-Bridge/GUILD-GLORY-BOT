import os, sys
os.environ.setdefault("DISCORD_ADMIN_IDS", "111")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest

@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test runs in an empty temp dir with empty in-memory state: never touches real accounts."""
    import dashboard_server as ds
    monkeypatch.chdir(tmp_path)
    for k in ("DASHBOARD_PASSWORD","DASHBOARD_HOST","DASHBOARD_PORT","GUILDINFO_API_KEY","DASHBOARD_AUTO_JOIN",
              "DASHBOARD_TRUST_PROXY","DASHBOARD_COOKIE_SECURE","DISCORD_PUBLIC_AUTOJOIN"):
        monkeypatch.delenv(k, raising=False)
    st = ds.bot_state
    saved = (dict(st.accounts), list(st.logs), dict(st.account_workers), dict(st.refresh_callbacks),
             dict(st.account_credentials), set(st.paused_accounts), dict(st.game_to_auth_id), dict(st.auth_to_game_id))
    st.accounts.clear(); st.logs.clear(); st.account_workers.clear(); st.refresh_callbacks.clear()
    st.account_credentials.clear(); st.paused_accounts.clear(); st.game_to_auth_id.clear(); st.auth_to_game_id.clear()
    yield
    st.accounts.clear(); st.accounts.update(saved[0]); st.logs[:] = saved[1]
