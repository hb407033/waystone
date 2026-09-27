"""OAuth 授权服务：供 Claude、Claude Code、Codex 的远程连接器注册、授权和刷新令牌；数据存 SQLite，令牌只存哈希。"""
import anyio
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizeError, RefreshToken, RegistrationError, TokenError, construct_redirect_uri
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from .store import digest, fail

CLAUDE_CALLBACK='https://claude.ai/api/mcp/auth_callback'
SCOPE='memory';ACCESS_TTL=3600;REFRESH_TTL=30*86400;CODE_TTL=300;REQUEST_TTL=600;STALE_CLIENT=86400
MAX_CLIENT_INFO=8192;MAX_STATE=1024;MAX_REQUESTS_PER_CLIENT=20;MAX_REQUESTS=1000
LOOPBACK={'127.0.0.1','localhost','::1'}

def loopback(uri):
    u=urlsplit(str(uri))
    return u.scheme=='http' and u.hostname in LOOPBACK and not u.username and not u.password and not u.fragment

def redirect_allowed(uri,extra=()):
    # DCR 允许任何人注册客户端；只放行 Claude 托管回调、本机回环地址和部署方显式配置的地址，防止钓鱼链接把授权码带到外部网站。
    if uri==CLAUDE_CALLBACK or uri in extra:return True
    return loopback(uri)

def normalize_public_url(url):
    # issuer、resource、允许的 Host 都从这里派生；带路径时 SDK 元数据会指向不存在的地址，所以只接受 origin。
    u=urlsplit(url.strip());scheme=u.scheme.lower();host=(u.hostname or '').lower()
    ok=host and (scheme=='https' or (scheme=='http' and host in LOOPBACK)) and not u.username and not u.password and not u.query and not u.fragment and u.path in ('','/')
    if not ok:raise ValueError('PUBLIC_URL 必须是 https://主机[:端口] 形式的地址，不能带路径、查询参数或账号')
    host=f'[{host}]' if ':' in host else host
    return f'{scheme}://{host}'+(f':{u.port}' if u.port else '')

class PublicClient(OAuthClientInformationFull):
    def validate_redirect_uri(self,redirect_uri):
        # RFC 8252 §7.3：本机应用每次监听的端口不同，回环回调必须忽略端口；Codex 还会把注册时的 127.0.0.1 换成 localhost（openai/codex#31038），所以三种回环主机视为同一主机。路径和查询参数仍须一致，非回环地址逐字匹配。
        if redirect_uri is None or redirect_uri in (self.redirect_uris or []):return super().validate_redirect_uri(redirect_uri)
        want=urlsplit(str(redirect_uri))
        if loopback(redirect_uri) and any(loopback(r) and (urlsplit(str(r)).path,urlsplit(str(r)).query)==(want.path,want.query) for r in self.redirect_uris or []):return redirect_uri
        raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")

