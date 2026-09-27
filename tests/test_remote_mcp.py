"""远程 MCP 端到端：真实 uvicorn + MCP SDK 的 OAuth 客户端，走一遍 Claude 连接器会用到的注册、授权、调用全流程。"""
import asyncio
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse
import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata
from waystone.api import create_app
from test_service import Backend

class Memory(TokenStorage):
    def __init__(self):self.tokens=None;self.client=None
    async def get_tokens(self):return self.tokens
    async def set_tokens(self,tokens):self.tokens=tokens
    async def get_client_info(self):return self.client
    async def set_client_info(self,info):self.client=info

def test_sdk_oauth_client_end_to_end(tmp_path):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];url=f'http://127.0.0.1:{port}'
    app=create_app(str(tmp_path/'db.sqlite'),Backend(),public_url=url)
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(200):
            if server.started:break
            time.sleep(.02)
        assert server.started
        with httpx.Client(base_url=url) as h:
            session=h.post('/auth/login',json={'email':'owner@example.com','password':'test-password-123'}).json()['token']
            p=h.post('/projects',json={'name':'Alpha'},headers={'Authorization':'Bearer '+session}).json()['id']
        callback={}
        async def redirect_handler(authorization_url):
            # 模拟用户浏览器：打开授权地址 → 授权页核对应用 → 输入密码批准 → 从回调地址取出授权码。
            async with httpx.AsyncClient() as browser:
                r=await browser.get(authorization_url,follow_redirects=False)
                assert r.status_code==302,r.text
                request=r.headers['location'].split('#',1)[1]
                info=(await browser.post(url+'/oauth/consent/inspect',json={'request':request})).json()
                assert info['client_name']=='pytest remote' and info['loopback'] is True
                d=(await browser.post(url+'/oauth/consent/approve',json={'request':request,'email':'owner@example.com','password':'test-password-123'})).json()
                q=parse_qs(urlparse(d['redirect']).query);callback['code']=q['code'][0];callback['state']=q.get('state',[None])[0]
        async def callback_handler():return callback['code'],callback['state']
        storage=Memory()
        async def check():
            auth=OAuthClientProvider(server_url=url+'/mcp',client_metadata=OAuthClientMetadata(client_name='pytest remote',redirect_uris=['http://127.0.0.1:53682/callback'],grant_types=['authorization_code','refresh_token'],response_types=['code'],token_endpoint_auth_method='none'),storage=storage,redirect_handler=redirect_handler,callback_handler=callback_handler)
            async with httpx.AsyncClient(auth=auth,timeout=30) as http:
                async with streamable_http_client(url+'/mcp',http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as s:
                        await s.initialize()
                        names={t.name for t in (await s.list_tools()).tools}
                        assert names=={'project_list','memory_recall','memory_entries','memory_publish','memory_resolve','memory_rebase','memory_reject','memory_retract','memory_reindex'}
                        listed=await s.call_tool('project_list',{})
                        assert not listed.isError and p in str(listed.content)
                        pub=await s.call_tool('memory_publish',{'project_id':p,'content':'远程连接器默认使用中文回复','topic':'agent/reply-language','source':'pytest','kind':'convention'})
                        assert not pub.isError and 'active' in str(pub.content),pub
                        found=await s.call_tool('memory_recall',{'project_id':p,'query':'回复语言'})
                        assert not found.isError and '远程连接器默认使用中文回复' in str(found.content)
                        page=await s.call_tool('memory_entries',{'project_id':p})
                        assert not page.isError and 'agent/reply-language' in str(page.content)
                        secret=await s.call_tool('memory_publish',{'project_id':p,'content':'api_key = abcdefghijklmnopqrstuvwxyz','topic':'x/y','source':'pytest','kind':'background'})
                        assert secret.isError and '凭据' in str(secret.content)
                        denied=await s.call_tool('memory_recall',{'project_id':'0'*32,'query':'x'})
                        assert denied.isError and '权限' in str(denied.content)
                        missing_kind=await s.call_tool('memory_publish',{'project_id':p,'content':'缺少类型','topic':'x/z','source':'pytest'})
                        assert missing_kind.isError
        asyncio.run(check())
        with app.state.store.connect() as db:
            row=db.execute("SELECT agent,kind FROM entries WHERE topic='agent/reply-language'").fetchone()
        assert (row['agent'],row['kind'])==('remote-mcp','convention')
        # Claude 和 Codex 以公共客户端注册：注册结果不能带 client secret。
        assert storage.client.token_endpoint_auth_method=='none' and storage.client.client_secret is None
    finally:
        server.should_exit=True;thread.join(10)
        assert not thread.is_alive()
