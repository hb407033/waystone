import time
import pytest
from test_service import system, project, save, member

def test_full_rebuild_recovers_ready_rows(system):
    c,b,_=system;p=project(c);e=save(c,p).json();b.entries.clear()
    r=c.post(f'/projects/{p}/reindex',json={'full':True}).json()
    assert r['processed']==1 and e['id'] in b.entries

def test_rebase_and_reject_competing_proposals(system):
    c,_,_=system;p=project(c);a=save(c,p).json();b=save(c,p,content='B').json();d=save(c,p,content='C').json()
    c.post(f'/projects/{p}/entries/{b["id"]}/resolve',json={'expected_id':a['id']}).raise_for_status()
    h,_,_=member(c,p)
    path=f'/projects/{p}/entries/{d["id"]}'
    assert c.post(path+'/rebase',headers=h,json={'expected_id':b['id']}).status_code==403
    assert c.post(path+'/rebase',json={'expected_id':a['id']}).status_code==409
    assert c.post(path+'/rebase',json={'expected_id':b['id']}).status_code==200
    assert c.post(path+'/resolve',json={'expected_id':b['id']}).status_code==200
    e=save(c,p,content='D').json()
    assert c.post(f'/projects/{p}/entries/{e["id"]}/reject').json()['status']=='rejected'

def test_expired_handoff_can_be_saved_again(system):
    c,_,_=system;p=project(c);old=save(c,p,kind='handoff',expires_at=time.time()-1).json()
    new=save(c,p,kind='handoff').json()
    assert new['id']!=old['id'] and new['expires_at']>time.time()
    assert save(c,p,kind='handoff').json()['id']==new['id']

def test_readiness_probes_dependency(system):
    c,b,_=system
    b.ready=lambda: (_ for _ in ()).throw(RuntimeError('offline'))
    assert c.get('/health').status_code==200
    assert c.get('/ready').status_code==503
    b.ready=lambda:None
    assert c.get('/ready').status_code==200

def test_scope_selection_before_vector_ranking(system):
    c,b,_=system;p=project(c)
    for n in range(105):save(c,p,topic=f'dev/{n}',environment='dev')
    target=save(c,p,topic='prod/db',environment='prod').json()
    result=c.post(f'/projects/{p}/recall',json={'query':'数据库','environment':'prod'}).json()
    assert [e['id'] for e in result['entries']]==[target['id']]

def test_complete_pagination_and_rebuild_after_1000(system):
    c,b,app=system;p=project(c);seed=save(c,p).json()
    # Seed isolated SQL efficiently, preserving the same schema and required columns.
    with app.state.store.connect() as db:
        for i in range(1005):
            row={**seed,'id':f'test{i:04}','topic':f'topic/{i}','hash':f'hash{i}','index_status':'ready'}
            db.execute('INSERT INTO entries ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    seen=[];cursor=''
    while True:
        r=c.get(f'/projects/{p}/entries/page',params={'cursor':cursor,'limit':200}).json()
        seen.extend(x['id'] for x in r['entries']);cursor=r['next_cursor']
        if not cursor:break
    assert len(seen)==len(set(seen))==1006
    b.entries.clear();cursor='';processed=0
    while True:
        r=c.post(f'/projects/{p}/reindex',json={'full':True,'cursor':cursor,'limit':10}).json()
        processed+=r['processed'];cursor=r['next_cursor']
        if not cursor:break
    assert processed==1006 and len(b.entries)==1006

def test_mem0_search_contract_and_candidate_filter():
    from waystone.backend import Mem0Backend
    b=Mem0Backend('http://unused','unused');calls=[]
    b.request=lambda path,data:(calls.append((path,data)) or {'results':[]})
    assert b.search('p','q',8,['eligible'])==[]
    assert calls[0][1]['top_k']==8
    assert calls[0][1]['filters']['gateway_entry_id']=={'in':['eligible']}


def test_repeated_expiry_and_rebuild_authorization(system):
    c,_,app=system;p=project(c);ids=set()
    for i in range(5):
        e=save(c,p,kind='handoff').json();assert e['id'] not in ids;ids.add(e['id'])
        with app.state.store.connect() as db:db.execute('UPDATE entries SET expires_at=0 WHERE id=?',(e['id'],))
    h,_,_=member(c,p)
    assert c.post(f'/projects/{p}/reindex',json={'full':True},headers=h).status_code==403


def test_ten_simultaneous_members_smoke(system):
    from concurrent.futures import ThreadPoolExecutor
    c,_,_=system;p=project(c)
    identities=[c.headers['Authorization']]
    for i in range(9):identities.append(member(c,p,email=f'user{i}@example.com')[0]['Authorization'])
    def job(pair):
        i,token=pair
        r=c.post(f'/projects/{p}/entries',headers={'Authorization':token},json={'topic':f'user/{i}','content':f'Fact {i}','source':'concurrency-test'})
        assert r.status_code==200
        assert c.post(f'/projects/{p}/recall',headers={'Authorization':token},json={'query':'Fact'}).status_code==200
    with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(job,enumerate(identities)))
    assert len(c.get(f'/projects/{p}/entries').json())==10


