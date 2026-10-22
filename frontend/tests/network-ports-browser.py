"""Synthetic editor smoke test; API requests are fulfilled in Chromium.
Run from the project root: python3 -B frontend/tests/network-ports-browser.py
Requires installed Chromium and websocket-client; starts its own local Vite.
"""
import base64, importlib.util, json, os, subprocess, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('fixture_browser',ROOT/'frontend/tests/task-a-browser.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.BASE='http://127.0.0.1:3191'
OUT=Path('/tmp/secforge-ports-browser');OUT.mkdir(exist_ok=True)
USER=dict(id=7,username='synthetic_creator',role='creator',creator_access=True,email='creator@example.invalid')
ROOM=dict(m.ROOM,category='web-security',categories=['web-security'],room_type='PRACTICAL',exposed_ports=[dict(protocol='tcp',from_port=p,to_port=p) for p in (22,80)])
WORK=dict(room=ROOM,revision=1,dirty=False,tasks=[],questions=[],image_status=None,image_template_id=None,image_is_published=False)
class Browser(m.Browser):
 def respond(self,p):
  r=p['request'];path=urlparse(r['url']).path.removeprefix('/api');self.requests.append((r['method'],path));status=200
  if path=='/auth/me':body=USER
  elif path=='/rooms/42':body=ROOM
  elif path=='/rooms/42/workspace':
   if r['method']=='PATCH':
    payload=json.loads(r.get('postData','{}'));assert payload['revision']==WORK['revision'] and payload['action']=='metadata'
    ROOM.update(payload['data']);WORK['revision']+=1;WORK['dirty']=True
   body=WORK
  elif path in ['/rooms/42/builder','/rooms/42/test-lab']:status,body=404,dict(detail='No machine in synthetic fixture')
  elif path=='/rooms/42/templates':body=[]
  elif path=='/rooms/42/questions':body=dict(questions=[],authoring_locked=False)
  elif path=='/rooms/42/tasks':body=dict(tasks=[])
  elif path=='/rooms/42/progress':body=dict(room_id=42,solved_question_ids=[],questions_solved=0,questions_total=0,points_earned=0,points_total=0,completed=False,completed_at=None)
  else:status,body=404,dict(detail='Unknown synthetic endpoint')
  assert r['method']=='GET' or (r['method']=='PATCH' and path=='/rooms/42/workspace'), 'Unexpected synthetic mutation'
  self.send('Fetch.fulfillRequest',dict(requestId=p['requestId'],responseCode=status,responseHeaders=[dict(name='Content-Type',value='application/json')],body=base64.b64encode(json.dumps(body).encode()).decode()))
def input_value(b,selector,value):
 b.js("(()=>{const el=document.querySelector("+json.dumps(selector)+");Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,"+json.dumps(value)+");el.dispatchEvent(new Event('input',{bubbles:true}));})()")
with open('/tmp/secforge-ports-vite.log','wb') as log:
 process=subprocess.Popen([str(ROOT/'frontend/node_modules/.bin/vite'),'--host','127.0.0.1','--port','3191','--strictPort'],cwd=ROOT/'frontend',env={**os.environ,'VITE_API_BASE_URL':''},stdout=log,stderr=log)
try:
 for _ in range(100):
  try:urllib.request.urlopen(m.BASE,timeout=1);break
  except Exception:time.sleep(.1)
 with Browser() as b:
  b.go('/login','!!document.querySelector("#username")')
  b.js('localStorage.setItem("access_token","synthetic-only");localStorage.setItem("user",'+json.dumps(json.dumps(USER))+')')
  b.go('/rooms/42','!!document.querySelector("#network-ports")')
  b.click('#room-tab-lab');b.wait("!document.querySelector('#room-panel-lab').hidden")
  b.button('Edit ports')
  b.button('Add port rule')
  input_value(b,'#port-start-2','40000');input_value(b,'#port-end-2','40100')
  b.button('Add port rule')
  b.js('(()=>{const el=document.querySelector("#port-protocol-3");el.value="udp";el.dispatchEvent(new Event("change",{bubbles:true}));})()')
  input_value(b,'#port-start-3','53')
  checks=[]
  for theme in ['light','dark']:
   b.js('document.documentElement.dataset.theme='+json.dumps(theme))
   for width in [1440,390,320]:
    b.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=1000,deviceScaleFactor=1,mobile=False))
    b.js('document.querySelector("#network-ports").scrollIntoView()')
    b.wait('document.fonts.status === "loaded"');time.sleep(.2)
    checks.append(dict(theme=theme,width=width,overflow=b.js('document.documentElement.scrollWidth>innerWidth'),labels=b.js('[...document.querySelectorAll("#network-ports input,#network-ports select")].every(el=>document.querySelector(`label[for="${el.id}"]`))')))
    data=b.send('Page.captureScreenshot',dict(captureBeyondViewport=False))['data'];(OUT/f'{theme}-{width}.png').write_bytes(base64.b64decode(data))
  b.button('Save ports');b.wait('!!document.querySelector("#network-ports [role=status]")')
  assert any(p['protocol']=='udp' and p['from_port']==53 for p in ROOM['exposed_ports'])
  assert any(p['from_port']==40000 and p['to_port']==40100 for p in ROOM['exposed_ports'])
  assert all(not c['overflow'] and c['labels'] for c in checks),checks
  assert not b.errors,b.errors
  print(json.dumps(dict(synthetic=True,checks=checks,saved_rules=ROOM['exposed_ports'],browser_errors=b.errors)))
finally:
 process.terminate();process.wait(timeout=10)
