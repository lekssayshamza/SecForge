"""Private avatar security, immutable ledger summaries and additive migration."""
import io,os,time
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image,PngImagePlugin
from sqlalchemy import text,inspect,select
from test_profiles import env
from app import avatars
from app.models import User,Room,RoomProgress,QuestionSolve,ChallengeQuestion
from app.profile_v2_migration import migrate

@pytest.fixture(autouse=True)
def picture_store(tmp_path,monkeypatch):
    root=tmp_path/'pictures';root.mkdir(mode=0o700)
    monkeypatch.setattr(avatars.settings,'avatar_storage_dir',str(root))
    return root

def picture(fmt='PNG',size=(32,32)):
    out=io.BytesIO();image=Image.new('RGB',size,(100,80,40));kwargs={}
    if fmt=='PNG':
        info=PngImagePlugin.PngInfo();info.add_text('private','metadata should disappear');kwargs['pnginfo']=info
    image.save(out,format=fmt,**kwargs);return out.getvalue()

def upload(env,data=None,mime='image/png',role='learner'):
    return env.client.put('/api/users/me/avatar',content=picture() if data is None else data,headers={**env.headers[role],'Content-Type':mime})

@pytest.mark.parametrize('fmt,mime',[('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp')])
def test_image_is_decoded_reencoded_private_and_persistent(env,picture_store,fmt,mime):
    r=upload(env,picture(fmt),mime);assert r.status_code==200
    key=r.json()['avatar_version'];assert len(key)==32
    path=picture_store/(key+'.png');assert path.stat().st_mode&0o777==0o600
    result=env.client.get('/api/users/me/avatar',headers=env.headers['learner']);assert result.status_code==200
    assert result.headers['cache-control']=='private, no-store'
    with Image.open(io.BytesIO(result.content)) as img:assert img.format=='PNG' and img.size==(256,256) and not img.info
    assert env.client.get('/api/auth/me',headers=env.headers['learner']).json()['avatar_version']==key
    assert env.client.get('/api/users/me/avatar',headers=env.headers['creator']).status_code==404
    assert env.client.get('/api/users/me/avatar').status_code in (401,403)
    assert str(picture_store) not in r.text

@pytest.mark.parametrize('data,mime,status',[(b'<svg><script/></svg>','image/svg+xml',415),(b'<svg/>','image/png',422),(b'#!/bin/sh\n','image/jpeg',422),(picture('JPEG'),'image/png',422),(picture(size=(8,8)),'image/png',422),(picture(size=(2049,20)),'image/png',422),(picture()[:25],'image/png',422),(b'x'*(2*1024*1024+1),'image/png',413)])
def test_invalid_or_malicious_images_rejected(env,picture_store,data,mime,status):
    assert upload(env,data,mime).status_code==status
    assert not list(picture_store.glob('*.png'))
    with env.maker() as db:assert db.get(User,env.ids['learner']).avatar_version is None

def test_animation_rejected(env):
    out=io.BytesIO();frames=[Image.new('RGB',(32,32),c) for c in ('red','green')];frames[0].save(out,format='WEBP',save_all=True,append_images=frames[1:],duration=100,loop=0)
    assert upload(env,out.getvalue(),'image/webp').status_code==422

def test_replace_remove_and_generated_fallback(env,picture_store):
    first=upload(env).json()['avatar_version'];second=upload(env).json()['avatar_version']
    assert second!=first and not (picture_store/(first+'.png')).exists()
    assert env.client.delete('/api/users/me/avatar',headers=env.headers['learner']).json()['avatar_version'] is None
    assert not list(picture_store.glob('*.png'))
    assert env.client.delete('/api/users/me/avatar',headers=env.headers['learner']).status_code==200
    assert env.client.get('/api/users/me/avatar',headers=env.headers['learner']).status_code==404

def test_paths_role_and_other_user_cannot_be_written(env):
    for fields in ({'avatar_version':'../private'},{'role':'admin'},{'bio':'a'*281},{'bio':'bad\x00text'}):assert env.client.patch('/api/users/me',headers=env.headers['learner'],json=fields).status_code==422
    assert env.client.put(f"/api/users/{env.ids['creator']}/avatar",headers=env.headers['learner'],content=picture()).status_code in (404,405)
    r=env.client.patch('/api/users/me',headers=env.headers['learner'],json={'bio':' <script>alert(1)</script> '});assert r.status_code==200 and r.json()['bio']=='<script>alert(1)</script>'

def test_symlinks_and_disk_limit_fail_closed(env,picture_store,monkeypatch,tmp_path):
    monkeypatch.setattr(avatars,'MAX_DISK',1);assert upload(env).status_code==507
    assert not list(picture_store.glob('*.png'))
    alias=tmp_path/'alias';alias.symlink_to(picture_store);monkeypatch.setattr(avatars.settings,'avatar_storage_dir',str(alias));assert upload(env).status_code==503

def test_orphan_sweep_preserves_referenced_unknown_and_recent_files(env,picture_store):
    key=upload(env).json()['avatar_version'];owned=picture_store/('a'*32+'.png');owned.write_bytes(b'old');unknown=picture_store/'important.txt';unknown.write_bytes(b'keep');recent=picture_store/('b'*32+'.png');recent.write_bytes(b'recent')
    for p in (owned,unknown,picture_store/(key+'.png')):os.utime(p,(time.time()-90000,)*2)
    with env.maker() as db:avatars.sweep_owned(picture_store,db)
    assert not owned.exists() and unknown.exists() and recent.exists() and (picture_store/(key+'.png')).exists()

