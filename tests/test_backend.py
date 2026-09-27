"""Mem0 适配层：连接复用、Key 只读一次、发布查重不把正文当查询词。"""
import json
import httpx
from waystone.backend import Mem0Backend

def make(tmp_path,handler):
    key=tmp_path/'key';key.write_text('k1\n')
    return Mem0Backend('http://mem0',str(key),transport=httpx.MockTransport(handler)),key

def test_index_dedupe_check_does_not_embed_content(tmp_path):
    seen=[]
    def handler(req):
        seen.append((req.url.path,json.loads(req.content or b'{}'),req.headers.get('x-api-key')))
        return httpx.Response(200,json={'results':[]})
    b,_=make(tmp_path,handler)
    entry={'id':'e1','content':'很长的正文'*80,'source':'s','agent':'a','author':'u','kind':'decision'}
    b.index('p1',entry)
    assert [s[0] for s in seen]==['/search','/memories']
    assert '很长的正文' not in seen[0][1]['query'] and seen[0][1]['filters']=={'user_id':'waystone:p1','gateway_entry_id':'e1'}
    assert seen[1][1]['messages'][0]['content']==entry['content'] and seen[0][2]=='k1'

def test_client_and_key_are_reused(tmp_path):
    b,key=make(tmp_path,lambda req:httpx.Response(200,json={'results':[]}))
    first=b.http();b.ready();key.write_text('k2')
    assert b.http() is first
    b.ready()
    assert b.http() is first
