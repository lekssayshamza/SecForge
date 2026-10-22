"""Opt-in Chromium UI smoke test. ALL /api requests are fulfilled with synthetic fixtures.

No application records, VPN profiles or infrastructure are accessed or mutated.
Requires the existing Vite server, Chromium and the already-installed websockets module.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

import websockets


class CommandError(AssertionError):
    def __init__(self, method, reason):
        super().__init__(f'CDP command failed: {method}')
        self.reason = reason


class Browser:
    def __init__(self, ws, output):
        self.ws, self.output = ws, output
        self.seq, self.pending, self.errors, self.calls = 0, {}, [], []
        self.role, self.room_status, self.lab, self.builder = 'learner', 'published', None, None
        self.fail, self.hold, self.held = None, None, []
        self.fail_status, self.fail_method = 503, None
        self.versions = [self.template(7, 2, 'available'), self.template(6, 1, 'failed')]
        self.request_tasks, self.cancelled_static_requests = set(), 0
        self.reader = asyncio.create_task(self.read())

    def template(self, identity, version, status):
        return dict(id=identity, room_id=42, version=version, created_by=8, is_published=False,
                    created_at='2026-10-11T09:00:00', machines=[dict(id=identity, status=status,
                    ami_id='ami-synthetic-test-only' if status == 'available' else None)])

    def room(self):
        return dict(id=42, creator_id=8, name='The Forgotten Web Server', description='Investigate an overlooked internal service. Explore the web application and locate the challenge flag.\nConnect through your private VPN before starting.', category='Web security', difficulty='easy', status=self.room_status, created_at='2026-10-10T09:00:00', updated_at='2026-10-11T09:00:00')

    def environment(self, status):
        return dict(id=21, room_id=42, status=status, private_ip='10.0.1.42' if status == 'running' else None,
                    instance_id=None, security_group_id=None, terraform_state_path='synthetic-only', error_message=None,
                    expires_at='2026-10-11T15:00:00', created_at='2026-10-11T09:00:00', updated_at='2026-10-11T09:00:00', stopped_at=None)

    async def call(self, method, params=None):
        self.seq += 1
        ident = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        await self.ws.send(json.dumps(dict(id=ident, method=method, params=params or {})))
        result = await asyncio.wait_for(future, 15)
        if 'error' in result:
            raise CommandError(method, result['error'].get('message',''))
        return result.get('result', {})

    async def read(self):
        async for raw in self.ws:
            data = json.loads(raw)
            if 'id' in data:
                future = self.pending.pop(data['id'], None)
                if future and not future.done():
                    future.set_result(data)
            elif data.get('method') == 'Fetch.requestPaused':
                task = asyncio.create_task(self.request(data['params']))
                self.request_tasks.add(task)
                def completed(done):
                    self.request_tasks.discard(done)
                    if not done.cancelled() and done.exception() is not None:
                        self.errors.append('Request interception failed')
                task.add_done_callback(completed)
            elif data.get('method') == 'Runtime.exceptionThrown':
                self.errors.append('Uncaught JavaScript exception')

    async def request(self, event):
        request = event['request']
        url = urlparse(request['url'])
        if url.path.startswith('/api/'):
            path = url.path[4:]
            self.calls.append((request['method'], path, request.get('postData')))
            if path == self.hold and request['method'] == 'POST':
                self.held.append(event)
                return
            await self.respond(event)
        elif url.hostname in ('127.0.0.1', 'fonts.googleapis.com', 'fonts.gstatic.com'):
            try:
                await self.call('Fetch.continueRequest', dict(requestId=event['requestId']))
            except CommandError as error:
                # Navigation may cancel an old static request before continuation.
                # API fulfilment errors and other protocol failures remain test failures.
                if error.reason not in ('Invalid InterceptionId.', 'Invalid interceptionId.'):
                    raise
                self.cancelled_static_requests += 1
        else:
            await self.call('Fetch.failRequest', dict(requestId=event['requestId'], errorReason='BlockedByClient'))

    async def respond(self, event):
        request = event['request']
        path = urlparse(request['url']).path[4:]
        method = request['method']
        status, body = 200, None
        headers = [{'name': 'Content-Type', 'value': 'application/json'}, {'name': 'Access-Control-Allow-Origin', 'value': '*'}, {'name': 'Access-Control-Allow-Headers', 'value': '*'}, {'name': 'Access-Control-Allow-Methods', 'value': '*'}, {'name': 'Access-Control-Expose-Headers', 'value': 'Content-Disposition'}]
        if method == 'OPTIONS':
            body = {}
        elif path == self.fail and (self.fail_method is None or method == self.fail_method):
            status, body = self.fail_status, {'detail': 'DO_NOT_RENDER_DIAGNOSTIC'}
        elif path == '/auth/login':
            body = {'access_token': 'synthetic-ui-test-only', 'token_type': 'bearer'}
        elif path == '/auth/me' or path == '/users':
            body = dict(id=8 if self.role == 'creator' else 9, username='studio_creator' if self.role == 'creator' else 'learning_user', email='test@example.invalid', role=self.role)
        elif path == '/rooms':
            if method == 'POST':
                self.room_status = 'draft'
                body = self.room()
            else:
                body = [self.room(), *[dict(self.room(), id=43+i, name=name, category=topic, difficulty=difficulty) for i, (name, topic, difficulty) in enumerate([
                    ('Packet Trail', 'Network analysis', 'medium'), ('Cipher Notes', 'Cryptography', 'hard'), ('Binary Workshop', 'Reverse engineering', 'medium')])]]
        elif path == '/rooms/42':
            body = self.room()
        elif path == '/rooms/42/questions':
            body = {'questions': [], 'authoring_locked': False}
        elif path == '/rooms/42/progress':
            body = dict(room_id=42, solved_question_ids=[], questions_solved=0, questions_total=0, points_earned=0, points_total=0, completed=False, completed_at=None)
        elif path == '/rooms/42/builder':
            if method == 'POST':
                self.builder = self.environment('pending')
            body = self.builder
        elif path == '/rooms/42/builder/stop':
            self.builder = self.environment('stopping')
            body = self.builder
        elif path in ('/rooms/42/lab', '/rooms/42/test-lab'):
            if method == 'POST':
                self.lab = self.environment('pending')
            body = self.lab
        elif path in ('/rooms/42/lab/stop', '/rooms/42/test-lab/stop'):
            self.lab = self.environment('stopping')
            body = self.lab
        elif path == '/rooms/42/templates':
            if method == 'POST':
                self.versions.insert(0, self.template(8, 3, 'pending'))
                body = self.versions[0]
            else:
                body = self.versions
        elif path.endswith('/lab') and path.startswith('/rooms/42/templates/'):
            self.lab = self.environment('pending')
            body = self.lab
        elif path.endswith('/publish'):
            self.room_status = 'published'
            if '/templates/' in path:
                template_id = int(path.split('/')[-2])
                for template in self.versions:
                    template['is_published'] = template['id'] == template_id
            body = self.room()
        elif path.endswith('/unpublish'):
            self.room_status = 'draft'
            body = self.room()
        elif path == '/vpn/profile':
            body = 'synthetic-download-test-only; not a VPN profile'
            headers[0]['value'] = 'application/octet-stream'
            headers.append({'name': 'Content-Disposition', 'value': 'attachment; filename="secforge.ovpn"'})
        else:
            self.errors.append('Unexpected API endpoint')
            status, body = 404, {'detail': 'Unexpected test endpoint'}
        raw = body if isinstance(body, str) else json.dumps(body)
        await self.call('Fetch.fulfillRequest', dict(requestId=event['requestId'], responseCode=status,
                         responseHeaders=headers, body=base64.b64encode(raw.encode()).decode()))

    async def evaluate(self, expression):
        result = await self.call('Runtime.evaluate', dict(expression=expression, returnByValue=True, awaitPromise=True))
        if 'exceptionDetails' in result:
            raise AssertionError('Browser assertion script failed')
        return result['result'].get('value')

    async def wait(self, expression, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.evaluate(expression):
                return
            await asyncio.sleep(.08)
        raise AssertionError(f'UI condition did not become true: {expression}')

    async def click(self, text):
        label = json.dumps(text)
        await self.wait(f"[...document.querySelectorAll('button,a,summary')].some(e=>e.textContent.trim()==={label} && !e.disabled)")
        point = await self.evaluate(f"(()=>{{let e=[...document.querySelectorAll('button,a,summary')].find(e=>e.textContent.trim()==={label} && !e.disabled); e.scrollIntoView({{block:'center',behavior:'instant'}}); let r=e.getBoundingClientRect(); return {{x:r.x+r.width/2,y:r.y+r.height/2}}}})()")
        await self.call('Input.dispatchMouseEvent', dict(type='mousePressed', button='left', clickCount=1, **point))
        await self.call('Input.dispatchMouseEvent', dict(type='mouseReleased', button='left', clickCount=1, **point))

    async def fill(self, selector, value):
        await self.evaluate(f"(()=>{{let e=document.querySelector({json.dumps(selector)}); Object.getOwnPropertyDescriptor(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(e,{json.dumps(value)}); e.dispatchEvent(new Event('input',{{bubbles:true}}))}})()")

    async def navigate(self, path):
        await self.call('Page.navigate', dict(url='http://127.0.0.1:3000'+path))
        await self.wait("document.readyState==='complete' && !!document.querySelector('#root').firstElementChild")

    async def screenshot(self, name):
        await self.evaluate('''(async()=>{
          await document.fonts.ready;
          await new Promise(requestAnimationFrame);
          await Promise.all(document.getAnimations().filter(a=>a.effect.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})));
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
          return true;
        })()''')
        result = await self.call('Page.captureScreenshot', dict(format='png', captureBeyondViewport=False))
        (self.output / (name+'.png')).write_bytes(base64.b64decode(result['data']))

    async def check_layout(self):
        assert await self.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Horizontal overflow'
        assert await self.evaluate("[...document.querySelectorAll('button')].every(e=>e.getAttribute('aria-label') || e.textContent.trim())"), 'Unnamed button'
        assert await self.evaluate("[...document.querySelectorAll('input,textarea,select')].every(e=>e.getAttribute('aria-label') || e.labels.length)"), 'Unlabelled field'

    async def press(self, key, code, number):
        for kind in ('keyDown', 'keyUp'):
            params = dict(type=kind,key=key,code=code,windowsVirtualKeyCode=number,nativeVirtualKeyCode=number)
            if kind=='keyDown' and key in ('Enter',' '):
                params['text'] = '\r' if key=='Enter' else ' '
            await self.call('Input.dispatchKeyEvent', params)

    async def click_selector(self, selector, touch=False):
        point = await self.evaluate(f"(()=>{{let e=document.querySelector({json.dumps(selector)}); e.scrollIntoView({{block:'center',behavior:'instant'}}); let r=e.getBoundingClientRect(); return {{x:r.x+r.width/2,y:r.y+r.height/2}}}})()")
        if touch:
            await self.call('Input.dispatchTouchEvent', dict(type='touchStart',touchPoints=[point]))
            await self.call('Input.dispatchTouchEvent', dict(type='touchEnd',touchPoints=[]))
        else:
            for kind in ('mousePressed','mouseReleased'):
                await self.call('Input.dispatchMouseEvent', dict(type=kind,button='left',clickCount=1,**point))

    async def logout(self):
        await self.click_selector('[aria-label="Account options"]')
        await self.wait("!!document.querySelector('#account-options')")
        await self.click('Sign out')

    async def set_theme(self, theme):
        if await self.evaluate('document.documentElement.dataset.theme') != theme:
            await self.click_selector('.theme-toggle')
        await self.wait(f"document.documentElement.dataset.theme==={json.dumps(theme)}")
        assert await self.evaluate(f"localStorage.getItem('secforge-theme')==={json.dumps(theme)}")
        assert await self.evaluate(f"document.querySelector('link[rel=icon]').getAttribute('href')==='/favicon-{theme}.svg'")

    async def navigation_check(self, width):
        assert await self.evaluate("!document.querySelector('.sidebar')"), 'Sidebar remains'
        assert await self.evaluate("document.querySelector('.navbar').getBoundingClientRect().height===58"), 'Navbar is not compact'
        assert await self.evaluate("!!document.querySelector('.main-nav a[aria-current=page]')"), 'Active route missing'
        if width < 641:
            await self.call('Emulation.setTouchEmulationEnabled',dict(enabled=True))
            await self.click_selector('[aria-label="Open navigation"]', touch=True)
            await self.wait("document.querySelector('#main-navigation').classList.contains('is-open')")
            if width==390: await self.screenshot(f'{self.role}-mobile-navigation')
            assert await self.evaluate("document.querySelector('[aria-label=\"Close navigation\"]').getAttribute('aria-expanded')==='true'")
            await self.press('Escape','Escape',27)
            await self.wait("!document.querySelector('#main-navigation').classList.contains('is-open')")
            assert await self.evaluate("document.activeElement.getAttribute('aria-label')==='Open navigation'")
            # Real keyboard activation, followed by the role-appropriate functional route.
            await self.press('Enter','Enter',13)
            await self.wait("document.querySelector('#main-navigation').classList.contains('is-open')")
            await self.click_selector('.main-nav a[href="/rooms"]', touch=True)
            await self.wait("location.pathname==='/rooms' && !document.querySelector('#main-navigation').classList.contains('is-open')")
            await self.click('Manage room' if self.role=='creator' else 'View challenge')
            await self.wait("!!document.querySelector('.lab-page')")
            await self.call('Emulation.setTouchEmulationEnabled',dict(enabled=False))
        else:
            assert await self.evaluate("getComputedStyle(document.querySelector('.main-nav')).display!=='none'")
        # Account popover works without hover and Escape restores the trigger.
        await self.click_selector('[aria-label="Account options"]')
        await self.wait("!!document.querySelector('#account-options')")
        await self.press('Escape','Escape',27)
        await self.wait("!document.querySelector('#account-options')")
        assert await self.evaluate("document.activeElement.getAttribute('aria-label')==='Account options'")

    async def themed_screenshots(self, name):
        for theme in ('light','dark'):
            await self.set_theme(theme)
            await self.evaluate("window.scrollTo({top:0,behavior:'instant'})")
            await self.check_layout()
            await self.screenshot(f'{name}-{theme}')
        await self.set_theme('light')

    async def login(self):
        await self.navigate('/login')
        await self.wait("!!document.querySelector('#username')")
        await self.fill('#username', 'synthetic-test-user')
        await self.fill('#password', 'synthetic-test-input')
        await self.click('Sign in')
        await self.wait("!!document.querySelector('.app-shell')")
        await self.navigate('/')
        await self.wait("!!document.querySelector('.overview-welcome')")


async def run(output):
    profile = output / 'owned-browser-profile'
    profile.mkdir(mode=0o700)
    process = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir='+str(profile), 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        active = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 15
        while not active.exists() and process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        if not active.exists():
            raise RuntimeError('Owned Chromium failed to start')
        port = int(active.read_text().splitlines()[0])
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
            target = next(item for item in json.load(response) if item['type'] == 'page')
        async with websockets.connect(target['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
            b = Browser(ws, output)
            await b.call('Runtime.enable')
            await b.call('Page.enable')
            await b.call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
            await b.call('Emulation.setDeviceMetricsOverride', dict(width=1440, height=1050, deviceScaleFactor=1, mobile=False))
            await b.navigate('/login')
            await b.wait("!!document.querySelector('#username')")
            assert await b.evaluate("document.documentElement.dataset.theme==='light'"), 'New visitor is not light by default'
            await b.themed_screenshots('login-desktop')
            await b.set_theme('dark')
            await b.call('Page.reload')
            await b.wait("!!document.querySelector('#username') && document.documentElement.dataset.theme==='dark'")
            await b.set_theme('light')
            await b.check_layout()
            await b.navigate('/register')
            await b.wait("!!document.querySelector('#confirmPassword')")
            await b.themed_screenshots('register-desktop')
            assert await b.evaluate('document.documentElement.scrollHeight<=innerHeight'), 'Desktop registration needs scrolling'
            await b.fill('#username', 'synthetic-user')
            await b.fill('#email', 'test@example.invalid')
            await b.fill('#password', 'synthetic-password')
            await b.fill('#confirmPassword', 'mismatch-password')
            await b.click('Create account')
            await b.wait("document.querySelector('[role=alert]')?.textContent.includes('do not match')")
            assert not any(c[0] == 'POST' and c[1] == '/users' for c in b.calls)
            await b.fill('#confirmPassword','synthetic-password')
            await b.click('Create account')
            await b.wait("!!document.querySelector('.app-shell')")
            assert any(c[0]=='POST' and c[1]=='/users' for c in b.calls)
            await b.logout()
            await b.wait("!!document.querySelector('#username')")
            b.fail, b.fail_method, b.fail_status = '/auth/login', 'POST', 401
            await b.fill('#username','synthetic-test-user')
            await b.fill('#password','synthetic-test-input')
            await b.click('Sign in')
            await b.wait("document.body.textContent.includes('Username or password is incorrect')")
            b.fail, b.fail_method, b.fail_status = None, None, 503
            await b.login()
            await b.themed_screenshots('learner-overview')
            await b.check_layout()
            await b.navigate('/rooms')
            await b.wait("!!document.querySelector('.room-card')")
            for width in (1440,1024,768,390,320):
                await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=950,deviceScaleFactor=1,mobile=width<500))
                await b.themed_screenshots(f'learner-catalog-{width}')
                columns = await b.evaluate("getComputedStyle(document.querySelector('.room-grid')).gridTemplateColumns.split(' ').length")
                assert columns == (4 if width==1440 else 3 if width==1024 else 2 if width==768 else 1), 'Unexpected room grid density'
            await b.call('Emulation.setDeviceMetricsOverride',dict(width=1440,height=1050,deviceScaleFactor=1,mobile=False))
            await b.fill('input[type=search]', 'missing topic')
            await b.wait("document.body.textContent.includes('No matching rooms')")
            await b.click('Clear filters')
            await b.click('View challenge')
            await b.wait("document.body.textContent.includes('Start lab')")
            assert not any('/builder' in c[1] or '/templates' in c[1] or '/test-lab' in c[1] for c in b.calls), 'Learner made creator-only requests'
            b.fail, b.fail_method, b.fail_status = '/rooms/42/lab', 'POST', 409
            await b.click('Start lab')
            await b.wait("document.body.textContent.includes('launch conflicts')")
            assert b.lab is None
            b.fail_status = 503
            await b.click('Start lab')
            await b.wait("document.body.textContent.includes('service is temporarily unavailable')")
            assert b.lab is None
            b.fail, b.fail_method, b.fail_status = None, None, 503
            launches_before = len([c for c in b.calls if c[0]=='POST' and c[1]=='/rooms/42/lab'])
            b.hold = '/rooms/42/lab' 
            await b.click('Start lab')
            await b.wait("[...document.querySelectorAll('button')].some(e=>e.disabled && e.textContent.includes('Starting'))")
            deadline = time.monotonic()+10
            while not b.held and time.monotonic()<deadline:
                await asyncio.sleep(.05)
            assert b.held, 'Launch request was not received'
            assert len([c for c in b.calls if c[0]=='POST' and c[1]=='/rooms/42/lab']) == launches_before+1
            b.hold = None
            await b.respond(b.held.pop())
            await b.wait("!!document.querySelector('.lab-instance-pending')")
            b.lab = b.environment('provisioning')
            await b.click('Refresh lab status')
            await b.wait("!!document.querySelector('.lab-instance-provisioning')")
            b.lab = b.environment('running')
            await b.click('Refresh lab status')
            await b.wait("!!document.querySelector('.connection-guide')")
            await b.themed_screenshots('learner-room-running')
            assert await b.evaluate("!document.querySelector('.connection-help').open"), 'Help is permanently expanded'
            await b.evaluate("document.querySelector('.connection-help summary').focus()")
            assert await b.evaluate("document.activeElement.matches('.connection-help summary')"), 'Summary cannot receive keyboard focus'
            await b.press('Enter','Enter',13)
            await b.wait("document.querySelector('.connection-help').open")
            await b.screenshot('learner-connection-help-keyboard')
            await b.click('VPN & SSH instructions')
            await b.wait("!document.querySelector('.connection-help').open")
            await b.check_layout()
            for width in (1440,1024,768,390,320):
                await b.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=950,deviceScaleFactor=1,mobile=width<500))
                await b.check_layout()
                await b.evaluate("window.scrollTo({top:0,behavior:'instant'})")
                await b.navigation_check(width)
                if width < 500:
                    await b.call('Emulation.setTouchEmulationEnabled',dict(enabled=True))
                    await b.click_selector('.connection-help summary', touch=True)
                    await b.wait("document.querySelector('.connection-help').open")
                    await b.click_selector('.connection-help summary', touch=True)
                    await b.wait("!document.querySelector('.connection-help').open")
                    await b.call('Emulation.setTouchEmulationEnabled',dict(enabled=False))
                await b.themed_screenshots(f'learner-room-{width}')
            await b.call('Emulation.setDeviceMetricsOverride', dict(width=1440,height=1050,deviceScaleFactor=1,mobile=False))
            await b.call('Browser.setDownloadBehavior', dict(behavior='allow', downloadPath=str(output / 'downloads')))
            await b.click('Download VPN Profile')
            deadline = time.monotonic()+10
            while not (output/'downloads'/'secforge.ovpn').exists() and time.monotonic()<deadline:
                await asyncio.sleep(.1)
            assert (output/'downloads'/'secforge.ovpn').read_text().startswith('synthetic-download-test-only')
            b.fail = '/vpn/profile'
            await b.click('Download VPN Profile')
            await b.wait("document.body.textContent.includes('VPN service is currently unavailable')")
            assert 'DO_NOT_RENDER_DIAGNOSTIC' not in await b.evaluate('document.body.textContent')
            b.fail = None
            await b.click('Stop lab')
            await b.wait("!!document.querySelector('dialog[open]')")
            assert await b.evaluate("document.activeElement.textContent==='Cancel'"), 'Safe initial dialog focus'
            for _ in range(4):
                await b.call('Input.dispatchKeyEvent', dict(type='keyDown', key='Tab', code='Tab', windowsVirtualKeyCode=9,nativeVirtualKeyCode=9))
                await b.call('Input.dispatchKeyEvent', dict(type='keyUp', key='Tab', code='Tab', windowsVirtualKeyCode=9,nativeVirtualKeyCode=9))
                assert await b.evaluate("!!document.activeElement.closest('dialog')"), 'Dialog did not trap focus'

            await b.call('Input.dispatchKeyEvent', dict(type='keyDown', key='Escape', code='Escape', windowsVirtualKeyCode=27, nativeVirtualKeyCode=27))
            await b.call('Input.dispatchKeyEvent', dict(type='keyUp', key='Escape', code='Escape', windowsVirtualKeyCode=27, nativeVirtualKeyCode=27))
            await b.wait("!document.querySelector('dialog[open]')")
            assert not any(c[1]=='/rooms/42/lab/stop' for c in b.calls)
            assert await b.evaluate("document.activeElement.textContent.trim()==='Stop lab'"), 'Stop dialog did not restore focus'
            await b.click('Stop lab')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.evaluate('history.back()')
            await b.wait("location.pathname==='/rooms' && !document.querySelector('dialog[open]')")
            assert not any(c[1]=='/rooms/42/lab/stop' for c in b.calls), 'Route change accepted a stale Stop confirmation'
            await b.click('View challenge')
            await b.wait("!!document.querySelector('.lab-instance-running')")
            b.fail, b.fail_method = '/rooms/42/lab/stop', 'POST' 
            await b.click('Stop lab')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.evaluate("[...document.querySelectorAll('dialog button')].find(e=>e.textContent==='Stop lab').click()")
            await b.wait("document.body.textContent.includes('service is temporarily unavailable')")
            assert b.lab['status']=='running'
            assert await b.evaluate("!!document.querySelector('.lab-instance-running')")
            b.fail, b.fail_method = None, None
            await b.click('Stop lab')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.evaluate("[...document.querySelectorAll('dialog button')].find(e=>e.textContent==='Stop lab').click()")
            await b.wait("!!document.querySelector('.lab-instance-stopping')")
            b.lab = b.environment('stopped')
            await b.click('Refresh lab status')
            await b.wait("!!document.querySelector('.lab-instance-stopped')")
            await b.logout()
            await b.wait("!!document.querySelector('#username')")
            b.role, b.room_status, b.builder, b.lab = 'creator', 'draft', b.environment('running'), None
            await b.login()
            await b.themed_screenshots('creator-overview')
            await b.click('Create room')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.check_layout()
            assert await b.evaluate("document.activeElement.id==='name'")
            await b.fill('#name','A new challenge')
            await b.fill('#category','Web security')
            await b.fill('#description','A useful learner briefing')
            await b.click('Create draft room')
            await b.wait("!!document.querySelector('.lab-page')")
            payload = json.loads(next(c[2] for c in b.calls if c[0]=='POST' and c[1]=='/rooms'))
            assert payload['name']=='A new challenge' and payload['category']=='Web security'
            await b.navigate('/rooms/42')
            await b.wait("document.body.textContent.includes('Test-launch v2')")
            assert not any(c[1]=='/rooms/42/lab' and c[0]=='GET' for c in b.calls[b.calls.index(next(c for c in b.calls if c[1]=='/rooms/42/templates')):])
            assert await b.evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent==='Test-launch v1').disabled")
            await b.themed_screenshots('creator-studio')
            await b.evaluate("document.querySelector('#saved-versions').scrollIntoView({block:'start',behavior:'instant'})")
            await b.screenshot('creator-saved-versions-light')
            await b.set_theme('dark')
            await b.evaluate("document.querySelector('#saved-versions').scrollIntoView({block:'start',behavior:'instant'})")
            await b.screenshot('creator-saved-versions-dark')
            await b.set_theme('light')
            await b.click('Edit')
            await b.wait("!!document.querySelector('#edit-name')")
            await b.screenshot('creator-edit-dialog')
            await b.fill('#edit-description','Updated learner briefing')
            await b.click('Save changes')
            await b.wait("!document.querySelector('dialog[open]')")
            assert any(c[0]=='PATCH' and c[1]=='/rooms/42' and json.loads(c[2])['description']=='Updated learner briefing' for c in b.calls)
            b.versions[0]['machines'][0]['ami_id'] = None
            await b.click('Refresh status and versions')
            await b.wait("document.body.textContent.includes('Unavailable image')")
            assert await b.evaluate("[...document.querySelectorAll('.badge-failed')].some(e=>e.textContent==='Unavailable image')")
            assert await b.evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent==='Test-launch v2').disabled")
            b.versions[0]['machines'][0]['ami_id'] = 'ami-synthetic-test-only'
            await b.click('Refresh status and versions')
            await b.wait("[...document.querySelectorAll('button')].some(e=>e.textContent==='Test-launch v2' && !e.disabled)")
            await b.click('Test-launch v2')
            await b.wait("!!document.querySelector('.lab-instance-pending')")
            assert any(c[0]=='POST' and c[1]=='/rooms/42/templates/7/lab' for c in b.calls)
            assert b.room_status == 'draft'
            assert not any(c[0]=='POST' and c[1].endswith('/publish') for c in b.calls)
            b.lab = b.environment('running')
            await b.click('Refresh status and versions')
            await b.wait("!!document.querySelector('.lab-instance-running')")
            await b.click('Stop lab')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.click('Stop test lab')
            await b.wait("!!document.querySelector('.lab-instance-stopping')")
            b.lab = b.environment('stopped')
            await b.click('Refresh status and versions')
            await b.wait("!!document.querySelector('.lab-instance-stopped')")
            await b.click('Save challenge image')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.click('Save image')
            await b.wait("document.body.textContent.includes('Version 3')")
            assert await b.evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent==='Test-launch v3').disabled")
            await b.click('Publish this version')
            await b.wait("!!document.querySelector('dialog[open]')")
            await b.click('Publish v2')
            await b.wait("!!document.querySelector('.lab-tags .badge-published')")
            assert any(c[0]=='POST' and c[1]=='/rooms/42/templates/7/publish' for c in b.calls)
            b.builder = b.environment('stopped')
            await b.click('Refresh builder status')
            await b.wait("!!document.querySelector('.builder-stopped')")
            assert await b.evaluate("document.body.textContent.includes('Version 2')"), 'Saved versions disappeared after builder Stop'
            b.fail = '/rooms/42/templates'
            await b.click('Refresh status and versions')
            await b.wait("document.body.textContent.includes('temporarily unavailable')", timeout=20)
            assert 'DO_NOT_RENDER_DIAGNOSTIC' not in await b.evaluate('document.body.textContent')
            assert await b.evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent==='Test-launch v2').disabled")
            b.fail = None
            await b.click('Refresh status and versions')
            await b.wait("!document.body.textContent.includes('temporarily unavailable')")
            for width in (1440,1024,768,390,320):
                await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=950, deviceScaleFactor=1, mobile=width<500))
                await b.check_layout()
                await b.evaluate("window.scrollTo({top:0,behavior:'instant'})")
                await b.navigation_check(width)
                await b.themed_screenshots(f'creator-room-{width}')
            await b.logout()
            await b.wait("!!document.querySelector('#username')")
            await b.check_layout()
            for width in (1440,1024,768,390,320):
                await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=800,deviceScaleFactor=1,mobile=width<500))
                await b.navigate('/login')
                await b.wait("!!document.querySelector('#username')")
                await b.themed_screenshots(f'login-{width}')
                await b.navigate('/register')
                await b.wait("!!document.querySelector('#confirmPassword')")
                await b.themed_screenshots(f'register-{width}')
                if width >= 768:
                    assert await b.evaluate('document.documentElement.scrollHeight<=innerHeight'), 'Desktop registration needs scrolling'
            if b.request_tasks: await asyncio.wait_for(asyncio.gather(*b.request_tasks),10)
            assert not b.errors, b.errors
            result = dict(passed=True, api_mode='all synthetic; no live application writes', api_requests=len(b.calls), uncaught_errors=len(b.errors), navigation_cancelled_static_requests=b.cancelled_static_requests, viewports=[1440,1024,768,390,320], themes=['light','dark'], screenshots=sorted(p.name for p in output.glob('*.png')))
            b.reader.cancel()
    finally:
        if 'b' in locals():
            b.reader.cancel()
        # The browser has its own process group; never signal the user's Chromium session.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        def owned_workers():
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if os.getpgid(int(entry.name)) == process.pid and (entry/'stat').read_text().split(')')[1].split()[0] != 'Z':
                        return True
                except (ProcessLookupError, FileNotFoundError, PermissionError):
                    continue
            return False
        deadline = time.monotonic()+5
        while owned_workers() and time.monotonic()<deadline:
            await asyncio.sleep(.05)
        if owned_workers():
            os.killpg(process.pid, signal.SIGKILL)
            deadline = time.monotonic()+5
            while owned_workers() and time.monotonic()<deadline:
                await asyncio.sleep(.05)
        if owned_workers():
            raise RuntimeError('Owned Chromium workers did not stop; artifacts preserved')
        shutil.rmtree(profile)
        if (output/'downloads').exists():
            shutil.rmtree(output/'downloads')
    result['owned_browser_and_downloads_cleaned'] = True
    (output/'results.json').write_text(json.dumps(result,indent=2))
    print('PASS: authentication forms, role isolation, discovery/search, learner lifecycle/conflicts, keyboard-safe Stop confirmation, VPN download/errors, creator controls, exact-version launch/publish, capture states, saved-version retention, responsive layouts and owned-browser cleanup. No live API writes.')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-synthetic-browser-test', action='store_true', required=True)
    args = parser.parse_args()
    if not args.allow_synthetic_browser_test:
        parser.error('Explicit opt-in required')
    os.umask(0o077)
    output = Path(tempfile.mkdtemp(prefix='secforge-frontend-redesign-'))
    print('Owned screenshot/report directory:', output)
    asyncio.run(asyncio.wait_for(run(output), 180))


if __name__ == '__main__':
    main()
