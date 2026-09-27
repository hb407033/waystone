import time
import pytest
from fastapi.testclient import TestClient
from waystone.api import create_app

class Backend:
    def __init__(self): self.entries={}; self.fail=False; self.fail_delete=set()
    def authenticate_admin(self,email,password):
        return {'id':'owner','email':email,'name':'Owner'} if email=='owner@example.com' and password=='test-password-123' else None
    def index(self,project,entry):
        if self.fail: raise RuntimeError('offline')
        self.entries[entry['id']]=(project,entry)
    def ready(self):
        if self.fail:raise RuntimeError('offline')
    # 假向量库以向量 ID 为键；index 写入的向量 ID 等于记录 ID，测试可额外塞入其他向量 ID 模拟一条记录对应多个向量。
    def vector_ids(self,project,entry_id):
        if self.fail: raise RuntimeError('offline')
        return [vid for vid,(p,e) in self.entries.items() if p==project and e['id']==entry_id]
    def delete_vector(self,vector_id):
        if vector_id in self.fail_delete: raise RuntimeError('offline')
        self.entries.pop(vector_id,None)
    def search(self,project,query,limit,eligible_ids=None):
        return [e['id'] for p,e in self.entries.values() if p==project and (eligible_ids is None or e['id'] in eligible_ids)][:limit]

@pytest.fixture
def system(tmp_path):
    backend=Backend(); app=create_app(str(tmp_path/'db.sqlite'),backend)
    client=TestClient(app)
    token=client.post('/auth/login',json={'email':'owner@example.com','password':'test-password-123'}).json()['token']
    client.headers['Authorization']='Bearer '+token
    return client,backend,app

def project(c,name='Alpha'): return c.post('/projects',json={'name':name}).json()['id']
def member(c,p,role='collaborator',email='member@example.com'):
    inv=c.post(f'/projects/{p}/invites',json={'role':role,'email':email}).json()['invite']
    r=c.post('/auth/join',json={'invite':inv,'email':email,'name':'Member','password':'member-password-123'})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']},r.json()['user_id'],inv

def save(c,p,**kw):
    return c.post(f'/projects/{p}/entries',json={'content':'使用 PostgreSQL 数据库','topic':'database','source':'README.md','kind':'decision','agent':'test',**kw})

def test_auth_and_project_isolation(system):
    c,b,app=system; p=project(c); q=project(c,'Beta'); h,uid,_=member(c,p)
    assert c.get('/projects',headers={'Authorization':'Bearer invalid'}).status_code==401
    assert c.get(f'/projects/{q}/entries',headers=h).status_code==403
    e=save(c,q).json()
    assert c.post(f'/projects/{p}/entries/{e["id"]}/resolve',json={},headers=h).status_code==404
    assert c.get(f'/projects/{p}/entries',headers=h).status_code==200
    assert c.delete(f'/projects/{p}/members/{uid}').status_code==200
    assert c.get(f'/projects/{p}/entries',headers=h).status_code==403

def test_invite_once_and_bound_email(system):
    c,_,_=system;p=project(c);h,uid,invite=member(c,p)
    data={'invite':invite,'email':'other@example.com','name':'Other','password':'another-password-123'}
    assert c.post('/auth/join',json=data).status_code==400
    inv=c.post(f'/projects/{p}/invites',json={'role':'reader','email':'reader@example.com','expires_hours':1}).json()['invite']
    assert c.post('/auth/join',json={**data,'invite':inv}).status_code==400

def test_read_only(system):
    c,_,_=system;p=project(c);h,_,_=member(c,p,'reader')
    assert c.post(f'/projects/{p}/entries',headers=h,json={'content':'abc','topic':'x','source':'x'}).status_code==403
    assert c.post(f'/projects/{p}/invites',headers=h,json={'email':'x@example.com'}).status_code==403

