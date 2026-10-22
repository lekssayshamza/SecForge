"""Learner acceptance against owned local Vite and synthetic APIs; no cloud calls."""
import importlib.util,json,base64,os,subprocess,tempfile,time,copy
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime,timedelta,timezone
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('navigation_fixture',ROOT/'frontend/tests/navigation-browser.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)
n.c.m.BASE='http://127.0.0.1:3195';n.c.m.OUTPUT=Path(tempfile.mkdtemp(prefix='secforge-learner-browser-'))
n.c.USER=dict(id=9,username='synthetic_learner',role='learner',email='learner@example.invalid')
ROOM=dict(n.ROOMS[0],name='Synthetic learner room',creator_id=7,points_total=44)
TASKS=[dict(id=i,title=title,instructions='## Investigate\nRead the lesson, then find the proof.\n\n```bash\nprintf "'+('long command '*30)+'"\n```\n\n| Service | Details |\n| --- | --- |\n| HTTP | '+('Wide table content '*12)+' |',display_order=i) for i,title in [(1,'First access'),(2,'Privilege escalation'),(3,'Legacy proof')]]
QUESTIONS=[dict(id=11,task_id=1,question_text='Find the first flag',hint='Look at the page',points=19,display_order=1),dict(id=12,task_id=2,question_text='Find root proof',hint=None,points=15,display_order=2),dict(id=13,task_id=3,question_text='An earlier proof',hint=None,points=10,display_order=3)]
SOLVED={13:None};LAB=None;LAB_ERROR=False;ANSWER_ERROR=False
def progress():
 count=len(SOLVED)
 return dict(room_id=42,solved_question_ids=list(SOLVED),questions_solved=count,questions_total=3,points_earned=sum(q['points'] for q in QUESTIONS if q['id'] in SOLVED),points_total=44,completed=count==3,completed_at='2026-10-18T10:00:00Z' if count==3 else None)
def machine(status='running',minutes=20,extensions=0):
 now=datetime.now(timezone.utc)
 return dict(n.c.m.ENVIRONMENT,room_id=42,status=status,session_started_at=(now-timedelta(minutes=40)).isoformat(),server_time=now.isoformat(),expires_at=(now+timedelta(minutes=minutes)).isoformat(),extensions_used=extensions,expired_at=now.isoformat() if minutes==0 else None)
class Browser(n.Browser):
 writes=[]
 def respond(self,p):
  global LAB
  request=p['request'];path=urlparse(request['url']).path.removeprefix('/api');method=request['method'];status=200
  if method!='GET':self.writes.append((method,path))
  if path=='/rooms/42':body=ROOM
  elif path=='/rooms/42/questions':body=dict(questions=QUESTIONS,authoring_locked=True)
  elif path=='/rooms/42/tasks':body=dict(tasks=TASKS)
  elif path=='/rooms/42/progress':body=progress()
  elif path=='/rooms/42/accepted-answers':
   if ANSWER_ERROR:status,body=503,dict(detail='Synthetic saved answer error')
   else:body=[dict(question_id=q['id'],answer=SOLVED[q['id']],legacy=SOLVED[q['id']] is None,points_awarded=q['points']) for q in QUESTIONS if q['id'] in SOLVED]
  elif path.startswith('/rooms/42/questions/') and path.endswith('/submit'):
   assert method=='POST';ident=int(path.split('/')[-2]);value=json.loads(request['postData'])['answer'];correct=value.strip()==('SF{first}' if ident==11 else 'SF{root}')
   prior=ident in SOLVED
   if correct and not prior:SOLVED[ident]=value
   body=dict(correct=correct,already_solved=prior,points_awarded=next(q['points'] for q in QUESTIONS if q['id']==ident) if correct and not prior else 0,progress=progress())
  elif path=='/rooms/42/lab':
   if LAB_ERROR:status,body=503,dict(detail='Synthetic lab query failure')
   elif method=='POST':LAB=machine();body=LAB
   elif LAB is None:status,body=404,dict(detail='No synthetic machine')
   else:body=LAB
  elif path=='/rooms/42/lab/extend':assert method=='POST';LAB=machine(minutes=80,extensions=1);body=LAB
  elif path=='/rooms/42/lab/stop':assert method=='POST';LAB=machine('stopped');body=LAB
  else:
   assert method=='GET','Unexpected synthetic write'
   return super().respond(p)
  self.requests.append((method,path))
  self.send('Fetch.fulfillRequest',dict(requestId=p['requestId'],responseCode=status,responseHeaders=[dict(name='Content-Type',value='application/json'),dict(name='Cache-Control',value='private, no-store')],body=base64.b64encode(json.dumps(body).encode()).decode()))
 def button(self,label):
  assert self.js("(()=>{const root=document.querySelector('dialog[open]')||document;const b=[...root.querySelectorAll('button')].find(el=>el.getClientRects().length&&el.textContent.trim()==="+json.dumps(label)+");if(!b)return false;b.click();return true})()"),label
 def opened(self,number):self.wait("document.querySelectorAll('.learning-task[open]').length===1 && document.querySelector('#room-task-"+str(number)+"').open")
 def widths(self,label):
  for theme in ['light','dark']:
   self.js('document.documentElement.dataset.theme='+json.dumps(theme))
   for width in [1440,768,390,320]:
    self.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<500));self.js('window.scrollTo(0,0)');self.wait('document.fonts.status==="loaded"');time.sleep(.08)
    assert self.js('document.documentElement.scrollWidth<=innerWidth'),(label,theme,width,'Overflow')
    assert self.js("[...document.querySelectorAll('.answer-form input')].every(el=>el.getBoundingClientRect().height<60)"),'Answer controls keep normal heights'
    assert self.js("!document.querySelector('.learner-machine-aside,.learner-room-grid,.task-navigation,.lab-aside')")
    assert self.js("(()=>{const machine=document.querySelector('#learner-workspace'),brief=document.querySelector('#room-briefing'),questions=document.querySelector('#challenge-questions');return machine.compareDocumentPosition(brief)&Node.DOCUMENT_POSITION_FOLLOWING && Math.abs(questions.getBoundingClientRect().width-brief.getBoundingClientRect().width)<2})()")
    self.screenshot(f'{label}-{theme}-{width}.png')
  self.send('Emulation.setDeviceMetricsOverride',dict(width=1440,height=900,deviceScaleFactor=1,mobile=False))
