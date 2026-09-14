"""事务化项目权限和可追溯记忆；向量库只是可重建的检索索引。"""
import json
import re
import hashlib
import hmac
import secrets
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from fastapi import HTTPException


def fail(status, message): raise HTTPException(status, message)
def ident(): return uuid.uuid4().hex
def digest(text): return hashlib.sha256(text.encode()).hexdigest()
def normalized(text): return ' '.join(unicodedata.normalize('NFKC',text).split())
def topic_key(text):
    return '/'.join(re.sub(r'\s+', '-', part.strip().casefold()) for part in unicodedata.normalize('NFKC',text).split('/'))
RETRACTED='[已撤回]'

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + value

def check_password(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT,upstream TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,archived INTEGER DEFAULT 0,created REAL);
            CREATE TABLE IF NOT EXISTS members(project TEXT REFERENCES projects(id),user_id TEXT REFERENCES users(id),role TEXT NOT NULL,PRIMARY KEY(project,user_id));
            CREATE TABLE IF NOT EXISTS invites(hash TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),email TEXT,role TEXT,expires REAL,used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),topic TEXT,content TEXT,hash TEXT,kind TEXT,source TEXT,agent TEXT,author TEXT REFERENCES users(id),status TEXT,index_status TEXT,supersedes TEXT,created REAL,expires_at REAL,UNIQUE(project,hash));
            CREATE TABLE IF NOT EXISTS devices(hash TEXT PRIMARY KEY,code_hash TEXT UNIQUE,label TEXT,expires REAL,user_id TEXT REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,project TEXT,actor TEXT,action TEXT,target TEXT,created REAL);
            ''')
        # Additive fields preserve legacy content; never guess old scope or rewrite its topics.
        with self.connect() as c:
            columns={r['name'] for r in c.execute('PRAGMA table_info(entries)')}
            for field in ('environment','branch','source_version'):
                if field not in columns:c.execute(f"ALTER TABLE entries ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
            c.execute('DROP INDEX IF EXISTS active_topic')
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_scoped_topic ON entries(project,topic,environment,branch) WHERE status='active'")
        Path(path).chmod(0o600)

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=20)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()

    def audit(self,c,p,u,action,target):
        c.execute('INSERT INTO audit(project,actor,action,target,created) VALUES(?,?,?,?,?)',(p,u,action,target,time.time()))

    def session(self,c,uid):
        token=secrets.token_urlsafe(40)
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),uid,time.time()+86400*7))
        return {'token':token,'user_id':uid,'expires_in':86400*7}

    def identity(self,token):
        with self.connect() as c:
            row=c.execute('SELECT user_id FROM sessions WHERE hash=? AND expires>?',(digest(token),time.time())).fetchone()
            if not row:fail(401,'请登录项目记忆服务')
            return row['user_id']

    def login(self,email,password,backend):
        with self.connect() as c:
            row=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            if row and row['password']:
                if not check_password(password,row['password']):fail(401,'账号或密码不正确')
                return self.session(c,row['id'])
        # 上游管理员密码只用于认证请求，不写入项目数据库。
        upstream=backend.authenticate_admin(email,password)
        if not upstream:fail(401,'账号或密码不正确')
        with self.connect() as c:
            row=c.execute('SELECT * FROM users WHERE upstream=?',(str(upstream['id']),)).fetchone()
            if not row:
                if c.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone():fail(409,'账号身份冲突')
                uid=ident()
                c.execute('INSERT INTO users VALUES(?,?,?,?,?)',(uid,email,upstream['name'],None,str(upstream['id'])))
            else:uid=row['id']
            return self.session(c,uid)

    def acl(self,c,p,u,roles=None,write=False):
        row=c.execute('SELECT m.role,p.archived FROM members m JOIN projects p ON p.id=m.project WHERE m.project=? AND m.user_id=?',(p,u)).fetchone()
        if not row or (roles and row['role'] not in roles):fail(403,'没有此项目的访问权限')
        if write and row['archived']:fail(409,'项目已归档')
        return row['role']

    def expire_due(self,c,p):
        # 在调用方事务内把到期的有效记录和提案标记为 expired：读写接口看到的状态一致，提案不会过期后仍卡在 proposed。
        # 审计 actor 记为 system：过期由时间触发，不是恰好发起本次请求的成员所为。
        now=time.time()
        for r in c.execute("SELECT id FROM entries WHERE project=? AND status IN ('active','proposed') AND expires_at IS NOT NULL AND expires_at<=?",(p,now)).fetchall():
            c.execute("UPDATE entries SET status='expired' WHERE id=?",(r['id'],))
            self.audit(c,p,'system','expire',r['id'])

    def create(self,u,name):
        p=ident()
        with self.connect() as c:
            c.execute('INSERT INTO projects VALUES(?,?,0,?)',(p,name,time.time()))
            c.execute('INSERT INTO members VALUES(?,?,?)',(p,u,'owner'))
            self.audit(c,p,u,'create',p)
        return {'id':p,'name':name}

    def projects(self,u):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT p.*,m.role FROM projects p JOIN members m ON p.id=m.project WHERE m.user_id=?',(u,))]

    def invite(self,p,u,email,role,hours):
        token=secrets.token_urlsafe(32)
        with self.connect() as c:
            self.acl(c,p,u,{'owner'},True)
            c.execute('INSERT INTO invites VALUES(?,?,?,?,?,0)',(digest(token),p,email,role,time.time()+hours*3600))
            self.audit(c,p,u,'invite',email)
        return {'invite':token,'project_id':p,'expires_hours':hours}

    def join(self,token,email,name,password,uid=None):
        with self.connect() as c:
            inv=c.execute('SELECT * FROM invites WHERE hash=? AND used=0 AND expires>?',(digest(token),time.time())).fetchone()
            if not inv or inv['email']!=email:fail(400,'邀请无效、过期或邮箱不匹配')
            if c.execute('SELECT archived FROM projects WHERE id=?',(inv['project'],)).fetchone()[0]:fail(409,'项目已归档')
            user=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            if uid:
                if not user or user['id']!=uid:fail(400,'当前身份与邀请邮箱不匹配')
            else:
                if user:fail(409,'账号已存在，请先登录再接受邀请')
                uid=ident()
                c.execute('INSERT INTO users VALUES(?,?,?,?,NULL)',(uid,email,name,password_hash(password)))
            if c.execute('SELECT 1 FROM members WHERE project=? AND user_id=?',(inv['project'],uid)).fetchone():fail(409,'已经是项目成员')
            c.execute('INSERT INTO members VALUES(?,?,?)',(inv['project'],uid,inv['role']))
            c.execute('UPDATE invites SET used=1 WHERE hash=?',(digest(token),))
            self.audit(c,inv['project'],uid,'join',uid)
            return {**self.session(c,uid),'project_id':inv['project']}

    def members(self,p,u):
        with self.connect() as c:
            self.acl(c,p,u)
            return [dict(r) for r in c.execute('SELECT u.id,u.name,u.email,m.role FROM users u JOIN members m ON u.id=m.user_id WHERE m.project=?',(p,))]

    def remove_member(self,p,u,target):
        with self.connect() as c:
            self.acl(c,p,u,{'owner'},True)
            row=c.execute('SELECT role FROM members WHERE project=? AND user_id=?',(p,target)).fetchone()
            if not row:fail(404,'成员不存在')
            if row['role']=='owner':fail(409,'不能移除项目所有者')
            c.execute('DELETE FROM members WHERE project=? AND user_id=?',(p,target))
            self.audit(c,p,u,'remove_member',target)
        return {'ok':True}

    def save(self,p,u,data):
        data={**data,'topic':topic_key(data['topic'])}
        scope=tuple(data.get(k,'') for k in ('environment','branch','source_version'))
        with self.connect() as c:
            self.acl(c,p,u,{'owner','collaborator'},True)
            self.expire_due(c,p)
            # Match legacy spelling without rewriting old records or bypassing their conflict gate.
            matches=[r for r in c.execute("SELECT * FROM entries WHERE project=? AND environment=? AND branch=? AND status='active'",(p,scope[0],scope[1])) if topic_key(r['topic'])==data['topic']]
            if len(matches)>1:fail(409,'存在多个历史主题别名，请先人工核对，不能自动选择')
            predecessor=matches[0] if matches else None
            # 重试去重：同主题、同范围、同来源版本、同正文的有效记录或待确认提案已存在就直接返回。
            # 不沿哈希链查找：链上记录被撤回后哈希会被替换，链会断。已过期、被替代、被拒绝或已撤回的记录不拦截，保存会生成新记录。
            for r in c.execute("SELECT * FROM entries WHERE project=? AND environment=? AND branch=? AND source_version=? AND status IN ('active','proposed')",(p,*scope)).fetchall():
                if topic_key(r['topic'])==data['topic'] and normalized(r['content'])==normalized(data['content']):return dict(r)
            if not predecessor:
                # 当前版本被撤回后主题暂时没有有效版本，但重新发布仍须经所有者确认：挂到最近一次撤回的记录下成为提案，否则作者可以先撤回再发布来绕过审核。
                retracted=[r for r in c.execute("SELECT * FROM entries WHERE project=? AND environment=? AND branch=? AND status='retracted' ORDER BY created DESC",(p,scope[0],scope[1])).fetchall() if topic_key(r['topic'])==data['topic']]
                if retracted:predecessor=retracted[0]
            if predecessor:data['topic']=predecessor['topic']
            # 哈希只用于项目内唯一约束：被历史记录占用时派生新哈希。
            h=digest(json.dumps([data['topic'],*scope,normalized(data['content'])],ensure_ascii=False))
            old=c.execute('SELECT * FROM entries WHERE project=? AND hash=?',(p,h)).fetchone()
            while old:
                h=digest(h+':after:'+old['id'])
                old=c.execute('SELECT * FROM entries WHERE project=? AND hash=?',(p,h)).fetchone()
            eid=ident(); now=time.time()
            expires=data.get('expires_at')
            if data['kind']=='handoff' and expires is None:expires=now+7*86400
            row=(eid,p,data['topic'],data['content'],h,data['kind'],data['source'],data['agent'],u,'proposed' if predecessor else 'active','pending',predecessor['id'] if predecessor else None,now,expires)
            c.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row+scope)
            self.audit(c,p,u,'propose' if predecessor else 'publish',eid)
            return dict(c.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone())

    def entries(self,p,u):
        with self.connect() as c:
            self.acl(c,p,u)
            self.expire_due(c,p)
            return [dict(r) for r in c.execute('SELECT * FROM entries WHERE project=? ORDER BY created DESC,id DESC',(p,))]

    def entry_page(self,p,u,cursor='',limit=200,status=None):
        with self.connect() as c:
            self.acl(c,p,u)
            self.expire_due(c,p)
            where='project=? AND id>?';args=[p,cursor]
            if status:where+=' AND status=?';args.append(status)
            rows=[dict(r) for r in c.execute('SELECT * FROM entries WHERE '+where+' ORDER BY id LIMIT ?',(*args,limit+1))]
            return {'entries':rows[:limit],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    def candidates(self,p,u,environment='',branch=''):
        with self.connect() as c:
            self.acl(c,p,u)
            rows=c.execute("SELECT id,environment,branch FROM entries WHERE project=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",(p,time.time()))
            scoped=[];unknown=[]
            for r in rows:
                if (environment and r['environment'] and environment!=r['environment']) or (branch and r['branch'] and branch!=r['branch']):continue
                (unknown if (environment and not r['environment']) or (branch and not r['branch']) else scoped).append(r['id'])
            return scoped,unknown

    def revise_proposal(self,p,u,eid,action,expected=None):
        # 过期清扫单独提交：下面的检查返回 409 回滚时，到期状态和审计也已经落库。
        with self.connect() as c:
            self.acl(c,p,u,{'owner'},True)
            self.expire_due(c,p)
        with self.connect() as c:
            self.acl(c,p,u,{'owner'},True)
            row=c.execute('SELECT * FROM entries WHERE project=? AND id=?',(p,eid)).fetchone()
            if not row:fail(404,'记录不存在')
            if row['status']=='expired':fail(409,'提案已过期并自动失效；如仍需要请重新保存')
            # 拒绝是终态：已拒绝的提案不能再 rebase 或重复拒绝；需要重新考虑时重新保存内容，会生成带新审计的提案。
            if row['status']!='proposed':fail(409,'只能处理待确认提案')
            if row['expires_at'] is not None and row['expires_at']<=time.time():fail(409,'提案已过期，请重新整理内容')
            if action=='rebase':
                current=c.execute("SELECT id FROM entries WHERE project=? AND topic=? AND environment=? AND branch=? AND status='active'",(p,row['topic'],row['environment'],row['branch'])).fetchone()
                if not current or current['id']!=expected:fail(409,'当前版本已变化，请重新核对')
                c.execute("UPDATE entries SET supersedes=?,status='proposed' WHERE id=?",(expected,eid))
                self.audit(c,p,u,'rebase',json.dumps({'entry':eid,'previous':row['supersedes'],'current':expected}))
            else:
                c.execute("UPDATE entries SET status='rejected' WHERE id=?",(eid,))
                self.audit(c,p,u,'reject',eid)
            return dict(c.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone())

    def resolve(self,p,u,eid,expected,explicit=True):
        # 过期清扫单独提交：下面的检查返回 409 回滚时，到期状态和审计也已经落库。
        with self.connect() as c:
            self.acl(c,p,u)
            self.expire_due(c,p)
        with self.connect() as c:
            self.acl(c,p,u)
            row=c.execute('SELECT * FROM entries WHERE id=? AND project=?',(eid,p)).fetchone()
            if not row:fail(404,'记录不存在')
            self.acl(c,p,u,{'owner'},True)
            current=c.execute("SELECT id FROM entries WHERE project=? AND topic=? AND environment=? AND branch=? AND status='active'",(p,row['topic'],row['environment'],row['branch'])).fetchone()
            if row['status']=='expired':fail(409,'提案已过期并自动失效；如仍需要请重新保存')
            if row['expires_at'] is not None and row['expires_at']<=time.time():fail(409,'提案已过期')
            if row['status']!='proposed':fail(409,'版本已变化，请重新核对冲突')
            if current:
                if current['id']!=expected or row['supersedes']!=expected:fail(409,'版本已变化，请重新核对冲突')
                c.execute("UPDATE entries SET status='superseded' WHERE id=?",(expected,))
            # 当前没有有效版本（被替代的版本已撤回或过期）：只有所有者在请求体里显式传 expected_id=null 才让提案生效，省略该字段不算确认，不自动批准。
            elif expected is not None or not explicit:fail(409,'版本已变化，请重新核对冲突')
            c.execute("UPDATE entries SET status='active' WHERE id=?",(eid,))
            self.audit(c,p,u,'resolve',eid)
            return dict(c.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone())

    def mark_indexed(self,eid):
        with self.connect() as c:c.execute("UPDATE entries SET index_status='ready' WHERE id=? AND status='active'",(eid,))

    def is_active(self,eid):
        with self.connect() as c:return c.execute("SELECT 1 FROM entries WHERE id=? AND status='active'",(eid,)).fetchone() is not None

    def retract(self,p,u,eid):
        # 撤回用于清除误发内容：抹掉正文并换掉内容哈希（原哈希可用来验证猜测的原文），保留主题、作者、时间和审计。
        # 归档项目不按写操作拦截，否则归档后误发的秘密无法清理；所有者可撤回任何记录，其他成员只能撤回自己发布的。
        with self.connect() as c:
            role=self.acl(c,p,u)
            row=c.execute('SELECT * FROM entries WHERE project=? AND id=?',(p,eid)).fetchone()
            if not row:fail(404,'记录不存在')
            if role!='owner' and row['author']!=u:fail(403,'只有项目所有者或作者本人可以撤回')
            if row['status']!='retracted':
                c.execute("UPDATE entries SET status='retracted',content=?,hash=?,index_status='purge_pending' WHERE id=?",(RETRACTED,digest('retracted:'+eid),eid))
                self.audit(c,p,u,'retract',eid)
            return dict(c.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone())

    def log_vectors(self,p,u,eid,vectors):
        # 删除前先记下目标向量 ID：Mem0 自身的历史库可能仍留有原文，删除中途失败也不会丢失需要到服务器核对的 ID。
        with self.connect() as c:self.audit(c,p,u,'purge_vectors',json.dumps({'entry':eid,'vectors':vectors}))

    def mark_purged(self,eid):
        with self.connect() as c:c.execute("UPDATE entries SET index_status='purged' WHERE id=? AND status='retracted'",(eid,))

    def recall(self,p,u,ids,limit):
        # 索引返回的任何 ID 都必须重新通过项目归属/状态过滤，拒绝跨项目和过期结果。
        with self.connect() as c:
            self.acl(c,p,u)
            result=[]
            for eid in dict.fromkeys(ids):
                r=c.execute("SELECT * FROM entries WHERE id=? AND project=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",(eid,p,time.time())).fetchone()
                if r:result.append(dict(r))
                if len(result)>=limit:break
            return result

    def archive(self,p,u):
        with self.connect() as c:
            self.acl(c,p,u,{'owner'})
            c.execute('UPDATE projects SET archived=1 WHERE id=?',(p,))
            self.audit(c,p,u,'archive',p)
        return {'ok':True}