def test_dedupe_and_explicit_conflict(system):
    c,b,_=system;p=project(c)
    one=save(c,p).json();same=save(c,p).json()
    assert one['id']==same['id']
    two=save(c,p,content='改为 MySQL').json()
    assert two['status']=='proposed' and two['supersedes']==one['id']
    assert two['id'] not in b.entries
    result=c.post(f'/projects/{p}/recall',json={'query':'数据库'}).json()
    assert [x['id'] for x in result['entries']]==[one['id']]
    resolved=c.post(f'/projects/{p}/entries/{two["id"]}/resolve',json={'expected_id':one['id']})
    assert resolved.status_code==200
    assert c.post(f'/projects/{p}/entries/{two["id"]}/resolve',json={'expected_id':one['id']}).status_code==409
    result=c.post(f'/projects/{p}/recall',json={'query':'数据库'}).json()
    assert [x['id'] for x in result['entries']]==[two['id']]

def test_outage_retains_record_and_retry(system):
    c,b,_=system;p=project(c);b.fail=True
    e=save(c,p).json();assert e['index_status']=='pending'
    b.fail=False
    assert c.post(f'/projects/{p}/reindex').status_code==200
    assert save(c,p).json()['index_status']=='ready'

def test_client_cannot_forge_scope(system):
    c,_,_=system;p=project(c)
    assert save(c,p,user_id='other',project_id='other').status_code==422
    assert c.post(f'/projects/{p}/recall',json={'query':'x','filters':{'user_id':'other'}}).status_code==422

def test_archive_and_expired_handoff(system):
    c,_,_=system;p=project(c)
    save(c,p,kind='handoff',expires_at=time.time()-1)
    assert c.post(f'/projects/{p}/recall',json={'query':'x'}).json()['entries']==[]
    assert c.post(f'/projects/{p}/archive').status_code==200
    assert save(c,p).status_code==409

def test_index_cannot_return_other_projects(system):
    c,b,_=system;p=project(c);q=project(c,'Other')
    other=save(c,q).json();own=save(c,p).json()
    b.search=lambda *args:[other['id'],own['id']]
    result=c.post(f'/projects/{p}/recall',json={'query':'x'}).json()
    assert [x['id'] for x in result['entries']]==[own['id']]

def test_expired_invite_and_session(system):
    c,b,app=system;p=project(c)
    inv=c.post(f'/projects/{p}/invites',json={'email':'new@example.com'}).json()['invite']
    with app.state.store.connect() as db:db.execute('UPDATE invites SET expires=0')
    r=c.post('/auth/join',json={'invite':inv,'email':'new@example.com','password':'test-password-123','name':'New'})
    assert r.status_code==400
    with app.state.store.connect() as db:db.execute('UPDATE sessions SET expires=0')
    assert c.get('/projects').status_code==401

def test_member_login_and_existing_accept(system):
    c,b,_=system;p=project(c);q=project(c,'Second');h,uid,_=member(c,p)
    r=c.post('/auth/login',json={'email':'member@example.com','password':'member-password-123'})
    assert r.status_code==200
    assert c.post('/auth/login',json={'email':'member@example.com','password':'wrong'}).status_code==401
    inv=c.post(f'/projects/{q}/invites',json={'email':'member@example.com','role':'reader'}).json()['invite']
    assert c.post('/invites/accept',headers=h,json={'invite':inv}).status_code==200
    assert c.get(f'/projects/{q}/entries',headers=h).status_code==200

def test_parallel_dedup(system):
    from concurrent.futures import ThreadPoolExecutor
    c,b,_=system;p=project(c)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:save(c,p).json()['id'],range(8)))
    assert len(set(results))==1
    assert len(c.get(f'/projects/{p}/entries').json())==1

def test_scoped_topics_versions_and_recall(system):
    c,_,_=system;p=project(c)
    a=save(c,p,environment='prod',branch='main',source_version='abc123').json()
    other=save(c,p,environment='dev',branch='main').json()
    assert a['id']!=other['id']
    assert other['status']=='active'
    proposal=save(c,p,environment='prod',branch='main',content='改用 SQLite').json()
    assert proposal['status']=='proposed'
    assert proposal['supersedes']==a['id']
    legacy=save(c,p,topic='legacy',content='旧记录').json()
    r=c.post(f'/projects/{p}/recall',json={'query':'数据库','environment':'prod','branch':'main'}).json()
    assert [e['id'] for e in r['entries']]==[a['id']]
    assert legacy['id'] in [e['id'] for e in r['unscoped_entries']]
    assert r['conflicts'][0]['id']==proposal['id']
    assert a['source_version']=='abc123'
    assert c.post(f'/projects/{p}/entries/{proposal["id"]}/resolve',json={'expected_id':a['id']}).status_code==200
    assert c.post(f'/projects/{p}/entries/{proposal["id"]}/resolve',json={'expected_id':a['id']}).status_code==409

