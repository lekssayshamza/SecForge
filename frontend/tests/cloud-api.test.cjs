const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const path = require('node:path')
function client(value, pathname = '/profile') {
  const source = fs.readFileSync(path.join(__dirname, '../src/api/axios.ts'), 'utf8').replace('import.meta.env.VITE_API_BASE_URL', JSON.stringify(value) ?? 'undefined')
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  let config, reject
  const instance = { interceptors: { request: { use() {} }, response: { use(_resolve, onError) { reject = onError } } } }
  const module = { exports: {} }
  const storage = new Map([['access_token', 'synthetic-session'], ['user', 'synthetic-user']])
  const location = { pathname, href: pathname }
  vm.runInNewContext(code, { module, exports: module.exports, window: { location }, localStorage: { removeItem: key => storage.delete(key) }, require: () => ({ default: { create(c) { config = c; return instance } } }) })
  return { baseURL: config.baseURL, reject, storage, location }
}
test('production explicitly selects same-origin API', () => assert.equal(client('').baseURL, '/api'))
test('development keeps its local backend URL', () => assert.equal(client(undefined).baseURL, 'http://localhost:8000/api'))
test('explicit configured API URL is preserved', () => assert.equal(client('https://api.example.test').baseURL, 'https://api.example.test/api'))
for (const url of ['/auth/password', '/auth/google/link']) {
  test(`${url}: expired or revoked sessions clear credentials and return to login`, async () => {
    const c = client('')
    const error = { config: { url }, response: { status: 401, data: { detail: 'Invalid or expired token' } } }
    await assert.rejects(c.reject(error), value => value === error)
    assert.equal(c.storage.size, 0)
    assert.equal(c.location.href, '/login')
  })
  test(`${url}: incorrect reauthentication preserves the session and form`, async () => {
    const c = client('')
    const detail = url === '/auth/password' ? 'Current password is incorrect' : 'Reauthenticate with your current password'
    const error = { config: { url }, response: { status: 401, data: { detail } } }
    await assert.rejects(c.reject(error), value => value === error)
    assert.equal(c.storage.size, 2)
    assert.equal(c.location.href, '/profile')
  })
}
test('expired remembered sessions leave public authentication pages accessible', async () => {
  const c = client('', '/register')
  const error = { config: { url: '/auth/me' }, response: { status: 401 } }
  await assert.rejects(c.reject(error), value => value === error)
  assert.equal(c.storage.size, 0)
  assert.equal(c.location.href, '/register')
})
test('failed cookie-based Google handoff stays on its retry screen', async () => {
  const c = client('', '/auth/google')
  const error = { config: { url: '/auth/google/complete' }, response: { status: 401 } }
  await assert.rejects(c.reject(error), value => value === error)
  assert.equal(c.location.href, '/auth/google')
})

test('expired remembered token on public room detail clears credentials without redirecting', async () => {
  const c = client('', '/rooms/42')
  const error = { config: { url: '/auth/me' }, response: { status: 401 } }
  await assert.rejects(c.reject(error), value => value === error)
  assert.equal(c.storage.size, 0)
  assert.equal(c.location.href, '/rooms/42')
})
