"""Opt-in learning E2E against the running LOCAL application and PostgreSQL.

Creates dedicated accounts and one questions-only room; never launches infrastructure.
Run from backend so the existing runtime configuration is selected. No secrets are logged.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import websockets

PROJECT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('smoke', Path(__file__).with_name('browser-smoke.py'))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def api(method, path, data=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request('http://127.0.0.1:8000/api' + path,
        data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response) if response.status != 204 else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Local test API returned HTTP {error.code}') from None


def snapshot():
    from sqlalchemy import select, text
    from app.database import SessionLocal
    from app.models import BuilderEnvironment, MachineTemplate, Room, RoomTemplate
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        result = []
        for model, clause in [(Room, Room.id.in_([1089,3169])),
            (RoomTemplate, RoomTemplate.room_id.in_([1089,3169])),
            (MachineTemplate, MachineTemplate.room_template_id.in_(select(RoomTemplate.id).where(RoomTemplate.room_id.in_([1089,3169])))),
            (BuilderEnvironment, BuilderEnvironment.id == 213)]:
            result.extend([{c.name: str(getattr(row,c.name)) for c in model.__table__.columns}
                for row in db.scalars(select(model).where(clause).order_by(model.id))])
        return hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()


def prepare_accounts():
    from sqlalchemy import select, text
    from app.database import SessionLocal
    from app.models import User
    from app.security import create_access_token
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        admin = db.scalar(select(User).where(User.role == 'admin').order_by(User.id))
        if not admin: raise RuntimeError('An existing authorized administrator is required for a test creator invitation')
        admin_token = create_access_token(admin.id, admin.role)
    tag = secrets.token_hex(5)
    accounts = []
    for role in ('creator', 'learner'):
        password = secrets.token_urlsafe(32)
        user = api('POST','/users', {'username': f'learning_test_{role}_{tag}', 'email': f'{role}_{tag}@example.com', 'password': password})
        if role == 'creator':
            token = api('POST','/auth/login',{'username': user['username'], 'password': password})['access_token']
            invitation = api('POST','/admin/invitations', {'expires_in_days': 1}, admin_token)
            api('POST','/creator/invitations/accept', {'token': invitation['token']}, token)
        accounts.append((user['id'],user['username'],password))
    return accounts


class LiveBrowser(smoke.Browser):
    def __init__(self, ws, output, creator_id):
        super().__init__(ws, output)
        self.creator_id, self.room_id = creator_id, None
        self.created = False

    async def request(self, event):
        request = event['request']; url = urlparse(request['url']); method = request['method']
        if url.path.startswith('/api/'):
            # Record method/path only, never bodies, passwords, flags or headers.
            self.calls.append((method,url.path,None))
            allowed = url.hostname == '127.0.0.1' and url.port == 8000
            path = url.path[4:]
            if method == 'OPTIONS': pass
            elif path in ('/auth/login','/auth/me'): allowed &= method == ('POST' if path.endswith('login') else 'GET')
            elif path == '/rooms':
                allowed &= method == 'GET' or method == 'POST' and not self.created
                if method == 'POST': self.created = True
            else:
                match = re.fullmatch(r'/rooms/(\d+)(.*)',path)
                if match and self.room_id is None and method == 'GET':
                    from app.database import SessionLocal
                    from app.models import Room
                    from sqlalchemy import text
                    with SessionLocal() as db:
                        db.execute(text('SET TRANSACTION READ ONLY'))
                        room = db.get(Room,int(match[1]))
                        if room and room.creator_id == self.creator_id and room.name.startswith('Learning interaction test'):
                            self.room_id = room.id
                allowed &= bool(match and int(match[1]) == self.room_id)
                suffix = match[2] if match else 'invalid'
                allowed &= (method == 'GET' and suffix in ('','/builder','/templates','/test-lab','/lab','/questions','/progress')
                    or method == 'POST' and suffix in ('/questions','/publish','/unpublish')
                    or method == 'PUT' and suffix == '/questions/order'
                    or method in ('PATCH','DELETE') and bool(re.fullmatch(r'/questions/\d+',suffix))
                    or method == 'POST' and bool(re.fullmatch(r'/questions/\d+/submit',suffix)))
            if allowed:
                await self.call('Fetch.continueRequest',dict(requestId=event['requestId']))
            else:
                self.errors.append('Blocked an out-of-scope API operation')
                await self.call('Fetch.failRequest',dict(requestId=event['requestId'],errorReason='BlockedByClient'))
        else:
            await super().request(event)

    async def sign_in(self, account):
        await self.navigate('/login'); await self.wait("!!document.querySelector('#username')")
        await self.fill('#username',account[1]); await self.fill('#password',account[2]); await self.click('Sign in')
        await self.wait("!!document.querySelector('.app-shell')")


async def cleanup_browser(process, profile):
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL); process.wait(timeout=5)
    def workers():
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit(): continue
            try:
                if os.getpgid(int(entry.name)) == process.pid and (entry/'stat').read_text().split(')')[1].split()[0] != 'Z': return True
            except (ProcessLookupError,FileNotFoundError,PermissionError): pass
        return False
    end = time.monotonic()+5
    while workers() and time.monotonic()<end: await asyncio.sleep(.05)
    if workers():
        os.killpg(process.pid,signal.SIGKILL)
        end=time.monotonic()+5
        while workers() and time.monotonic()<end: await asyncio.sleep(.05)
    if workers(): raise RuntimeError('Owned browser workers remain; artifacts preserved')
    shutil.rmtree(profile)


async def run(output):
    from sqlalchemy import select,text
    from app.database import SessionLocal
    from app.models import ChallengeQuestion,LabInstance,QuestionSolve,Room,RoomProgress
    before=snapshot(); creator,learner=prepare_accounts()
    answers=[secrets.token_urlsafe(24),secrets.token_urlsafe(24)]
    profile=output/'owned-browser-profile'; profile.mkdir(mode=0o700)
    process=subprocess.Popen(['/usr/bin/chromium','--headless=new','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+str(profile),'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    result={'passed':False,'test_account_ids':[creator[0],learner[0]],'no_infrastructure_operations':True}
    try:
        end=time.monotonic()+15
        while not (profile/'DevToolsActivePort').exists() and process.poll() is None and time.monotonic()<end: await asyncio.sleep(.1)
        if not (profile/'DevToolsActivePort').exists(): raise RuntimeError('Owned Chromium failed startup')
        port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list',timeout=5) as response: target=next(x for x in json.load(response) if x['type']=='page')
        async with websockets.connect(target['webSocketDebuggerUrl'],max_size=20_000_000) as ws:
            b=LiveBrowser(ws,output,creator[0])
            await b.call('Runtime.enable'); await b.call('Page.enable')
            await b.call('Fetch.enable',dict(patterns=[dict(urlPattern='*',requestStage='Request')]))
            await b.call('Emulation.setDeviceMetricsOverride',dict(width=1440,height=1050,deviceScaleFactor=1,mobile=False))
            await b.sign_in(creator); await b.click('Create room')
            await b.wait("!!document.querySelector('dialog[open] #name')")
            await b.fill('#name','Learning interaction test '+secrets.token_hex(3))
            await b.fill('#description','Dedicated question and progress verification. No AWS environment is required.')
            await b.fill('#category','Learning validation'); await b.click('Create draft room')
            await b.wait("!!document.querySelector('#challenge-questions') && !!document.querySelector('.lab-brief')")
            assert b.room_id, 'Owned test room was not identified'
            result['test_room_id']=b.room_id
            for index in range(3):
                await b.click('Add question'); await b.wait("!!document.querySelector('dialog[open] #question-text')")
                assert await b.evaluate("document.activeElement.id==='question-text'"),'Editor did not receive keyboard focus'
                await b.fill('#question-text',['Identify the first answer','Identify the second answer','Temporary authoring question'][index])
                await b.fill('#question-hint','Use the dedicated test answer supplied during verification.')
                await b.fill('#question-answer',answers[index] if index < 2 else secrets.token_urlsafe(24))
                await b.fill('#question-points',str([100,75,25][index])); await b.click('Save question')
                await b.wait(f"!document.querySelector('dialog[open]') && document.querySelectorAll('.question-item').length==={index+1}")
            await b.click_selector('.question-item:last-child .question-actions button:last-child')
            await b.wait("!!document.querySelector('dialog[open]')"); await b.click_selector('dialog[open] .btn-danger')
            await b.wait("!document.querySelector('dialog[open]') && document.querySelectorAll('.question-item').length===2")
            await b.click_selector('[aria-label="Move question 1 down"]')
            await b.wait("document.querySelector('.question-item h3').textContent==='Identify the second answer'")
            await b.click_selector('[aria-label="Move question 1 down"]')
            await b.wait("document.querySelector('.question-item h3').textContent==='Identify the first answer'")
            await b.click('Preview questions')
            assert not await b.evaluate("!!document.querySelector('.question-item .question-actions')")
            markup=await b.evaluate('document.body.innerHTML')
            assert not any(a in markup for a in answers),'Stored answers leaked into rendered markup'
            await b.themed_screenshots('creator-question-preview'); await b.click('Back to editor')
            await b.click('Publish Room'); await b.wait("!!document.querySelector('dialog[open]')"); await b.click('Publish room')
            await b.wait("document.body.textContent.includes('Unpublish Room')")
            await b.logout(); await b.sign_in(learner)
            await b.navigate('/rooms'); await b.wait("!!document.querySelector('input[type=search]')")
            with SessionLocal() as db:
                db.execute(text('SET TRANSACTION READ ONLY')); name=db.get(Room,b.room_id).name
            await b.fill('input[type=search]',name); await b.wait("document.querySelectorAll('.room-card').length===1")
            await b.click('View challenge'); await b.wait("document.querySelectorAll('.answer-form').length===2")
            token=await b.evaluate("localStorage.getItem('access_token')")
            payload=api('GET',f'/rooms/{b.room_id}/questions',token=token)
            assert not any(a in json.dumps(payload) for a in answers),'Expected answers leaked into the API'
            qids=[q['id'] for q in payload['questions']]
            await b.fill(f'#answer-{qids[0]}','definitely incorrect')
            await b.evaluate(f"document.querySelector('#answer-{qids[0]}').focus()")
            await b.press('Enter','Enter',13); await b.wait("document.body.textContent.includes('Incorrect answer. Try again.')")
            assert await b.evaluate("document.querySelector('.question-progress').textContent.includes('0/175 points')")
            await b.fill(f'#answer-{qids[0]}','  '+answers[0]+'  ')
            await b.evaluate(f"document.querySelector('#answer-{qids[0]}').focus()")
            await b.press('Enter','Enter',13); await b.wait("document.querySelector('.question-progress').textContent.includes('100/175 points')")
            replay=api('POST',f'/rooms/{b.room_id}/questions/{qids[0]}/submit',{'answer':answers[0]},token)
            assert replay['already_solved'] and replay['points_awarded']==0 and replay['progress']['points_earned']==100
            await b.call('Page.reload'); await b.wait("document.querySelector('.question-progress')?.textContent.includes('100/175 points')")
            assert await b.evaluate("document.querySelectorAll('.answer-form').length===1")
            await b.themed_screenshots('learner-partial-progress')
            await b.fill(f'#answer-{qids[1]}',answers[1]); await b.evaluate(f"document.querySelector('#answer-{qids[1]}').focus()")
            await b.press('Enter','Enter',13)
            await b.wait("document.querySelector('.question-progress')?.textContent.includes('Completed') && document.querySelector('.question-progress').textContent.includes('175/175 points')")
            await b.logout(); await b.sign_in(learner); await b.navigate(f'/rooms/{b.room_id}')
            await b.wait("document.querySelector('.question-progress')?.textContent.includes('Completed')")
            for width in (1440,1024,768,390,320):
                await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=1050 if width>=768 else 844,deviceScaleFactor=1,mobile=width<768))
                await b.themed_screenshots(f'learner-completed-{width}')
            await b.call('Emulation.setDeviceMetricsOverride',dict(width=1440,height=1050,deviceScaleFactor=1,mobile=False))
            await b.logout(); await b.sign_in(creator); await b.navigate(f'/rooms/{b.room_id}')
            await b.wait("document.body.textContent.includes('Learner credit is recorded')")
            assert await b.evaluate("[...document.querySelectorAll('#challenge-questions button')].find(b=>b.textContent.includes('Add question')).disabled")
            await b.click_selector('.question-item:first-child .question-actions button:first-child')
            await b.wait("!!document.querySelector('dialog[open] #question-text')")
            assert await b.evaluate("document.querySelector('#question-answer').disabled && document.querySelector('#question-points').disabled")
            await b.fill('#question-hint','Clarified after completion; earned credit is preserved.'); await b.click('Save question')
            await b.wait("!document.querySelector('dialog[open]')")
            with SessionLocal() as db:
                db.execute(text('SET TRANSACTION READ ONLY'))
                p=db.scalar(select(RoomProgress).where(RoomProgress.room_id==b.room_id,RoomProgress.learner_id==learner[0]))
                assert p.points_earned==175 and p.completed_at and len(db.scalars(select(QuestionSolve).where(QuestionSolve.learner_id==learner[0])).all())==2
                assert db.scalar(select(LabInstance.id).where(LabInstance.room_id==b.room_id)) is None
                for q in db.scalars(select(ChallengeQuestion).where(ChallengeQuestion.room_id==b.room_id)):
                    assert q.answer_digest not in answers and len(q.answer_digest)==64
            # Keep disposable content out of normal discovery; preserve its earned credit.
            await b.click('Unpublish Room'); await b.wait("!!document.querySelector('dialog[open]')")
            await b.click('Unpublish room'); await b.wait("document.body.textContent.includes('Publish Room')")
            result['test_room_unpublished_after_verification'] = True
            if b.request_tasks: await asyncio.wait_for(asyncio.gather(*b.request_tasks),10)
            assert not b.errors, 'Browser encountered an exception or blocked operation'
            assert snapshot()==before,'Protected metadata changed'
            result.update(passed=True,actual_password_login=True,question_crud_reorder_preview=True,publication=True,
                incorrect_and_correct_feedback=True,keyboard_submission=True,once_only_points=True,
                refresh_and_logout_persistence=True,room_completed=True,earned_points=175,post_credit_edit_preserves_progress=True,
                answer_confidentiality=True,protected_metadata_unchanged=True,themes=['light','dark'],viewports=[1440,1024,768,390,320])
    finally:
        if 'b' in locals(): b.reader.cancel()
        await cleanup_browser(process,profile)
        result['owned_browser_cleaned']=True
        (output/'results.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result)); print('Sanitized screenshots and results:',output)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-local-learning-write-test',action='store_true',required=True)
    parser.parse_args()
    if Path.cwd()!=PROJECT/'backend': parser.error('Run from the existing backend runtime directory')
    sys.path.insert(0,str(PROJECT/'backend')); logging.disable(logging.CRITICAL); os.umask(0o077)
    output=Path(tempfile.mkdtemp(prefix='secforge-learning-browser-'))
    try: asyncio.run(asyncio.wait_for(run(output),180))
    except Exception as error:
        print('Failure category:',type(error).__name__)
        if isinstance(error,AssertionError) and str(error).startswith('UI condition did not become true:'):
            print(str(error))
        print('FAIL: Local learning browser verification did not complete. Dedicated fixtures retained; no infrastructure operations allowed.')
        print('Owned sanitized artifact directory:',output)
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