def test_expired_proposal_leaves_queue_and_can_be_saved_again(system):
    c,_,app=system;p=project(c)
    a=save(c,p,kind='handoff').json()
    b=save(c,p,kind='handoff',content='交接更新').json()
    assert b['status']=='proposed' and b['expires_at'] is not None
    with app.state.store.connect() as db:db.execute('UPDATE entries SET expires_at=? WHERE id=?',(time.time()-1,b['id']))
    # 直接处理到期提案：即使返回 409，到期状态也必须已经落库。
    r=c.post(f'/projects/{p}/entries/{b["id"]}/reject')
    assert r.status_code==409 and '重新保存' in r.json()['detail']
    with app.state.store.connect() as db:assert db.execute('SELECT status FROM entries WHERE id=?',(b['id'],)).fetchone()['status']=='expired'
    rows={e['id']:e for e in c.get(f'/projects/{p}/entries/page').json()['entries']}
    assert rows[b['id']]['status']=='expired' and rows[a['id']]['status']=='active'
    again=save(c,p,kind='handoff',content='交接更新').json()
    assert again['id']!=b['id'] and again['status']=='proposed' and again['supersedes']==a['id']
    with app.state.store.connect() as db:db.execute('UPDATE entries SET expires_at=? WHERE id=?',(time.time()-1,again['id']))
    assert c.post(f'/projects/{p}/entries/{again["id"]}/resolve',json={'expected_id':a['id']}).status_code==409
    with app.state.store.connect() as db:assert db.execute('SELECT status FROM entries WHERE id=?',(again['id'],)).fetchone()['status']=='expired'


def test_expiry_is_audited_as_system(system):
    c,_,app=system;p=project(c);h,uid,_=member(c,p)
    e=save(c,p,kind='handoff').json()
    with app.state.store.connect() as db:db.execute('UPDATE entries SET expires_at=? WHERE id=?',(time.time()-1,e['id']))
    r=c.post(f'/projects/{p}/entries',headers=h,json={'content':'无关记录','topic':'other','source':'test'})
    assert r.status_code==200
    expired=[x for x in c.get(f'/projects/{p}/audit').json() if x['action']=='expire']
    assert [(x['actor'],x['target']) for x in expired]==[('system',e['id'])]


def test_rejected_proposal_is_final_and_can_be_proposed_again(system):
    c,_,_=system;p=project(c);a=save(c,p).json();b=save(c,p,content='改为 MySQL').json()
    path=f'/projects/{p}/entries/{b["id"]}'
    assert c.post(path+'/reject').json()['status']=='rejected'
    assert c.post(path+'/rebase',json={'expected_id':a['id']}).status_code==409
    assert c.post(path+'/reject').status_code==409
    again=save(c,p,content='改为 MySQL').json()
    assert again['id']!=b['id'] and again['status']=='proposed' and again['supersedes']==a['id']
    assert save(c,p,content='改为 MySQL').json()['id']==again['id']


