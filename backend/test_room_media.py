"""Lesson image privacy, real decoding, publication and storage integrity; offline."""
import io
import os
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import select, func, inspect, text
from test_challenges import env, add, publish
from test_room_workspace import theory, edit, release, read
from app.models import Room, RoomDraft, RoomMedia, RoomReleaseMedia, RoomRelease, User
from app.room_media import decode_image, image_ids, path_for
from app.room_media_migration import migrate

@pytest.fixture(autouse=True)
def private_storage(tmp_path, monkeypatch):
    root = tmp_path / 'avatars';root.mkdir(mode=0o700)
    monkeypatch.setattr('app.avatars.AVATAR_ROOT', str(root), raising=False)
    monkeypatch.setattr('app.room_media.avatar_storage', lambda: root)
    monkeypatch.setattr('app.room_media.picture_guard', lambda: __import__('contextlib').nullcontext())
    return root

def png(color='blue', fmt='PNG', size=(80,40)):
    out=io.BytesIO();image=Image.new('RGB',size,color)
    info=PngImagePlugin.PngInfo();info.add_text('private-metadata','must disappear')
    image.save(out,fmt,**({'pnginfo':info} if fmt=='PNG' else {}));return out.getvalue()

def put(e, data=None, role='creator', mime='image/png', room=None):
    return e.client.post(f'/api/rooms/{room or e.room}/media', content=png() if data is None else data,headers={**e.headers[role],'Content-Type':mime})

def get(e,item,role='learner'):
    return e.client.get(item['url'],headers=e.headers[role])

def task_image(e,item):
    return edit(e,'task_create',{'title':'Illustrated lesson','instructions':f'![A useful explanation]({item["url"]})'})

def test_upload_reencodes_and_deduplicates_without_metadata(env,private_storage):
    response=put(env);assert response.status_code==201,response.text
    item=response.json();assert set(item)=={'id','room_id','url','media_type','width','height','byte_size','created_at','in_use'}
    assert put(env).json()['id']==item['id']
    result=get(env,item,'creator');assert result.status_code==200
    assert b'must disappear' not in result.content
    assert result.headers['cache-control']=='private, no-store' and result.headers['x-content-type-options']=='nosniff'
    assert len(list((private_storage/'room-media').iterdir()))==1
    assert (private_storage/'room-media').stat().st_mode&0o777==0o700
    assert next((private_storage/'room-media').iterdir()).stat().st_mode&0o777==0o600
    assert env.client.get(item['url']).status_code==401
    assert get(env,item).status_code==404
    assert get(env,item,'other').status_code==404 and get(env,item,'admin').status_code==404
    assert put(env,role='learner').status_code==403 and put(env,role='other').status_code==404

@pytest.mark.parametrize('fmt,mime',[('JPEG','image/jpeg'),('WEBP','image/webp'),('PNG','image/png')])
def test_supported_formats_preserve_aspect_and_bound_dimensions(fmt,mime):
    body,w,h=decode_image(png(fmt=fmt,size=(2400,1200)),mime)
    assert (w,h)==(1920,960)
    with Image.open(io.BytesIO(body)) as i:assert i.format==fmt and not i.info.get('exif')

@pytest.mark.parametrize('body,mime,status',[(b'<svg onload="alert(1)"/>','image/svg+xml',415),(b'<svg/>','image/png',422),(b'X'*2097153,'image/png',413),(b'','image/png',413),(b'<script>bad</script>','image/webp',422)])
def test_malicious_uploads_rejected(env,body,mime,status):
    assert put(env,body,mime=mime).status_code==status
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(RoomMedia))==0

@pytest.mark.parametrize('size',[(15,40),(4097,30),(4000,4000)])
def test_dimensions_rejected(env,size):assert put(env,png(size=size)).status_code==422

def test_mismatched_mime_and_animated_images_rejected(env):
    assert put(env,mime='image/jpeg').status_code==422
    stream=io.BytesIO();Image.new('RGBA',(32,32),'red').save(stream,'PNG',save_all=True,append_images=[Image.new('RGBA',(32,32),'blue')],duration=10)
    assert put(env,stream.getvalue()).status_code==422

