"""Opt-in Chromium timing UX checks; synthetic APIs, no AWS/database/VPN writes."""
import base64
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

spec=importlib.util.spec_from_file_location('task_b',Path(__file__).with_name('task-b-browser.py'))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
OUTPUT=Path('/tmp/secforge-task-c-browser');OUTPUT.mkdir(mode=0o700,exist_ok=True)


class TimingBrowser(b.ProfileBrowser):
    def __enter__(self):
        result=super().__enter__();self.lab=None;self.now=datetime.now(timezone.utc);self.initial=self.now;self.fail_extension=False
        return result

    def respond(self,params):
        request=params['request'];path=urlparse(request['url']).path.removeprefix('/api');method=request['method']
        if method=='OPTIONS' or path not in ('/rooms/42/lab','/rooms/42/lab/extend','/rooms/42/lab/stop'):
            return super().respond(params)
        self.requests.append((method,path));status=200
        if path=='/rooms/42/lab' and method=='POST':
            assert json.loads(request['postData'])=={'ttl_hours':1}
            self.lab={**b.a.ENVIRONMENT,'session_started_at':self.now.isoformat(),'extensions_used':0,'expired_at':None,'expires_at':(self.now+timedelta(hours=1)).isoformat(),'status':'running'};status=201
        elif path.endswith('/extend'):
            data=json.loads(request['postData']);assert set(data)=={'lab_id','expected_extensions_used'} and data['lab_id']==self.lab['id']
            if self.fail_extension:
                self.fail_extension=False;status=503
            elif data['expected_extensions_used']==self.lab['extensions_used']:
                self.lab['extensions_used']+=1;self.lab['expires_at']=(datetime.fromisoformat(self.lab['expires_at'])+timedelta(hours=1)).isoformat()
        elif path.endswith('/stop'):
            self.lab['status']='stopped';self.lab['private_ip']=None
        if self.lab is None:status=404;body={'detail':'No synthetic lab'}
        elif status==503:body={'detail':'Synthetic time service unavailable'}
        else:body={**self.lab,'server_time':self.now.isoformat()}
        self.send('Fetch.fulfillRequest',dict(requestId=params['requestId'],responseCode=status,responseHeaders=[dict(name='Content-Type',value='application/json'),dict(name='Access-Control-Allow-Origin',value=b.a.BASE)],body=base64.b64encode(json.dumps(body).encode()).decode()))

    def advance(self,minutes):
        self.wait('!![...document.querySelectorAll("button")].find(x=>x.textContent.trim()==="Refresh lab status" && !x.disabled)')
        self.now=self.initial+timedelta(minutes=minutes)
        self.button('Refresh lab status')


def main():
    with TimingBrowser() as client:
        client.go('/login','!!document.querySelector("#username")');client.fill('username','synthetic_learner');client.fill('password','synthetic-not-a-real-password');client.click('button[type="submit"]');client.wait('location.pathname === "/"')
        client.go('/rooms/42','!![...document.querySelectorAll("#learner-workspace button")].find(x=>x.textContent.trim()==="Start lab" && !x.disabled)');client.button('Start lab')
        client.wait('document.querySelector(".lab-timer [role=timer]")?.textContent.startsWith("00:59:") || document.querySelector(".lab-timer [role=timer]")?.textContent === "01:00:00"')
        assert not client.js('[...document.querySelectorAll("button")].some(x=>x.textContent==="Extend +1 Hour")')
        client.advance(30);client.wait('!![...document.querySelectorAll("button")].find(x=>x.textContent==="Extend +1 Hour")')
        before=client.js('document.querySelector("[role=timer]").textContent')
        client.js('Date.now=()=>0;true')
        assert client.js('document.querySelector("[role=timer]").textContent')==before
        client.fail_extension=True;client.button('Extend +1 Hour');client.wait('document.querySelector(".lab-timer [role=alert]")?.textContent.includes("unavailable")')
        assert client.lab['extensions_used']==0
        client.button('Extend +1 Hour');client.wait('document.querySelector(".lab-timer").textContent.includes("1 extensions remaining")')
        assert client.lab['extensions_used']==1
        client.send('Page.reload');client.wait('document.querySelector(".lab-timer")?.textContent.includes("1 extensions remaining")')
        client.advance(90);client.wait('!![...document.querySelectorAll("button")].find(x=>x.textContent==="Extend +1 Hour")');client.button('Extend +1 Hour');client.wait('document.querySelector(".lab-timer").textContent.includes("0 extensions remaining")')
        assert client.lab['extensions_used']==2
        client.advance(170);client.wait('document.querySelector(".lab-timer [role=status]")?.textContent.includes("10 minutes")')
        client.advance(175);client.wait('document.querySelector(".lab-timer [role=status]")?.textContent.includes("5 minutes")')
        for theme in ['light','dark']:
            if theme=='dark':client.click('button[aria-label="Switch to dark theme"]')
            for width in [1440,1024,768,390,320]:
                client.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=1000,deviceScaleFactor=1,mobile=False));client.wait(f'innerWidth === {width}')
                assert client.js('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
                image=client.send('Page.captureScreenshot',{'captureBeyondViewport':True})['data'];(OUTPUT/f'lab-timer-{theme}-{width}.png').write_bytes(base64.b64decode(image))
        client.click('button[aria-label="Account options"]');client.click('button[aria-label="Sign out"]');client.wait('location.pathname === "/login"')
        client.fill('username','synthetic_learner');client.fill('password','synthetic-not-a-real-password');client.click('button[type="submit"]');client.wait('!!document.querySelector(".account-button")')
        client.go('/rooms/42','!!document.querySelector(".lab-timer")');assert client.js('document.querySelector(".lab-timer").textContent.includes("0 extensions remaining")')
        client.advance(180);client.wait('document.querySelector(".lab-timer").textContent.includes("Access expired")')
        assert not client.js('[...document.querySelectorAll("button")].some(x=>x.textContent==="Extend +1 Hour")')
        assert not client.js('!!document.querySelector("#learner-workspace .connection-guide")')
        assert client.js('document.querySelector(".lab-header-status").textContent.includes("Expired")')
        assert not client.errors
    print('PASS: synthetic lab launch, server-clock countdown, two extensions/quota, 503 feedback, refresh/logout/login persistence, 10/5-minute warnings, expired controls, both themes at five widths. No real provisioning or cleanup.')
    print('Screenshots: '+str(OUTPUT))


if __name__=='__main__':main()