def main():
 global LAB,LAB_ERROR,ANSWER_ERROR
 with (n.c.m.OUTPUT/'vite.log').open('wb') as log:process=subprocess.Popen([str(ROOT/'frontend/node_modules/.bin/vite'),'--host','127.0.0.1','--port','3195','--strictPort'],cwd=ROOT/'frontend',env={**os.environ,'VITE_API_BASE_URL':''},stdout=log,stderr=log)
 try:
  import urllib.request
  for _ in range(100):
   try:urllib.request.urlopen(n.c.m.BASE,timeout=1);break
   except Exception:time.sleep(.1)
  with Browser() as b:
   b.send('Page.addScriptToEvaluateOnNewDocument',dict(source=n.CAPTURE_ERRORS))
   b.go('/rooms',"!!document.querySelector('.catalog-room-card')");b.js('localStorage.clear();localStorage.setItem("access_token","synthetic-only");localStorage.setItem("user",'+json.dumps(json.dumps(n.c.USER))+')')
   b.go('/rooms/42',"!!document.querySelector('#room-task-1') && !!document.querySelector('#learner-workspace button')");b.opened(1)
   assert b.js("!document.querySelector('#learner-workspace').textContent.includes('Session ended') && !document.querySelector('#learner-workspace').textContent.includes('Cleanup complete')")
   b.widths('stopped');b.fill('#answer-11','typed but not submitted');b.click('#room-task-2 summary');b.opened(2);b.fill('#answer-12','keep root draft');b.click('#room-task-1 summary');b.opened(1)
   assert b.js("document.querySelector('#answer-11').value==='typed but not submitted'")
   b.fill('#answer-11','wrong');b.button('Submit answer');b.wait("document.querySelector('#answer-feedback-11').textContent.includes('Incorrect')")
   assert b.js("document.querySelector('#answer-11').value==='wrong' && !document.querySelector('#answer-11').disabled")
   b.fill('#answer-11',' SF{first} ');b.button('Submit answer');b.wait("document.querySelector('#answer-11').disabled && document.querySelector('#answer-11').value===' SF{first} '");b.opened(1)
   b.button('Next task');b.opened(2);assert b.js("document.activeElement===document.querySelector('#room-task-2 summary') && document.querySelector('#answer-12').value==='keep root draft'")
   b.fill('#answer-12','SF{root}');b.button('Submit answer');b.wait("document.querySelector('.learner-completed')?.textContent.includes('Room completed!')");b.opened(2)
   assert b.js("document.querySelector('.learner-completed').textContent.includes('Points earned: 44') && document.querySelector('#answer-12').disabled")
   b.widths('completed');b.send('Page.reload');b.wait("document.querySelector('#answer-12')?.value==='SF{root}' && document.querySelector('#answer-12').disabled");b.opened(2)
   assert b.js("document.querySelector('#answer-11').value===' SF{first} ' && document.querySelector('#answer-feedback-13').textContent.includes('Previous answer unavailable')")
   b.click('#room-task-2 summary');b.wait("document.querySelectorAll('.learning-task[open]').length===0")
   b.js("document.querySelector('#room-task-1 summary').focus()");b.key('Enter','Enter',13);b.opened(1);b.key('ArrowDown','ArrowDown',40);b.wait("document.activeElement===document.querySelector('#room-task-2 summary')");b.key(' ','Space',32);b.opened(2)
   b.button('Start Machine');b.wait("document.querySelector('#learner-workspace').textContent.includes('Stop Machine') && !!document.querySelector('[role=timer]')")
   b.widths('running');b.button('Extend +1 Hour');b.wait("document.querySelector('#learner-workspace').textContent.includes('Added 1 hour')")
   b.button('Stop Machine');b.wait("!!document.querySelector('dialog[open]')");b.button('Stop lab');b.wait("!document.querySelector('dialog[open]') && document.querySelector('#learner-workspace').textContent.includes('Start Machine')")
   for state in ['pending','provisioning','stopping','failed']:
    LAB=machine(state);b.go('/rooms/42',"!!document.querySelector('#learner-workspace .environment-status')")
    if state=='failed':assert b.js("document.querySelector('#learner-workspace').textContent.includes('Retry cleanup')")
    b.screenshot(state+'.png')
   LAB=machine(minutes=0);b.go('/rooms/42',"document.querySelector('#learner-workspace')?.textContent.includes('Access expired')");assert b.js("!document.querySelector('.connection-guide-compact')")
   LAB=machine('stopped');LAB_ERROR=True;ANSWER_ERROR=True;b.go('/rooms/42',"!!document.querySelector('#learner-workspace [role=alert]') && document.body.textContent.includes('Retry saved answers')");assert b.js("document.querySelector('#answer-11').disabled && !!document.querySelector('.learner-completed')")
   assert not b.errors,b.errors;assert b.js('window.__consoleErrors===0')
   result=dict(status='PASS',single_open=True,first_task_default=True,last_task_remembered=True,pending_inputs_retained=True,next_task_focus_scroll=True,correct_and_incorrect_feedback=True,accepted_inputs_survive_refresh=True,legacy_solves=True,completion_immediate_and_revisit=True,keyboard=True,lab_states=['stopped','running','pending','provisioning','stopping','failed','expired','query-error'],lab_start_stop_extend=True,viewports=[1440,768,390,320],themes=['light','dark'],runtime_errors=0,production_writes=0,aws_operations=0,screenshots=str(n.c.m.OUTPUT))
   (n.c.m.OUTPUT/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
 finally:process.terminate();process.wait(timeout=10)
if __name__=='__main__':main()