def test_publication_only_grants_latest_explicit_images(env):
    theory(env);add(env)
    used=put(env).json();unused=put(env,png('red')).json()
    assert task_image(env,used).status_code==200
    assert get(env,used).status_code==404
    assert release(env).status_code==200
    assert get(env,used).status_code==200 and get(env,unused).status_code==404
    assert 'media' not in env.client.get(f'/api/public/rooms/{env.room}').text
    task=read(env)['tasks'][0]['id']
    assert edit(env,'task_update',{'id':task,'title':'Illustrated lesson','instructions':'Replaced illustration'}).status_code==200
    assert get(env,used).status_code==200
    assert release(env).status_code==200 and get(env,used).status_code==404
    # Historical assets remain available to their owner and cannot be removed.
    assert get(env,used,'creator').status_code==200
    assert env.client.delete(used['url'],headers=env.headers['creator']).status_code==409
    assert env.client.delete(unused['url'],headers=env.headers['creator']).status_code==204
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(RoomReleaseMedia))==1

def test_unpublish_revokes_images_and_saved_drafts_prevent_deletion(env):
    theory(env);item=put(env).json();task_image(env,item)
    assert env.client.delete(item['url'],headers=env.headers['creator']).status_code==409
    assert release(env).status_code==200
    with env.maker() as db:db.get(Room,env.room).status='draft';db.commit()
    assert get(env,item).status_code==404

@pytest.mark.parametrize('markup', ['![alt](https://outside.invalid/x.png)','![](/api/rooms/1/media/'+'a'*32+')','![alt](/api/rooms/999/media/'+'a'*32+')','![alt](/api/rooms/1/media/'+'f'*32+')'])
def test_invalid_image_publish_rolls_back_existing_publication(env,markup):
    theory(env);add(env);assert release(env).status_code==200
    original=env.client.get(f'/api/public/rooms/{env.room}').json()
    edit(env,'metadata',{'name':'Must not become public'})
    edit(env,'task_create',{'title':'Untrusted','instructions':markup})
    assert release(env).status_code==422
    assert env.client.get(f'/api/public/rooms/{env.room}').json()==original
    assert read(env)['dirty']

def test_parser_does_not_publish_code_examples_or_link_mentions():
    source='/api/rooms/12/media/'+'a'*32
    assert image_ids({'tasks':[{'instructions':f'```md\n![alt]({source})\n```\n\n[Link]({source})\n`![alt]({source})`'}]},12,True)==set()
    assert image_ids({'tasks':[{'instructions':f'![Diagram]({source})'}]},12,True)=={'a'*32}

def test_storage_tamper_path_and_symlink_rejected(env,private_storage):
    item=put(env).json();root=private_storage/'room-media';file=next(root.iterdir())
    file.write_bytes(b'corrupt');assert get(env,item,'creator').status_code==404
    file.unlink();file.symlink_to('/etc/passwd');assert get(env,item,'creator').status_code==404
    assert env.client.get(f'/api/rooms/{env.room}/media/../../etc/passwd',headers=env.headers['creator']).status_code==404

def test_only_old_unregistered_files_are_swept(env,private_storage):
    saved=put(env).json();root=private_storage/'room-media'
    old=root/('a'*32+'.png');old.write_bytes(png('green'));os.utime(old,(time.time()-8*86400,)*2)
    recent=root/('b'*32+'.png');recent.write_bytes(png('green'))
    unrecognized=root/'keep-me.txt';unrecognized.write_text('safe')
    registered=next(p for p in root.iterdir() if p.stem==saved['id']);os.utime(registered,(time.time()-8*86400,)*2)
    assert put(env,png('red')).status_code==201
    assert not old.exists() and recent.exists() and registered.exists() and unrecognized.exists()

def test_quota_and_pagination_bounded(env):
    put(env)
    with patch('app.room_media.MAX_OWNER_IMAGES',1):assert put(env,png('red')).status_code==507
    assert env.client.get(f'/api/rooms/{env.room}/media?page_size=25',headers=env.headers['creator']).status_code==422
    assert env.client.get(f'/api/rooms/{env.room}/media',headers=env.headers['learner']).status_code==403
    result=env.client.get(f'/api/rooms/{env.room}/media',headers=env.headers['creator']);assert result.status_code==200 and result.json()['total']==1

def test_additive_migration_rerun_preserves_rooms(env):
    with env.maker() as db:engine=db.get_bind()
    with engine.begin() as c:
        before=c.execute(text('SELECT id,name,creator_id FROM rooms ORDER BY id')).all()
        RoomReleaseMedia.__table__.drop(c);RoomMedia.__table__.drop(c)
        assert migrate(c) is True
        assert migrate(c) is False
        assert c.execute(text('SELECT id,name,creator_id FROM rooms ORDER BY id')).all()==before
        assert set(inspect(c).get_table_names()) >= {'room_media','room_release_media'}
