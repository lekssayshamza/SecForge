"""Opt-in Chromium checks with synthetic APIs; no database/AWS writes.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -B tests/task-a-browser.py
Uses installed Chromium and Python websocket-client. No packages are installed.
"""
import base64
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
import websocket

BASE = 'http://127.0.0.1:3000'
OUTPUT = Path('/tmp/secforge-task-a-browser')
OUTPUT.mkdir(mode=0o700, exist_ok=True)
ROOM = dict(id=42, creator_id=7, name='Synthetic Linux room', description='Investigate the private machine. Read the questions and connect through VPN.', category='Linux', difficulty='easy', status='published', created_at='2026-10-11T10:00:00', updated_at='2026-10-11T10:00:00')
QUESTION = dict(id=8, question_text='Find the service', hint='Inspect the machine', points=25, display_order=1)
ENVIRONMENT = dict(id=9, room_id=42, status='running', private_ip='10.0.1.42', expires_at='2026-10-12T10:00:00', created_at='2026-10-11T10:00:00', updated_at='2026-10-11T10:00:00', ssh_username='challenge_admin')


class Browser:
    def __enter__(self):
        self.profile = tempfile.TemporaryDirectory(prefix='secforge-task-a-chromium-')
        self.process = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--remote-allow-origins=http://localhost', '--user-data-dir=' + self.profile.name, 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            port_file = Path(self.profile.name) / 'DevToolsActivePort'
            deadline = time.monotonic() + 30
            while not port_file.exists():
                if time.monotonic() >= deadline or self.process.poll() is not None:
                    raise RuntimeError('Owned Chromium did not start')
                time.sleep(.05)
            port = port_file.read_text().splitlines()[0]
            targets = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5))
            target = next(x for x in targets if x['type'] == 'page')
            self.ws = websocket.create_connection(target['webSocketDebuggerUrl'], origin='http://localhost', timeout=30)
            self.counter, self.responses, self.requests, self.errors = 0, {}, [], []
            self.role = None
            self.send('Page.enable')
            self.send('Runtime.enable')
            self.send('Fetch.enable', {'patterns': [{'urlPattern': origin + '/api/*', 'requestStage': 'Request'} for origin in ['http://localhost:8000', 'http://127.0.0.1:8000', BASE]]})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if hasattr(self, 'ws'): self.ws.close()
        os.killpg(self.process.pid, signal.SIGTERM)
        try: self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL); self.process.wait(timeout=5)
        # Owned Chromium children may finish profile writes after the leader.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.profile.cleanup()
                break
            except OSError:
                if time.monotonic() >= deadline: raise
                time.sleep(.05)

    def send(self, method, params=None):
        self.counter += 1
        ident = self.counter
        self.ws.send(json.dumps(dict(id=ident, method=method, params=params or {})))
        while ident not in self.responses:
            message = json.loads(self.ws.recv())
            if 'id' in message: self.responses[message['id']] = message
            elif message.get('method') == 'Fetch.requestPaused': self.respond(message['params'])
            elif message.get('method') == 'Runtime.exceptionThrown': self.errors.append('Browser runtime exception')
        response = self.responses.pop(ident)
        if 'error' in response: raise RuntimeError(response['error']['message'])
        return response.get('result', {})

    def respond(self, params):
        request = params['request']
        path = urlparse(request['url']).path.removeprefix('/api')
        self.requests.append((request['method'], path))
        if self.role is None:
            self.send('Fetch.failRequest', {'requestId': params['requestId'], 'errorReason': 'BlockedByClient'})
            return
        status, body = 200, None
        if request['method'] == 'OPTIONS':
            self.send('Fetch.fulfillRequest', dict(requestId=params['requestId'], responseCode=204, responseHeaders=[dict(name='Access-Control-Allow-Origin', value=BASE), dict(name='Access-Control-Allow-Headers', value='content-type,authorization'), dict(name='Access-Control-Allow-Methods', value='GET,POST,PATCH,PUT,DELETE')]))
            return
        if request['method'] != 'GET' and path != '/auth/login':
            raise AssertionError('Unexpected write in read-only browser test')
        if path == '/auth/login': body = dict(access_token='synthetic-browser-token', token_type='bearer')
        elif path == '/auth/me' and self.role == 'expired': status, body = 401, dict(detail='Expired synthetic session')
        elif path == '/auth/me': body = dict(id=7 if self.role == 'creator' else 9, username='synthetic_' + self.role, email='synthetic@example.invalid', role=self.role)
        elif path == '/rooms': body = [ROOM]
        elif path == '/rooms/42': body = ROOM
        elif path == '/rooms/42/questions': body = dict(questions=[QUESTION], authoring_locked=False)
        elif path == '/rooms/42/accepted-answers': body = []
        elif path == '/rooms/42/progress': body = dict(room_id=42, solved_question_ids=[], questions_solved=0, questions_total=1, points_earned=0, points_total=25, completed=False, completed_at=None)
        elif path in ['/rooms/42/builder', '/rooms/42/lab']: body = ENVIRONMENT
        elif path == '/rooms/42/templates': body = []
        elif path == '/rooms/42/test-lab': status, body = 404, dict(detail='No test lab')
        else: raise AssertionError('Unknown API in browser test: ' + path)
        self.send('Fetch.fulfillRequest', dict(requestId=params['requestId'], responseCode=status, responseHeaders=[dict(name='Content-Type', value='application/json'), dict(name='Access-Control-Allow-Origin', value=BASE)], body=base64.b64encode(json.dumps(body).encode()).decode()))

    def js(self, expression):
        result = self.send('Runtime.evaluate', dict(expression=expression, returnByValue=True, awaitPromise=True, userGesture=True))
        if 'exceptionDetails' in result: raise AssertionError('Browser assertion script failed')
        return result.get('result', {}).get('value')

    def wait(self, expression):
        deadline = time.monotonic() + 30
        while not self.js(expression):
            if time.monotonic() >= deadline: raise AssertionError('Browser condition timed out: ' + expression + '; path=' + str(self.js('location.pathname')) + '; errors=' + str(self.errors) + '; h1=' + str(self.js('document.querySelector("h1")?.textContent')))
            time.sleep(.05)

    def go(self, path, condition):
        self.send('Page.navigate', {'url': BASE + path})
        self.wait(condition)

    def click(self, selector):
        assert self.js(f"(()=>{{const el=document.querySelector({json.dumps(selector)}); if(!el) return false; el.click(); return true}})()"), selector

    def button(self, label):
        assert self.js(f"(()=>{{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==={json.dumps(label)}); if(!b)return false; b.click(); return true}})()"), label

    def screenshot(self, name):
        # Callers wait for readiness. Failure capture must not recursively
        # invoke wait() when diagnosing a stalled external font request.
        image = self.send('Page.captureScreenshot', {'captureBeyondViewport': True})['data']
        (OUTPUT / name).write_bytes(base64.b64decode(image))

    def widths(self, prefix):
        for theme in ['light', 'dark']:
            if theme == 'dark': self.click('button[aria-label="Switch to dark theme"]')
            for width in [1440, 1024, 768, 390, 320]:
                self.send('Emulation.setDeviceMetricsOverride', dict(width=width, height=1000, deviceScaleFactor=1, mobile=False))
                self.wait(f'window.innerWidth === {width}')
                assert self.js('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), f'{prefix}: overflow at {width}'
                self.screenshot(f'{prefix}-{theme}-{width}.png')
            self.send('Page.reload')
            self.wait(f'document.documentElement?.dataset.theme === "{theme}" && !!document.querySelector("h1")')


def main():
    with Browser() as b:
        b.go('/', 'document.querySelector("h1")?.textContent.includes("Learn cybersecurity")')
        assert b.js('location.pathname === "/" && document.documentElement?.dataset.theme === "light"')
        b.widths('landing')
        b.click('a[href="/login"]')
        b.wait('location.pathname === "/login" && !!document.querySelector("#username")')
        b.click('a[aria-label="SecForge home"]')
        b.wait('location.pathname === "/" && !!document.querySelector("#landing-title")')
        b.click('a[href="/register"]')
        b.wait('location.pathname === "/register" && !!document.querySelector("#email")')
        b.go('/rooms/42', 'location.pathname === "/login" && !!document.querySelector("#username")')
        assert not b.errors
    
    with Browser() as b:
        b.role = 'expired'
        b.go('/', 'document.querySelector("h1")?.textContent.includes("Learn cybersecurity")')
        b.js('localStorage.setItem("access_token", "synthetic-expired-token"); localStorage.setItem("user", JSON.stringify({id:9,username:"synthetic_learner",role:"learner"}))')
        b.send('Page.reload')
        b.wait('location.pathname === "/" && document.querySelector("h1")?.textContent.includes("Learn cybersecurity") && localStorage.getItem("access_token") === null')
        assert not b.errors
    
    for role in ['learner', 'creator']:
        with Browser() as b:
            b.role = role
            b.go('/login', '!!document.querySelector("#username")')
            # Use native input setters so React receives real form state updates.
            b.js("(()=>{const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; for(const [id,value] of [['username','synthetic-only'],['password','synthetic-not-a-real-password']]) {const el=document.getElementById(id);set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));}})()")
            b.click('button[type="submit"]')
            b.wait('location.pathname === "/" && !!document.querySelector(".account-button")')
            b.go('/register', 'location.pathname === "/" && !!document.querySelector(".account-button")')
            b.go('/rooms/42', 'document.querySelector("h1")?.textContent === "Synthetic Linux room" && !!document.querySelector("#challenge-questions")')
            if role == 'learner':
                b.wait('!!document.querySelector("#learner-workspace .connection-guide")')
                assert b.js("!!(document.querySelector('#learner-workspace').compareDocumentPosition(document.querySelector('#challenge-questions')) & Node.DOCUMENT_POSITION_FOLLOWING)")
                assert b.js("![...document.querySelectorAll('button')].some(b=>['Add question','Start builder'].includes(b.textContent.trim()))")
                assert not any('/builder' in path or '/templates' in path or '/test-lab' in path for _, path in b.requests)
            else:
                b.wait('!!document.querySelector(".builder-ssh-command code")')
                command = b.js('document.querySelector(".builder-ssh-command code").textContent')
                assert 'challenge_admin@10.0.1.42' in command and '-i /path/to/authorized-builder-key.pem' in command
                b.send('Page.bringToFront')
                b.send('Browser.grantPermissions', {'origin': BASE, 'permissions': ['clipboardReadWrite', 'clipboardSanitizedWrite']})
                b.click('button[aria-label="Copy builder SSH command"]')
                b.wait('document.querySelector(".connection-guide [role=status]")?.textContent === "SSH command copied"')
                assert b.js('navigator.clipboard.readText()') == command
                for label in ['Add question', 'Edit question']:
                    b.button(label)
                    b.wait('!!document.querySelector("dialog[open] #question-answer")')
                    assert b.js('document.querySelector("#question-answer").type === "text" && document.querySelector("#question-answer").value === ""')
                    b.js('document.querySelector("#question-answer").value = "synthetic-visible-answer"')
                    assert b.js('document.querySelector("#question-answer").value === "synthetic-visible-answer"')
                    b.send('Input.dispatchKeyEvent', dict(type='keyDown', key='Escape', code='Escape', windowsVirtualKeyCode=27))
                    b.send('Input.dispatchKeyEvent', dict(type='keyUp', key='Escape', code='Escape', windowsVirtualKeyCode=27))
                    b.wait('!document.querySelector("dialog")')
                assert b.js('!document.querySelector("#learner-workspace")')
            # The actionable development command is reachable on both roles,
            # and copying it must retain the personalized filename unchanged.
            b.click('.connection-help summary')
            b.wait('document.querySelector(".connection-help").open')
            dev_command = b.js('document.querySelector(".connection-help .builder-ssh-command code").textContent')
            assert dev_command.startswith('sudo /usr/local/libexec/secforge/learner_vpn ')
            b.send('Page.bringToFront')
            b.send('Browser.grantPermissions', {'origin': BASE, 'permissions': ['clipboardReadWrite', 'clipboardSanitizedWrite']})
            b.click('button[aria-label="Copy development VPN command"]')
            b.wait('document.querySelector(".connection-guide [role=status]")?.textContent === "Development VPN command copied"')
            assert b.js('navigator.clipboard.readText()') == dev_command
            b.widths(role)
            b.click('button[aria-label="Open navigation"]')
            b.wait('document.querySelector("#main-navigation").classList.contains("is-open")')
            b.send('Input.dispatchKeyEvent', dict(type='keyDown', key='Escape', windowsVirtualKeyCode=27))
            b.wait('document.activeElement?.getAttribute("aria-label") === "Open navigation"')
            assert not b.errors
    print('PASS: public/login/signup and authenticated/protected routing; creator SSH and visible Add/Edit answers; learner lab placement; both themes at 1440/1024/768/390/320px. Authenticated APIs were synthetic. No application writes.')
    print(f'Screenshots: {OUTPUT}')


if __name__ == "__main__":
    main()
