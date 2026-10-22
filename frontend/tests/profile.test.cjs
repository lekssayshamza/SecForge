const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const React = require('react')
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup } = require('react-dom/server')
function load(file, dependencies, globals = {}) {
  const filename = path.join(__dirname, '..', file)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, { module, exports: module.exports, ...globals, require(name) { if (name.endsWith('/ContextHelp')) return require('./context-help-helper.cjs'); if (name.endsWith('/SocialConnections')) return {default:()=>null}; 
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
    return dependencies[name]
  } }, { filename })
  return module.exports
}
const base = { react: React, 'react/jsx-runtime': jsx, 'react-router-dom': require('react-router-dom') }
const name = load('src/lib/vpnFilename.ts', {})
const USER = { id: 7, username: 'testlearner2004', display_name: 'Test learner', email: 'learner@example.com', role: 'learner', avatar_style: 'initials', avatar_color: 'red' }
const Avatar = load('src/components/Avatar.tsx', { ...base, './AvatarImageContext': { AvatarImageContext: React.createContext(null) }, './Icon': { default: ({ name }) => jsx.jsx('svg', { 'data-icon': name }) } })

test('profile updates target only the authenticated me endpoint and errors propagate', async () => {
  const calls = []
  const api = load('src/api/profile.ts', { './axios': { default: { patch: async (...args) => { calls.push(args); return { data: USER } } } } })
  assert.equal(await api.updateProfile({ display_name: 'Test learner' }), USER)
  assert.equal(calls[0][0], '/users/me')
  const error = { response: { status: 409 } }
  const failing = load('src/api/profile.ts', { './axios': { default: { patch: async () => { throw error } } } })
  await assert.rejects(failing.updateProfile({ username: 'taken' }), e => e === error)
})

test('generated avatars render selected options, escape names and refuse arbitrary image URLs', () => {
  const render = user => renderToStaticMarkup(jsx.jsx(Avatar.default, { user, large: true }))
  assert.match(render(USER), /TL/)
  assert.match(render({ ...USER, avatar_style: 'terminal', avatar_color: 'blue' }), /data-icon="terminal"/)
  const html = render({ ...USER, display_name: '<script>bad</script>', avatar_style: 'https://evil.invalid/avatar', avatar_color: 'url(evil)' })
  assert.match(html, /avatar-red/); assert.match(html, /&lt;script&gt;/)
  assert.ok(!html.includes('src=') && !html.includes('evil') && !html.includes('<script>'))
})

test('profile form provides a preview, editable names and opt-in privacy controls without privilege fields', () => {
  const Component = load('src/pages/Profile.tsx', { ...base,
    '../context/AuthContext': { useAuth: () => ({ user: USER, updateProfile() {} }) },
    '@tanstack/react-query': {}, '../hooks/useLearning': {}, '../api/profile': {}, '../components/LearningPanels': {}, '../components/ProfileIdentity': require('./batch-a-helpers.cjs').identity, '../components/Dialog': { default: ({children}) => children }, '../components/SignInMethods': { default: () => null }, '../components/Avatar': Avatar, '../lib/presentation': { actionError: () => 'Unable to save' },
  }).ProfileEditor
  const html = renderToStaticMarkup(jsx.jsx(Component, { onClose() {} }))
  assert.match(html, /aria-label="Profile picture"/)
  assert.match(html, /id="display-name"/); assert.match(html, /id="profile-username"/)
  assert.doesNotMatch(html, /Generated avatar fallback|Avatar color|Use terminal avatar/)
  assert.match(html, /Show my uploaded picture publicly/); assert.match(html, /Show my bio publicly/)
  assert.match(html, /Save profile/)
  assert.ok(html.includes('type="file"') && !html.includes('name="role"') && !html.includes('password'))
})

