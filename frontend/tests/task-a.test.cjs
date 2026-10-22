const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const React = require('react')
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup } = require('react-dom/server')
const router = require('react-router-dom')
function load(file, dependencies, transform = s => s) {
  const filename = path.join(__dirname, '..', file)
  const code = ts.transpileModule(transform(fs.readFileSync(filename, 'utf8')), { fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, { module, exports: module.exports, require(name) {if(name.endsWith('/CreatorSshSettings')) return {default:()=>null,BuilderCreatorKey:()=>null,AdminCreatorKey:()=>null}; if(name.endsWith('/WorkspaceShell')) return require('./workspace-shell-helper.cjs'); if(name.endsWith('/CategoryMultiSelect')) return require('./discovery-helpers.cjs').CategoryMultiSelect; if (name.endsWith('/ContextHelp')) return require('./context-help-helper.cjs'); if (name.endsWith('/SocialConnections')) return {default:()=>null};  if (name.endsWith('/lib/permissions')) return require('./permissions-helper.cjs'); 
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
    return dependencies[name]
  } }, { filename })
  return module.exports
}
const base = { react: React, 'react/jsx-runtime': jsx }
const nullComponent = { default: () => null }
const CyberHero = load('src/components/CyberHero.tsx', base).default
const Landing = load('src/pages/Landing.tsx', { ...base, 'react-router-dom': router,
  '../components/Logo': nullComponent, '../components/Icon': nullComponent,
  '../components/ThemeToggle': { default: () => jsx.jsx('button', { children: 'Toggle theme' }) },
  '../components/CyberHero': { default: CyberHero }, './Landing.css': {},
}).default
function route(pathname, user = null, loading = false) {
  const App = load('src/App.tsx', { ...base,
    'react-router-dom': { ...router, Navigate: ({ to, state }) => jsx.jsx('span', { 'data-redirect': to, 'data-from': state?.from?.pathname }) },
    './context/AuthContext': { useAuth: () => ({ user, isLoading: loading }) },
    './pages/CreatorAnalytics': nullComponent, './pages/Administration': nullComponent,
    './pages/Saved': { default: () => jsx.jsx('h1', { children: 'Private saved content' }) }, './pages/PasswordChange': nullComponent, './pages/GoogleComplete': nullComponent, './pages/CanonicalProfile': { default: () => jsx.jsx('h1', { children: 'Public learner' }), ProfileAlias: () => jsx.jsx('h1', { children: 'Private profile' }) }, './pages/Landing': { default: Landing }, './pages/Login': { default: () => jsx.jsx('h1', { children: 'Login form' }) },
    './pages/Register': { default: () => jsx.jsx('h1', { children: 'Signup form' }) },
    './pages/Leaderboard': { default: () => jsx.jsx('h1', {children:'Public rankings'}), PublicLearnerProfile: () => jsx.jsx('h1', {children:'Public learner'}) }, './pages/LearnerDashboard': { default: () => jsx.jsx('div', { children: 'Authenticated overview' }) }, './pages/Dashboard': { default: () => jsx.jsx('h1', { children: 'Authenticated overview' }) },
    './pages/PublicRooms': {PublicCatalog:()=>jsx.jsx('h1',{children:'Public catalog'}),PublicRoomDetail:()=>jsx.jsx('h1',{children:'Public briefing'})}, './pages/LearningPaths': { default: () => jsx.jsx('div', { children: 'Learning paths' }), LearningPathDetail: () => jsx.jsx('div', { children: 'Path detail' }) },
    './pages/RoomDetail': { default: () => jsx.jsx('h1', { children: 'Private room' }) },
    './components/SiteHeader': nullComponent, './pages/AccountSettings': nullComponent, './components/AppLayout': { default: () => jsx.jsx(router.Outlet, {}) }, './App.css': {},
  }).default
  return renderToStaticMarkup(jsx.jsx(router.MemoryRouter, { initialEntries: [pathname], children: jsx.jsx(App, {}) }))
}
test('unauthenticated root renders a public landing with real login and signup destinations', () => {
  const html = route('/')
  assert.match(html, /Learn cybersecurity/)
  assert.match(html, /href="\/login"/); assert.match(html, /href="\/register"/)
  for (const step of ['Discover a room', 'Launch your machine', 'Connect through VPN', 'Find the answers', 'Earn your points']) assert.ok(html.includes(step))
  assert.ok(!html.includes('data-redirect') && !html.includes('Authenticated overview'))
})
test('authenticated root remains the overview for creators and learners', () => {
  for (const role of ['creator', 'learner']) {
    const html = route('/', { role })
    assert.match(html, /Authenticated overview/); assert.ok(!html.includes('Learn cybersecurity'))
  }
})
test('guest room routes are public while private profile still requires login', () => {
  const html = route('/rooms/42')
  assert.match(html, /Public briefing/); assert.doesNotMatch(html, /data-redirect|Private room/)
  assert.match(route('/rooms'), /Public catalog/)
  assert.match(route('/profile'), /data-redirect="\/login"/)
  assert.match(route('/rooms/42', { role: 'learner' }), /Private room/)
})
test('login and signup remain public-only, and loading never flashes public content', () => {
  assert.match(route('/login'), /Login form/); assert.match(route('/register'), /Signup form/)
  assert.match(route('/login', { role: 'creator' }), /data-redirect="\/"/)
  assert.match(route('/register', { role: 'learner' }), /data-redirect="\/"/)
  const html = route('/', null, true)
  assert.match(html, /Opening SecForge/); assert.ok(!html.includes('Learn cybersecurity'))
})
function editor(question, locked) {
  // Expose the internal editor in the test module only, without changing its
  // production exports or mounting the full query-based workspace.
  const component = load('src/components/ChallengeQuestions.tsx', { ...base,
    '@tanstack/react-query': {}, '../context/AuthContext': {}, '../api/challenges': {},
    '../lib/presentation': {}, '../lib/format': {}, './Icon': nullComponent, './ConfirmProvider': {},
    './TaskEditor': { default: () => null }, './SafeMarkdown': { default: ({text}) => jsx.jsx('div',{children:text}) }, '../hooks/useUnsavedChanges': { useUnsavedChanges() {}, confirmDiscard: () => true }, './Dialog': { default: ({ children }) => jsx.jsx('div', { children }) },
  }, s => s.replace('function QuestionEditor(', 'export function QuestionEditor(')).QuestionEditor
  return renderToStaticMarkup(jsx.jsx(component, { roomId: 42, question, locked, onClose() {}, async onSaved() {} }))
}
test('creator answer entry is visible text for Add and Edit, without returning stored flags', () => {
  for (const question of [null, { id: 8, question_text: 'Find a file', points: 25 }]) {
    const html = editor(question, false)
    if (!question) assert.match(html, /id="question-answer"[^>]*type="text"/)
    else assert.match(html, /Opening saved answer/)
    if (!question) assert.match(html, /autoComplete="off"/)
    assert.ok(!html.includes('type="password"'))
    if (question) assert.doesNotMatch(html, /Keep saved answer|Replace answer/)
    else assert.match(html, /required=""/)
  }
})
test('visible answer entry remains disabled when earned learner credit locks it', () => {
  assert.match(editor({ id: 8, question_text: 'Find a file', points: 25 }, true), /id="question-answer"[^>]*disabled=""/)
})
function roomView(role, lab, roomType = 'PRACTICAL', ownerId = 8) {
  const Component = load('src/pages/RoomDetail.tsx', { ...base,
    'react-router-dom': { ...router, useParams: () => ({ roomId: '42' }), useNavigate: () => () => {} },
    '../context/AuthContext': { useAuth: () => ({ user: { id: 7, role } }) },
    '@tanstack/react-query': { useQueryClient: () => ({}), useIsFetching: () => 0, useIsMutating: () => 0,
      useMutation: () => ({}), useQuery: options => ({ data: options.queryKey[0] === 'room-workspace' ? { revision: 0, dirty: false, tasks: [], questions: [], image_status: null, room: { id:42, creator_id:ownerId, room_type:roomType, status:'published', name:'Synthetic room', difficulty:'easy', category:'linux' } } : { id: 42, room_type: roomType, creator_id: ownerId, name: 'Synthetic room', status: 'published', difficulty: 'easy', category: 'linux', description: 'Briefing content' } }) },
    '../components/CategorySelect': require('./discovery-helpers.cjs').CategorySelect, '../components/BookmarkButton': nullComponent, '../lib/categories': require('./discovery-helpers.cjs').categories, '../hooks/useDiscoveryState': require('./discovery-helpers.cjs').state,
    '../api/workspace': {}, '../components/WorkspaceNav': nullComponent, '../components/BuilderSshAuth': nullComponent, '../components/NetworkPorts': nullComponent, '../api/rooms': {}, '../api/templates': {}, '../api/vpn': {}, '../lib/format': { formatDateTime: () => 'Today' },
    '../lib/vpnFilename': { vpnProfileFilename: () => 'secforge-synthetic.ovpn' }, '../lib/idempotency': {}, '../lib/presentation': {}, '../hooks/useUnsavedChanges': { useUnsavedChanges() {}, confirmDiscard: () => true }, '../hooks/useBuilder': { useBuilder: () => ({}) },
    '../hooks/useLab': { useLab: () => ({ lab }), useCreatorTestLab: () => ({}) },
    '../components/Icon': nullComponent, '../components/Dialog': nullComponent,
    '../components/ConnectionGuide': { default: () => jsx.jsx('p', { children: 'VPN and SSH connection' }) },
    '../components/EnvironmentStatus': nullComponent,
    '../components/LabTimer': { default: nullComponent.default, useLabClock: () => null },
    '../components/RoomLearningProgress': {default:()=>jsx.jsx('section',{children:'Learner progress'})},
    '../components/ChallengeQuestions': { default: () => jsx.jsx('section', { id: 'challenge-questions', children: 'Questions' }) },
    '../components/ConfirmProvider': { useConfirm: () => async () => true },
  }).default
  return renderToStaticMarkup(jsx.jsx(router.MemoryRouter, { children: jsx.jsx(Component, {}) }))
}
test('learner lab controls appear below progress and before the briefing and questions, with or without a running machine', () => {
  for (const lab of [null, { status: 'running', private_ip: '10.0.1.42' }]) {
    const html = roomView('learner', lab)
    assert.ok(html.indexOf('Learner progress') < html.indexOf('id="learner-workspace"'))
    assert.ok(html.indexOf('id="learner-workspace"') < html.indexOf('Briefing content'))
    assert.ok(html.indexOf('id="learner-workspace"') < html.indexOf('id="challenge-questions"'))
    assert.ok(!html.includes('Start builder') && !html.includes('Publish Room'))
    assert.match(html, lab ? /Stop Machine/ : /Start Machine/)
  }
})
test('another creator receives their own learner lifecycle controls', () => {
  const html = roomView('creator', null)
  assert.ok(html.includes('id="learner-workspace"') && html.includes('Start Machine'))
})

test('an expired remembered session keeps public home accessible while private requests still redirect', async () => {
  for (const [pathname, url, redirects] of [['/', '/auth/me', false], ['/rooms', '/auth/me', false], ['/profile', '/auth/me', true], ['/', '/rooms', true], ['/login', '/auth/login', false]]) {
    let reject
    const location = { pathname, href: 'unchanged' }
    const cleared = []
    const transport = { interceptors: { request: { use() {} }, response: { use(ok, error) { reject = error } } } }
    const filename = path.join(__dirname, '..', 'src/api/axios.ts')
    const code = ts.transpileModule(fs.readFileSync(filename, 'utf8').replace('import.meta.env.VITE_API_BASE_URL', 'undefined'), { fileName: filename, compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
    const module = { exports: {} }
    vm.runInNewContext(code, { module, exports: module.exports, require: () => ({ default: { create: () => transport } }), window: { location }, localStorage: { removeItem: key => cleared.push(key) } })
    const error = { config: { url }, response: { status: 401 } }
    await assert.rejects(reject(error), e => e === error)
    assert.equal(location.href, redirects ? '/login' : 'unchanged')
    assert.deepEqual(cleared, url === '/auth/login' ? [] : ['access_token', 'user'])
  }
})

test('theory learner and creator pages retain lessons without machine/VPN/timer controls', () => {
  for(const role of ['learner','creator']) {
    const html=roomView(role,null,'THEORY',7)
    assert.match(html,/Theory Room/);assert.match(html,/id="challenge-questions"/)
    assert.doesNotMatch(html,/Start Machine|Stop Machine|Extend \+1 Hour|Start builder|saved-versions|Download VPN|SSH connection|learner-workspace/)
    if(role==='creator')assert.match(html,/Unpublish room/)
  }
})

 test('creator room management has three accessible sections without redundant panels',()=>{
  const html=roomView('creator',null,'THEORY',7)
  assert.match(html,/role="tablist" aria-label="Room management"/)
  for(const name of ['content','lab','publish'])assert.ok(html.includes(`id="room-tab-${name}"`))
  assert.match(html,/Edit room/);assert.match(html,/id="room-panel-lab"[^>]*hidden=""/)
  assert.doesNotMatch(html,/Room details|Edit briefing|Publication options|workflow-map|draft-status|Learners see the current published room/)
 })
 test('learner room has one content column without creator tabs or details sidebar',()=>{
  const html=roomView('learner',null)
  assert.doesNotMatch(html,/room-manager-tabs|Edit room|room-panel-publish/)
  assert.doesNotMatch(html,/Room details|learner-machine-aside|learner-room-grid/);assert.match(html,/Start Machine/)
 })
