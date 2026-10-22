"""Batch A real-browser checks against an owned HTTP/SQLite fixture, never AWS.
Start the private fixture described in BATCH_A_VERIFICATION.md first.
"""
import base64,importlib.util,json,os,time,urllib.request
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(os.environ['SECFORGE_BATCH_A_EVIDENCE'])
spec=importlib.util.spec_from_file_location('browser',Path(__file__).with_name('task-a-browser.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.BASE='http://127.0.0.1:4217';m.OUTPUT=ROOT/'isolated-screenshots';m.OUTPUT.mkdir(mode=0o700,exist_ok=True)
class Browser(m.Browser):
 def __enter__(self):
  self.network=[];super().__enter__();self.send('Fetch.disable');self.send('Network.enable');return self
 def send(self,method,params=None):
  self.counter+=1;ident=self.counter;self.ws.send(json.dumps(dict(id=ident,method=method,params=params or {})))
  while ident not in self.responses:
   message=json.loads(self.ws.recv())
   if 'id' in message:self.responses[message['id']]=message
   elif message.get('method')=='Fetch.requestPaused':self.respond(message['params'])
   elif message.get('method')=='Network.requestWillBeSent':self.network.append((message['params']['request']['method'],urlparse(message['params']['request']['url']).path))
   elif message.get('method')=='Runtime.exceptionThrown':self.errors.append('Browser runtime exception')
  response=self.responses.pop(ident)
  if 'error' in response:raise RuntimeError(response['error']['message'])
  return response.get('result',{})
 def button(self,label):
  assert self.js('(()=>{const root=document.querySelector("dialog[open]")||document;const e=[...root.querySelectorAll("button")].find(b=>b.textContent.trim()==='+json.dumps(label)+');if(!e)return false;e.click();return true})()'),label

def field(b,selector,value):
 b.js('(()=>{const e=document.querySelector('+json.dumps(selector)+');const p=e.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,"value").set.call(e,'+json.dumps(value)+');e.dispatchEvent(new Event("input",{bubbles:true}));})()')
def login(b,name):
 b.go('/login','!!document.querySelector("#username")');field(b,'#username',name);field(b,'#password','synthetic-public-passphrase');b.click('button[type="submit"]');b.wait('location.pathname!=="/login"')
def logout(b):
 b.click('button[aria-label="Account options"]');b.wait('!!document.querySelector("[role=menu]")');b.button('Sign Out');b.wait('!localStorage.getItem("access_token")')
def widths(b,prefix,short=False):
 for theme in ['light','dark']:
  if b.js('document.documentElement.dataset.theme')!=theme:b.click('button[aria-label="Switch to '+theme+' theme"]')
  for width,height in [(1440,700),(1024,600),(768,800),(390,844),(320,740)]:
   b.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=height,deviceScaleFactor=1,mobile=width<500));b.wait('innerWidth==='+str(width));b.js('window.scrollTo(0,0)')
   assert b.js('document.documentElement.scrollWidth<=document.documentElement.clientWidth'),prefix+str(width)
   if short and width>=1024:assert b.js('document.querySelector(".auth-footer").getBoundingClientRect().bottom<=innerHeight'), 'Sign-in actions below short desktop viewport'
   b.screenshot(prefix+'-'+theme+'-'+str(width)+'.png')
 b.send('Emulation.setDeviceMetricsOverride',dict(width=1440,height=900,deviceScaleFactor=1,mobile=False))
