"""Opt-in Chromium profile/VPN filename UX tests with intercepted synthetic APIs.
No real profile, VPN identity, AWS resource or database is changed by this script.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -B tests/task-b-browser.py
"""
import base64
import importlib.util
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

spec=importlib.util.spec_from_file_location('task_a_browser',Path(__file__).with_name('task-a-browser.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
OUTPUT=Path('/tmp/secforge-task-b-browser');OUTPUT.mkdir(mode=0o700,exist_ok=True)

class ProfileBrowser(a.Browser):
    def __enter__(self):
        result=super().__enter__()
        self.role='learner'
        self.user=dict(id=9,username='synthetic_learner',email='synthetic@example.com',role='learner',display_name=None,avatar_style='initials',avatar_color='red')
        self.updates=[]
        self.initial_username=self.user['username']
        return result

    def respond(self,params):
        request=params['request'];path=urlparse(request['url']).path.removeprefix('/api')
        if request['method']=='OPTIONS' or path not in ('/auth/me','/users/me','/vpn/profile'):
            return super().respond(params)
        self.requests.append((request['method'],path))
        status,body,headers=200,self.user,[]
        if path=='/users/me':
            assert request['method']=='PATCH'
            update=json.loads(request['postData']);self.updates.append(update)
            assert set(update)<= {'username','display_name','avatar_style','avatar_color'}
            if update.get('username','').lower()=='taken_name':status,body=409,dict(detail='Already taken')
            elif ' ' in update.get('username','').strip():status,body=422,dict(detail='Invalid username')
            else:
                if 'username' in update:update['username']=update['username'].strip().lower()
                self.user.update(update);body=self.user
        elif path=='/vpn/profile':
            headers=[dict(name='Content-Disposition',value=f'attachment; filename="secforge-{self.user["username"]}.ovpn"'),dict(name='Access-Control-Expose-Headers',value='Content-Disposition')]
            # Deliberately unusable synthetic profile, no credentials/certificates.
            body='synthetic VPN profile bytes -- not a real VPN credential'
        payload=body.encode() if isinstance(body,str) else json.dumps(body).encode()
        headers += [dict(name='Content-Type',value='application/x-openvpn-profile' if path=='/vpn/profile' else 'application/json'),dict(name='Access-Control-Allow-Origin',value=a.BASE)]
        self.send('Fetch.fulfillRequest',dict(requestId=params['requestId'],responseCode=status,responseHeaders=headers,body=base64.b64encode(payload).decode()))

    def fill(self,element,value):
        self.js(f"(()=>{{const el=document.getElementById({json.dumps(element)});Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,{json.dumps(value)});el.dispatchEvent(new Event('input',{{bubbles:true}}));}})()")

    def capture_widths(self):
        for theme in ['light','dark']:
            if theme=='dark':self.click('button[aria-label="Switch to dark theme"]')
            for width in [1440,1024,768,390,320]:
                self.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=1000,deviceScaleFactor=1,mobile=False))
                self.wait(f'innerWidth === {width}')
                assert self.js('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
                image=self.send('Page.captureScreenshot',{'captureBeyondViewport':True})['data']
                (OUTPUT/f'profile-{theme}-{width}.png').write_bytes(base64.b64decode(image))


def download(b,directory,name):
    b.go('/rooms/42','!!document.querySelector("#learner-workspace .connection-guide")')
    b.button('Download VPN Profile')
    path=Path(directory)/f'secforge-{name}.ovpn'
    deadline=time.monotonic()+8
    while not path.exists():
        b.js('document.readyState')  # process CDP events / intercepted request
        if time.monotonic()>deadline:raise AssertionError('Synthetic profile download did not complete')
        time.sleep(.05)
    return path.read_bytes()


def main():
    with tempfile.TemporaryDirectory(prefix='secforge-task-b-downloads-') as downloads,ProfileBrowser() as b:
        b.send('Browser.setDownloadBehavior',dict(behavior='allow',downloadPath=downloads))
        b.go('/login','!!document.querySelector("#username")')
        b.fill('username','synthetic_learner');b.fill('password','synthetic-not-a-real-password');b.click('button[type="submit"]')
        b.wait('location.pathname === "/" && !!document.querySelector(".account-button")')
        original=download(b,downloads,'synthetic_learner')
        b.click('button[aria-label="Account options"]');b.click('a[href="/profile"]')
        b.wait('location.pathname === "/profile" && !!document.querySelector("#profile-username")')
        b.fill('display-name','Security Learner')
        b.wait('document.querySelector(".profile-preview h2").textContent === "Security Learner"')
        assert b.js('document.querySelector(".account-name").textContent === "synthetic_learner"')
        b.fill('profile-username','taken_name');b.button('Save profile')
        b.wait('document.querySelector("[role=alert]")?.textContent.includes("already in use")')
        assert b.user['username']=='synthetic_learner'
        b.fill('profile-username','Renamed_Learner')
        b.click('button[aria-label="Use code avatar"]');b.click('button[aria-label="Use blue avatar color"]')
        b.button('Save profile')
        b.wait('document.querySelector(".profile-success")?.textContent.includes("Profile saved")')
        assert b.js('document.querySelector(".account-name").textContent === "Security Learner"')
        assert b.js('document.querySelector("#profile-username").value === "renamed_learner"')
        b.capture_widths()
        b.send('Page.reload');b.wait('document.querySelector("#profile-username")?.value === "renamed_learner"')
        assert b.js('document.querySelector("#display-name").value === "Security Learner"')
        assert b.js("""document.querySelector('button[aria-label="Use code avatar"]').getAttribute('aria-pressed') === 'true'""")
        changed=download(b,downloads,'renamed_learner');assert changed==original
        # Sign out and sign in again: updated server-backed profile remains.
        b.click('button[aria-label="Account options"]');b.click('button[aria-label="Sign out"]')
        b.wait('location.pathname === "/login"')
        b.fill('username','renamed_learner');b.fill('password','synthetic-not-a-real-password');b.click('button[type="submit"]')
        b.wait('document.querySelector(".account-name")?.textContent === "Security Learner"')
        b.go('/profile','!!document.querySelector("#profile-username")')
        b.fill('profile-username','bad name');b.button('Save profile')
        b.wait('document.querySelector("[role=alert]")?.textContent.includes("Check your username")')
        assert b.user['username']=='renamed_learner'
        assert not b.errors
        assert len(b.updates)==3
    print('PASS: profile preview/save/collision/validation, navbar and reload/login persistence, personalized VPN downloads with identical synthetic content, light/dark at 5 widths. APIs mocked; no database/AWS/VPN writes.')
    print(f'Screenshots: {OUTPUT}')

if __name__=='__main__':main()
