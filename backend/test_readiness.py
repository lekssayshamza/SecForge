"""Creator capability preservation and explicit first-admin bootstrap safety."""
from pathlib import Path
import importlib.util
from sqlalchemy import select,func,text
from app.models import User,AdminAudit,Room
from app.readiness_migration import migrate
from app.permissions import has_creator_access
from test_operations import ops,env

def bootstrap_module():
 spec=importlib.util.spec_from_file_location('bootstrap_readiness',Path(__file__).parent/'deploy/cloud/bootstrap_admin.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_approved_creator_promotion_retains_ownership_credentials_and_creator_access(ops):
 e=ops
 with e.maker() as db:
  for u in db.scalars(select(User).where(User.role=='admin')):u.role='learner'
  db.commit();u=db.get(User,e.ids['owner']);before=(u.id,u.password_hash,u.email,u.username);owned=db.scalar(select(Room.creator_id).where(Room.id==1))
  bootstrap_module().bootstrap(db,u.id,u.username,'PROMOTE '+str(u.id)+' '+u.username)
  assert (u.id,u.password_hash,u.email,u.username)==before and u.role=='admin' and u.creator_access and has_creator_access(u)
  assert db.scalar(select(Room.creator_id).where(Room.id==1))==owned
  assert db.scalar(select(AdminAudit.action))=='first-admin-bootstrap'
  assert not db.get(User,e.ids['learner']).creator_access
 from app.security import create_access_token
 assert e.client.get('/api/auth/me',headers=e.header('owner')).status_code==401
 header={'Authorization':'Bearer '+create_access_token(e.ids['owner'],'admin',1)}
 for path in ['/api/admin','/api/creator/analytics','/api/rooms','/api/paths']:
  r=e.client.get(path,headers=header);assert r.status_code==200,(path,r.text)
 assert e.client.get('/api/creator/analytics/rooms/2',headers=header).status_code==404
 assert e.client.patch('/api/rooms/2',headers=header,json={'name':'Forbidden'}).status_code==404
 assert e.client.patch('/api/rooms/1',headers=header,json={'description':'Owned content retained'}).status_code==200
 assert e.client.get('/api/auth/me',headers=header).json()['creator_access'] is True

def test_admin_authority_does_not_imply_creator_capability(ops):
 e=ops
 assert not has_creator_access(None)
 with e.maker() as db:assert has_creator_access(db.get(User,e.ids['admin']))
 assert e.client.get('/api/creator/analytics',headers=e.header('admin')).status_code==200
 assert e.client.patch('/api/users/me',headers=e.header('learner'),json={'creator_access':True}).status_code==422
 assert e.client.post('/api/users',json={'username':'baduser','email':'bad@example.com','password':'Some-valid-test-password','creator_access':True}).status_code==422

def test_capability_migration_is_additive_no_automatic_grants(ops):
 e=ops
 with e.engine.begin() as c:
  before=list(c.execute(text('SELECT id,role,password_hash FROM users ORDER BY id')))
  assert migrate(c);assert not migrate(c)
  assert list(c.execute(text('SELECT id,role,password_hash FROM users ORDER BY id')))==before
  assert c.scalar(text('SELECT count(*) FROM users WHERE creator_access'))==0
