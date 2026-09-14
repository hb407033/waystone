"""限流按真实来源 IP 计数：部署配置必须把可信来源 IP 传进容器，uvicorn 按同样的环境变量采信它。"""
import re,socket,subprocess,threading,time
from pathlib import Path
import httpx,pytest,uvicorn
from waystone.api import create_app
from test_service import Backend

ROOT=Path(__file__).resolve().parents[1]
# 取自 https://www.cloudflare.com/ips-v4 与 ips-v6（2026-09-14）。
CLOUDFLARE=['173.245.48.0/20','103.21.244.0/22','103.22.200.0/22','103.31.4.0/22','141.101.64.0/18','108.162.192.0/18','190.93.240.0/20','188.114.96.0/20','197.234.240.0/22','198.41.128.0/17','162.158.0.0/15','104.16.0.0/13','104.24.0.0/14','172.64.0.0/13','131.0.72.0/22',
    '2400:cb00::/32','2606:4700::/32','2803:f800::/32','2405:b500::/32','2405:8100::/32','2a06:98c0::/29','2c0f:f248::/32']

def test_deploy_passes_real_client_ip():
    compose=(ROOT/'deploy/compose.yaml').read_text()
    caddy=(ROOT/'deploy/Caddyfile.example').read_text()
    assert "FORWARDED_ALLOW_IPS: '*'" in compose
    assert 'header CF-Connecting-IP *' in caddy
    assert 'header_up X-Forwarded-For {http.request.header.CF-Connecting-IP}' in caddy
    assert 'header_up X-Forwarded-For {remote_host}' in caddy
    for net in CLOUDFLARE:assert net in caddy

def test_rate_limit_keys_on_forwarded_client(tmp_path,monkeypatch):
    # 与 deploy/compose.yaml 相同的取值；uvicorn 在创建 Config 时读取它。
    monkeypatch.setenv('FORWARDED_ALLOW_IPS','*')
    app=create_app(str(tmp_path/'db.sqlite'),Backend())
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        assert server.started
        bad={'email':'nobody@example.com','password':'wrong-password'}
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as c:
            codes=[c.post('/auth/login',json=bad,headers={'X-Forwarded-For':'203.0.113.10'}).status_code for _ in range(21)]
            assert codes[:20]==[401]*20 and codes[20]==429
            assert c.post('/auth/login',json=bad,headers={'X-Forwarded-For':'203.0.113.20'}).status_code==401
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()

def caddy_image_available():
    try:return subprocess.run(['docker','image','inspect','caddy:2'],capture_output=True,timeout=20).returncode==0
    except (OSError,subprocess.TimeoutExpired):return False

@pytest.mark.skipif(not caddy_image_available(),reason='需要 docker 与本地 caddy:2 镜像（docker pull caddy:2）')
def test_caddy_overrides_forwarded_for(tmp_path):
    # 取 Caddyfile 中 forwarding-begin/end 之间的真实配置，把 Cloudflare 网段换成测试网段后在容器里实跑。
    # 8080 端口模拟“对端属于 Cloudflare”，8081 端口模拟“绕过 Cloudflare 直连”；8900 端口回显服务收到的 X-Forwarded-For。
    block=(ROOT/'deploy/Caddyfile.example').read_text().split('# forwarding-begin')[1].split('# forwarding-end')[0]
    ranges=re.search(r'remote_ip ([^\n]+)',block).group(1)
    def site(port,trusted):return f':{port} {{\n'+block.replace(ranges,trusted)+'}\n'
    config='{\n    auto_https off\n    admin off\n}\n'+site(8080,'127.0.0.1/32')+site(8081,'10.255.255.0/24')+':8900 {\n    respond "XFF=[{header.X-Forwarded-For}]"\n}\n'
    (tmp_path/'Caddyfile').write_text(config)
    script="""caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/tmp/caddy.log 2>&1 &
for i in $(seq 50); do wget -qO- http://127.0.0.1:8900/ >/dev/null 2>&1 && break; sleep 0.1; done
q(){ wget -qO- "$@"; echo; }
q --header "X-Forwarded-For: 6.6.6.6" --header "CF-Connecting-IP: 203.0.113.7" http://127.0.0.1:8080/
q --header "X-Forwarded-For: 6.6.6.6" --header "X-Forwarded-For: 7.7.7.7" --header "CF-Connecting-IP: 203.0.113.7" http://127.0.0.1:8080/
q --header "X-Forwarded-For: 6.6.6.6" http://127.0.0.1:8080/
q --header "X-Forwarded-For: 6.6.6.6" --header "CF-Connecting-IP: 9.9.9.9" http://127.0.0.1:8081/
q --header "CF-Connecting-IP: 2001:db8::1" http://127.0.0.1:8080/
"""
    r=subprocess.run(['docker','run','--rm','-v',f'{tmp_path/"Caddyfile"}:/etc/caddy/Caddyfile:ro','caddy:2','sh','-c',script],capture_output=True,text=True,timeout=120)
    # 依次是：CF 回源伪造 XFF；CF 回源重复 XFF；CF 回源缺 CF 头；直连伪造 CF 头；CF 回源 IPv6。
    assert r.stdout.split()==['XFF=[203.0.113.7]','XFF=[203.0.113.7]','XFF=[127.0.0.1]','XFF=[127.0.0.1]','XFF=[2001:db8::1]'],r.stdout+r.stderr