def test_topic_normalization_and_version_dedup(system):
    c,_,_=system;p=project(c)
    a=save(c,p,topic='Auth / Session',source_version='v1').json()
    b=save(c,p,topic='auth/session',content='新结论',source_version='v2').json()
    assert b['status']=='proposed'
    assert b['supersedes']==a['id']
    version=save(c,p,topic='auth/session',source_version='v3').json()
    assert version['id']!=a['id']
    assert version['status']=='proposed'

def test_existing_database_upgrade_preserves_records(tmp_path):
    import sqlite3
    from waystone.store import Store
    path=str(tmp_path/'legacy.sqlite')
    c=sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE entries(id TEXT PRIMARY KEY,project TEXT,topic TEXT,content TEXT,hash TEXT,kind TEXT,source TEXT,agent TEXT,author TEXT,status TEXT,index_status TEXT,supersedes TEXT,created REAL,expires_at REAL,UNIQUE(project,hash));
    CREATE UNIQUE INDEX active_topic ON entries(project,topic) WHERE status='active';
    INSERT INTO entries VALUES('old','p','Database','原始结论','hash','decision','README','old-agent','u','active','ready',NULL,1,NULL);
    """)
    c.close()
    Store(path);store=Store(path)
    with store.connect() as c:
        row=dict(c.execute('SELECT * FROM entries').fetchone())
        assert row['id']=='old' and row['content']=='原始结论' and row['topic']=='Database'
        assert row['environment']==row['branch']==row['source_version']==''

def test_scoped_recall_cannot_disclose_other_project(system):
    c,b,_=system;p=project(c);q=project(c,'Other')
    hidden=save(c,q,environment='prod',branch='main').json()
    b.search=lambda *args:[hidden['id']]
    r=c.post(f'/projects/{p}/recall',json={'query':'数据库','environment':'prod','branch':'main'}).json()
    assert r['entries']==r['unscoped_entries']==r['conflicts']==[]


def test_legacy_topic_alias_keeps_conflict_gate(system):
    c,_,app=system;p=project(c)
    a=save(c,p,topic='auth/session').json()
    with app.state.store.connect() as db:
        db.execute("UPDATE entries SET topic='Auth / Session',hash='old-hash' WHERE id=?",(a['id'],))
    assert save(c,p,topic='auth/session').json()['id']==a['id']
    b=save(c,p,topic='auth/session',content='新的方案').json()
    assert b['status']=='proposed' and b['supersedes']==a['id']


@pytest.mark.parametrize('field,value',[('content','部署时使用 api_key=abcdefghijklmnop'),('content','{"token":"abcdefghijklmnop"}'),('agent','secret: abcdefghijklmnop'),('source_version','sk-abcdefghijklmnop')])
def test_publish_rejects_obvious_credentials(system,field,value):
    c,b,_=system;p=project(c)
    r=save(c,p,**{field:value})
    assert r.status_code==400 and '凭据' in r.json()['detail'] and value not in r.text
    assert c.get(f'/projects/{p}/entries').json()==[] and b.entries=={}


def test_recall_uses_one_vector_search_and_splits_scopes(system):
    c,b,_=system;p=project(c)
    assert save(c,p,topic='db/engine',environment='prod',branch='main').status_code==200
    assert save(c,p,topic='db/notes',content='未注明范围的数据库资料').status_code==200
    calls=[];orig=b.search
    def spy(*a,**k):calls.append(a);return orig(*a,**k)
    b.search=spy
    r=c.post(f'/projects/{p}/recall',json={'query':'数据库','environment':'prod','branch':'main'}).json()
    assert len(calls)==1
    assert [e['topic'] for e in r['entries']]==['db/engine'] and [e['topic'] for e in r['unscoped_entries']]==['db/notes']

def test_recall_conflicts_do_not_load_all_entries(system):
    c,b,app=system;p=project(c)
    first=save(c,p).json();prop=save(c,p,content='改用 MySQL 数据库').json()
    assert prop['status']=='proposed'
    app.state.store.entries=lambda *a,**k:(_ for _ in ()).throw(AssertionError('recall 不应读取全部记忆'))
    r=c.post(f'/projects/{p}/recall',json={'query':'数据库'}).json()
    assert [e['id'] for e in r['conflicts']]==[prop['id']] and first['id'] in [e['id'] for e in r['entries']]


def test_index_locks_are_per_entry(system):
    import threading
    c,b,app=system;p=project(c)
    lk=app.state.entry_lock
    assert lk('a') is lk('a') and lk('a') is not lk('b')
    # 两条不同记录必须能同时进入索引：栅栏要等两方都到达才放行，若被同一把锁串行化会超时，索引失败留在 pending。
    orig=b.index;gate=threading.Barrier(2,timeout=3)
    def paired(project,entry):gate.wait();orig(project,entry)
    b.index=paired
    out=[None,None]
    def run(i):out[i]=save(c,p,topic=f't/{i}',content=f'事实 {i}').json()
    threads=[threading.Thread(target=run,args=(i,)) for i in range(2)]
    [t.start() for t in threads];[t.join(10) for t in threads]
    assert not any(t.is_alive() for t in threads)
    assert [o['index_status'] for o in out]==['ready','ready']

def test_retract_waits_for_inflight_index_of_same_entry(system):
    import threading
    c,b,app=system;p=project(c);e=save(c,p).json()
    orig=b.index;entered=threading.Event();release=threading.Event();done=threading.Event();res={}
    def blocked(project,entry):entered.set();release.wait(5);orig(project,entry)
    b.index=blocked
    reindex=threading.Thread(target=lambda:res.update(reindex=c.post(f'/projects/{p}/reindex',json={'full':True}).status_code));reindex.start()
    assert entered.wait(5)
    def do_retract():res['retract']=c.post(f'/projects/{p}/entries/{e["id"]}/retract').json();done.set()
    retract=threading.Thread(target=do_retract);retract.start()
    # 撤回的数据库部分会先提交，但清理向量必须等同一记录上正在进行的索引结束，否则刚删掉的向量会被写回。
    assert not done.wait(0.5)
    release.set();reindex.join(10);retract.join(10)
    assert res['reindex']==200 and res['retract']['index_status']=='purged'
    assert e['id'] not in b.entries

def test_session_slides_and_expired_rows_are_purged(system):
    c,_,app=system;s=app.state.store
    with s.connect() as db:db.execute('UPDATE sessions SET expires=?',(time.time()+86400,))
    assert c.get('/projects').status_code==200
    with s.connect() as db:assert db.execute('SELECT max(expires) FROM sessions').fetchone()[0]>time.time()+29*86400
    uid=c.post('/auth/login',json={'email':'owner@example.com','password':'test-password-123'}).json()['user_id']
    with s.connect() as db:
        db.execute("INSERT INTO sessions VALUES('stale',?,0)",(uid,))
        db.execute("INSERT INTO devices VALUES('olddev','oldcode','x',0,NULL)")
    r=c.post('/auth/login',json={'email':'owner@example.com','password':'test-password-123'}).json()
    assert r['expires_in']==30*86400
    with s.connect() as db:
        assert not db.execute("SELECT 1 FROM sessions WHERE hash='stale'").fetchone()
        assert not db.execute("SELECT 1 FROM devices WHERE hash='olddev'").fetchone()
    bad=c.get('/projects',headers={'Authorization':'Bearer nope'})
    assert bad.status_code==401 and 'waystone login' in bad.json()['detail']

def test_expired_session_is_not_revived(system):
    c,_,app=system
    with app.state.store.connect() as db:db.execute('UPDATE sessions SET expires=?',(time.time()-1,))
    assert c.get('/projects').status_code==401
    with app.state.store.connect() as db:assert db.execute('SELECT max(expires) FROM sessions').fetchone()[0]<time.time()
