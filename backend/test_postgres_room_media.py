"""Real PostgreSQL image FK, migration, publication and deletion races; offline only."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL concurrency runner',allow_module_level=True)
from fastapi import HTTPException
from sqlalchemy import select,func,text
from app.models import Room,RoomDraft,RoomMedia,RoomRelease,RoomReleaseMedia,User
from app.room_media import register_release_media,remove
from app.room_media_migration import migrate
from test_postgres_lab_concurrency import pg_sessions
from test_postgres_challenges import seed,interleave


def test_postgres_image_migration_reruns_and_preserves_data(pg_sessions):
    room,owner,*_=seed(pg_sessions)
    with pg_sessions() as db:
        before=db.execute(text('SELECT id,username,role FROM users ORDER BY id')).all()
        db.execute(text('DROP TABLE room_release_media'));db.execute(text('DROP TABLE room_media'));db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.execute(text('SELECT id,username,role FROM users ORDER BY id')).all()==before
        item=RoomMedia(id='a'*32,room_id=room,content_digest='b'*64,media_type='image/png',width=80,height=40,byte_size=100)
        db.add(item);db.commit()
        release=RoomRelease(room_id=room,document={'tasks':[{'instructions':f'![Diagram](/api/rooms/{room}/media/{item.id})'}]},published_by=owner)
        db.add(release);register_release_media(db,release,release.document);db.commit()
        assert db.scalar(select(func.count()).select_from(RoomReleaseMedia))==1
        db.execute(text('DELETE FROM room_releases WHERE id=:id'),{'id':release.id});db.commit()
        assert db.scalar(select(func.count()).select_from(RoomReleaseMedia))==0 and db.get(RoomMedia,item.id)
        db.execute(text('DELETE FROM rooms WHERE id=:id'),{'id':room});db.commit()
        assert db.scalar(select(func.count()).select_from(RoomMedia))==0


@pytest.mark.parametrize('publish_first',[True,False])
def test_publication_and_unused_image_deletion_serialize(pg_sessions,monkeypatch,tmp_path,publish_first):
    from app.room_workspace import publish_workspace,PublishWrite,edit_workspace,WorkspaceWrite
    from app.room_media import owned
    from contextlib import nullcontext
    monkeypatch.setattr('app.room_media.picture_guard',lambda:nullcontext())
    monkeypatch.setattr('app.room_media.storage',lambda:tmp_path)
    room,owner,*_=seed(pg_sessions)
    key='c'*32
    with pg_sessions() as db:
        db.get(Room,room).room_type='THEORY';db.get(Room,room).status='draft'
        db.add(RoomMedia(id=key,room_id=room,content_digest='d'*64,media_type='image/png',width=40,height=40,byte_size=100));db.commit()
        revision=edit_workspace(room,WorkspaceWrite(revision=0,action='task_create',data={'title':'Diagram lesson','instructions':f'![Diagram](/api/rooms/{room}/media/{key})'}),db.get(User,owner),db).revision
    # Saved drafts already protect deletion; the shared room row lock also
    # serializes publication so a reference cannot appear during deletion.
    def publication(db):return publish_workspace(room,PublishWrite(revision=revision),db.get(User,owner),db)
    def deletion(db):return remove(room,key,db.get(User,owner),db)
    r=interleave(pg_sessions,publication if publish_first else deletion,deletion if publish_first else publication,lock_entity=Room)
    assert r['first'][0]==(200 if publish_first else 409)
    assert r['second'][0]==(409 if publish_first else 200)
    with pg_sessions() as db:
        assert db.get(RoomMedia,key) and db.scalar(select(func.count()).select_from(RoomReleaseMedia))==1


def test_room_categories_real_migration_rerun_and_secondary_filter(pg_sessions):
    from app.models import RoomCategory
    from app.categories import set_room_categories
    from app.room_categories_migration import migrate as category_migrate
    from app.discovery import room_page
    room,owner,*_=seed(pg_sessions)
    with pg_sessions() as db:
        db.get(Room,room).category='linux';db.commit()
        before=db.execute(text('SELECT id,name,creator_id,status FROM rooms ORDER BY id')).all()
        db.execute(text('DROP TABLE room_categories'));db.commit()
        assert category_migrate(db.connection());db.commit();assert not category_migrate(db.connection());db.commit()
        assert db.execute(text('SELECT id,name,creator_id,status FROM rooms ORDER BY id')).all()==before
        target=db.get(Room,room);set_room_categories(target,['linux','web-security']);db.commit()
        for category in ['linux','web-security','linux,web-security']:
            page=room_page(db,category=category);assert page.total==1 and page.items[0].categories==['linux','web-security']
        db.execute(text('ANALYZE room_categories'));db.commit()
        plan='\n'.join(db.scalars(text('EXPLAIN (ANALYZE,BUFFERS) SELECT room_id FROM room_categories WHERE category=\'web-security\'')))
        assert 'Execution Time:' in plan


def test_creator_key_migration_and_concurrent_stale_registration(pg_sessions,monkeypatch):
    from app.models import CreatorSshKey
    from app.creator_keys import KeyWrite,save_key
    from app.creator_keys_migration import migrate as keys_migrate
    from test_creator_keys import key
    _,owner,*_=seed(pg_sessions)
    monkeypatch.setattr('app.creator_keys.management_fingerprint',lambda:'SHA256:isolated-management')
    with pg_sessions() as db:
        before=db.execute(text('SELECT id,username,role,password_hash FROM users ORDER BY id')).all()
        db.execute(text('DROP TABLE creator_ssh_keys'));db.commit()
        assert keys_migrate(db.connection());db.commit();assert not keys_migrate(db.connection());db.commit()
        assert db.execute(text('SELECT id,username,role,password_hash FROM users ORDER BY id')).all()==before
    first,second=key(),key()
    r=interleave(pg_sessions,lambda db:save_key(KeyWrite(public_key=first,revision=0),db.get(User,owner),db),lambda db:save_key(KeyWrite(public_key=second,revision=0),db.get(User,owner),db),lock_entity=User)
    assert r['first'][0]==200 and r['second'][0]==409
    with pg_sessions() as db:assert db.get(CreatorSshKey,owner).public_key==first


@pytest.mark.parametrize('admin_first',[True,False])
def test_personal_key_registration_and_admin_guest_revocation_do_not_deadlock(pg_sessions,monkeypatch,admin_first):
    from datetime import datetime,timedelta
    from app.models import CreatorSshKey,CreatorBuilderKey,BuilderEnvironment
    from app.creator_keys import KeyWrite,save_key,apply_to_builder
    from test_creator_keys import key
    room,owner,*_=seed(pg_sessions)
    monkeypatch.setattr('app.creator_keys.management_fingerprint',lambda:'SHA256:isolated-management')
    monkeypatch.setattr('app.creator_keys.run_guest',lambda builder,registered:None)
    with pg_sessions() as db:
        actor=User(username='ssh_key_admin',email='ssh-key-admin@example.com',role='admin',password_hash='unused');db.add(actor);db.flush();admin=actor.id
        guest=BuilderEnvironment(room_id=room,creator_id=owner,status='running',instance_id='i-0123456789abcdef0',private_ip='10.0.1.25',terraform_state_path='offline-only',expires_at=datetime.utcnow()+timedelta(hours=1));db.add(guest);db.flush();bid=guest.id
        db.add(CreatorSshKey(user_id=owner,public_key=key(),fingerprint='SHA256:old-isolated',revision=2,enabled=False,pending=True));db.add(CreatorBuilderKey(builder_id=bid,user_id=owner,revision=1,enabled=True));db.commit()
    new=key()
    def registration(db):return save_key(KeyWrite(public_key=new,revision=2),db.get(User,owner),db)
    def removal(db):return apply_to_builder(db,db.get(BuilderEnvironment,bid),db.get(User,admin),True)
    r=interleave(pg_sessions,removal if admin_first else registration,registration if admin_first else removal,lock_entity=CreatorSshKey if admin_first else User)
    assert r['first'][0]==200 and r['second'][0]==(200 if admin_first else 403)
    with pg_sessions() as db:
        assert db.get(CreatorSshKey,owner).enabled and db.get(CreatorSshKey,owner).revision==3
        assert db.get(CreatorBuilderKey,bid).revision==3


def test_path_creator_options_real_postgres_filters_points_and_query_plan(pg_sessions):
    from fastapi import Response
    from app.learning_paths import eligible
    from app.models import RoomCategory
    from sqlalchemy import event
    import time
    room,owner,*_=seed(pg_sessions)
    with pg_sessions() as db:
        db.get(Room,room).category='linux'
        db.add_all([Room(creator_id=owner,name=f'Cohort fixture {i:04}',description='Linux investigation',category='linux',difficulty='easy',room_type='THEORY',status='published') for i in range(2000)])
        db.flush();ids=list(db.scalars(select(Room.id).where(Room.name.like('Cohort fixture %')).order_by(Room.id)))
        db.add_all([RoomCategory(room_id=i,category='web-security') for i in ids]);db.commit()
        statements=[]
        def observed(*args):statements.append(args[2])
        event.listen(db.bind,'before_cursor_execute',observed)
        started=time.monotonic()
        result=eligible(Response(),q='  COHORT investigation ',category='web-security',difficulty='easy',page=1,user=db.get(User,owner),db=db)
        elapsed=(time.monotonic()-started)*1000
        event.remove(db.bind,'before_cursor_execute',observed)
        assert len(result.items)==24 and result.has_more and all(r.points_total==0 and r.publication_status=='published' for r in result.items)
        assert len(statements)<=5 and elapsed<5000
        db.execute(text('ANALYZE room_categories'));db.commit()
        plan='\n'.join(db.scalars(text("EXPLAIN (ANALYZE,BUFFERS) SELECT room_id FROM room_categories WHERE category='web-security' ORDER BY room_id LIMIT 24")))
        assert 'Index' in plan and 'Execution Time:' in plan
        print(f'PERFORMANCE creator_room_picker rooms=2000 bounded=24 queries={len(statements)} milliseconds={elapsed:.1f} category_index=true')
