import os, sys, json, asyncio, types
os.environ["DISCORD_ADMIN_IDS"] = "111,abc"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest, discord
import discord_control as dc
import dashboard_server as ds

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class Resp:
    def __init__(s): s.sent=[]; s.done=False; s.modal=None
    def is_done(s): return s.done
    async def send_message(s, *a, **k):
        s.done=True
        if a: k['content']=a[0]
        s.sent.append(k)
    async def defer(s, **k): s.done=True
    async def send_modal(s, m): s.done=True; s.modal=m
class FU:
    async def send(s,*a,**k):
        if a: k['content']=a[0]
        s.sent.append(k)
    def __init__(s): s.sent=[]
def inter(uid, cid=None, cmd=None):
    i=types.SimpleNamespace(user=types.SimpleNamespace(id=uid), response=Resp(), followup=FU(),
        data={"custom_id":cid} if cid else {}, command=types.SimpleNamespace(name=cmd) if cmd else None)
    return i

def test_admin_parsing():
    assert dc.ADMIN_IDS == {"111"} and dc.INVALID_ADMIN_IDS == ["abc"]

def test_every_telegram_command_exists():
    names = {c.name for c in dc.tree.get_commands()}
    tg = {"status","accounts","add","token","delete","pause","pauseall","refresh","restart","logs","clearlogs","clanjoin","guild","start","help"}
    assert tg <= names and "autojoin" in names

def test_command_callbacks_real_and_view_buttons():
    for c in dc.tree.get_commands(): assert callable(c.callback)
    ids = {b.custom_id for b in dc.PanelView().children}
    assert {"gg:status","gg:accounts","gg:add","gg:token","gg:pauseall","gg:logs","gg:settings","gg:help",
            "gg:pause","gg:refresh","gg:restart","gg:delete","gg:guild","gg:clan","gg:join"} <= ids
    rows = {}
    for b in dc.PanelView().children: rows[b.row]=rows.get(b.row,0)+1
    assert all(v<=5 for v in rows.values())

@pytest.mark.asyncio
async def test_tree_blocks_non_admin_allows_admin():
    t = dc.tree
    for cmd in ("status","delete","add","clanjoin","clearlogs"):
        i=inter(222,cmd=cmd); assert await t.interaction_check(i) is False
        assert i.response.sent[0]["ephemeral"]
        assert await t.interaction_check(inter(111,cmd=cmd)) is True

@pytest.mark.asyncio
async def test_actions_recheck_admin_even_if_tree_bypassed():
    ds.bot_state.logs.append({"time":"x","message":"m"})
    for fn,args in [(dc.act_clearlogs,()),(dc.act_pauseall,()),(dc.act_delete,("1",)),(dc.act_add,("1","p")),
                    (dc.act_token,("tok",)),(dc.act_refresh,("1",)),(dc.act_restart,("1",)),
                    (dc.act_clanjoin,("1",)),(dc.act_guild,("1","IND")),(dc.act_pause,("1",))]:
        i=inter(222); await fn(i,*args)
        assert "Admin access only" in i.response.sent[0]["content"], fn.__name__
    assert ds.bot_state.logs  # not cleared

@pytest.mark.asyncio
async def test_panel_and_modal_checks(monkeypatch):
    v=dc.PanelView()
    for cid in ("gg:delete","gg:add","gg:logs"):
        assert await v.interaction_check(inter(222,cid=cid)) is False
        assert await v.interaction_check(inter(111,cid=cid)) is True
    monkeypatch.delenv("DISCORD_PUBLIC_AUTOJOIN", raising=False)
    assert await v.interaction_check(inter(222,cid="gg:join")) is False
    monkeypatch.setenv("DISCORD_PUBLIC_AUTOJOIN","true")
    assert await v.interaction_check(inter(222,cid="gg:join")) is True
    for m in (dc.AddModal(), dc.TokenModal(), dc.ClanJoinModal(), dc.GuildModal(), dc.UidModal("t", dc.act_delete)):
        assert await m.interaction_check(inter(222)) is False
        assert await m.interaction_check(inter(111)) is True
    monkeypatch.delenv("DISCORD_PUBLIC_AUTOJOIN")
    assert await dc.JoinModal().interaction_check(inter(222)) is False
    assert await dc.JoinOnlyView().interaction_check(inter(222)) is False
    i=inter(222); await dc.cmd_autojoin.callback(i); assert "restricted" in i.response.sent[0]["content"]

def test_parse_accounts_matches_telegram():
    txt="# c\n\n1:a\n1:dup\n2:b:c\nbad\n:x\n3:\n  4 : p \n"
    accts,inv = dc.parse_accounts(txt)
    assert accts==[("1","a"),("2","b:c"),("4","p")] and inv==3

def test_join_api_parity_with_telegram():
    src = open(os.path.join(ROOT,"telegram_control.py"),encoding="utf-8").read()
    for const in (dc.JOIN_JWT_API_URL, dc.JOIN_API_URL, dc.JOIN_KEY.decode(), dc.JOIN_IV.decode(), "OB55", "2018.4.12f1"):
        assert const in src
    import telegram_control as tc
    for gid in (1, 127, 128, 123456789, 2**40):
        assert dc._join_body(gid)==tc._join_body(gid)

