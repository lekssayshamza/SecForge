"""Creator editor acceptance using synthetic APIs, never a live database or AWS.
Run: python3 -B frontend/tests/room-management-browser.py
Owns a local Vite and Chromium; all mutations below are fixture-only.
"""
import base64, copy, importlib.util, json, os, subprocess, tempfile, time, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('navigation_fixture',ROOT/'frontend/tests/navigation-browser.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)
n.c.m.BASE='http://127.0.0.1:3194';n.c.m.OUTPUT=Path(tempfile.mkdtemp(prefix='secforge-room-management-browser-'))
n.c.USER=dict(id=7,username='synthetic_creator',role='creator',creator_access=True,email='creator@example.invalid')
ROOM=dict(n.ROOMS[0],objectives='Inspect a web service.',prerequisites='Basic Linux')
TASKS=[dict(id=1,title='Initial access',instructions='Investigate the company.',display_order=1),dict(id=2,title='Privilege escalation',instructions='Inspect permissions.',display_order=2)]
QUESTIONS=[dict(id=11,task_id=1,question_text='Find the first proof',hint='Inspect HTTP',points=100,display_order=1,has_saved_answer=True),dict(id=12,task_id=1,question_text='Find the legacy proof',hint=None,points=50,display_order=2,has_saved_answer=True),dict(id=13,task_id=2,question_text='Find the administrator proof',hint=None,points=50,display_order=3,has_saved_answer=True)]
ANSWERS={11:'flag',12:None,13:'rootflag'}
WORK=dict(room=ROOM,revision=1,dirty=False,tasks=TASKS,questions=QUESTIONS,image_status='available',image_template_id=20,image_is_published=True)
TEMPLATES=[dict(id=20,room_id=42,version=1,is_published=True,created_at='2026-10-18T10:00:00',machines=[dict(id=20,role='primary',ami_id='ami-synthetic-only',status='available')])]
BUILDER=None;TEST_LAB=None;FAIL_BUILDER=False;FAIL_ANSWER=False


class Browser(n.Browser):
    writes=[]
    def respond(self,p):
        global BUILDER, TEST_LAB
        request=p['request'];url=urlparse(request['url']);path=url.path.removeprefix('/api');method=request['method'];status=200
        payload=json.loads(request.get('postData','{}')) if request.get('postData') else {}
        if method!='GET':
            assert (method=='PATCH' and path=='/rooms/42/workspace') or (method=='POST' and path in ['/rooms/42/workspace/publish','/rooms/42/unpublish']), 'Infrastructure or unexpected mutation attempted'
            self.writes.append((method,path,copy.deepcopy(payload)))
        if path=='/rooms/42':body=ROOM
        elif path=='/rooms/42/workspace':
            if method=='PATCH':
                assert payload['revision']==WORK['revision']
                action=payload['action'];data=payload['data']
                if action=='metadata':ROOM.update(data)
                else:
                    kind,verb=action.split('_');items=QUESTIONS if kind=='question' else TASKS
                    if verb=='create':
                        ident=min([v['id'] for v in QUESTIONS+TASKS]+[0])-1
                        item=dict(data,id=ident,display_order=len(items)+1)
                        if kind=='question':ANSWERS[ident]=item.pop('answer');item.setdefault('task_id',None);item['has_saved_answer']=True
                        items.append(item)
                    elif verb=='update':
                        item=next(v for v in items if v['id']==data['id']);values=dict(data)
                        if kind=='question' and 'answer' in values:ANSWERS[item['id']]=values.pop('answer')
                        item.update(values)
                    elif verb=='remove':
                        items[:]=[v for v in items if v['id']!=data['id']]
                        if kind=='task':
                            for q in QUESTIONS:
                                if q['task_id']==data['id']:q['task_id']=None
                    elif verb=='order':items[:]=[next(v for v in items if v['id']==ident) for ident in data['ids']]
                    for index,item in enumerate(items):item['display_order']=index+1
                WORK['revision']+=1;WORK['dirty']=True
            body=WORK
        elif path.startswith('/rooms/42/workspace/questions/') and path.endswith('/answer'):
            ident=int(path.split('/')[-2]);params=parse_qs(url.query)
            if FAIL_ANSWER:status,body=503,dict(detail='Synthetic answer storage unavailable')
            elif int(params.get('revision',[WORK['revision']])[0])!=WORK['revision']:status,body=409,dict(detail='Draft changed')
            else:body=dict(answer=ANSWERS[ident],legacy=ANSWERS[ident] is None)
        elif path=='/rooms/42/workspace/publish':
            assert payload['revision']==WORK['revision'];ROOM['status']='published';WORK['dirty']=False;WORK['revision']+=1;body=WORK
        elif path=='/rooms/42/unpublish':ROOM['status']='draft';body=ROOM
        elif path=='/rooms/42/builder':
            if FAIL_BUILDER:status,body=503,dict(detail='Synthetic unavailable builder')
            elif BUILDER is None:status,body=404,dict(detail='No builder')
            else:body=BUILDER
        elif path=='/rooms/42/test-lab':
            if TEST_LAB is None:status,body=404,dict(detail='No test lab')
            else:body=TEST_LAB
        elif path=='/rooms/42/templates':body=TEMPLATES
        else:return super().respond(p)
        self.requests.append((method,path))
        self.send('Fetch.fulfillRequest',dict(requestId=p['requestId'],responseCode=status,responseHeaders=[dict(name='Content-Type',value='application/json')],body=base64.b64encode(json.dumps(body).encode()).decode()))

    def fill(self,selector,value):
        self.js("(()=>{const e=document.querySelector("+json.dumps(selector)+");e.focus();Object.getOwnPropertyDescriptor(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(e,"+json.dumps(value)+");e.dispatchEvent(new Event('input',{bubbles:true}));})()")

    def button(self,label):
        assert self.js("(()=>{const root=document.querySelector('dialog[open]')||document;const b=[...root.querySelectorAll('button')].find(b=>b.getClientRects().length&&b.textContent.trim()==="+json.dumps(label)+");if(!b)return false;b.click();return true})()"),label

    def tab(self,name):
        self.click('#room-tab-'+name);self.wait("document.querySelector('#room-tab-"+name+"')?.getAttribute('aria-selected')==='true'")

    def widths(self,prefix):
        for theme in ['light','dark']:
            self.js('document.documentElement.dataset.theme='+json.dumps(theme))
            for width in [1440,768,390,320]:
                self.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<500));self.js('window.scrollTo(0,0)');self.wait('document.fonts.status==="loaded"');time.sleep(.1)
                assert self.js('document.documentElement.scrollWidth<=innerWidth'),(prefix,width,'overflow')
                assert self.js("document.querySelectorAll('.room-manager > [role=tabpanel]:not([hidden])').length===1"), 'Exactly one visible panel'
                self.screenshot(f'{prefix}-{theme}-{width}.png')
        self.send('Emulation.setDeviceMetricsOverride',dict(width=1440,height=900,deviceScaleFactor=1,mobile=False))