test('signup explains and validates the same username alphabet as profile editing', () => {
  const Component = load('src/pages/Register.tsx', { ...base,
    'react-router-dom': { useLocation: () => ({search: ''}), Link: ({ children }) => children, useNavigate: () => () => {} },
    '../context/AuthContext': { useAuth: () => ({ register() {} }) },
    '../lib/returnTo': {authDestination:()=> '/',authLink:p=>p}, '../components/PasswordField': { default: () => null }, '../components/GoogleButton': { default: () => null }, '../components/AuthArtwork': { default: () => null },
    '../lib/presentation': { actionError: () => 'Unable to register' },
  }).default
  const html = renderToStaticMarkup(jsx.jsx(Component, {}))
  assert.match(html, /minLength="3"/); assert.match(html, /maxLength="50"/)
  assert.match(html, /aria-describedby="registration-username-help"/)
  assert.match(html, /dots, underscores or hyphens/)
  assert.match(html, /usernames are stored lowercase/)
})

function auth({ failure, changedSession = false } = {}) {
  const updated = { ...USER, username: 'updated_learner', display_name: 'Updated name', avatar_style: 'code' }
  const states = [USER, 'synthetic-token', false], setters = []
  let index = 0, context
  const storage = new Map([['access_token', changedSession ? 'different-token' : 'synthetic-token']])
  const cache = []
  const module = load('src/context/AuthContext.tsx', {
    react: { ...React, useState: () => { const i = index++; return [states[i], value => { states[i] = value; setters.push(i) }] }, useEffect() {},
      createContext: () => ({ Provider: ({ value }) => { context = value; return null } }) },
    'react/jsx-runtime': jsx, '../api/auth': {}, '../components/AvatarImageContext': { AvatarImageProvider: ({children}) => children },
    '@tanstack/react-query': { useQueryClient: () => ({ setQueryData: (...args) => cache.push(args), invalidateQueries: () => Promise.resolve() }) },
    '../api/profile': { updateProfile: async () => { if (failure) throw failure; return updated } },
  }, { localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) } })
  renderToStaticMarkup(jsx.jsx(module.AuthProvider, { children: null }))
  return { context, states, storage, cache, updated }
}
test('saving refreshes navbar/auth state and cached user without changing token or clearing progress', async () => {
  const result = auth()
  await result.context.updateProfile({ username: 'updated_learner' })
  assert.equal(result.states[0], result.updated)
  assert.equal(result.states[1], 'synthetic-token')
  assert.equal(JSON.parse(result.storage.get('user')).username, 'updated_learner')
  assert.equal(result.cache[0][0][0], 'current-user')
  assert.equal(result.cache[0][1], result.updated)
})
test('failed profile saves and responses from an old login do not restore or overwrite auth state', async () => {
  const failure = { response: { status: 409 } }
  const failed = auth({ failure })
  await assert.rejects(failed.context.updateProfile({ username: 'taken' }), e => e === failure)
  assert.equal(failed.states[0], USER); assert.equal(failed.cache.length, 0)
  const switched = auth({ changedSession: true })
  await assert.rejects(switched.context.updateProfile({ username: 'updated' }), /session changed/)
  assert.equal(switched.states[0], USER); assert.equal(switched.cache.length, 0)
})

test('VPN filenames use current usernames, never room metadata, and accept personalized server headers', () => {
  assert.equal(name.vpnProfileFilename('TestLearner2004'), 'secforge-testlearner2004.ovpn')
  assert.equal(name.safeProfileFilename(null, 'updated_learner'), 'secforge-updated_learner.ovpn')
  assert.equal(name.safeProfileFilename('attachment; filename="secforge.ovpn"', 'updated_learner'), 'secforge-updated_learner.ovpn')
  assert.equal(name.safeProfileFilename('attachment; filename="secforge-server-name.ovpn"', 'old_name'), 'secforge-server-name.ovpn')
})
test('unsafe download headers and legacy usernames cannot introduce paths, controls or unexpected extensions', () => {
  for (const disposition of ['attachment; filename="../../evil.ovpn"', 'attachment; filename="secforge-x.exe"', "attachment; filename*=UTF-8''%00bad.ovpn", 'attachment; filename="secforge-%ZZ.ovpn"']) {
    assert.equal(name.safeProfileFilename(disposition, 'safe_user'), 'secforge-safe_user.ovpn')
  }
  assert.equal(name.vpnProfileFilename('../danger\r\n"x'), 'secforge-danger---x.ovpn')
  assert.equal(name.vpnProfileFilename('é'), 'secforge-user.ovpn')
  assert.equal(name.vpnProfileFilename('a'.repeat(80)).length, 64)
})
