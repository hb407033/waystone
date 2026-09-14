import asyncio
import sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

def test_real_stdio_tools_and_offline_preview(tmp_path):
    (tmp_path/'README.md').write_text('项目约定默认使用中文。')
    async def check():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','waystone.mcp_server'])) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools=await session.list_tools()
                assert {'project_init','memory_preview','memory_recall','memory_publish','memory_retract'} <= {t.name for t in tools.tools}
                result=await session.call_tool('memory_preview',{'directory':str(tmp_path),'files':['README.md']})
                assert not result.isError
                assert '项目约定默认使用中文' in str(result)
    asyncio.run(check())

def test_mcp_to_http_project_roundtrip(tmp_path):
    import socket, threading, time, os, json
    import httpx, uvicorn
    from waystone.api import create_app
    from waystone.client import private_write
    from test_service import Backend
    backend=Backend();app=create_app(str(tmp_path/'db'),backend)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        assert server.started
        url=f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=url) as c:
            session=c.post('/auth/login',json={'email':'owner@example.com','password':'test-password-123'}).json()
        profile=tmp_path/'session.json';private_write(profile,{'server':url,'token':session['token']})
        directory=tmp_path/'repo';directory.mkdir()
        async def check():
            params=StdioServerParameters(command=sys.executable,args=['-m','waystone.mcp_server'],env={**os.environ,'WAYSTONE_PROFILE':str(profile)})
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as s:
                    await s.initialize()
                    created=await s.call_tool('project_init',{'name':'MCP验收','directory':str(directory)})
                    assert not created.isError
                    published=await s.call_tool('memory_publish',{'directory':str(directory),'content':'项目统一使用中文','topic':'language','source':'user-confirmed'})
                    assert not published.isError
                    recalled=await s.call_tool('memory_recall',{'directory':str(directory),'query':'项目语言'})
                    assert not recalled.isError and '项目统一使用中文' in str(recalled)
        asyncio.run(check())
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
