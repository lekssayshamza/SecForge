const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
function load(relative, dependencies = {}, globals = {}) {
  const filename = path.join(__dirname, '..', relative)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, { ...globals, module, exports: module.exports, require(name) { assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`); return dependencies[name] } }, { filename })
  return module.exports
}
const { actionError, filterRooms, isImageReady } = load('src/lib/presentation.ts')
test('room deletion explains protected learner progress using a fixed safe message', () => {
  const message = actionError({ response: { status: 409, data: { detail: 'Rooms with learner progress cannot be deleted.' } } }, 'delete the room')
  assert.match(message, /contains learner progress/)
  assert.match(message, /Unpublish/)
  assert.ok(!message.includes('another environment'))
})
const rooms = [
  { id: 1, name: 'Hidden service', description: 'Explore HTTP', category: 'Web', difficulty: 'easy', status: 'published' },
  { id: 2, name: 'Packet trail', description: null, category: 'Network', difficulty: 'hard', status: 'draft' },
]
const ids = rows => Array.from(rows, row => row.id)
test('search matches names, descriptions and topics without mutating rooms', () => {
  assert.deepEqual(ids(filterRooms(rooms, ' HTTP ', '', '', '')), [1])
  assert.deepEqual(ids(filterRooms(rooms, 'network', '', '', '')), [2])
  assert.deepEqual(ids(filterRooms(rooms, 'hidden', '', '', '')), [1])
  assert.equal(rooms.length, 2)
})
test('difficulty, publication and category filters compose', () => {
  assert.deepEqual(ids(filterRooms(rooms, '', 'hard', 'draft', 'Network')), [2])
  assert.deepEqual(ids(filterRooms(rooms, '', 'hard', 'published', '')), [])
  assert.deepEqual(ids(filterRooms(rooms, '', '', '', '')), [1,2])
})
for (const status of [401,403,404,409,422,502,503,500]) {
  test(`HTTP ${status} produces actionable text without raw diagnostics`, () => {
    const text = actionError({ response: { status, data: { detail: 'PRIVATE_DIAGNOSTIC' } } }, 'start the lab')
    assert.equal(typeof text, 'string')
    assert.ok(text.length > 30)
    assert.ok(!text.includes('PRIVATE_DIAGNOSTIC'))
  })
}
test('transport failures and malformed errors remain safe', () => {
  assert.match(actionError(undefined, 'download a profile'), /connection/)
  assert.match(actionError({ response: { data: { detail: [{ anything: 'PRIVATE' }] } } }, 'save'), /Unable to save/)
})
test('ready image requires available status and a nonempty image identifier', () => {
  for (const status of ['pending','creating','unknown','failed','unavailable']) assert.equal(isImageReady([{ status, ami_id: 'synthetic-image' }]), false)
  assert.equal(isImageReady([{ status: 'available', ami_id: null }]), false)
  assert.equal(isImageReady([{ status: 'available', ami_id: ' ' }]), false)
  assert.equal(isImageReady([]), false)
  assert.equal(isImageReady([{ status: 'available', ami_id: 'synthetic-image' }]), true)
})
test('creator launch passes selected version identity unchanged', async () => {
  const calls = []
  const api = load('src/api/labs.ts', { './axios': { default: { post: async (...args) => { calls.push(args); return { data: { status: 'pending' } } } } }, '../lib/labClock': { stampLabTime: lab => lab } })
  await api.startTestLab(42, 71, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['/rooms/42/templates/71/lab', { ttl_hours: 1 }]])
})
test('exact-version publication is distinct from ordinary publication', async () => {
  const calls = []
  const api = load('src/api/templates.ts', { './axios': { default: { post: async (...args) => { calls.push(args); return { data: {} } } } } })
  await api.publishRoomWithTemplate(42,71)
  assert.equal(calls[0][0], '/rooms/42/templates/71/publish')
})
test('capture keeps the caller idempotency key', async () => {
  const calls = []
  const api = load('src/api/templates.ts', { './axios': { default: { post: async (...args) => { calls.push(args); return { data: {} } } } } })
  await api.saveRoomTemplate(42, 'synthetic-request-key')
  assert.equal(calls[0][0], '/rooms/42/templates')
  assert.equal(calls[0][2].headers['Idempotency-Key'], 'synthetic-request-key')
})
test('VPN download keeps the binary response and filename header', async () => {
  const calls = [], blob = { synthetic: true }
  const api = load('src/api/vpn.ts', {
    './axios': { default: { get: async (...args) => {
      calls.push(args)
      return { data: blob, headers: { 'content-disposition': 'attachment; filename="secforge.ovpn"' } }
    } } },
  })
  const result = await api.downloadVpnProfile()
  assert.equal(result.blob, blob)
  assert.equal(result.contentDisposition, 'attachment; filename="secforge.ovpn"')
  assert.equal(calls[0][1].responseType, 'blob')
})
test('lab lifecycle polls transitional states and writes returned Stop state', async () => {
  const queries = [], mutations = [], writes = []
  const hook = load('src/hooks/useLab.ts', {
    '../api/labs': { getLab: async () => null, getTestLab: async () => null, startLab: async () => null, startTestLab: async () => null, stopLab: async () => ({ status: 'stopping' }), stopTestLab: async () => ({ status: 'stopping' }) },
    '@tanstack/react-query': {
      useQueryClient: () => ({ setQueryData: (...args) => writes.push(args), invalidateQueries: () => Promise.resolve() }),
      useQuery: options => { queries.push(options); return {} },
      useMutation: options => { mutations.push(options); return { mutateAsync: options.mutationFn, isPending: false } },
    },
  })
  const state = hook.useLab(42, true)
  for (const status of ['pending','provisioning','stopping']) assert.equal(queries[0].refetchInterval({ state: { data: { status } } }),5000)
  assert.equal(queries[0].refetchInterval({ state: { data: { status: 'running' } } }),15000)
  assert.equal(queries[0].refetchIntervalInBackground,false)
  for (const status of ['stopped','failed']) assert.equal(queries[0].refetchInterval({ state: { data: { status } } }),false)
  await mutations[1].onSuccess(await state.stopLabAsync())
  assert.equal(writes[0][1].status, 'stopping')
  hook.useLab(42, false)
  assert.equal(queries[1].enabled,false)
  hook.useCreatorTestLab(42,8)
  assert.deepEqual(Array.from(queries[2].queryKey), ['creator-test-lab',42,8])
  assert.equal(queries[2].refetchInterval({ state: { data: { status: 'running' } } }),false)
})
const { formatDate, formatDateTime } = load('src/lib/format.ts')
test('naive backend UTC timestamps match explicit UTC and offset timestamps', () => {
  assert.equal(formatDateTime('2026-10-11T09:00:00'), formatDateTime('2026-10-11T09:00:00Z'))
  assert.equal(formatDateTime('2026-10-11T09:00:00Z'), formatDateTime('2026-10-11T11:00:00+02:00'))
  assert.equal(formatDate('2026-10-11T09:00:00'), formatDate('2026-10-11T09:00:00Z'))
})
test('malformed timestamps do not become fabricated dates', () => {
  assert.equal(formatDateTime('invalid'), 'Not available')
  assert.equal(formatDate('invalid'), 'Not available')
})
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup } = require('react-dom/server')
const Icon = load('src/components/Icon.tsx', { 'react/jsx-runtime': jsx })
const Status = load('src/components/EnvironmentStatus.tsx', { 'react/jsx-runtime': jsx, './Icon': Icon }).default
for (const status of ['pending','provisioning','stopping','failed']) {
  test(`${status} status never claims a ready or cleaned environment`, () => {
    const html = renderToStaticMarkup(jsx.jsx(Status, { status }))
    assert.match(html, /role="status"/)
    assert.ok(!html.includes('Ready to connect'))
    assert.ok(!html.includes('Cleanup is complete'))
    assert.ok(!html.includes('%'))
  })
}
test('unknown lifecycle state never invents progress', () => {
  assert.equal(renderToStaticMarkup(jsx.jsx(Status, { status: 'unknown' })), '')
})

const { resolveTheme } = load('src/lib/theme.ts')
test('new visitors and invalid preferences default to light; only an explicit dark choice selects dark', () => {
  for (const preference of [null, '', 'system', 'Dark', 'light', 'unexpected']) assert.equal(resolveTheme(preference), 'light')
  assert.equal(resolveTheme('dark'), 'dark')
})
test('theme remains usable when browser storage is denied', () => {
  const changes = []
  const document = { documentElement: { dataset: {}, style: {} }, querySelector: selector => ({ setAttribute: (...args) => changes.push([selector, ...args]) }) }
  const theme = load('src/lib/theme.ts', {}, { document, localStorage: { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } } })
  assert.equal(theme.readTheme(), 'light')
  theme.applyTheme('dark')
  assert.equal(document.documentElement.dataset.theme, 'dark')
  assert.equal(document.documentElement.style.colorScheme, 'dark')
  assert.ok(changes.some(change => change[2] === '/favicon-dark.svg'))
})
test('theme choice persists and updates the browser surface and matching favicon', () => {
  const values = new Map(), changes = []
  const document = { documentElement: { dataset: {}, style: {} }, querySelector: selector => ({ setAttribute: (...args) => changes.push([selector, ...args]) }) }
  const theme = load('src/lib/theme.ts', {}, { document, localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } })
  theme.applyTheme('dark'); assert.equal(theme.readTheme(), 'dark')
  theme.applyTheme('light'); assert.equal(theme.readTheme(), 'light')
  assert.ok(changes.some(change => change[2] === '#F7F8FA'))
  assert.ok(changes.some(change => change[2] === '/favicon-light.svg'))
})
test('initial HTML applies the saved theme before React, including blocked storage', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8')
  const bootstrap = fs.readFileSync(path.join(__dirname, '..', 'public/theme-init.js'), 'utf8')
  assert.ok(html.indexOf('src="/theme-init.js"') < html.indexOf('type="module"'))
  for (const preference of [null, 'dark', 'light', 'unknown', 'blocked']) {
    const nodes = {}, document = { documentElement: { dataset: {}, style: {} }, querySelector: selector => nodes[selector] ??= {}, addEventListener() {} }
    vm.runInNewContext(bootstrap, { document, localStorage: { getItem() { if (preference === 'blocked') throw new Error('blocked'); return preference } } })
    const expected = preference === 'dark' ? 'dark' : 'light'
    assert.equal(document.documentElement.dataset.theme, expected)
    assert.equal(nodes['link[rel="icon"]'].href, `/favicon-${expected}.svg`)
  }
})
const { topicIcon } = load('src/lib/topics.ts')
test('room topic treatments use real categories, with a neutral fallback', () => {
  for (const [topic, icon] of [['Web security','web'],['Network analysis','network'],['Reverse engineering','code'],['Cryptography','key'],['Linux basics','terminal'],['Unclassified','terminal']]) assert.equal(topicIcon(topic), icon)
})
test('SF logo is an original flat two-path monogram at small and large sizes', () => {
  const Logo = load('src/components/Logo.tsx', { 'react/jsx-runtime': jsx }).default
  for (const size of [16,24,32]) {
    const html = renderToStaticMarkup(jsx.jsx(Logo, { size }))
    assert.match(html, new RegExp(`width="${size}"`))
    assert.match(html, /class="brand-s"/); assert.match(html, /class="brand-f"/)
    assert.ok(!/gradient|filter|stroke/.test(html))
  }
})
test('small theme text and primary buttons meet WCAG AA contrast', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'src/App.css'), 'utf8')
  const luminance = hex => {
    const rgb = [1,3,5].map(i => parseInt(hex.slice(i,i+2),16)/255).map(v => v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4)
    return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722
  }
  const pairs = [['color-text','color-bg'],['color-text-muted','color-bg-panel'],['color-text-faint','color-bg'],['color-text-faint','color-bg-raised'],['color-button-text','color-button'],['color-success','color-success-bg'],['color-danger','color-danger-bg'],['color-warning','color-warning-bg'],['color-info','color-info-bg'],['color-accent-text','color-accent-muted']]
  for (const theme of [css.split(":root[data-theme")[0],css.split(":root[data-theme")[1].split('}')[0]]) {
    const values = Object.fromEntries([...theme.matchAll(/--([\w-]+): (#[0-9a-fA-F]{6})/g)].map(m => [m[1],m[2]]))
    for (const [a,b] of pairs) { const levels = [luminance(values[a]),luminance(values[b])].sort((a,b)=>a-b); assert.ok((levels[1]+.05)/(levels[0]+.05)>=4.5, `${a} on ${b}`) }
    for (const surface of ['color-bg','color-bg-panel','color-bg-input']) { const levels=[luminance(values['color-border-strong']),luminance(values[surface])].sort((a,b)=>a-b); assert.ok((levels[1]+.05)/(levels[0]+.05)>=3, `Control boundary on ${surface}`) }
  }
})