def test_jwt_parsing_and_send_mocked(monkeypatch):
    class R:
        status_code=200
        def __init__(s,j): s._j=j
        def raise_for_status(s): pass
        def json(s): return s._j
    for payload,exp in [("tok",'tok'),({"token":"t"},"t"),({"data":{"jwt":"j"}},"j"),({"x":1},None)]:
        monkeypatch.setattr(dc.requests,"get",lambda *a,payload=payload,**k:R(payload))
        assert dc._join_generate_jwt("u","p")==exp
    seen={}
    def post(url,headers,data,timeout): seen.update(url=url,h=headers,d=data); return types.SimpleNamespace(status_code=200)
    monkeypatch.setattr(dc.requests,"post",post)
    assert dc._join_send("JWT",5).status_code==200
    assert seen["url"]==dc.JOIN_API_URL and seen["h"]["Authorization"]=="Bearer JWT" and seen["d"]==dc._join_body(5)
    def boom(*a,**k): raise dc.requests.ConnectionError()
    monkeypatch.setattr(dc.requests,"post",boom); assert dc._join_send("J",5) is None

@pytest.mark.asyncio
async def test_do_add_files_and_worker_cancel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); monkeypatch.setattr(dc,"ACCOUNTS_FILE","accounts.json")
    called=[]
    async def cb(d): called.append(d)
    ds.bot_state.refresh_callbacks["on_account_added"]=cb
    class W:
        c=False
        def cancel(s): s.c=True
    w=W(); ds.bot_state.account_workers["77"]=w
    assert await dc.do_add({"uid":"77","password":"pw"})=="77" and w.c
    assert await dc.do_add({"uid":"77","password":"pw2"})=="77"
    lab=await dc.do_add({"token":"ABCDEFGHIJKLMNOP"}); assert "ABCDEFGH" not in lab
    await asyncio.sleep(0.05)
    data=json.load(open("accounts.json")); assert {"uid":"77","password":"pw2"} in data and len(data)==2
    assert len(called)==3
    with pytest.raises(ValueError): await dc.do_add({"uid":"","password":"x"})
    with pytest.raises(ValueError): await dc.do_add({"token":"  "})

@pytest.mark.asyncio
async def test_clanjoin_and_guild_mocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    json.dump({"1":{"token":"t","region":"IND"},"2":{}},open("token_cache.json","w"))
    async def fake(tok,cid,reg): return 200,""
    monkeypatch.setattr(ds,"request_clan_async",fake)
    i=inter(111); await dc.act_clanjoin(i,"99"); assert "1/1" in i.followup.sent[0]["embed"].description
    i=inter(111); await dc.act_clanjoin(i,"abc"); assert "Invalid" in i.response.sent[0]["embed"].title
    monkeypatch.setattr(ds,"fetch_single_clan",lambda c,r,cache:{"clan_id":c,"raw_response_bytes":"ff"})
    i=inter(111); await dc.act_guild(i,"5","ind"); d=i.followup.sent[0]["embed"].description
    assert "raw_response" not in d and '"clan_id"' in d
    i=inter(111); await dc.act_guild(i,"5","I N D!"); assert "Invalid" in i.response.sent[0]["embed"].title

@pytest.mark.asyncio
async def test_run_join_end_to_end_mocked(monkeypatch):
    monkeypatch.setattr(dc,"_join_generate_jwt",lambda u,p: None if u=="2" else "J")
    monkeypatch.setattr(dc,"_join_send",lambda j,g: None if g==1 else types.SimpleNamespace(status_code=200))
    async def nosleep(*a): pass
    monkeypatch.setattr(dc.asyncio,"sleep",nosleep)
    edits=[]
    i=inter(111)
    async def edit(**k): edits.append(k)
    i.edit_original_response=edit
    await dc.run_join(i,[("1","SECRETPW1"),("2","SECRETPW2"),("3","SECRETPW3")],0,555)
    fin=edits[-1]; names={f.name:f.value for f in fin["embed"].fields}
    assert names["HTTP 200"]=="2" and names["JWT failed"]=="1" and fin["attachments"][0].filename=="join_report_555.txt"
    body=fin["attachments"][0].fp.read().decode(); assert "HTTP_200" in body and "JWT_FAILED" in body and "SECRETPW" not in body
    assert 111 not in dc._join_running

@pytest.mark.asyncio
async def test_start_join_validation(monkeypatch):
    monkeypatch.setenv("DISCORD_PUBLIC_AUTOJOIN","true")
    for content,gid,msg in [("1:a","x","Invalid Guild"),("nonsense","5","No valid"),
                            ("\n".join(f"{n}:p" for n in range(101)),"5","Too many")]:
        i=inter(222); await dc.start_join(i,content,gid); assert msg in i.response.sent[0]["embed"].title+i.response.sent[0]["embed"].description

def test_accounts_pagination():
    ds.bot_state.accounts={str(n):{"uid":str(n)} for n in range(25)}
    e,p=dc.accounts_page_embed(2); assert p==3 and "Page 3/3" in e.footer.text and len(e.description)<4000
    ds.bot_state.accounts={}

@pytest.mark.asyncio
async def test_start_bot_paths(monkeypatch, capsys):
    monkeypatch.setattr(dc,"BOT_TOKEN","")
    await dc.start_discord_bot(); assert "Disabled" in capsys.readouterr().out
    monkeypatch.setattr(dc,"BOT_TOKEN","x.y.z")
    async def bad(tok): raise discord.LoginFailure("no")
    monkeypatch.setattr(dc.client,"start",bad)
    await dc.start_discord_bot(); out=capsys.readouterr().out
    assert "invalid" in out and "x.y.z" not in out