def test_archived_project_can_rebuild_index(system):
    c,b,_=system;p=project(c);e=save(c,p).json()
    assert c.post(f'/projects/{p}/archive').status_code==200
    b.entries.clear()
    r=c.post(f'/projects/{p}/reindex',json={'full':True})
    assert r.status_code==200 and r.json()['processed']==1 and e['id'] in b.entries
    assert save(c,p,topic='new').status_code==409


def test_retract_wipes_content_vector_and_respects_roles(system):
    c,b,app=system;p=project(c);h,uid,_=member(c,p)
    mine=c.post(f'/projects/{p}/entries',headers=h,json={'content':'误发的内部地址 10.0.0.8','topic':'ops/leak','source':'chat','kind':'background'}).json()
    other=save(c,p).json()
    assert mine['id'] in b.entries
    assert c.post(f'/projects/{p}/entries/{other["id"]}/retract',headers=h).status_code==403
    r=c.post(f'/projects/{p}/entries/{mine["id"]}/retract',headers=h)
    assert r.status_code==200,r.text
    body=r.json()
    assert body['status']=='retracted' and body['content']=='[已撤回]' and body['index_status']=='purged'
    assert mine['id'] not in b.entries
    with app.state.store.connect() as db:row=db.execute('SELECT content,hash FROM entries WHERE id=?',(mine['id'],)).fetchone()
    assert '10.0.0.8' not in row['content'] and row['hash']!=mine['hash']
    recalled=c.post(f'/projects/{p}/recall',json={'query':'内部地址'}).json()
    assert mine['id'] not in [e['id'] for e in recalled['entries']+recalled['unscoped_entries']]
    assert c.post(f'/projects/{p}/entries/{other["id"]}/retract').json()['status']=='retracted'
    assert c.post(f'/projects/{p}/entries/{other["id"]}/retract').status_code==200
    actions=[(x['action'],x['actor']) for x in c.get(f'/projects/{p}/audit').json()]
    assert actions.count(('retract',uid))==1 and len([a for a in actions if a[0]=='retract'])==2
    assert len([a for a in actions if a[0]=='purge_vectors'])==2


def test_retract_retries_vector_purge_and_works_after_archive(system):
    c,b,_=system;p=project(c);e=save(c,p).json()
    assert c.post(f'/projects/{p}/archive').status_code==200
    b.fail=True
    r=c.post(f'/projects/{p}/entries/{e["id"]}/retract').json()
    assert r['status']=='retracted' and r['index_status']=='purge_pending' and e['id'] in b.entries
    b.fail=False
    r=c.post(f'/projects/{p}/entries/{e["id"]}/retract').json()
    assert r['index_status']=='purged' and e['id'] not in b.entries


def test_partial_vector_purge_keeps_every_vector_id_in_audit(system):
    c,b,_=system;p=project(c);e=save(c,p).json()
    b.entries['v2']=b.entries[e['id']];b.fail_delete={'v2'}
    r=c.post(f'/projects/{p}/entries/{e["id"]}/retract').json()
    assert r['index_status']=='purge_pending' and e['id'] not in b.entries and 'v2' in b.entries
    b.fail_delete=set()
    assert c.post(f'/projects/{p}/entries/{e["id"]}/retract').json()['index_status']=='purged' and 'v2' not in b.entries
    logged=''.join(x['target'] for x in c.get(f'/projects/{p}/audit').json() if x['action']=='purge_vectors')
    assert e['id'] in logged and 'v2' in logged


def test_reindex_does_not_restore_retracted_content(system):
    c,b,app=system;p=project(c);e=save(c,p).json()
    stale={**e,'index_status':'pending'}
    assert c.post(f'/projects/{p}/entries/{e["id"]}/retract').json()['index_status']=='purged'
    # 模拟 reindex 在撤回之前读到的旧页面：写向量前必须按数据库当前状态复核。
    app.state.store.entry_page=lambda *args,**kwargs:{'entries':[stale],'next_cursor':None}
    assert c.post(f'/projects/{p}/reindex',json={'full':True}).status_code==200
    assert e['id'] not in b.entries


