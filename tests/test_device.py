from test_service import system

def test_device_requires_approval_and_is_single_use(system):
    c,_,_=system
    start=c.post('/device/start',json={'label':'test laptop'}).json()
    assert c.post('/device/poll',json={'device_code':start['device_code']}).status_code==202
    assert c.post('/device/approve',json={'user_code':start['user_code']}).status_code==200
    token=c.post('/device/poll',json={'device_code':start['device_code']}).json()['token']
    assert c.get('/projects',headers={'Authorization':'Bearer '+token}).status_code==200
    assert c.post('/device/poll',json={'device_code':start['device_code']}).status_code==400

def test_device_expiry_and_bad_code(system):
    c,_,app=system
    start=c.post('/device/start',json={'label':'test laptop'}).json()
    with app.state.store.connect() as db:db.execute('UPDATE devices SET expires=0')
    assert c.post('/device/approve',json={'user_code':start['user_code']}).status_code==400
    assert c.post('/device/poll',json={'device_code':'invalid'}).status_code==400
