"""OAuth 授权服务与远程 MCP 鉴权：注册白名单、授权页、PKCE、令牌轮换与撤销、令牌与会话隔离。"""
import base64
import json
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from waystone.api import create_app
from waystone.store import Store
from test_service import Backend

PUBLIC='http://127.0.0.1:8900'
CB='http://127.0.0.1:53682/callback'
CLAUDE='https://claude.ai/api/mcp/auth_callback'
INIT={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'pytest','version':'1'}}}
MCP_HEADERS={'Accept':'application/json, text/event-stream','Content-Type':'application/json'}

def test_store_oauth_tables_and_authenticate(tmp_path):
    s=Store(str(tmp_path/'db.sqlite'));b=Backend()
    with s.connect() as c:names={r['name'] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'oauth_clients','oauth_requests','oauth_codes','oauth_tokens'}<=names
    uid=s.authenticate('owner@example.com','test-password-123',b)
    with s.connect() as c:assert c.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
    with pytest.raises(HTTPException) as e:s.authenticate('owner@example.com','wrong-password-000',b)
    assert e.value.status_code==401
    assert s.login('owner@example.com','test-password-123',b)['user_id']==uid

@pytest.fixture
def remote(tmp_path):
    app=create_app(str(tmp_path/'db.sqlite'),Backend(),public_url=PUBLIC)
    with TestClient(app,base_url=PUBLIC) as c:
        session=c.post('/auth/login',json={'email':'owner@example.com','password':'test-password-123'}).json()['token']
        yield c,app,session

