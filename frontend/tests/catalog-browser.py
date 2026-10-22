"""Real Chromium interaction/responsive checks against synthetic APIs only.
Run: python3 -B frontend/tests/catalog-browser.py (owns its local Vite process).
No application database, cloud API, or production requests are used.
"""
import base64, importlib.util, json, os, subprocess, tempfile, time, urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('catalog_browser_base', ROOT / 'frontend/tests/task-a-browser.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.BASE = 'http://127.0.0.1:3192'
m.OUTPUT = Path(tempfile.mkdtemp(prefix='secforge-catalog-browser-'))
USER = dict(id=9, username='synthetic_learner', role='learner', email='learner@example.invalid')
TAXONOMY = json.loads((ROOT / 'backend/app/category_taxonomy.json').read_text())
ROOMS = [
    dict(id=42, name='Web to Root', description='Investigate the company website and find a route to root.', category='web-security', categories=['web-security', 'linux', 'privilege-escalation'], difficulty='easy', room_type='PRACTICAL', points_total=150, progress='completed'),
    dict(id=43, name='Linux First Steps', description='Learn the basics.', category='linux', categories=['linux'], difficulty='easy', room_type='PRACTICAL', points_total=104, progress='in-progress'),
    dict(id=44, name='A long theory title covering network foundations and advanced investigation techniques ' * 3, description='A longer description with detailed context and objectives. ' * 18, category='networking', categories=['networking', 'linux'], difficulty='medium', room_type='THEORY', points_total=50, progress='not-started'),
    dict(id=45, name='Advanced investigation', description='Examine several related topics.', category='web-security', categories=[c['slug'] for c in TAXONOMY[:8]], difficulty='hard', room_type='PRACTICAL', points_total=0, progress='not-started'),
]


class Browser(m.Browser):
    bookmarks = {43, 44}
    fail_catalog = False
    catalog_requests = []

    def wait(self, expression):
        try: super().wait(expression)
        except AssertionError:
            self.screenshot('failure.png')
            raise AssertionError(expression + '; focus=' + str(self.js('({tag:document.activeElement?.tagName,label:document.activeElement?.getAttribute("aria-label")})')) + '; popovers=' + str(self.js("[...document.querySelectorAll('.anchored-popover > button')].map(el=>({label:el.textContent,open:el.getAttribute('aria-expanded')}))"))) from None

    def respond(self, paused):
        request = paused['request']; url = urlparse(request['url']); path = url.path.removeprefix('/api'); method = request['method']; params = parse_qs(url.query)
        self.requests.append((method, path)); status = 200
        if path == '/auth/me': body = USER
        elif path.startswith('/bookmarks/rooms/'):
            ident = int(path.split('/')[-1])
            if method == 'PUT': self.bookmarks.add(ident)
            elif method == 'DELETE': self.bookmarks.discard(ident)
            else: assert method == 'GET'
            body = dict(bookmarked=ident in self.bookmarks)
        elif path in ['/public/rooms', '/discovery/rooms', '/saved/rooms']:
            assert method == 'GET'; self.catalog_requests.append((path, params))
            items = [dict(r, bookmarked=r['id'] in self.bookmarks) for r in ROOMS if path != '/saved/rooms' or r['id'] in self.bookmarks]
            q = params.get('q', [''])[0].lower(); selected = params.get('category', [''])[0].split(',')
            items = [r for r in items if q in (r['name'] + ' ' + r['description']).lower() and (selected == [''] or any(c in selected for c in r['categories']))]
            for field in ['difficulty', 'room_type', 'progress']:
                value = params.get(field, [''])[0]
                if value and value != 'all': items = [r for r in items if r[field] == value]
            if params.get('sort') == ['alphabetical']: items.sort(key=lambda r: r['name'])
            body = dict(items=items, total=len(items), page=1, page_size=24, categories=[c['slug'] for c in TAXONOMY])
            if self.fail_catalog: status, body = 503, dict(detail='Synthetic unavailable catalog')
        elif path in ['/saved/paths', '/public/paths', '/discovery/paths']:
            assert method == 'GET'
            body = dict(items=[dict(id=7, title='Network essentials', category='linux', difficulty='easy', description='Learn in order.', room_count=3, rooms_completed=1, percent=33, bookmarked=True)], total=1, page=1, page_size=24, categories=['linux'])
        elif path.startswith('/rooms/'):
            assert method == 'GET'
            parts = path.split('/'); ident = int(parts[2]); room = next(r for r in ROOMS if r['id'] == ident)
            if len(parts) == 3: body = dict(room, creator_id=7, status='published')
            elif parts[3] == 'questions': body = dict(questions=[], authoring_locked=False)
            elif parts[3] == 'tasks': body = dict(tasks=[])
            elif parts[3] == 'accepted-answers': body = []
            elif parts[3] == 'progress': body = dict(room_id=ident, solved_question_ids=[], questions_solved=0, questions_total=0, points_earned=0, points_total=room['points_total'], completed=False)
            elif parts[3] == 'templates': body = []
            elif parts[3] in ['builder', 'lab', 'test-lab']: status, body = 404, dict(detail='No synthetic instance')
            else: raise AssertionError('Unexpected room endpoint: ' + path)
        else: raise AssertionError('Unexpected synthetic endpoint: ' + path)
        assert method == 'GET' or path.startswith('/bookmarks/rooms/'), 'Unexpected mutation'
        self.send('Fetch.fulfillRequest', dict(requestId=paused['requestId'], responseCode=status, responseHeaders=[dict(name='Content-Type', value='application/json')], body=base64.b64encode(json.dumps(body).encode()).decode()))

    def key(self, key, code, number):
        for event in ['keyDown', 'keyUp']:
            params = dict(type=event, key=key, code=code, windowsVirtualKeyCode=number, nativeVirtualKeyCode=number)
            if event == 'keyDown' and key == 'Enter': params.update(text='\r', unmodifiedText='\r')
            if event == 'keyDown' and key == ' ': params.update(text=' ', unmodifiedText=' ')
            self.send('Input.dispatchKeyEvent', params)

    def fill(self, selector, value):
        self.js("(()=>{const el=document.querySelector(" + json.dumps(selector) + ");el.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el," + json.dumps(value) + ");el.dispatchEvent(new Event('input',{bubbles:true}));})()")

    def choose(self, label):
        self.js("(()=>{const label=[...document.querySelectorAll('.category-options label')].find(el=>el.textContent.trim()===" + json.dumps(label) + ");if(!label)throw Error('Missing category');const input=label.querySelector('input');input.focus();input.click()})()")

    def select(self, label, value):
        self.js("(()=>{const el=document.querySelector('select[aria-label=" + json.dumps(label) + "]');el.value=" + json.dumps(value) + ";el.dispatchEvent(new Event('change',{bubbles:true}));})()")

    def widths(self, page):
        for theme in ['light', 'dark']:
            self.js('document.documentElement.dataset.theme=' + json.dumps(theme))
            for width in [1440, 1024, 768, 390, 320]:
                self.send('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=False))
                self.js('window.scrollTo(0,0)'); self.wait('document.fonts.status === "loaded"'); time.sleep(.08)
                assert self.js('document.documentElement.scrollWidth <= innerWidth'), (page, theme, width, 'overflow')
                assert self.js("(()=>{const cards=[...document.querySelectorAll('.catalog-room-card')];return cards.every(a=>cards.filter(b=>b.offsetTop===a.offsetTop).every(b=>Math.abs(a.getBoundingClientRect().height-b.getBoundingClientRect().height)<1 && Math.abs(a.querySelector('.catalog-room-metadata').getBoundingClientRect().top-b.querySelector('.catalog-room-metadata').getBoundingClientRect().top)<1 && Math.abs(a.querySelector('.room-card-actions').getBoundingClientRect().top-b.querySelector('.room-card-actions').getBoundingClientRect().top)<1))})()"), (page, width, 'alignment')
                assert self.js("[...document.querySelectorAll('.catalog-room-briefing')].every(el=>el.nextElementSibling.getBoundingClientRect().top-el.getBoundingClientRect().bottom<=11)"), 'No artificial gap before topics'
                if page == 'rooms':
                    before = self.js("document.querySelector('.discovery-filters').getBoundingClientRect().height")
                    self.click('.category-filter-trigger'); self.wait("!!document.querySelector('.category-search input')")
                    assert self.js("(()=>{const r=document.querySelector('.category-filter .anchored-panel').getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight})()"), (theme, width, 'popover viewport')
                    assert self.js("document.querySelector('.category-options').scrollHeight > document.querySelector('.category-options').clientHeight")
                    self.choose('Linux'); self.choose('Web Security')
                    self.wait("document.querySelector('.category-filter-trigger')?.textContent.includes('(2)')")
                    assert self.js("document.querySelector('.discovery-filters').getBoundingClientRect().height") == before
                    self.screenshot(f'{page}-{theme}-{width}-popover.png')
                    self.button('Clear selection'); self.button('Done'); self.wait("document.querySelectorAll('.catalog-room-card').length===4")
                    controls = self.js("[...document.querySelector('.discovery-filters').children].map(el=>el.getBoundingClientRect().height)")
                    assert max(controls) - min(controls) < 1, controls
                    if width == 1440: assert self.js("new Set([...document.querySelector('.discovery-filters').children].map(el=>el.getBoundingClientRect().top)).size===1")
                self.screenshot(f'{page}-{theme}-{width}.png')


def main():
    log_path = m.OUTPUT / 'vite.log'
    with log_path.open('wb') as log:
        process = subprocess.Popen([str(ROOT/'frontend/node_modules/.bin/vite'), '--host', '127.0.0.1', '--port', '3192', '--strictPort'], cwd=ROOT/'frontend', env={**os.environ, 'VITE_API_BASE_URL': ''}, stdout=log, stderr=log)
    try:
        for _ in range(100):
            try: urllib.request.urlopen(m.BASE, timeout=1); break
            except Exception: time.sleep(.1)
        with Browser() as b:
            b.go('/rooms', "document.querySelectorAll('.catalog-room-card').length===4")
            assert b.js("!!document.querySelector('.catalog-room-card a[href^=\"/login?returnTo=\"]')"), 'Guest Save retains login destination'
            b.js('localStorage.setItem("access_token","synthetic-only");localStorage.setItem("user",' + json.dumps(json.dumps(USER)) + ')')
            b.send('Page.reload'); b.wait("!!document.querySelector('select[aria-label=\"Learning progress\"]') && document.querySelectorAll('.catalog-room-card').length===4")
            b.js("document.querySelector('.category-filter-trigger').focus()")
            b.key('Enter', 'Enter', 13); b.wait("document.activeElement===document.querySelector('.category-search input')")
            b.fill('.category-search input', 'linux'); b.wait("document.querySelectorAll('.category-options label').length===1")
            b.key('ArrowDown', 'ArrowDown', 40); b.wait("document.activeElement?.type==='checkbox'")
            b.key(' ', 'Space', 32); b.wait("location.search.includes('category=linux')")
            b.fill('.category-search input', 'web'); b.choose('Web Security'); b.wait("document.querySelector('.category-filter-trigger').textContent.includes('(2)')")
            b.key('Escape', 'Escape', 27); b.wait("!document.querySelector('.anchored-panel') && document.activeElement===document.querySelector('.category-filter-trigger')")
            b.js('history.back()'); b.wait("new URLSearchParams(location.search).get('category')==='linux'")
            b.js('history.forward()'); b.wait("new URLSearchParams(location.search).get('category')==='linux,web-security'")
            b.click('.category-filter-trigger'); b.wait("document.querySelectorAll('.category-options input:checked').length===2")
            b.choose('Linux'); b.wait("new URLSearchParams(location.search).get('category')==='web-security'")
            b.button('Clear selection'); b.wait("!new URLSearchParams(location.search).has('category')"); b.button('Done')
            b.click('.category-filter-trigger'); b.wait("!!document.querySelector('.category-options')")
            for c in TAXONOMY[:8]: b.choose(c['label'])
            b.wait("document.querySelectorAll('.category-options input:disabled').length===15")
            b.choose(TAXONOMY[0]['label']); b.wait("document.querySelectorAll('.category-options input:disabled').length===0")
            b.fill('.category-search input', 'no-such-category'); b.wait("document.querySelector('.category-options [role=status]')?.textContent==='No matching categories'")
            b.button('Clear selection'); b.button('Done')
            b.fill('.discovery-toolbar > .search-field input', 'Linux'); b.wait("new URLSearchParams(location.search).get('q')==='Linux'")
            b.click('.category-filter-trigger'); b.wait("!!document.querySelector('.category-options')"); b.choose('Linux'); b.button('Done')
            for label, value in [('Filter by difficulty', 'easy'), ('Room type', 'PRACTICAL'), ('Learning progress', 'in-progress'), ('Sort results', 'alphabetical')]: b.select(label, value)
            b.wait("document.querySelectorAll('.catalog-room-card').length===1 && document.querySelector('.room-card-title')?.textContent==='Linux First Steps'")
            params = b.catalog_requests[-1][1]
            assert all(params.get(k) == [v] for k, v in dict(q='Linux', category='linux', difficulty='easy', room_type='PRACTICAL', progress='in-progress', sort='alphabetical').items()), params
            b.send('Page.reload'); b.wait("document.querySelectorAll('.catalog-room-card').length===1 && document.querySelector('.discovery-toolbar input').value==='Linux'")
            b.button('Clear filters'); b.wait("!location.search && document.querySelectorAll('.catalog-room-card').length===4")
            b.click('.more-topics'); b.wait("!!document.querySelector('.room-topics-list')")
            assert b.js("[...document.querySelectorAll('.room-topics-list li')].map(el=>el.textContent).includes('Privilege Escalation')")
            b.key('Escape', 'Escape', 27); b.wait("!document.querySelector('.room-topics-list')")
            b.click('.category-filter-trigger'); b.wait("!!document.querySelector('.category-search input')")
            b.js("document.querySelector('.category-popover-footer button:last-child').focus()")
            b.key('Tab', 'Tab', 9); b.wait("!document.querySelector('.category-filter .anchored-panel')")
            b.click('.category-filter-trigger'); b.wait("!!document.querySelector('.category-search input')")
            b.send('Input.dispatchMouseEvent', dict(type='mousePressed', x=10, y=300, button='left', clickCount=1)); b.send('Input.dispatchMouseEvent', dict(type='mouseReleased', x=10, y=300, button='left', clickCount=1))
            b.wait("!document.querySelector('.category-filter .anchored-panel')")
            b.widths('rooms')
            b.send('Emulation.setDeviceMetricsOverride', dict(width=390, height=600, deviceScaleFactor=1, mobile=False))
            b.click('.category-filter-trigger'); b.wait("!!document.querySelector('.category-search input')")
            assert b.js("(()=>{const r=document.querySelector('.category-filter .anchored-panel').getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight})()"), 'Short mobile viewport'
            b.button('Done')
            b.send('Emulation.setDeviceMetricsOverride', dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
            b.go('/saved', "document.querySelectorAll('.catalog-room-card').length===2")
            b.widths('saved')
            b.click('button[aria-label^="Remove Linux First Steps"]'); b.wait("document.querySelectorAll('.catalog-room-card').length===1")
            b.click('button[aria-label^="Remove A long theory title"]'); b.wait("document.querySelector('.empty-state h2')?.textContent==='No saved rooms yet'")
            b.click('.empty-state a'); b.wait("location.pathname==='/rooms' && document.querySelectorAll('.catalog-room-card').length===4")
            b.click('button[aria-label="Save Web to Root"]'); b.wait("document.querySelector('button[aria-label=" + json.dumps('Remove Web to Root from saved content') + "]')?.getAttribute('aria-pressed')==='true'")
            b.go('/saved', "document.querySelectorAll('.catalog-room-card').length===1")
            assert b.js("document.querySelector('.card-open').getAttribute('href')==='/rooms/42'")
            b.click('.card-open'); b.wait("location.pathname==='/rooms/42'")
            assert b.js("history.state.usr.discovery==='/saved'")
            b.js('history.back()'); b.wait("location.pathname==='/saved' && !!document.querySelector('.catalog-room-card')")
            b.click('button[aria-label="Remove Web to Root from saved content"]'); b.wait("!!document.querySelector('.empty-state')")
            b.click('a[href="/saved?tab=paths"]'); b.wait("!!document.querySelector('.path-card')")
            assert b.js("!document.querySelector('.catalog-room-card') && document.querySelector('.path-card').textContent.includes('1/3 rooms completed')")
            b.fail_catalog = True; b.go('/rooms', "!!document.querySelector('[role=alert]')")
            b.fail_catalog = False; b.button('Try again'); b.wait("document.querySelectorAll('.catalog-room-card').length===4")
            b.fill('.discovery-toolbar > .search-field input', 'no-such-room'); b.wait("document.querySelector('.empty-state h2')?.textContent==='No matching content'")
            assert not b.errors, b.errors
            result = dict(status='PASS', viewports=[1440,1024,768,390,320], themes=['light','dark'], pages=['rooms','saved'], keyboard=True, url_history=True, combined_filters=True, saved_removal_and_navigation=True, errors=b.errors, screenshots=str(m.OUTPUT))
            (m.OUTPUT / 'result.json').write_text(json.dumps(result, indent=2)); print(json.dumps(result))
    finally:
        process.terminate(); process.wait(timeout=10)


if __name__ == '__main__': main()