def test_ambiguous_commit_preserves_recoverable_picture(env,picture_store):
    with env.maker() as db:
        original=db.commit
        def accepted_then_lost():original();raise RuntimeError('synthetic lost acknowledgment')
        db.commit=accepted_then_lost
        with pytest.raises(RuntimeError):avatars.save_picture(picture(),'image/png',env.ids['learner'],0,db)
    with env.maker() as db:
        key=db.get(User,env.ids['learner']).avatar_version;assert (picture_store/(key+'.png')).exists()

def seed_history(env,completed=1):
    with env.maker() as db:
        for i in range(6):
            room=Room(name=f'Actual room {i}',creator_id=env.ids['creator'],status='published',category='Linux',difficulty='easy');db.add(room);db.flush()
            for j in range(2):
                q=ChallengeQuestion(room_id=room.id,question_text='Question',answer_salt='s'*32,answer_digest='x'*64,points=25,display_order=j+1);db.add(q);db.flush()
                if i<5 and (j==0 or i<completed):db.add(QuestionSolve(learner_id=env.ids['learner'],question_id=q.id,points_awarded=25))
            if i<completed:db.add(RoomProgress(learner_id=env.ids['learner'],room_id=room.id,points_earned=9999,completed_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)))
        db.commit()

def test_empty_learning_is_honest_private_and_read_only(env):
    for path in ('learning','activity'):assert env.client.get('/api/users/me/'+path).status_code in (401,403)
    r=env.client.get('/api/users/me/learning',headers=env.headers['learner']);assert r.status_code==200
    value=r.json();assert value['stats']==dict(points=0,rooms_completed=0,rooms_in_progress=0,questions_solved=0)
    assert all(not a['earned'] and a['earned_at'] is None for a in value['achievements'])
    assert not value['activity'] and not value['recommendations'];assert r.headers['cache-control']=='private, no-store'

@pytest.mark.parametrize('completed',[1,5])
def test_real_ledger_completion_badges_and_no_duplicate_awards(env,completed):
    seed_history(env,completed)
    values=[env.client.get('/api/users/me/learning',headers=env.headers['learner']).json() for _ in range(2)]
    assert values[0]==values[1];v=values[0]
    assert v['stats']==dict(points=(5+completed)*25,questions_solved=5+completed,rooms_completed=completed,rooms_in_progress=5-completed)
    assert [a['earned'] for a in v['achievements']]==[True,True,completed==5]
    assert len(v['recommendations'])==1
    if completed==1:assert len(v['continue_learning'])==4 and all(r['percentage']==50 for r in v['continue_learning'])
    other=env.client.get('/api/users/me/learning',headers=env.headers['creator']).json();assert other['stats']['points']==0 and not other['activity']
    assert not any(word in str(v) for word in ('answer_digest','password_hash','ami_id','email','token_version'))

def test_private_rooms_hide_metadata_but_keep_earned_history(env):
    seed_history(env)
    with env.maker() as db:
        room=db.scalar(select(Room).where(Room.name=='Actual room 0'));room.status='draft';db.commit()
    v=env.client.get('/api/users/me/learning',headers=env.headers['learner']).json()
    assert v['completed_rooms'][0]['name']=='Unavailable room' and v['completed_rooms'][0]['questions']==0 and v['completed_rooms'][0]['percentage'] is None
    assert 'Actual room 0' not in str(v)

@pytest.mark.parametrize('query',['page=0','page=1001','page_size=51','page_size=-1'])
def test_activity_pagination_bounds(env,query):assert env.client.get('/api/users/me/activity?'+query,headers=env.headers['learner']).status_code==422

def test_activity_pagination_and_progress_after_stop(env):
    seed_history(env)
    r=env.client.get('/api/users/me/activity?page_size=2',headers=env.headers['learner']);assert r.status_code==200 and r.json()['total']==7 and len(r.json()['items'])==2
    r2=env.client.get('/api/users/me/activity?page_size=2&page=2',headers=env.headers['learner']);assert not {i['id'] for i in r.json()['items']} & {i['id'] for i in r2.json()['items']}
    with env.maker() as db:assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(QuestionSolve))==6

def test_additive_migration_idempotent_and_preserves_accounts(env):
    with env.maker() as db:
        db.execute(text('ALTER TABLE users DROP COLUMN bio'));db.execute(text('ALTER TABLE users DROP COLUMN avatar_version'));db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.get(User,env.ids['learner']).username=='learner_profile' and db.get(User,env.ids['learner']).bio is None

def test_backup_lock_blocks_edits_with_bounded_retry(env,picture_store,monkeypatch):
    import fcntl
    lock=picture_store/'.backup.lock';lock.write_bytes(b'');lock.chmod(0o600)
    fd=os.open(lock,os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX)
    monkeypatch.setattr(avatars,'MAX_LOCK_WAIT',-1)
    try:
        with env.maker() as db:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc:avatars.save_picture(picture(),'image/png',env.ids['learner'],0,db)
            assert exc.value.status_code==503
    finally:os.close(fd)
    assert not list(picture_store.glob('*.png'))
