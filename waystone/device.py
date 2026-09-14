import secrets
import time
from pathlib import Path
from fastapi import Depends,HTTPException
from fastapi.responses import HTMLResponse,JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from .store import digest

class DeviceInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    label:str=Field(default='Agent client',min_length=1,max_length=100)
class Poll(BaseModel):
    device_code:str=Field(min_length=1,max_length=128)
class Approval(BaseModel):
    user_code:str=Field(min_length=1,max_length=30)

def install_device_routes(app,store,user,limited):
    @app.post('/device/start',dependencies=[Depends(limited)])
    def start(body:DeviceInput):
        device=secrets.token_urlsafe(32);code=secrets.token_hex(5).upper()
        with store.connect() as c:
            c.execute('DELETE FROM devices WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO devices VALUES(?,?,?,?,NULL)',(digest(device),digest(code),body.label,time.time()+600))
        return {'device_code':device,'user_code':code,'expires_in':600,'interval':5}
    @app.post('/device/inspect',dependencies=[Depends(limited)])
    def inspect(body:Approval):
        with store.connect() as c:
            row=c.execute('SELECT label FROM devices WHERE code_hash=? AND expires>? AND user_id IS NULL',(digest(body.user_code.strip().upper()),time.time())).fetchone()
            if not row:raise HTTPException(400,'授权码无效或已过期')
            return {'label':row['label']}
    @app.post('/device/approve',dependencies=[Depends(limited)])
    def approve(body:Approval,u=Depends(user)):
        with store.connect() as c:
            cur=c.execute('UPDATE devices SET user_id=? WHERE code_hash=? AND expires>? AND user_id IS NULL',(u,digest(body.user_code.strip().upper()),time.time()))
            if cur.rowcount!=1:raise HTTPException(400,'授权码无效或已使用')
        return {'ok':True}
    @app.post('/device/poll',dependencies=[Depends(limited)])
    def poll(body:Poll):
        with store.connect() as c:
            row=c.execute('SELECT * FROM devices WHERE hash=? AND expires>?',(digest(body.device_code),time.time())).fetchone()
            if not row:raise HTTPException(400,'设备授权无效或过期')
            if not row['user_id']:return JSONResponse(status_code=202,content={'status':'pending'})
            token=store.session(c,row['user_id'])
            c.execute('DELETE FROM devices WHERE hash=?',(digest(body.device_code),))
            return token
    @app.get('/activate',response_class=HTMLResponse)
    @app.get('/invite',response_class=HTMLResponse)
    def page():return Path(__file__).with_name('authorization.html').read_text()
