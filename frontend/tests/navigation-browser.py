"""Workspace/navigation acceptance in Chromium using synthetic APIs only.
Run: python3 -B frontend/tests/navigation-browser.py
Owns its Vite/Chromium processes; no backend, AWS or production access.
"""
import base64, importlib.util, json, os, subprocess, tempfile, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('catalog_fixture', ROOT/'frontend/tests/catalog-browser.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
c.m.BASE = 'http://127.0.0.1:3193'
c.m.OUTPUT = Path(tempfile.mkdtemp(prefix='secforge-navigation-browser-'))
ROOMS = [dict(room, creator_id=7, status='published', created_at='2026-10-18T10:00:00', updated_at='2026-10-18T10:00:00', exposed_ports=[dict(protocol='tcp',from_port=22,to_port=22),dict(protocol='tcp',from_port=80,to_port=80)]) for room in c.ROOMS]
WORK = dict(room=ROOMS[0], revision=1, dirty=False, tasks=[], questions=[], image_status=None, image_template_id=None, image_is_published=False)
PATH = dict(id=7, title='Synthetic learning journey', category='linux', difficulty='easy', description='Learn Linux in order.', objectives='', status='draft', room_count=1, rooms=[dict(ROOMS[0],publication_status='published')], unavailable_count=0)
ANALYTICS = dict(owned_rooms=4, published_rooms=4, draft_rooms=0, theory_rooms=1, practical_rooms=3, unique_learners=2, total_starts=2, completions=0, completion_rate=0, solves=0, points=0, published_paths=0, lab_requests=0, trend=[], paths=[])
METRIC = dict(id=42, title='Web to Root', room_type='PRACTICAL', status='published', learners_started=2, total_starts=2, completions=0, completion_rate=0, solves=0, points=0)
ADMIN = ['/admin','/admin/users','/admin/rooms','/admin/paths','/admin/labs','/admin/infrastructure','/admin/audit']
CAPTURE_ERRORS = """(()=>{const error=console.error;window.__consoleErrors=0;console.error=function(...args){window.__consoleErrors++;return error.apply(console,args)};addEventListener('unhandledrejection',()=>window.__consoleErrors++)})()"""


class Browser(c.Browser):
    def __enter__(self):
        super().__enter__()
        self.send('Page.addScriptToEvaluateOnNewDocument',dict(source='window.__navigationDocument = crypto.randomUUID()'))
        return self

    def go(self, path, condition):
        previous = self.js('window.__navigationDocument || null')
        self.send('Page.navigate',dict(url=c.m.BASE+path))
        self.wait('!!window.__navigationDocument && window.__navigationDocument !== '+json.dumps(previous)+' && ('+condition+')')

    def wait(self, expression):
        # CDP navigation can briefly have no body/execution context. Wait for
        # the document before evaluating page assertions against the new DOM.
        try: c.m.Browser.wait(self, 'document.readyState === "complete" && !!document.body && (' + expression + ')')
        except AssertionError:
            diagnostic=dict(condition=expression,path=self.js('location.pathname'),ready=self.js('document.readyState'),errors=self.errors,requests=self.requests,body=self.js('document.body.innerText'),console_errors=self.js('window.__consoleErrors'))
            (c.m.OUTPUT/'failure.json').write_text(json.dumps(diagnostic,indent=2))
            self.screenshot('failure.png')
            raise

    def respond(self, paused):
        request = paused['request']; path = urlparse(request['url']).path.removeprefix('/api'); method = request['method']; status = 200
        assert method == 'GET', 'Synthetic navigation check must not mutate data'
        if path == '/auth/me': body = c.USER
        elif path == '/auth/options': body = dict(google_enabled=False, registration_enabled=False)
        elif path == '/rooms': body = ROOMS
        elif path == '/rooms/42': body = ROOMS[0]
        elif path == '/rooms/42/workspace': body = WORK
        elif path == '/paths': body = dict(items=[PATH],total=1,page=1,page_size=24,categories=['linux'])
        elif path == '/paths/7': body = PATH
        elif path == '/paths/eligible-rooms': body = dict(items=ROOMS,has_more=False)
        elif path == '/creator/analytics': body = ANALYTICS
        elif path == '/creator/analytics/rooms': body = dict(items=[METRIC],total=1,page=1,page_size=20)
        elif path == '/creator/analytics/rooms/42': body = dict(room=METRIC,questions=[])
        elif path.startswith('/admin'):
            assert c.USER['role'] == 'admin', 'Unauthorized frontend made a privileged request'
            if path == '/admin': body = dict(accounts=3,learners=1,creators=1,administrators=1,suspended=0,published_rooms=4,draft_rooms=0,theory_rooms=1,practical_rooms=3,published_paths=0,active_labs=0,active_builders=0,failed_labs=0,expired_labs=0,recent_issues=[])
            elif path == '/admin/infrastructure': body = dict(available=True,observed_at='2026-10-18T10:00:00Z',instances=[],running_count=0,truncated=False,pricing_note='No synthetic cost data',issue=None)
            else: body = dict(items=[],total=0,page=1,page_size=20)
        else:
            return super().respond(paused)
        self.requests.append((method,path))
        self.send('Fetch.fulfillRequest',dict(requestId=paused['requestId'],responseCode=status,responseHeaders=[dict(name='Content-Type',value='application/json')],body=base64.b64encode(json.dumps(body).encode()).decode()))


def login(b, role):
    c.USER = dict(id=7,username='synthetic_'+role,role=role,creator_access=role in ['creator','admin'],email='synthetic@example.invalid')
    b.go('/rooms',"!!document.querySelector('.catalog-room-card')")
    b.js('localStorage.clear()')
    b.go('/login','!!document.querySelector("#username")')
    b.js('localStorage.setItem("access_token","synthetic-only");localStorage.setItem("user",'+json.dumps(json.dumps(c.USER))+')')


def no_sidebar(b):
    assert b.js("!document.querySelector('.workspace-sidebar,.workspace-layout,.workspace-content,.workspace-menu-toggle,nav[aria-label=\"Management workspace\"]')")
    assert b.js("!document.querySelector('a[href=\"/admin\"],a[href^=\"/admin/\"]')"), 'No admin links anywhere on the page'


def widths(b, name, sizes=(1440,768,390)):
    for theme in ['light','dark']:
        b.js('document.documentElement.dataset.theme='+json.dumps(theme))
        for width in sizes:
            b.send('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<500))
            b.js('window.scrollTo(0,0)'); b.wait('document.fonts.status==="loaded"'); time.sleep(.08)
            no_sidebar(b)
            assert b.js('document.documentElement.scrollWidth<=innerWidth'), (name,width,'horizontal overflow')
            assert b.js("(()=>{const el=document.querySelector('#main-content > .workspace');if(!el)return false;const rect=el.getBoundingClientRect();return rect.width<=1248 && Math.abs(rect.left-(document.documentElement.clientWidth-rect.width)/2)<2})()"), (name,width,'Centered existing maximum width')
            if name == 'creator':
                assert b.js("(()=>{const grid=document.querySelector('.room-grid'),toolbar=document.querySelector('.page-toolbar'),filters=document.querySelector('.catalog-filters');return Math.abs(grid.getBoundingClientRect().left-toolbar.getBoundingClientRect().left)<1 && Math.abs(grid.getBoundingClientRect().width-toolbar.getBoundingClientRect().width)<1 && Math.abs(grid.getBoundingClientRect().width-filters.getBoundingClientRect().width)<1})()"), 'Grid fills content width'
                if width>=1440: assert b.js("getComputedStyle(document.querySelector('.room-grid')).gridTemplateColumns.split(' ').length===4")
            b.screenshot(f'{name}-{theme}-{width}.png')
    b.send('Emulation.setDeviceMetricsOverride',dict(width=1440,height=900,deviceScaleFactor=1,mobile=False))


def account(b, creator):
    b.click('button[aria-label="Account options"]');b.wait('!!document.querySelector("[role=menu]") && document.activeElement?.getAttribute("role")==="menuitem"')
    no_sidebar(b)
    links=b.js("[...document.querySelectorAll('[role=menuitem][href]')].map(el=>el.getAttribute('href'))")
    for path in ['/creator','/paths/mine','/creator/analytics']: assert (path in links)==creator
    assert len(links)==len(set(links)), 'No duplicated destinations'
    b.key('End','End',35);b.wait("document.activeElement?.getAttribute('aria-label')==='Sign out'")
    b.key('Escape','Escape',27);b.wait("!document.querySelector('[role=menu]') && document.activeElement?.getAttribute('aria-label')==='Account options'")


def main():
    with (c.m.OUTPUT/'vite.log').open('wb') as log:
        command=[str(ROOT/'frontend/node_modules/.bin/vite')]+(['preview'] if os.environ.get('SECFORGE_BROWSER_DIST')=='1' else [])+['--host','127.0.0.1','--port','3193','--strictPort']
        process=subprocess.Popen(command,cwd=ROOT/'frontend',env={**os.environ,'VITE_API_BASE_URL':''},stdout=log,stderr=log)
    try:
        for _ in range(100):
            try: urllib.request.urlopen(c.m.BASE,timeout=1);break
            except Exception: time.sleep(.1)
        with Browser() as b:
            if os.environ.get('SECFORGE_FIXTURE_OFFLINE_FONTS')=='1':
                b.send('Network.enable');b.send('Network.setBlockedURLs',dict(urls=['*://fonts.googleapis.com/*','*://fonts.gstatic.com/*']))
            b.send('Page.addScriptToEvaluateOnNewDocument',dict(source=CAPTURE_ERRORS))
            login(b,'admin');b.go('/creator',"document.querySelectorAll('.room-card').length===4")
            widths(b,'creator',(1920,1440,1024,768,390,320));account(b,True)
            b.fill('.catalog-filters input','Linux First Steps');b.wait("document.querySelectorAll('.room-card').length===1")
            b.fill('.catalog-filters input','');b.select('Filter by difficulty','medium');b.wait("document.querySelectorAll('.room-card').length===1")
            b.select('Filter by difficulty','');b.wait("document.querySelectorAll('.room-card').length===4")
            b.button('Create room');b.wait('!!document.querySelector("dialog[open] #name")');b.key('Escape','Escape',27);b.wait('!document.querySelector("dialog[open]")')
            for destination,condition,name in [('/paths/mine',"!!document.querySelector('.path-card')",'paths'),('/creator/analytics',"!!document.querySelector('.operations-metrics')",'analytics')]:
                b.click('button[aria-label="Account options"]');b.wait('!!document.querySelector("[role=menu]")');b.click('[role="menuitem"][href="'+destination+'"]');b.wait('location.pathname==='+json.dumps(destination)+' && '+condition);widths(b,name)
            b.go('/creator/analytics/rooms/42',"document.body.textContent.includes('Question performance')");widths(b,'room-analytics')
            b.go('/rooms/42',"!!document.querySelector('#network-ports')");b.click('#room-tab-lab');b.wait("!document.querySelector('#room-panel-lab').hidden");widths(b,'room-editor')
            assert b.js("document.body.textContent.includes('Network & Exposed Ports') && document.body.textContent.includes('Publish')")
            b.go('/paths/7/edit',"!!document.querySelector('.path-wizard-steps')");widths(b,'path-editor')
            for route,marker in zip(ADMIN,['Recent lifecycle issues','Platform accounts','No matching rooms','No matching paths','No matching sessions','EC2 inventory','No administrative actions recorded']):
                b.go(route,"location.pathname==="+json.dumps(route)+" && !!document.querySelector('.administration-page') && document.body.textContent.includes("+json.dumps(marker)+")")
                no_sidebar(b);assert not b.js("!!document.querySelector('[role=alert]')");account(b,True)
            widths(b,'admin-audit')
            assert not b.errors and b.js('window.__consoleErrors===0')
            for role in ['creator','learner']:
                login(b,role)
                for route in ADMIN:
                    before=len([p for method,p in b.requests if p.startswith('/admin')])
                    b.go(route,"document.querySelector('h1')?.textContent==='Administrator access required'")
                    assert len([p for method,p in b.requests if p.startswith('/admin')])==before
                    account(b,role=='creator')
                if role=='learner':
                    b.go('/creator/analytics',"document.querySelector('h1')?.textContent==='Creator access required'")
                    b.go('/paths/mine',"document.querySelector('h1')?.textContent==='Creator access required'")
                assert not b.errors
            b.js('localStorage.clear()')
            before=len([p for _,p in b.requests if p.startswith('/admin')])
            for route in ADMIN: b.go(route,"location.pathname==='/login' && !!document.querySelector('#username')")
            assert len([p for _,p in b.requests if p.startswith('/admin')])==before
        result=dict(status='PASS',creator_pages=6,admin_direct_routes=7,unauthorized_roles=['guest','learner','creator'],viewports=[1920,1440,1024,768,390,320],themes=['light','dark'],sidebar_absent=True,grid_fills_container=True,account_navigation=True,room_search_filters=True,create_dialog=True,network_ports_preserved=True,mutations=0,runtime_errors=0,console_errors=0,screenshots=str(c.m.OUTPUT))
        (c.m.OUTPUT/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:
        process.terminate();process.wait(timeout=10)


if __name__=='__main__': main()
