"""隔离项目元数据，使用真实 Mem0 验证发布/协作/隔离；只清理本次测试向量。"""
import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from waystone.api import create_app
from waystone.backend import Mem0Backend
from waystone.store import password_hash

backend=Mem0Backend(os.environ['MEM0_URL'],os.environ['MEM0_KEY_FILE'])
projects=[]
with tempfile.TemporaryDirectory() as temp:
    app=create_app(temp+'/smoke.sqlite',backend)
    client=TestClient(app)
    password=secrets.token_urlsafe(24)
    with app.state.store.connect() as c:
        c.execute('INSERT INTO users VALUES(?,?,?,?,NULL)',('test-owner','owner@example.invalid','Smoke Owner',password_hash(password)))
    session=client.post('/auth/login',json={'email':'owner@example.invalid','password':password}).json()
    client.headers['Authorization']='Bearer '+session['token']
    try:
        p=client.post('/projects',json={'name':'隔离验收-'+uuid.uuid4().hex}).json()['id'];projects.append(p)
        q=client.post('/projects',json={'name':'禁止跨项目-'+uuid.uuid4().hex}).json()['id'];projects.append(q)
        invite=client.post(f'/projects/{p}/invites',json={'email':'member@example.invalid'}).json()['invite']
        joined=client.post('/auth/join',json={'email':'member@example.invalid','name':'Smoke Member','password':secrets.token_urlsafe(24),'invite':invite}).json()
        member={'Authorization':'Bearer '+joined['token']}
        entry={'content':'星舟项目技术说明统一使用简体中文。','topic':'语言规范','source':'smoke-test','kind':'convention','agent':'agent-a'}
        r=client.post(f'/projects/{p}/entries',json=entry)
        assert r.status_code==200,r.text
        assert r.json()['index_status']=='ready',r.text
        eid=r.json()['id']
        assert client.post(f'/projects/{p}/entries',json=entry).json()['id']==eid
        recalled=client.post(f'/projects/{p}/recall',headers=member,json={'query':'技术说明使用什么语言？'}).json()
        assert recalled['entries'] and recalled['entries'][0]['id']==eid,recalled
        assert client.get(f'/projects/{q}/entries',headers=member).status_code==403
        proposed=client.post(f'/projects/{p}/entries',json={**entry,'content':'星舟项目改用英文技术说明。'}).json()
        assert proposed['status']=='proposed'
        resolved=client.post(f'/projects/{p}/entries/{proposed["id"]}/resolve',json={'expected_id':eid})
        assert resolved.status_code==200 and resolved.json()['index_status']=='ready',resolved.text
        latest=client.post(f'/projects/{p}/recall',headers=member,json={'query':'技术说明语言'}).json()
        assert [e['id'] for e in latest['entries']]==[proposed['id']],latest
        # Delete ONLY this isolated test project's vectors, then rebuild from its SQL.
        key=Path(os.environ['MEM0_KEY_FILE']).read_text().strip()
        with httpx.Client(base_url=os.environ['MEM0_URL'],headers={'X-API-Key':key},timeout=30,trust_env=False) as upstream:
            rows=upstream.get('/memories',params={'user_id':backend.namespace(p)}).json()['results']
            for row in rows:
                assert row['user_id']==backend.namespace(p)
                upstream.delete('/memories/'+row['id']).raise_for_status()
        rebuilt=client.post(f'/projects/{p}/reindex',json={'full':True})
        assert rebuilt.status_code==200 and rebuilt.json()['pending']==0
        restored=client.post(f'/projects/{p}/recall',json={'query':'技术说明语言'}).json()
        assert [e['id'] for e in restored['entries']]==[proposed['id']]
        # A different environment must not consume the scoped query's candidate budget.
        scoped=client.post(f'/projects/{p}/entries',json={**entry,'environment':'prod','branch':'main','source_version':'smoke-v04'}).json()
        scoped_result=client.post(f'/projects/{p}/recall',json={'query':'技术说明语言','environment':'prod','branch':'main'}).json()
        assert [e['id'] for e in scoped_result['entries']]==[scoped['id']],scoped_result
        assert client.get('/ready').status_code==200
        print('Real index loss/rebuild, scoped search and readiness passed')
        from concurrent.futures import ThreadPoolExecutor
        import time
        identities=[session['token'],joined['token']]
        for i in range(8):
            mail=f'smoke-{i}@example.invalid'
            invitation=client.post(f'/projects/{p}/invites',json={'email':mail}).json()['invite']
            identity=client.post('/auth/join',json={'invite':invitation,'email':mail,'name':'Smoke','password':secrets.token_urlsafe(24)}).json()
            identities.append(identity['token'])
        started=time.monotonic()
        def concurrent_read(token):
            r=client.post(f'/projects/{p}/recall',headers={'Authorization':'Bearer '+token},json={'query':'技术说明语言','environment':'prod','branch':'main'})
            assert r.status_code==200 and r.json()['entries']
        with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(concurrent_read,identities))
        print('10 isolated members concurrent real recall passed, seconds:',round(time.monotonic()-started,2))
        # 撤回必须同时抹掉 SQL 正文和真实 Mem0 中该记录的向量；放在并发召回之后，避免影响按范围召回的断言。
        retracted=client.post(f'/projects/{p}/entries/{scoped["id"]}/retract').json()
        assert retracted['status']=='retracted' and retracted['index_status']=='purged',retracted
        with httpx.Client(base_url=os.environ['MEM0_URL'],headers={'X-API-Key':key},timeout=30,trust_env=False) as upstream:
            rows=upstream.get('/memories',params={'user_id':backend.namespace(p)}).json()['results']
        assert not any((r.get('metadata') or {}).get('gateway_entry_id')==scoped['id'] for r in rows),rows
        print('Real retract purge passed')
        assert client.delete(f'/projects/{p}/members/{joined["user_id"]}').status_code==200
        assert client.get(f'/projects/{p}/entries',headers=member).status_code==403
        print(json.dumps({'success':True,'real_mem0':True,'two_member_sharing':True,'cross_project_denied':True,'dedupe':True,'conflict_resolution':True,'revocation':True},ensure_ascii=False))
    finally:
        key=Path(os.environ['MEM0_KEY_FILE']).read_text().strip()
        with httpx.Client(base_url=os.environ['MEM0_URL'],headers={'X-API-Key':key},timeout=30,trust_env=False) as c:
            removed=0
            for p in projects:
                ns=backend.namespace(p)
                response=c.get('/memories',params={'user_id':ns});response.raise_for_status()
                for item in response.json().get('results',[]):
                    assert item['user_id']==ns
                    r=c.delete('/memories/'+item['id']);r.raise_for_status();removed+=1
            print('本次验收向量已清理：'+str(removed))
