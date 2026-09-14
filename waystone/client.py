import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .sensitive import looks_like_secret


def private_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2)
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

class Client:
    def __init__(self,profile=None):
        self.profile=Path(profile or os.getenv('WAYSTONE_PROFILE',Path.home()/'.config/waystone/session.json'))
        self.config=json.loads(self.profile.read_text()) if self.profile.exists() else {}
        # 不内置默认服务地址：先用 login --server 保存到本机会话，或通过 WAYSTONE_SERVER 指定。
        self.url=(self.config.get('server') or os.getenv('WAYSTONE_SERVER','')).rstrip('/')
        parsed=urlparse(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('服务地址不能包含凭据或查询参数')
        if self.url and parsed.scheme!='https' and not(parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'}):raise ValueError('远程服务需要 HTTPS，本地可通过 SSH 隧道访问')
    def request(self,method,path,data=None,authenticated=True):
        if not self.url:raise ValueError('尚未配置服务地址：运行 waystone login --server <地址>，或设置 WAYSTONE_SERVER')
        headers={}
        if authenticated:
            if not self.config.get('token'):raise ValueError('先运行 waystone login')
            headers['Authorization']='Bearer '+self.config['token']
        # 默认保持原行为，不读取代理和证书环境变量；需要经代理或自定义 CA 访问的网络，由用户设置 WAYSTONE_TRUST_ENV=1 显式启用。
        try:
            with httpx.Client(timeout=90,trust_env=os.getenv('WAYSTONE_TRUST_ENV')=='1',follow_redirects=False) as c:r=c.request(method,self.url+path,json=data,headers=headers)
        except ImportError as e:
            # httpx 遇到 SOCKS 代理且未安装 socksio 时在构造阶段抛 ImportError；提示里不带代理地址，避免泄露其中的凭据。
            raise ValueError('当前客户端只支持 HTTP(S) 代理；检测到 SOCKS 等不受支持的代理设置，请改用 HTTP(S) 代理或取消 WAYSTONE_TRUST_ENV') from e
        except httpx.HTTPError as e:
            # 转成 ValueError，CLI 统一打印一行可读错误，而不是抛出 Python 堆栈。
            raise ValueError(f'无法连接项目记忆服务（{type(e).__name__}）；需要代理或自定义证书时设置 WAYSTONE_TRUST_ENV=1') from e
        if r.status_code>=300:
            try:message=r.json().get('detail','请求失败')
            except Exception:message='请求失败'
            if not isinstance(message,str):message='参数无效，请检查输入'
            raise ValueError(f'{r.status_code}: {message}')
        return r.json()
    def session(self,data):
        self.config={'server':self.url,'token':data['token'],'user_id':data['user_id']}
        private_write(self.profile,self.config)
    def binding(self,directory='.'):
        path=Path(directory).resolve()
        for root in [path,*path.parents]:
            file=root/'.waystone.json'
            if file.exists():
                data=json.loads(file.read_text())
                # 仓库文件不决定凭据发送位置；必须与用户的已登录服务一致。
                if data.get('server')!=self.url:raise ValueError('项目服务地址与本机登录配置不同，请先核对并登录对应服务')
                return data
        raise ValueError('当前目录尚未绑定项目，运行 init 或 bind')
    def bind(self,directory,project):
        projects=self.request('GET','/projects')
        item=next((p for p in projects if p['id']==project),None)
        if not item:raise ValueError('你不是此项目成员')
        dest=Path(directory).resolve()/'.waystone.json'
        data={'version':1,'project_id':project,'name':item['name'],'server':self.url}
        if dest.exists() and json.loads(dest.read_text()).get('project_id')!=project:raise ValueError('目录已绑定其他项目，不会覆盖')
        private_write(dest,data)
        return data
    def project_path(self,directory='.'):
        return '/projects/'+self.binding(directory)['project_id']


def preview(directory,files):
    """只处理明确指定的文本文件；此函数完全离线。"""
    root=Path(directory).resolve();result=[]
    for filename in files:
        path=(root/filename).resolve()
        if not path.is_relative_to(root):raise ValueError('导入文件必须位于项目目录内')
        if any(p.startswith('.') for p in path.relative_to(root).parts) or path.suffix.lower() not in {'.md','.txt'}:raise ValueError('仅支持显式指定的非隐藏 Markdown/TXT 文件')
        if path.stat().st_size>256_000:raise ValueError('单个文件上限 256 KB，请先整理')
        text=path.read_text()
        if looks_like_secret(text):
            raise ValueError(f'{filename} 疑似包含凭据，请先移除；未上传任何内容')
        heading='概述';count=0
        for paragraph in re.split(r'\n\s*\n',text):
            paragraph=paragraph.strip()
            if not paragraph:continue
            if paragraph.startswith('#'):heading=paragraph.splitlines()[0].lstrip('# ').strip()
            for start in range(0,len(paragraph),400):
                count+=1
                result.append({'content':paragraph[start:start+400],'topic':f'{filename}:{heading}:{count}'[:200],
                    'source':str(path.relative_to(root)),'kind':'background','agent':'import'})
    if len(result)>100:raise ValueError('每次最多 100 条，请缩小导入范围')
    return {'entries':result,'notice':'这是本地预览，尚未上传。请核对秘密、过期内容及正确性；同主题变更会成为冲突提案。'}
