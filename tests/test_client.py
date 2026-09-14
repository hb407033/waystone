import json
import pytest
from waystone.client import Client,preview,private_write

def test_preview_offline_and_bounded(tmp_path):
    (tmp_path/'README.md').write_text('# 项目\n\n'+'中文资料'*200)
    plan=preview(tmp_path,['README.md'])
    assert all(len(x['content'])<=400 for x in plan['entries'])
    assert len(plan['entries'])>1
    assert not (tmp_path/'.waystone.json').exists()

def test_preview_refuses_escape_and_secrets(tmp_path):
    (tmp_path/'notes.md').write_text('api_key=abcdefghijklmnop')
    with pytest.raises(ValueError):preview(tmp_path,['notes.md'])
    with pytest.raises(ValueError):preview(tmp_path,['../outside.md'])
    with pytest.raises(ValueError):preview(tmp_path,['.env'])

def test_binding_cannot_redirect_session(tmp_path):
    profile=tmp_path/'session.json';private_write(profile,{'server':'http://localhost:18900','token':'not-real'})
    (tmp_path/'.waystone.json').write_text(json.dumps({'server':'https://attacker.invalid','project_id':'x'}))
    with pytest.raises(ValueError):Client(profile).binding(tmp_path)
    assert profile.stat().st_mode&0o777==0o600

def test_remote_http_forbidden(tmp_path):
    profile=tmp_path/'session.json';private_write(profile,{'server':'http://example.com'})
    with pytest.raises(ValueError):Client(profile)

def test_http_errors_do_not_echo_credentials(tmp_path,monkeypatch):
    import httpx
    monkeypatch.setenv('WAYSTONE_SERVER','https://memory.example.com')
    def request(*args,**kwargs):return httpx.Response(422,json={'detail':[{'input':'sensitive-example-password'}]})
    monkeypatch.setattr(httpx.Client,'request',request)
    c=Client(tmp_path/'missing')
    with pytest.raises(ValueError) as caught:c.request('POST','/auth/login',{},False)
    assert 'sensitive-example-password' not in str(caught.value)


def test_cli_preview_without_credentials_or_binding(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from waystone import cli
    (tmp_path / 'README.md').write_text('A durable project fact.')
    def forbidden():
        raise AssertionError('Offline preview must not construct an authenticated client')
    monkeypatch.setattr(cli, 'Client', forbidden)
    cli.run(Namespace(command='import', directory=str(tmp_path), files=['README.md'], preview_only=True))
    assert 'A durable project fact.' in capsys.readouterr().out


def test_cli_retract_requires_confirmation(tmp_path,monkeypatch):
    from argparse import Namespace
    from waystone import cli
    calls=[]
    class Fake:
        def project_path(self,directory):return '/projects/p1'
        def request(self,method,path,data=None,authenticated=True):calls.append((method,path));return {'status':'retracted'}
    monkeypatch.setattr(cli,'Client',Fake)
    monkeypatch.setattr('builtins.input',lambda prompt:'no')
    with pytest.raises(ValueError):cli.run(Namespace(command='retract',directory=str(tmp_path),entry_id='e1'))
    assert calls==[]
    monkeypatch.setattr('builtins.input',lambda prompt:'yes')
    cli.run(Namespace(command='retract',directory=str(tmp_path),entry_id='e1'))
    assert calls==[('POST','/projects/p1/entries/e1/retract')]


def test_cli_resolve_without_expected_sends_null(tmp_path,monkeypatch):
    from argparse import Namespace
    from waystone import cli
    calls=[]
    class Fake:
        def project_path(self,directory):return '/projects/p1'
        def request(self,method,path,data=None,authenticated=True):calls.append((method,path,data));return []
    monkeypatch.setattr(cli,'Client',Fake)
    monkeypatch.setattr('builtins.input',lambda prompt:'yes')
    cli.run(Namespace(command='resolve',directory=str(tmp_path),entry_id='e1',expected=''))
    assert calls[-1]==('POST','/projects/p1/entries/e1/resolve',{'expected_id':None})


def test_proxy_environment_is_opt_in(tmp_path,monkeypatch):
    import httpx
    monkeypatch.setenv('WAYSTONE_SERVER','https://memory.example.com')
    seen=[]
    class Recorder(httpx.Client):
        def __init__(self,*args,**kwargs):seen.append(kwargs['trust_env']);super().__init__(*args,**kwargs)
        def request(self,*args,**kwargs):return httpx.Response(200,json={'ok':True})
    monkeypatch.setattr(httpx,'Client',Recorder)
    # 清掉本机代理变量：trust_env=True 时 httpx 会读取它们，SOCKS 代理缺依赖会在构造时报错，干扰本测试。
    for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):monkeypatch.delenv(k,raising=False)
    c=Client(tmp_path/'missing')
    monkeypatch.delenv('WAYSTONE_TRUST_ENV',raising=False)
    c.request('GET','/health',authenticated=False)
    monkeypatch.setenv('WAYSTONE_TRUST_ENV','1')
    c.request('GET','/health',authenticated=False)
    assert seen==[False,True]


def test_network_errors_become_readable(tmp_path,monkeypatch):
    import httpx
    monkeypatch.setenv('WAYSTONE_SERVER','https://memory.example.com')
    def request(*args,**kwargs):raise httpx.ConnectError('boom')
    monkeypatch.setattr(httpx.Client,'request',request)
    with pytest.raises(ValueError) as caught:Client(tmp_path/'missing').request('GET','/health',authenticated=False)
    assert '无法连接项目记忆服务' in str(caught.value) and 'WAYSTONE_TRUST_ENV' in str(caught.value)


def test_unsupported_proxy_is_readable(tmp_path,monkeypatch):
    import httpx
    monkeypatch.setenv('WAYSTONE_SERVER','https://memory.example.com')
    class Broken(httpx.Client):
        def __init__(self,*args,**kwargs):raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
    monkeypatch.setattr(httpx,'Client',Broken)
    monkeypatch.setenv('WAYSTONE_TRUST_ENV','1')
    with pytest.raises(ValueError) as caught:Client(tmp_path/'missing').request('GET','/health',authenticated=False)
    assert 'HTTP(S) 代理' in str(caught.value)


def test_client_requires_explicit_server(tmp_path,monkeypatch):
    monkeypatch.delenv('WAYSTONE_SERVER',raising=False)
    with pytest.raises(ValueError) as caught:Client(tmp_path/'missing').request('GET','/health',authenticated=False)
    assert 'WAYSTONE_SERVER' in str(caught.value)


def test_cli_login_requires_server(tmp_path,monkeypatch):
    from argparse import Namespace
    from waystone import cli
    monkeypatch.delenv('WAYSTONE_SERVER',raising=False)
    monkeypatch.setenv('WAYSTONE_PROFILE',str(tmp_path/'session.json'))
    with pytest.raises(ValueError) as caught:cli.run(Namespace(command='login',directory=str(tmp_path),server='',password_login=False))
    assert '--server' in str(caught.value)