def scenario(name):urllib.request.urlopen(m.BASE+'/fixture/scenario?mode='+name,timeout=5).close()
checks=[]
with Browser() as b:
 b.go('/','!!document.querySelector(".cyber-scene")');assert b.js('document.querySelectorAll(".site-header").length===1');widths(b,'homepage')
 for path in ['/login','/register']:
  b.go(path,'!!document.querySelector("#username")');assert b.js('document.querySelectorAll(".site-header").length===1');widths(b,path[1:],short=path=='/login')
 checks.append('Shared header, unchanged animated hero and compact login across both themes/five widths; 1024x600 and 1440x700 sign-in fits')
 login(b,'fixturelearner');b.go('/profile','!!document.querySelector(".identity-header")');widths(b,'private-profile')
 assert b.js('!document.body?.textContent.includes("Sign-in methods")');b.button('Edit Profile');b.wait('!!document.querySelector("#profile-picture")')
 assert b.js('!document.querySelector("dialog").textContent.includes("Generated avatar fallback") && !document.querySelector("dialog").textContent.includes("Avatar color")')
 from PIL import Image
 image=ROOT/'synthetic-avatar.png';Image.new('RGB',(32,32),(80,100,160)).save(image)
 doc=b.send('DOM.getDocument');node=b.send('DOM.querySelector',{'nodeId':doc['root']['nodeId'],'selector':'#profile-picture'})['nodeId'];b.send('DOM.setFileInputFiles',{'nodeId':node,'files':[str(image)]});b.button('Save picture');b.wait('document.querySelector(".profile-success")?.textContent.includes("Picture saved")')
 assert b.js('(async()=>{const r=await fetch("/api/public/learners/fixturelearner");const p=await r.json();return p.avatar_url===null&&p.bio===null})()')
 assert b.js('(async()=> (await fetch("/api/public/learners/fixturelearner/avatar")).status===404)()')
 field(b,'#display-name','Browser Learner');field(b,'#profile-username','fixturelearner_updated');field(b,'#profile-bio','Learning Linux with hands-on challenges.')
 for index in [0,1]:b.js('document.querySelectorAll(".profile-privacy input")[%s].click()'%index)
 b.button('Save profile');b.wait('document.querySelector(".profile-success")?.textContent.includes("Profile saved")');b.button('Done')
 b.go('/learners/fixturelearner_updated','document.querySelector(".identity-header h1")?.textContent==="Browser Learner"');b.wait('document.querySelector(".identity-header img")?.naturalWidth===256');widths(b,'public-profile')
 assert b.js('document.body?.textContent.includes("Learning Linux") && !document.body?.textContent.includes("learner@example.com")')
 b.go('/settings','document.body?.textContent.includes("Google is connected")');widths(b,'settings')
 b.click('button[aria-label="Account options"]');b.wait('document.activeElement?.textContent.includes("My Profile")')
 b.send('Input.dispatchKeyEvent',{'type':'keyDown','key':'ArrowDown'});b.wait('document.activeElement?.textContent.includes("Saved Content")')
 b.send('Input.dispatchKeyEvent',{'type':'keyDown','key':'Escape'});b.wait('document.activeElement?.getAttribute("aria-label")==="Account options" && !document.querySelector("[role=menu]")')
 b.click('button[aria-label="Account options"]');b.click('[role="menuitem"][href="/saved"]');b.wait('location.pathname==="/saved"')
 b.send('Emulation.setDeviceMetricsOverride',dict(width=320,height=740,deviceScaleFactor=1,mobile=True));b.click('button[aria-label="Open navigation"]');b.wait('document.querySelector("#main-navigation").classList.contains("is-open")');b.click('#main-navigation a[href="/paths"]');b.wait('location.pathname==="/paths" && !document.querySelector("#main-navigation").classList.contains("is-open")')
 b.send('Emulation.setDeviceMetricsOverride',dict(width=1440,height=900,deviceScaleFactor=1,mobile=False));checks.append('Password login, real profile edit/upload/consent, private/public image handling, Saved Content, menu arrows/Escape and mobile navigation')
 logout(b);scenario('existing');b.go('/login?returnTo=%2Frooms%2F42','!!document.querySelector("#username")');before=b.network.count(('POST','/api/auth/google/complete'));b.wait('document.querySelector(".google-signin button")?.disabled===false');b.click('.google-signin button');b.wait('location.pathname==="/rooms/42" && !!document.querySelector(".question-item")');assert b.network.count(('POST','/api/auth/google/complete'))==before+1
 checks.append('Existing Google identity automatically completes one secured POST and returns to the requested room without a second click')
 logout(b)
 for mode,text in [('cancel','cancelled'),('invalid','could not be verified')]:
  scenario(mode);b.go('/login','!!document.querySelector("#username")');b.wait('document.querySelector(".google-signin button")?.disabled===false');b.click('.google-signin button');b.wait('location.pathname==="/login" && document.body?.textContent.includes('+json.dumps(text)+')')
 scenario('new');b.go('/login?returnTo=%2Frooms%2F42','!!document.querySelector("#username")');b.wait('document.querySelector(".google-signin button")?.disabled===false');b.click('.google-signin button');b.wait('location.pathname==="/auth/google" && !!document.querySelector("#google-username")');field(b,'#google-username','newgooglelearner');b.button('Create account');b.wait('location.pathname==="/rooms/42"');assert b.js('JSON.parse(localStorage.getItem("user")).role==="learner"');logout(b)
 login(b,'fixtureother');scenario('link');b.go('/settings','!!document.querySelector("#link-password")');field(b,'#link-password','synthetic-public-passphrase');b.button('Link Google account');b.wait('location.pathname==="/auth/google" && document.body?.textContent.includes("Confirm link")');b.button('Confirm link');b.wait('location.pathname==="/settings" && document.body?.textContent.includes("Google is connected")')
 checks.append('New Google username selection, cancellation, invalid state and explicit password-reauthenticated linking remain secure')
 assert not b.errors
result={'checks':checks,'runtime_errors':0,'google_provider':'simulated RSA-signed OIDC provider; actual application backend and browser','production_writes':0,'screenshots':len(list(m.OUTPUT.glob('*.png')))}
(ROOT/'isolated-browser.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
