"""服务端 Mem0 适配；成员永远拿不到此服务凭据。"""
from pathlib import Path
import httpx

class Mem0Backend:
    def __init__(self,url,key_file):
        self.url=url.rstrip('/')
        self.key_file=key_file

    def authenticate_admin(self,email,password):
        with httpx.Client(timeout=20,trust_env=False) as c:
            response=c.post(self.url+'/auth/login',json={'email':email,'password':password})
            if response.status_code!=200:return None
            token=response.json()['access_token']
            r=c.get(self.url+'/auth/me',headers={'Authorization':'Bearer '+token})
            if r.status_code!=200 or r.json().get('role')!='admin':return None
            return r.json()

    def request(self,path,data,timeout=20):
        key=Path(self.key_file).read_text().strip()
        with httpx.Client(timeout=timeout,trust_env=False) as c:
            r=c.post(self.url+path,json=data,headers={'X-API-Key':key})
            r.raise_for_status()
            return r.json()

    def delete_vector(self,vector_id):
        key=Path(self.key_file).read_text().strip()
        with httpx.Client(timeout=20,trust_env=False) as c:
            c.delete(self.url+'/memories/'+vector_id,headers={'X-API-Key':key}).raise_for_status()

    def namespace(self,project):return 'waystone:'+project

    def index(self,project,entry):
        ns=self.namespace(project)
        # 若上次提交成功但响应丢失，先按唯一记录 ID 查回，避免重试重复写入。
        existing=self.request('/search',{'query':entry['content'],'filters':{'user_id':ns,'gateway_entry_id':entry['id']},'top_k':1})
        if existing.get('results'):return
        self.request('/memories',{'messages':[{'role':'user','content':entry['content']}],
            'user_id':ns,'infer':False,'metadata':{'gateway_entry_id':entry['id'],'project_id':project,
            'environment':entry.get('environment',''),'branch':entry.get('branch',''),'source_version':entry.get('source_version',''),
            'source':entry['source'],'source_agent':entry['agent'],'author_id':entry['author'],'kind':entry['kind']}})

    def ready(self):
        # A real, read-only embedding + vector query also validates service authentication.
        self.request('/search',{'query':'readiness','filters':{'user_id':'waystone:health-probe'},'top_k':1},timeout=8)

    def search(self,project,query,limit,eligible_ids=None):
        if eligible_ids==[]:return []
        batches=[None] if eligible_ids is None else [eligible_ids[i:i+500] for i in range(0,len(eligible_ids),500)]
        ranked={}
        for batch in batches:
            filters={'user_id':self.namespace(project)}
            if batch is not None:filters['gateway_entry_id']={'in':batch}
            r=self.request('/search',{'query':query,'filters':filters,'top_k':limit})
            for item in r.get('results',[]):
                eid=item.get('metadata',{}).get('gateway_entry_id')
                if eid and (batch is None or eid in batch):ranked[eid]=max(ranked.get(eid,float('-inf')),float(item.get('score',0)))
        return sorted(ranked,key=lambda eid:ranked[eid],reverse=True)[:limit]


    def vector_ids(self,project,entry_id):
        # 按记录 ID 过滤找出该记录的向量；查询词只是占位，命中范围由过滤条件决定。
        ns=self.namespace(project);ids=[]
        found=self.request('/search',{'query':'retracted entry','filters':{'user_id':ns,'gateway_entry_id':entry_id},'top_k':100})
        for item in found.get('results',[]):
            meta=item.get('metadata') or {}
            if meta.get('gateway_entry_id')!=entry_id:continue
            # 归属必须明确匹配：命名空间或项目 ID 缺失、矛盾时既不能删除，也不能当作已清理；抛错让撤回保持 purge_pending，等人工核对。
            if item.get('user_id')!=ns or meta.get('project_id')!=project:raise RuntimeError('vector ownership mismatch')
            ids.append(item['id'])
        return ids
