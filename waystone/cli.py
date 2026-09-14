import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from .client import Client, preview


def scope_args(args):
    return {k:getattr(args,k,'') for k in ('environment','branch','source_version')}

def emit(data):print(json.dumps(data,ensure_ascii=False,indent=2))
def confirm():
    if input('确认将上述内容发布到项目记忆服务？输入 yes：').strip()!='yes':raise ValueError('已取消，未上传')

def main():
    parser=argparse.ArgumentParser(description='项目记忆：初始化、邀请加入、预览导入和交接')
    parser.add_argument('--directory',default='.')
    parser.add_argument('--environment',default='')
    parser.add_argument('--branch',default='')
    parser.add_argument('--source-version',default='')
    commands=parser.add_subparsers(dest='command',required=True)
    login=commands.add_parser('login');login.add_argument('--server',default=os.getenv('WAYSTONE_SERVER',''));login.add_argument('--password-login',action='store_true')
    commands.add_parser('logout');commands.add_parser('projects');commands.add_parser('status')
    init=commands.add_parser('init');init.add_argument('name')
    bind=commands.add_parser('bind');bind.add_argument('project_id')
    invite=commands.add_parser('invite');invite.add_argument('email');invite.add_argument('--role',choices=['reader','collaborator'],default='collaborator')
    commands.add_parser('join')
    imp=commands.add_parser('import');imp.add_argument('files',nargs='+');imp.add_argument('--preview-only',action='store_true')
    for name in ['save','handoff']:
        s=commands.add_parser(name);s.add_argument('topic');s.add_argument('--file',required=True);s.add_argument('--source',required=True)
    recall=commands.add_parser('recall');recall.add_argument('query')
    for name in ['members','audit','archive']:commands.add_parser(name)
    entries=commands.add_parser('entries');entries.add_argument('--cursor',default='');entries.add_argument('--limit',type=int,default=200)
    reindex=commands.add_parser('reindex');reindex.add_argument('--full',action='store_true');reindex.add_argument('--cursor',default='');reindex.add_argument('--limit',type=int,default=5)
    rebase=commands.add_parser('rebase');rebase.add_argument('entry_id');rebase.add_argument('--expected',required=True)
    reject=commands.add_parser('reject');reject.add_argument('entry_id')
    retract=commands.add_parser('retract');retract.add_argument('entry_id')
    remove=commands.add_parser('remove-member');remove.add_argument('user_id')
    # 当前没有有效版本（旧版本已撤回或过期）时省略 --expected，服务端要求所有者显式确认。
    resolve=commands.add_parser('resolve');resolve.add_argument('entry_id');resolve.add_argument('--expected',default='')
    args=parser.parse_args()
    try:run(args)
    except (ValueError,OSError) as e:
        print(str(e),file=sys.stderr);raise SystemExit(1)