def register(c,redirect=CB,**kw):
    return c.post('/register',json={'client_name':'pytest agent','redirect_uris':[redirect],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none',**kw})
def pkce():
    v=secrets.token_urlsafe(48);return v,base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip('=')
def authorize(c,client_id,challenge,state='s1',redirect=CB,resource=PUBLIC+'/mcp'):
    return c.get('/authorize',params={'response_type':'code','client_id':client_id,'redirect_uri':redirect,'code_challenge':challenge,'code_challenge_method':'S256','state':state,'resource':resource},follow_redirects=False)
def start(c,client_id,challenge,**kw):
    r=authorize(c,client_id,challenge,**kw)
    assert r.status_code==302,r.text
    loc=r.headers['location'];assert loc.startswith(PUBLIC+'/oauth/consent#'),loc
    return loc.split('#',1)[1]
def approve(c,request,password='test-password-123',email='owner@example.com'):
    return c.post('/oauth/consent/approve',json={'request':request,'email':email,'password':password})
def code_from(redirect):
    q=parse_qs(urlparse(redirect).query);return q['code'][0],q.get('state',[None])[0]
def tokens(c,client_id,verifier,code,redirect=CB):
    return c.post('/token',data={'grant_type':'authorization_code','client_id':client_id,'code':code,'code_verifier':verifier,'redirect_uri':redirect,'resource':PUBLIC+'/mcp'})
def refresh(c,client_id,token):
    return c.post('/token',data={'grant_type':'refresh_token','client_id':client_id,'refresh_token':token})
def full_flow(c,email='owner@example.com',password='test-password-123'):
    cid=register(c).json()['client_id'];v,ch=pkce();code,_=code_from(approve(c,start(c,cid,ch),password,email).json()['redirect'])
    r=tokens(c,cid,v,code);assert r.status_code==200,r.text
    return cid,r.json()
def mcp_status(c,token):
    # 用合法的 initialize 请求：令牌有效必须是 200 且返回本服务的 JSON-RPC 结果，令牌无效必须是带 resource_metadata 的 401；其他状态码说明挂载或传输配置有问题，直接判失败。
    r=c.post('/mcp',headers={**MCP_HEADERS,'Authorization':'Bearer '+token},json=INIT)
    if r.status_code==200:
        assert r.json()['result']['serverInfo']['name']=='waystone',r.text
        return 200
    assert r.status_code==401 and 'resource_metadata=' in r.headers['www-authenticate'],(r.status_code,r.text)
    return 401
def call_tool(c,token,name,arguments):
    r=c.post('/mcp',headers={**MCP_HEADERS,'Authorization':'Bearer '+token},json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':arguments}})
    assert r.status_code==200,r.text
    return r.json()['result']

def test_remote_disabled_without_public_url(tmp_path,monkeypatch):
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    c=TestClient(create_app(str(tmp_path/'db.sqlite'),Backend()))
    assert c.get('/.well-known/oauth-authorization-server').status_code==404
    assert c.post('/mcp',content='{}').status_code==404
    assert c.get('/oauth/consent').status_code==404

def test_discovery_metadata_and_unauthenticated_challenge(remote):
    c,_,_=remote
    prm=c.get('/.well-known/oauth-protected-resource/mcp').json()
    meta=c.get('/.well-known/oauth-authorization-server').json()
    assert prm['resource']==PUBLIC+'/mcp'
    assert prm['authorization_servers']==[meta['issuer']] and meta['issuer'].rstrip('/')==PUBLIC
    assert prm['scopes_supported']==['memory'] and meta['scopes_supported']==['memory']
    assert meta['registration_endpoint']==PUBLIC+'/register' and meta['code_challenge_methods_supported']==['S256']
    r=c.post('/mcp',headers=MCP_HEADERS,json=INIT)
    assert r.status_code==401
    assert 'resource_metadata="'+PUBLIC+'/.well-known/oauth-protected-resource/mcp"' in r.headers['www-authenticate']

def test_registration_redirect_allowlist(remote):
    c,_,_=remote
    bad=register(c,'https://evil.example/callback')
    assert bad.status_code==400 and bad.json()['error']=='invalid_redirect_uri'
    for uri in ['http://127.0.0.1.evil.example/cb','https://claude.ai/api/mcp/auth_callback/../x','https://127.0.0.1:9/cb','http://user:pw@127.0.0.1:9/cb']:
        assert register(c,uri).status_code==400,uri
    for uri in ['https://claude.ai/api/mcp/auth_callback','http://127.0.0.1:53682/callback','http://localhost:9/cb','http://[::1]:7777/callback']:
        assert register(c,uri).status_code==201,uri

def test_extra_redirect_uris_from_env(tmp_path,monkeypatch):
    monkeypatch.setenv('OAUTH_EXTRA_REDIRECT_URIS',' https://agent.example.com/oauth/callback ,')
    with TestClient(create_app(str(tmp_path/'db.sqlite'),Backend(),public_url=PUBLIC),base_url=PUBLIC) as c:
        assert register(c,'https://agent.example.com/oauth/callback').status_code==201
        assert register(c,'https://agent.example.com/other').status_code==400

def test_consent_page_and_inspect(remote):
    c,_,_=remote
    page=c.get('/oauth/consent');h=page.headers
    assert page.status_code==200 and h['cache-control']=='no-store' and h['referrer-policy']=='no-referrer' and h['x-frame-options']=='DENY'
    assert "frame-ancestors 'none'" in h['content-security-policy'] and "form-action 'none'" in h['content-security-policy']
    assert '未经验证' in page.text and '别人发来的授权链接' in page.text
    name='<img src=x onerror=alert(1)>'
    cid=register(c,client_name=name).json()['client_id'];_,ch=pkce();req=start(c,cid,ch)
    assert c.post('/oauth/consent/inspect',json={'request':req}).json()=={'client_name':name,'client_id_tail':cid[-8:],'redirect_uri':CB,'redirect_host':'127.0.0.1','loopback':True,'scopes':['memory']}
    assert '<img' not in page.text
    assert c.post('/oauth/consent/inspect',json={'request':'x'*43}).status_code==400

def test_approve_checks_password_and_is_single_use(remote):
    c,_,_=remote
    cid=register(c).json()['client_id'];_,ch=pkce();req=start(c,cid,ch,state='abc')
    assert approve(c,req,'wrong-password-000').status_code==401
    r=approve(c,req);assert r.status_code==200
    assert r.json()['redirect'].startswith(CB+'?')
    assert code_from(r.json()['redirect'])[1]=='abc'
    assert approve(c,req).status_code==400

def test_deny_redirects_with_access_denied(remote):
    c,_,_=remote
    cid=register(c).json()['client_id'];_,ch=pkce();req=start(c,cid,ch,state='st')
    q=parse_qs(urlparse(c.post('/oauth/consent/deny',json={'request':req}).json()['redirect']).query)
    assert q['error']==['access_denied'] and q['state']==['st'] and 'code' not in q
    assert approve(c,req).status_code==400

def test_authorize_rejects_foreign_resource(remote):
    c,_,_=remote
    cid=register(c).json()['client_id'];_,ch=pkce()
    r=authorize(c,cid,ch,resource='https://other.example/mcp')
    assert r.status_code==302 and 'error=invalid_request' in r.headers['location'] and '/oauth/consent' not in r.headers['location']

def test_code_is_pkce_bound_and_single_use(remote):
    c,_,_=remote
    cid=register(c).json()['client_id'];v,ch=pkce();code,_=code_from(approve(c,start(c,cid,ch)).json()['redirect'])
    bad=tokens(c,cid,'wrong-'+v,code);assert bad.status_code==400 and bad.json()['error']=='invalid_grant'
    ok=tokens(c,cid,v,code);assert ok.status_code==200
    body=ok.json()
    assert body['token_type']=='Bearer' and body['expires_in']==3600 and body['refresh_token'] and body['scope']=='memory'
    again=tokens(c,cid,v,code);assert again.status_code==400 and again.json()['error']=='invalid_grant'

def test_refresh_rotation_and_reuse_revokes_family(remote):
    c,_,_=remote
    cid,t1=full_flow(c)
    assert mcp_status(c,t1['access_token'])==200
    r2=refresh(c,cid,t1['refresh_token']);assert r2.status_code==200,r2.text
    t2=r2.json()
    assert t2['refresh_token']!=t1['refresh_token'] and t2['access_token']!=t1['access_token']
    assert mcp_status(c,t2['access_token'])==200
    reuse=refresh(c,cid,t1['refresh_token'])
    assert reuse.status_code==400 and reuse.json()['error']=='invalid_grant'
    assert refresh(c,cid,t2['refresh_token']).status_code==400
    assert mcp_status(c,t1['access_token'])==401 and mcp_status(c,t2['access_token'])==401

def test_oauth_tokens_and_sessions_are_not_interchangeable(remote):
    c,_,session=remote
    _,t=full_flow(c)
    assert c.get('/projects',headers={'Authorization':'Bearer '+t['access_token']}).status_code==401
    assert c.get('/projects',headers={'Authorization':'Bearer '+session}).status_code==200
    assert mcp_status(c,session)==401

def test_access_token_resource_and_expiry(remote):
    c,app,_=remote
    _,t=full_flow(c)
    with app.state.store.connect() as db:db.execute("UPDATE oauth_tokens SET resource='http://127.0.0.1:8900/other' WHERE kind='access'")
    assert mcp_status(c,t['access_token'])==401
    with app.state.store.connect() as db:db.execute("UPDATE oauth_tokens SET resource=?,expires=0 WHERE kind='access'",(PUBLIC+'/mcp',))
    assert mcp_status(c,t['access_token'])==401

def test_secrets_are_stored_hashed(remote):
    c,app,_=remote
    cid=register(c).json()['client_id'];_,ch=pkce();req=start(c,cid,ch)
    with app.state.store.connect() as db:assert req not in str([tuple(r) for r in db.execute('SELECT * FROM oauth_requests')])
    _,t=full_flow(c)
    with app.state.store.connect() as db:
        dump=str([tuple(r) for tbl in ('oauth_tokens','oauth_codes','oauth_requests') for r in db.execute(f'SELECT * FROM {tbl}')])
    assert t['access_token'] not in dump and t['refresh_token'] not in dump

def test_connections_list_and_disconnect(remote):
    c,_,session=remote;h={'Authorization':'Bearer '+session}
    cid,t=full_flow(c)
    items=c.get('/auth/connections',headers=h).json()
    assert [(i['client_id'],i['client_name'],i['redirect_host']) for i in items]==[(cid,'pytest agent','127.0.0.1')]
    assert c.get('/auth/connections').status_code==401
    assert c.delete('/auth/connections/'+cid,headers=h).json()=={'revoked':2}
    assert mcp_status(c,t['access_token'])==401 and refresh(c,cid,t['refresh_token']).status_code==400
    assert c.get('/auth/connections',headers=h).json()==[]

def test_revoke_endpoint_revokes_family(remote):
    c,_,_=remote
    cid,t=full_flow(c)
    assert c.post('/revoke',data={'token':t['refresh_token'],'client_id':cid,'client_secret':''}).status_code==200
    assert refresh(c,cid,t['refresh_token']).status_code==400 and mcp_status(c,t['access_token'])==401

def test_stale_clients_without_tokens_are_purged(remote):
    c,app,_=remote
    old=register(c).json()['client_id']
    cid,_=full_flow(c)
    with app.state.store.connect() as db:db.execute('UPDATE oauth_clients SET created=0 WHERE client_id IN (?,?)',(old,cid))
    register(c)
    with app.state.store.connect() as db:
        assert not db.execute('SELECT 1 FROM oauth_clients WHERE client_id=?',(old,)).fetchone()
        assert db.execute('SELECT 1 FROM oauth_clients WHERE client_id=?',(cid,)).fetchone()

def test_remote_tools_enforce_membership_and_roles(remote):
    c,app,session=remote;owner={'Authorization':'Bearer '+session}
    a=c.post('/projects',json={'name':'Alpha'},headers=owner).json()['id'];b=c.post('/projects',json={'name':'Beta'},headers=owner).json()['id']
    eb=c.post(f'/projects/{b}/entries',json={'content':'Beta 项目的内部约定','topic':'beta/rule','source':'pytest','kind':'convention'},headers=owner).json()['id']
    inv=c.post(f'/projects/{a}/invites',json={'email':'reader@example.com','role':'reader'},headers=owner).json()['invite']
    assert c.post('/auth/join',json={'invite':inv,'email':'reader@example.com','name':'Reader','password':'reader-password-123'}).status_code==200
    _,t=full_flow(c,'reader@example.com','reader-password-123');tok=t['access_token']
    listed=call_tool(c,tok,'project_list',{})
    assert not listed.get('isError') and a in str(listed) and b not in str(listed)
    assert not call_tool(c,tok,'memory_recall',{'project_id':a,'query':'约定'}).get('isError')
    denied=[call_tool(c,tok,'memory_publish',{'project_id':a,'content':'只读成员不能写入','topic':'alpha/x','source':'pytest','kind':'background'}),
        call_tool(c,tok,'memory_recall',{'project_id':b,'query':'约定'}),
        call_tool(c,tok,'memory_entries',{'project_id':b}),
        call_tool(c,tok,'memory_retract',{'project_id':b,'entry_id':eb}),
        call_tool(c,tok,'memory_reindex',{'project_id':b})]
    assert all(r.get('isError') for r in denied),denied
    assert 'Beta 项目的内部约定' not in str(denied)
    with app.state.store.connect() as db:
        assert db.execute('SELECT status FROM entries WHERE id=?',(eb,)).fetchone()['status']=='active'
        assert not db.execute("SELECT 1 FROM entries WHERE topic='alpha/x'").fetchone()

def test_clients_are_forced_public_and_metadata_says_none(remote):
    c,app,_=remote
    r=c.post('/register',json={'client_name':'confidential','redirect_uris':[CB],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'client_secret_post'})
    assert r.status_code==201 and r.json()['token_endpoint_auth_method']=='none' and not r.json().get('client_secret')
    with app.state.store.connect() as db:info=db.execute('SELECT info FROM oauth_clients WHERE client_id=?',(r.json()['client_id'],)).fetchone()['info']
    assert json.loads(info).get('client_secret') is None
    meta=c.get('/.well-known/oauth-authorization-server').json()
    assert meta['token_endpoint_auth_methods_supported']==['none'] and meta['revocation_endpoint_auth_methods_supported']==['none']
    assert meta['registration_endpoint']==PUBLIC+'/register' and meta['code_challenge_methods_supported']==['S256']

def test_registration_metadata_limits(remote):
    c,_,_=remote
    assert register(c,client_name='x'*101).status_code==400
    assert c.post('/register',json={'client_name':'many','redirect_uris':[f'http://127.0.0.1:{9000+i}/cb' for i in range(5)],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'}).status_code==400
    assert register(c,'http://127.0.0.1:9/cb?'+'a'*2100).status_code==400

def test_register_and_token_are_rate_limited(remote):
    c,_,_=remote
    codes=[register(c).status_code for _ in range(21)]
    assert codes[:20]==[201]*20 and codes[20]==429
    r=register(c);assert r.status_code==429 and int(r.headers['retry-after'])>0
    bad=[c.post('/token',data={'grant_type':'refresh_token','client_id':'x','refresh_token':'y'}).status_code for _ in range(121)]
    assert 429 not in bad[:120] and bad[120]==429

def test_pending_client_cap(tmp_path,monkeypatch):
    monkeypatch.setenv('OAUTH_MAX_PENDING_CLIENTS','3')
    with TestClient(create_app(str(tmp_path/'db.sqlite'),Backend(),public_url=PUBLIC),base_url=PUBLIC) as c:
        assert [register(c).status_code for _ in range(4)]==[201,201,201,400]

def test_cleanup_removes_expired_rows_and_stale_clients(remote):
    c,app,_=remote
    cid=register(c).json()['client_id'];_,ch=pkce();start(c,cid,ch)
    code_from(approve(c,start(c,cid,ch)).json()['redirect'])
    full_flow(c)
    with app.state.store.connect() as db:
        for tbl in ('oauth_requests','oauth_codes','oauth_tokens'):db.execute(f'UPDATE {tbl} SET expires=0')
        db.execute('UPDATE oauth_clients SET created=0')
    register(c)
    with app.state.store.connect() as db:
        assert [db.execute(f'SELECT count(*) FROM {tbl}').fetchone()[0] for tbl in ('oauth_requests','oauth_codes','oauth_tokens')]==[0,0,0]
        assert db.execute('SELECT count(*) FROM oauth_clients').fetchone()[0]==1

def test_refresh_race_revokes_whole_family(remote):
    import anyio
    from mcp.server.auth.provider import TokenError
    c,app,_=remote;provider=app.state.oauth_provider
    cid,t=full_flow(c)
    client=anyio.run(provider.get_client,cid)
    rt=anyio.run(provider.load_refresh_token,client,t['refresh_token'])
    # 两个并发刷新都通过了 load 检查，只有一个能兑换；另一个必须失败并作废整个家族，包括刚签发的新令牌。
    first=anyio.run(provider.exchange_refresh_token,client,rt,['memory'])
    with pytest.raises(TokenError):anyio.run(provider.exchange_refresh_token,client,rt,['memory'])
    assert mcp_status(c,first.access_token)==401 and mcp_status(c,t['access_token'])==401
    assert refresh(c,cid,first.refresh_token).status_code==400

def test_disconnect_also_voids_unexchanged_codes(remote):
    c,_,session=remote;h={'Authorization':'Bearer '+session}
    cid,_=full_flow(c)
    v,ch=pkce();code,_=code_from(approve(c,start(c,cid,ch)).json()['redirect'])
    assert c.delete('/auth/connections/'+cid,headers=h).json()=={'revoked':2}
    r=tokens(c,cid,v,code);assert r.status_code==400 and r.json()['error']=='invalid_grant'
    assert c.get('/auth/connections',headers=h).json()==[]

def test_connections_ignore_used_or_expired_refresh_tokens(remote):
    c,app,session=remote;h={'Authorization':'Bearer '+session}
    cid,t=full_flow(c);assert refresh(c,cid,t['refresh_token']).status_code==200
    assert len(c.get('/auth/connections',headers=h).json())==1
    with app.state.store.connect() as db:db.execute("UPDATE oauth_tokens SET expires=1 WHERE kind='refresh' AND used=0")
    assert c.get('/auth/connections',headers=h).json()==[]

def test_authorize_requires_valid_s256_challenge(remote):
    c,_,_=remote
    cid=register(c).json()['client_id']
    for ch in ['short','a'*42+'!','a'*44]:
        r=authorize(c,cid,ch)
        assert r.status_code==302 and 'error=invalid_request' in r.headers['location'] and '/oauth/consent' not in r.headers['location'],ch
    r=c.get('/authorize',params={'response_type':'code','client_id':cid,'redirect_uri':CB,'code_challenge':pkce()[1],'code_challenge_method':'plain','state':'s'},follow_redirects=False)
    assert '/oauth/consent' not in r.headers.get('location','')

def test_loopback_redirect_ignores_port_and_loopback_alias(remote):
    c,_,_=remote
    cid=register(c).json()['client_id'];v,ch=pkce()
    other='http://localhost:61234/callback'
    redirect=approve(c,start(c,cid,ch,redirect=other)).json()['redirect']
    assert redirect.startswith(other+'?')
    code,_=code_from(redirect)
    assert tokens(c,cid,v,code,redirect=other).status_code==200
    for bad in ['http://127.0.0.1:61234/other','https://127.0.0.1:61234/callback','http://example.com:61234/callback']:
        assert authorize(c,cid,pkce()[1],redirect=bad).status_code==400,bad
    claude=register(c,CLAUDE).json()['client_id']
    assert authorize(c,claude,pkce()[1],redirect='https://claude.ai/api/mcp/other').status_code==400

def test_public_url_must_be_an_origin(tmp_path):
    for bad in ['http://ai.example.com','https://ai.example.com/base','https://u:p@ai.example.com','https://ai.example.com/?x=1','https://ai.example.com/#f','ftp://ai.example.com']:
        with pytest.raises(ValueError):create_app(str(tmp_path/'db.sqlite'),Backend(),public_url=bad)
    app=create_app(str(tmp_path/'db2.sqlite'),Backend(),public_url='HTTPS://AI.Example.com:8443/')
    assert app.state.oauth_provider.resource=='https://ai.example.com:8443/mcp'

def test_oauth_security_events_are_audited(remote):
    c,app,session=remote;h={'Authorization':'Bearer '+session}
    cid,t=full_flow(c)
    _,ch=pkce();c.post('/oauth/consent/deny',json={'request':start(c,cid,ch)})
    assert refresh(c,cid,t['refresh_token']).status_code==200
    assert refresh(c,cid,t['refresh_token']).status_code==400
    cid2,t2=full_flow(c)
    assert c.post('/revoke',data={'token':t2['refresh_token'],'client_id':cid2,'client_secret':''}).status_code==200
    cid3,_=full_flow(c);c.delete('/auth/connections/'+cid3,headers=h)
    with app.state.store.connect() as db:rows=[dict(r) for r in db.execute("SELECT * FROM audit WHERE action LIKE 'oauth_%' ORDER BY id")]
    actions=[r['action'] for r in rows]
    for a in ['oauth_approve','oauth_deny','oauth_refresh_reuse','oauth_revoke','oauth_disconnect']:assert a in actions,actions
    assert {r['target'] for r in rows if r['action']=='oauth_refresh_reuse'}=={cid}
    assert all(r['project'] is None for r in rows)
    dump=str(rows)
    assert t['access_token'] not in dump and t['refresh_token'] not in dump and 'test-password-123' not in dump

def test_authorize_bounds_pending_requests(remote):
    c,app,_=remote
    cid=register(c).json()['client_id']
    long=authorize(c,cid,pkce()[1],state='s'*1025)
    assert long.status_code==302 and 'error=invalid_request' in long.headers['location']
    for _ in range(20):start(c,cid,pkce()[1])
    over=authorize(c,cid,pkce()[1])
    assert over.status_code==302 and 'error=temporarily_unavailable' in over.headers['location']
    with app.state.store.connect() as db:assert db.execute('SELECT count(*) FROM oauth_requests').fetchone()[0]==20

def test_authorize_is_rate_limited_per_ip(remote):
    c,_,_=remote
    a=register(c).json()['client_id'];b=register(c).json()['client_id']
    codes=[authorize(c,a,pkce()[1]).status_code for _ in range(20)]+[authorize(c,b,pkce()[1]).status_code for _ in range(10)]
    assert codes==[302]*30
    r=authorize(c,b,pkce()[1]);assert r.status_code==429 and int(r.headers['retry-after'])>0

def test_registration_rejects_oversized_or_missing_metadata(remote):
    c,app,_=remote
    base={'client_name':'x','grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'}
    assert c.post('/register',json={**base,'redirect_uris':None}).status_code==400
    assert c.post('/register',json={**base,'redirect_uris':[CB],'jwks':{'keys':[{'k':'a'*20000}]}}).status_code==400
    with app.state.store.connect() as db:assert db.execute('SELECT count(*) FROM oauth_clients').fetchone()[0]==0

def test_authorization_server_metadata_allows_cors(remote):
    c,_,_=remote
    r=c.get('/.well-known/oauth-authorization-server',headers={'Origin':'https://inspector.example'})
    assert r.status_code==200 and r.headers.get('access-control-allow-origin')=='*'

def test_registration_size_limit_counts_utf8_bytes(remote):
    c,_,_=remote
    # 限额按 UTF-8 字节计：3000 个汉字不到 8192 个字符，但超过 8 KB。
    base={'client_name':'x','redirect_uris':[CB],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'}
    assert c.post('/register',json={**base,'jwks':{'keys':[{'k':'汉'*3000}]}}).status_code==400