def main():
    global BUILDER, TEST_LAB, FAIL_BUILDER, FAIL_ANSWER
    with (n.c.m.OUTPUT/'vite.log').open('wb') as log:
        process=subprocess.Popen([str(ROOT/'frontend/node_modules/.bin/vite'),'--host','127.0.0.1','--port','3194','--strictPort'],cwd=ROOT/'frontend',env={**os.environ,'VITE_API_BASE_URL':''},stdout=log,stderr=log)
    try:
        for _ in range(100):
            try:urllib.request.urlopen(n.c.m.BASE,timeout=1);break
            except Exception:time.sleep(.1)
        with Browser() as b:
            b.send('Page.addScriptToEvaluateOnNewDocument',dict(source='window.confirm=()=>true;'+n.CAPTURE_ERRORS))
            b.go('/rooms',"!!document.querySelector('.catalog-room-card')")
            b.js('localStorage.setItem("access_token","synthetic-only");localStorage.setItem("user",'+json.dumps(json.dumps(n.c.USER))+')')
            b.go('/rooms/42',"!!document.querySelector('.room-manager-tabs') && !!document.querySelector('#challenge-questions .learning-task')")
            assert not any(path.endswith('/answer') for _,path in b.requests),'No eager answer reads'
            assert b.js("!document.body.innerText.includes('Room details') && !document.body.innerText.includes('Edit briefing') && !document.body.innerText.includes('Publication options') && !document.querySelector('.workflow-map,.draft-status,.task-navigation')")
            b.widths('content')
            b.js("document.querySelector('#room-tab-content').focus()");b.key('ArrowRight','ArrowRight',39);b.wait("!document.querySelector('#room-panel-lab').hidden")
            b.widths('lab-idle');b.key('Home','Home',36) if b.js("document.activeElement?.getAttribute('role')==='tab'") else b.tab('content')
            b.tab('content');b.button('Edit room');b.wait("!!document.querySelector('dialog[open] #edit-name')")
            assert b.js("!document.querySelector('dialog').textContent.includes('Do not include answers')")
            b.fill('#edit-name','Updated synthetic room');b.fill('#edit-description','A revised public briefing.');b.fill('#edit-objectives','Investigate the web service.');b.fill('#edit-prerequisites','Basic command line')
            b.js("(()=>{const e=document.querySelector('#edit-category');e.value='networking';e.dispatchEvent(new Event('change',{bubbles:true}))})()")
            b.button('Save changes');b.wait("!document.querySelector('dialog[open]') && document.querySelector('h1').textContent==='Updated synthetic room'")
            assert ROOM['description']=='A revised public briefing.' and 'networking' in ROOM['categories']
            b.button('Edit question');b.wait("document.querySelector('#question-answer')?.value==='flag'")
            assert not b.js("document.querySelector('dialog').textContent.includes('Keep saved answer')")
            b.fill('#question-answer','');before=len(b.writes);b.button('Save question');assert len(b.writes)==before
            b.fill('#question-answer','newflag');b.button('Save question');b.wait("!document.querySelector('dialog[open]')")
            assert ANSWERS[11]=='newflag'
            b.button('Edit question');b.wait("document.querySelector('#question-answer')?.value==='newflag'");b.button('Cancel')
            b.click('#room-task-1 .question-list > li:nth-child(2) .question-actions button');b.wait("!!document.querySelector('#legacy-answer-help')")
            assert b.js("document.querySelector('#question-answer').value==='' && !document.querySelector('#question-answer').required")
            b.fill('#question-answer','legacyflag');b.button('Save question');b.wait("!document.querySelector('dialog[open]')")
            b.click('#room-task-1 .question-list > li:nth-child(2) .question-actions button');b.wait("document.querySelector('#question-answer')?.value==='legacyflag'");assert not b.js("!!document.querySelector('#legacy-answer-help')");b.button('Cancel')
            b.button('Add question');b.wait("!!document.querySelector('#question-text')");b.fill('#question-text','Synthetic added question');b.fill('#question-answer','addedflag');b.button('Save question');b.wait("!document.querySelector('dialog[open]')")
            assert len(QUESTIONS)==4 and ANSWERS[min(ANSWERS)]=='addedflag'
            b.click('#room-task-ungrouped summary')
            b.js("[...document.querySelectorAll('#room-task-ungrouped button')].find(b=>b.textContent==='Delete question').click()")
            b.wait("!!document.querySelector('dialog[open]')");b.button('Remove question');b.wait("!document.querySelector('dialog[open]')");assert len(QUESTIONS)==3
            b.button('Preview questions');assert not any('answer' in q for q in WORK['questions']);b.button('Back to editor')
            b.click('button[aria-label="Move question 1 down"]');b.wait("document.querySelector('#room-task-1 .question-title h3').textContent==='Find the legacy proof'")
            b.button('Add task');b.wait("!!document.querySelector('#task-title')");b.fill('#task-title','Extra task');b.button('Save task');b.wait("!document.querySelector('dialog[open]')");assert len(TASKS)==3
            b.button('Edit task');b.wait("!!document.querySelector('#task-title')");b.fill('#task-title','Updated access task');b.button('Save task');b.wait("!document.querySelector('dialog[open]')")
            assert TASKS[0]['title']=='Updated access task'
            b.button('Task down');b.wait("document.querySelector('.task-summary-title').textContent==='Privilege escalation'")
            extra=next(task for task in TASKS if task['title']=='Extra task')['id'];b.click(f'#room-task-{extra} summary')
            b.js("[...document.querySelectorAll('#room-task-"+str(extra)+" button')].find(b=>b.textContent==='Remove task').click()")
            b.wait("!!document.querySelector('dialog[open]')");b.button('Remove task');b.wait("!document.querySelector('dialog[open]')");assert len(TASKS)==2
            b.tab('lab');b.button('Edit ports');b.button('Add port rule');b.fill('#port-start-2','53');b.js("(()=>{const e=document.querySelector('#port-protocol-2');e.value='udp';e.dispatchEvent(new Event('change',{bubbles:true}))})()")
            b.button('Save ports');b.wait("!!document.querySelector('#network-ports [role=status]')");assert any(v['protocol']=='udp' for v in ROOM['exposed_ports'])
            b.button('Edit ports');b.fill('#port-start-0','0');before=len(b.writes);b.button('Save ports');b.wait("!!document.querySelector('#network-ports [role=alert]')");assert len(b.writes)==before;b.button('Cancel')
            b.tab('publish');b.widths('publish');b.button('Publish update');b.wait("!!document.querySelector('dialog[open]')");b.button('Publish room')
            b.wait("!document.querySelector('dialog[open]') && document.querySelector('#room-panel-publish').textContent.includes('No unpublished changes')")
            assert not WORK['dirty'];b.button('Unpublish room');b.wait("!!document.querySelector('dialog[open]')");b.button('Unpublish room')
            b.wait("!document.querySelector('dialog[open]') && document.querySelector('#room-panel-publish .badge').textContent==='draft'")
            assert ROOM['status']=='draft'
            b.button('Publish room');b.wait("!!document.querySelector('dialog[open]')");b.button('Publish room');b.wait("!document.querySelector('dialog[open]')")
            BUILDER=dict(n.c.m.ENVIRONMENT,room_id=42,expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat());TEST_LAB=dict(BUILDER)
            b.go('/rooms/42',"!!document.querySelector('.room-manager-tabs')");b.tab('lab');b.wait("document.querySelector('#builder-workspace')?.textContent.includes('Stop builder') && document.querySelector('#test-workspace')?.textContent.includes('Stop Machine')");b.widths('lab-running')
            assert b.js("document.querySelector('#builder-workspace').textContent.includes('10.0.1.42') && document.querySelector('#test-workspace').textContent.includes('Expires')")
            BUILDER=None;TEST_LAB=None;FAIL_BUILDER=True
            b.go('/rooms/42',"!!document.querySelector('.room-manager-tabs')");b.tab('lab');b.wait("!!document.querySelector('#builder-workspace [role=alert]')");assert b.js("document.querySelector('#builder-workspace').textContent.includes('Refresh builder status')")
            FAIL_BUILDER=False;FAIL_ANSWER=True
            b.go('/rooms/42',"!!document.querySelector('#challenge-questions .learning-task')");b.button('Edit question');b.wait("!!document.querySelector('dialog [role=alert]')");assert b.js("document.querySelector('#question-answer').disabled && [...document.querySelectorAll('dialog button')].find(b=>b.textContent==='Save question').disabled");b.button('Cancel')
            assert not b.errors,b.errors
            assert all(not any(t in path for t in ['/builder/start','/test-lab/start','/templates/capture']) for method,path,payload in b.writes)
            result=dict(status='PASS',three_sections=True,metadata=True,answers_prefill_update=True,legacy_replacement=True,no_eager_answers=True,question_task_crud_and_order=True,network_validation_and_cancel=True,publishing_and_unpublishing=True,operational_states=['idle','running','error'],viewports=[1440,768,390,320],themes=['light','dark'],runtime_errors=b.errors,production_writes=0,aws_operations=0,screenshots=str(n.c.m.OUTPUT))
            (n.c.m.OUTPUT/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:process.terminate();process.wait(timeout=10)


if __name__=='__main__':main()