class SqliteOAuthProvider:
    def __init__(self,store,public_url,extra_redirects=(),max_pending=500):
        self.store=store;self.public_url=public_url.rstrip('/');self.resource=self.public_url+'/mcp';self.extra=tuple(extra_redirects);self.max_pending=max_pending

    # 异步方法的数据库操作全部放进线程池：SQLite 连接以 BEGIN IMMEDIATE 加写锁，锁等待时不能阻塞事件循环，否则 /mcp 和其他 OAuth 请求会一起卡住；同步方法（inspect_request 等）由 FastAPI 在线程池调用，保持同步。
    async def get_client(self,client_id):
        def work():
            with self.store.connect() as c:
                row=c.execute('SELECT info FROM oauth_clients WHERE client_id=?',(client_id,)).fetchone()
                return PublicClient.model_validate_json(row['info']) if row else None
        return await anyio.to_thread.run_sync(work)

    def _cleanup(self,c,now):
        # 统一清理过期数据：过期的授权请求、授权码、令牌，以及创建超过 24 小时且三张表里都没有任何记录的客户端；DCR 无门槛，防止过期数据和无效注册无限增长。
        c.execute('DELETE FROM oauth_requests WHERE expires<?',(now,))
        c.execute('DELETE FROM oauth_codes WHERE expires<?',(now,))
        c.execute('DELETE FROM oauth_tokens WHERE expires<?',(now,))
        c.execute('DELETE FROM oauth_clients WHERE created<? AND client_id NOT IN (SELECT client_id FROM oauth_requests) AND client_id NOT IN (SELECT client_id FROM oauth_codes) AND client_id NOT IN (SELECT client_id FROM oauth_tokens)',(now-STALE_CLIENT,))

    async def register_client(self,client_info):
        uris=[str(u) for u in client_info.redirect_uris or []]
        # 注册体限制：防止洪泛注册用超大报文撑爆存储和解析；jwks、contacts 等字段也会整体入库，所以再限制序列化后的总大小。
        if not uris or len(uris)>4 or any(len(u)>2048 for u in uris) or len(client_info.client_name or '')>100 or len(client_info.model_dump_json().encode())>MAX_CLIENT_INFO:raise RegistrationError('invalid_client_metadata','客户端注册信息超出限制')
        if any(not redirect_allowed(u,self.extra) for u in uris):raise RegistrationError('invalid_redirect_uri','回调地址不在允许列表中')
        # 一律按公共客户端注册（RFC 7591 允许服务端覆盖客户端元数据）：SDK 默认生成的 client secret 会明文入库，且 Codex 在机密客户端模式下有已知缺陷（openai/codex#40928）。
        client_info.token_endpoint_auth_method='none';client_info.client_secret=None;client_info.client_secret_expires_at=None
        def work():
            with self.store.connect() as c:
                now=time.time();self._cleanup(c,now)
                # 未产生令牌的客户端只可能是洪水注册：限制总数，防止占满存储。检查和插入在同一事务里，并发注册不会同时通过；SDK 的 RegistrationError 是冻结 dataclass，不能在 with 块内抛（contextlib 回写 __traceback__ 会失败），所以在事务提交后再抛。
                over=c.execute('SELECT count(*) FROM oauth_clients WHERE client_id NOT IN (SELECT client_id FROM oauth_tokens)').fetchone()[0]>=self.max_pending
                if not over:c.execute('INSERT INTO oauth_clients VALUES(?,?,?)',(client_info.client_id,client_info.model_dump_json(),now))
            return over
        if await anyio.to_thread.run_sync(work):raise RegistrationError('invalid_client_metadata','待授权的客户端过多，请稍后再试')

    async def authorize(self,client,params):
        if params.resource and params.resource.rstrip('/')!=self.resource:raise AuthorizeError('invalid_request','resource 必须是本服务的 MCP 地址')
        # S256 的 challenge 是 32 字节 SHA-256 的 base64url 编码，固定 43 位；格式不对的请求直接拒绝，不进授权页。
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',params.code_challenge):raise AuthorizeError('invalid_request','code_challenge 必须是 S256 生成的 43 位 base64url 字符串')
        if params.state and len(params.state)>MAX_STATE:raise AuthorizeError('invalid_request','state 过长')
        def work():
            with self.store.connect() as c:
                now=time.time();self._cleanup(c,now)
                # /authorize 不需要登录就能调用：每个客户端和全局的待确认请求都设上限，防止匿名刷请求占满存储和写锁。超限在事务外抛错（AuthorizeError 是冻结 dataclass）。
                total,mine=c.execute('SELECT count(*),coalesce(sum(client_id=?),0) FROM oauth_requests',(client.client_id,)).fetchone()
                if total>=MAX_REQUESTS or mine>=MAX_REQUESTS_PER_CLIENT:return None
                rid=secrets.token_urlsafe(32)
                c.execute('INSERT INTO oauth_requests VALUES(?,?,?,?)',(digest(rid),client.client_id,json.dumps({'state':params.state,'scopes':params.scopes or [SCOPE],'code_challenge':params.code_challenge,'redirect_uri':str(params.redirect_uri),'redirect_uri_provided_explicitly':params.redirect_uri_provided_explicitly,'resource':self.resource},ensure_ascii=False),now+REQUEST_TTL))
            return rid
        rid=await anyio.to_thread.run_sync(work)
        if rid is None:raise AuthorizeError('temporarily_unavailable','待确认的授权请求过多，请稍后再试')
        return f'{self.public_url}/oauth/consent#{rid}'

    def inspect_request(self,request_id):
        with self.store.connect() as c:
            row=c.execute('SELECT client_id,params FROM oauth_requests WHERE hash=? AND expires>?',(digest(request_id),time.time())).fetchone()
            if not row:fail(400,'授权请求无效或已过期，请回到应用重新连接')
            info=json.loads(c.execute('SELECT info FROM oauth_clients WHERE client_id=?',(row['client_id'],)).fetchone()['info'])
            params=json.loads(row['params']);redirect=params['redirect_uri'];host=urlsplit(redirect).hostname
            return {'client_name':info.get('client_name') or '未命名应用','client_id_tail':row['client_id'][-8:],'redirect_uri':redirect,'redirect_host':host,'loopback':host in LOOPBACK,'scopes':params['scopes']}

    def approve_request(self,request_id,user_id):
        with self.store.connect() as c:
            row=c.execute('DELETE FROM oauth_requests WHERE hash=? AND expires>? RETURNING client_id,params',(digest(request_id),time.time())).fetchone()
            if not row:fail(400,'授权请求无效或已过期，请回到应用重新连接')
            params=json.loads(row['params']);code=secrets.token_urlsafe(32)
            c.execute('INSERT INTO oauth_codes VALUES(?,?,?,?,?)',(digest(code),row['client_id'],user_id,row['params'],time.time()+CODE_TTL))
            self.store.audit(c,None,user_id,'oauth_approve',row['client_id'])
            return construct_redirect_uri(params['redirect_uri'],code=code,state=params['state'])

    def deny_request(self,request_id):
        with self.store.connect() as c:
            row=c.execute('DELETE FROM oauth_requests WHERE hash=? AND expires>? RETURNING client_id,params',(digest(request_id),time.time())).fetchone()
            if not row:fail(400,'授权请求无效或已过期，请回到应用重新连接')
            params=json.loads(row['params'])
            self.store.audit(c,None,'anonymous','oauth_deny',row['client_id'])
            return construct_redirect_uri(params['redirect_uri'],error='access_denied',state=params['state'])

    async def load_authorization_code(self,client,authorization_code):
        def work():
            with self.store.connect() as c:
                row=c.execute('SELECT user_id,params,expires FROM oauth_codes WHERE hash=? AND client_id=?',(digest(authorization_code),client.client_id)).fetchone()
                if not row:return None
                p=json.loads(row['params'])
                return AuthorizationCode(code=authorization_code,scopes=p['scopes'],expires_at=row['expires'],client_id=client.client_id,code_challenge=p['code_challenge'],redirect_uri=p['redirect_uri'],redirect_uri_provided_explicitly=p['redirect_uri_provided_explicitly'],resource=p['resource'],subject=row['user_id'])
        return await anyio.to_thread.run_sync(work)

    async def exchange_authorization_code(self,client,authorization_code):
        def work():
            with self.store.connect() as c:
                cur=c.execute('DELETE FROM oauth_codes WHERE hash=? AND client_id=?',(digest(authorization_code.code),client.client_id))
                if cur.rowcount!=1:return TokenError('invalid_grant','授权码已使用或不存在')
                return self._issue(c,client.client_id,authorization_code.subject,authorization_code.scopes,authorization_code.resource,secrets.token_hex(16))
        # SDK 的 TokenError 是冻结 dataclass，在 with 块内抛会让 contextlib 回写 __traceback__ 失败，所以先提交再在事务外抛。
        result=await anyio.to_thread.run_sync(work)
        if isinstance(result,TokenError):raise result
        return result

    def _issue(self,c,client_id,user_id,scopes,resource,family):
        now=time.time()
        # 签发新令牌时顺手清理过期数据，不引入定时任务。
        self._cleanup(c,now)
        access=secrets.token_urlsafe(32);refresh=secrets.token_urlsafe(32);joined=' '.join(scopes)
        c.execute('INSERT INTO oauth_tokens VALUES(?,?,?,?,?,?,?,?,0)',(digest(access),'access',client_id,user_id,family,joined,resource,now+ACCESS_TTL))
        c.execute('INSERT INTO oauth_tokens VALUES(?,?,?,?,?,?,?,?,0)',(digest(refresh),'refresh',client_id,user_id,family,joined,resource,now+REFRESH_TTL))
        return OAuthToken(access_token=access,token_type='Bearer',expires_in=ACCESS_TTL,scope=joined,refresh_token=refresh)

    async def load_refresh_token(self,client,refresh_token):
        def work():
            with self.store.connect() as c:
                row=c.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='refresh' AND client_id=? AND expires>?",(digest(refresh_token),client.client_id,time.time())).fetchone()
                if not row:return None
                if row['used']:
                    # 已用过的刷新令牌再次出现，说明可能被窃取或网络重放：作废同一令牌家族的全部令牌，并记审计。
                    c.execute('DELETE FROM oauth_tokens WHERE family=?',(row['family'],))
                    self.store.audit(c,None,row['user_id'],'oauth_refresh_reuse',client.client_id)
                    return None
                return RefreshToken(token=refresh_token,client_id=row['client_id'],scopes=row['scopes'].split(),expires_at=int(row['expires']),resource=row['resource'],subject=row['user_id'])
        return await anyio.to_thread.run_sync(work)

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        def work():
            with self.store.connect() as c:
                now=time.time()
                # 原子地把刷新令牌标记为已用并取回家族：并发的第二个请求更新不到任何行，视为重放，作废整个家族（包括第一个请求刚签发的新令牌）。
                row=c.execute("UPDATE oauth_tokens SET used=1 WHERE hash=? AND kind='refresh' AND client_id=? AND used=0 AND expires>? RETURNING family,user_id,resource",
                    (digest(refresh_token.token),client.client_id,now)).fetchone()
                if row:return self._issue(c,client.client_id,row['user_id'],scopes,row['resource'],row['family']),None
                old=c.execute("SELECT family,user_id FROM oauth_tokens WHERE hash=? AND kind='refresh' AND client_id=?",(digest(refresh_token.token),client.client_id)).fetchone()
                if old:
                    c.execute('DELETE FROM oauth_tokens WHERE family=?',(old['family'],))
                    # 审计必须与删除家族在同一事务提交，否则重放事件可能在回滚后丢记录。
                    self.store.audit(c,None,old['user_id'],'oauth_refresh_reuse',client.client_id)
                return None,TokenError('invalid_grant','刷新令牌已失效')
        # 抛出 TokenError 会让 store.connect() 回滚整个事务，删除家族的语句也会被回滚（且 TokenError 是冻结 dataclass，with 块内抛会触发 contextlib 回写 __traceback__ 失败）；因此失败分支先提交删除，退出 with 块之后再抛错。
        token,error=await anyio.to_thread.run_sync(work)
        if error:raise error
        return token

    async def load_access_token(self,token):
        def work():
            with self.store.connect() as c:
                row=c.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='access' AND expires>?",(digest(token),time.time())).fetchone()
                if not row:return None
                return AccessToken(token=token,client_id=row['client_id'],scopes=row['scopes'].split(),expires_at=int(row['expires']),resource=row['resource'],subject=row['user_id'])
        return await anyio.to_thread.run_sync(work)

    async def revoke_token(self,token):
        def work():
            with self.store.connect() as c:
                row=c.execute('SELECT family,user_id,client_id FROM oauth_tokens WHERE hash=?',(digest(token.token),)).fetchone()
                if row:
                    c.execute('DELETE FROM oauth_tokens WHERE family=?',(row['family'],))
                    self.store.audit(c,None,row['user_id'],'oauth_revoke',row['client_id'])
        await anyio.to_thread.run_sync(work)

    def connections(self,user_id):
        with self.store.connect() as c:
            # 只统计仍可用的刷新令牌：已轮换掉的（used=1）和已过期的不再算有效连接。
            rows=c.execute("SELECT client_id,max(expires) AS expires FROM oauth_tokens WHERE user_id=? AND kind='refresh' AND used=0 AND expires>? GROUP BY client_id ORDER BY expires DESC",(user_id,time.time())).fetchall()
            result=[]
            for r in rows:
                info=c.execute('SELECT info FROM oauth_clients WHERE client_id=?',(r['client_id'],)).fetchone()
                data=json.loads(info['info']) if info else {}
                uris=data.get('redirect_uris') or ['']
                result.append({'client_id':r['client_id'],'client_name':data.get('client_name') or '未命名应用','redirect_host':urlsplit(str(uris[0])).hostname,'expires':r['expires']})
            return result

    def disconnect(self,user_id,client_id):
        with self.store.connect() as c:
            # 未兑换的授权码也一并作废，否则断开后 5 分钟内仍可用旧授权码换新令牌。
            c.execute('DELETE FROM oauth_codes WHERE user_id=? AND client_id=?',(user_id,client_id))
            revoked=c.execute('DELETE FROM oauth_tokens WHERE user_id=? AND client_id=?',(user_id,client_id)).rowcount
            self.store.audit(c,None,user_id,'oauth_disconnect',client_id)
            return {'revoked':revoked}

class ConsentRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request:str=Field(min_length=20,max_length=128)
class ConsentApproval(ConsentRequest):
    email:str=Field(min_length=3,max_length=254)
    password:str=Field(min_length=1,max_length=1024)
    @field_validator('email')
    @classmethod
    def email_clean(cls,v):return v.strip().lower()

# 授权页禁止被嵌入其他页面（防点击劫持）、不缓存、不外传 Referer；脚本和样式都内联，所以只放开 inline。
CONSENT_HEADERS={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
    'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}

def install_oauth_routes(app,store,user,limited,backend,provider):
    @app.get('/.well-known/oauth-authorization-server')
    def authorization_server_metadata():
        # SDK 固定声明 client_secret_post/basic，但本服务只签发公共客户端；按实际行为声明 none，Claude 和 Codex 才会走公共客户端流程。
        meta=build_metadata(AnyHttpUrl(provider.public_url),None,ClientRegistrationOptions(enabled=True,valid_scopes=['memory'],default_scopes=['memory']),RevocationOptions(enabled=True))
        meta.token_endpoint_auth_methods_supported=['none'];meta.revocation_endpoint_auth_methods_supported=['none']
        # SDK 自带的元数据路由允许跨域读取（浏览器里的 OAuth 客户端需要），覆盖后保持一致。
        return JSONResponse(meta.model_dump(mode='json',exclude_none=True),headers={'Access-Control-Allow-Origin':'*'})
    @app.get('/oauth/consent',response_class=HTMLResponse)
    def consent():return HTMLResponse(Path(__file__).with_name('consent.html').read_text(),headers=CONSENT_HEADERS)
    @app.post('/oauth/consent/inspect',dependencies=[Depends(limited)])
    def inspect(body:ConsentRequest):return provider.inspect_request(body.request)
    @app.post('/oauth/consent/approve',dependencies=[Depends(limited)])
    def approve(body:ConsentApproval):
        # 先验证密码再消耗授权请求：输错密码可以重试，请求不会因此作废。
        try:uid=store.authenticate(body.email,body.password,backend)
        except HTTPException:raise
        except Exception:raise HTTPException(502,'身份服务暂时不可用')
        return {'redirect':provider.approve_request(body.request,uid)}
    @app.post('/oauth/consent/deny',dependencies=[Depends(limited)])
    def deny(body:ConsentRequest):return {'redirect':provider.deny_request(body.request)}
    @app.get('/auth/connections')
    def connections(u=Depends(user)):return provider.connections(u)
    @app.delete('/auth/connections/{client_id}')
    def disconnect(client_id:str,u=Depends(user)):return provider.disconnect(u,client_id)