def run(args):
    cmd=args.command;directory=args.directory
    if cmd=='import' and args.preview_only:
        plan=preview(directory,args.files)
        for e in plan['entries']:e.update(scope_args(args))
        emit(plan);return
    c=Client()
    if cmd=='login':
        if not args.server:raise ValueError('login 需要 --server <服务地址>，或设置 WAYSTONE_SERVER')
        # 显式选择服务后才发送用户在终端输入的密码。
        c.config={'server':args.server};c.url=args.server.rstrip('/')
        from urllib.parse import urlparse
        parsed=urlparse(c.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme!='https' and not(parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'})):raise ValueError('使用 HTTPS 或本地 SSH 隧道地址')
        if args.password_login:
            result=c.request('POST','/auth/login',{'email':input('邮箱：'),'password':getpass.getpass('密码（隐藏）：')},False)
        else:
            import socket,time,webbrowser
            from urllib.parse import urlencode
            started=c.request('POST','/device/start',{'label':socket.gethostname()},False)
            url=c.url+'/activate?'+urlencode({'code':started['user_code']})
            print('请在浏览器核对设备并登录授权：'+url,flush=True)
            print('授权码：'+started['user_code'],flush=True)
            webbrowser.open(url)
            deadline=time.monotonic()+started['expires_in']
            while time.monotonic()<deadline:
                time.sleep(started['interval'])
                result=c.request('POST','/device/poll',{'device_code':started['device_code']},False)
                if result.get('token'):break
            else:raise ValueError('授权超时，请重新登录')
        c.session(result);print('登录成功；会话仅保存在本机私有配置中，有效期 7 天。');return
    if cmd=='logout':
        c.request('POST','/auth/logout');c.profile.unlink();print('已退出');return
    if cmd=='projects':emit(c.request('GET','/projects'));return
    if cmd=='init':
        if (Path(directory)/'.waystone.json').exists():raise ValueError('已存在项目绑定，请先运行 status')
        result=c.request('POST','/projects',{'name':args.name});emit(c.bind(directory,result['id']));return
    if cmd=='bind':emit(c.bind(directory,args.project_id));return
    if cmd=='join':
        token=getpass.getpass('邀请代码（隐藏）：')
        if c.config.get('token'):
            result=c.request('POST','/invites/accept',{'invite':token})
        else:
            result=c.request('POST','/auth/join',{'invite':token,'email':input('受邀邮箱：'),'name':input('姓名：'),'password':getpass.getpass('设置密码（至少 12 位）：')},False)
        c.session(result);emit(c.bind(directory,result['project_id']));return
    if cmd=='status':emit(c.binding(directory));return
    base=c.project_path(directory)
    if cmd=='invite':
        result=c.request('POST',base+'/invites',{'email':args.email,'role':args.role});result['invite_url']=c.url+'/invite#'+result['invite'];emit(result);return
    if cmd in {'members','audit'}:emit(c.request('GET',base+'/'+cmd));return
    if cmd=='recall':emit(c.request('POST',base+'/recall',{'query':args.query,'environment':args.environment,'branch':args.branch}));return
    if cmd=='entries':
        from urllib.parse import urlencode
        emit(c.request('GET',base+'/entries/page?'+urlencode({'cursor':args.cursor,'limit':args.limit})));return
    if cmd=='reindex':emit(c.request('POST',base+'/reindex',{'full':args.full,'cursor':args.cursor,'limit':args.limit}));return
    if cmd in {'rebase','reject'}:
        rows=c.request('GET',base+'/entries');emit([r for r in rows if r['id'] in {args.entry_id,getattr(args,'expected',None)}]);confirm()
        emit(c.request('POST',base+'/entries/'+args.entry_id+'/'+cmd,{'expected_id':args.expected} if cmd=='rebase' else None));return
    if cmd=='retract':
        print('将撤回记录并永久抹除正文、删除向量，不可恢复：'+args.entry_id);confirm();emit(c.request('POST',base+'/entries/'+args.entry_id+'/retract'));return
    if cmd=='remove-member':
        print('将移除成员：'+args.user_id);confirm();emit(c.request('DELETE',base+'/members/'+args.user_id));return
    if cmd=='archive':
        print('将归档项目，停止新增记忆，保留历史。');confirm();emit(c.request('POST',base+'/archive'));return
    if cmd=='resolve':
        rows=c.request('GET',base+'/entries');emit([r for r in rows if r['id'] in {args.entry_id,args.expected}]);confirm()
        emit(c.request('POST',base+'/entries/'+args.entry_id+'/resolve',{'expected_id':args.expected or None}));return
    if cmd=='import':
        plan=preview(directory,args.files)
        for e in plan['entries']:e.update(scope_args(args))
        emit(plan)
        if args.preview_only:return
        confirm();emit([c.request('POST',base+'/entries',e) for e in plan['entries']]);return
    if cmd in {'save','handoff'}:
        plan=preview(directory,[args.file])
        entries=plan['entries']
        for i,e in enumerate(entries):e.update(topic=args.topic if len(entries)==1 else f'{args.topic}:{i+1}',source=args.source,kind='handoff' if cmd=='handoff' else 'decision',agent='cli')
        for e in entries:e.update(scope_args(args))
        emit(entries);confirm();emit([c.request('POST',base+'/entries',e) for e in entries]);return

if __name__=='__main__':main()