def test_mem0_vector_ids_requires_explicit_ownership():
    from waystone.backend import Mem0Backend
    b=Mem0Backend('http://unused','unused');calls=[]
    good={'id':'m1','user_id':'waystone:p','metadata':{'gateway_entry_id':'e1','project_id':'p'}}
    other={'id':'m2','user_id':'waystone:p','metadata':{'gateway_entry_id':'other','project_id':'p'}}
    b.request=lambda path,data:(calls.append((path,data)) or {'results':[good,other]})
    assert b.vector_ids('p','e1')==['m1']
    assert calls[0][1]['filters']=={'user_id':'waystone:p','gateway_entry_id':'e1'}
    # 命中目标记录 ID 但归属缺失或矛盾：不能删除，也不能当作已清理。
    for bad in ({k:v for k,v in good.items() if k!='user_id'},{**good,'user_id':'waystone:q'},{**good,'metadata':{'gateway_entry_id':'e1','project_id':'q'}},{**good,'metadata':{'gateway_entry_id':'e1'}}):
        b.request=lambda path,data,bad=bad:{'results':[bad]}
        with pytest.raises(RuntimeError):b.vector_ids('p','e1')


def test_retracting_history_does_not_duplicate_open_proposal(system):
    c,_,_=system;p=project(c);a=save(c,p).json();r1=save(c,p,content='改为 MySQL').json()
    assert c.post(f'/projects/{p}/entries/{r1["id"]}/reject').json()['status']=='rejected'
    r2=save(c,p,content='改为 MySQL').json()
    assert r2['id']!=r1['id'] and r2['status']=='proposed'
    assert c.post(f'/projects/{p}/entries/{r1["id"]}/retract').json()['status']=='retracted'
    assert save(c,p,content='改为 MySQL').json()['id']==r2['id']


def test_proposal_can_be_resolved_after_current_version_is_retracted(system):
    c,b,_=system;p=project(c);a=save(c,p).json();pb=save(c,p,content='改为 MySQL').json()
    path=f'/projects/{p}/entries/{pb["id"]}/resolve'
    assert c.post(path,json={'expected_id':None}).status_code==409
    assert c.post(f'/projects/{p}/entries/{a["id"]}/retract').json()['status']=='retracted'
    assert save(c,p,content='改为 MySQL').json()['id']==pb['id']
    assert c.post(path,json={'expected_id':a['id']}).status_code==409
    r=c.post(path,json={'expected_id':None})
    assert r.status_code==200 and r.json()['status']=='active' and pb['id'] in b.entries


def test_republish_after_retract_still_needs_owner_review(system):
    c,_,_=system;p=project(c);h,_,_=member(c,p)
    def post(**kw):return c.post(f'/projects/{p}/entries',headers=h,json={'content':'使用 PostgreSQL 数据库','topic':'database','source':'README.md',**kw}).json()
    a=post();b=post(content='改为 MySQL')
    assert a['status']=='active' and b['status']=='proposed'
    assert c.post(f'/projects/{p}/entries/{a["id"]}/retract',headers=h).json()['status']=='retracted'
    again=post(content='改为 MySQL',source_version='v2')
    assert again['status']=='proposed' and again['supersedes']==a['id']
    assert c.post(f'/projects/{p}/entries/{again["id"]}/resolve',headers=h,json={'expected_id':None}).status_code==403
    assert c.post(f'/projects/{p}/entries/{again["id"]}/resolve',json={'expected_id':None}).json()['status']=='active'


def test_resolve_without_current_version_requires_explicit_null(system):
    c,_,_=system;p=project(c);a=save(c,p).json();pb=save(c,p,content='改为 MySQL').json()
    assert c.post(f'/projects/{p}/entries/{a["id"]}/retract').status_code==200
    path=f'/projects/{p}/entries/{pb["id"]}/resolve'
    assert c.post(path,json={}).status_code==409
    assert c.post(path,json={'expected_id':None}).json()['status']=='active'
