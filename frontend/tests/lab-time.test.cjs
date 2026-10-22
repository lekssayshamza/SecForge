const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const ts = require('typescript')
const React = require('react')
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup } = require('react-dom/server')
function load(file, dependencies = {}, globals = {}) {
  const filename = path.join(__dirname, '..', file)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, { module, exports: module.exports, ...globals, require(name) {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
    return dependencies[name]
  } }, { filename })
  return module.exports
}
const clock = load('src/lib/labClock.ts', {}, { performance: { now: () => 100 }, Date: { parse: Date.parse, now: () => -999999999 } })
const lab = { id: 9, status: 'running', extensions_used: 0, session_started_at: '2026-10-11T10:00:00Z', expires_at: '2026-10-11T11:00:00Z', server_time: '2026-10-11T10:00:00Z', clock_received_at: 100 }

test('countdown uses server UTC and monotonic elapsed time, not the browser wall clock', () => {
  assert.equal(clock.remainingSeconds(lab, 100), 3600)
  assert.equal(clock.remainingSeconds(lab, 600100), 3000)
  assert.equal(clock.remainingSeconds(lab, 7200100), 0)
  assert.equal(clock.formatCountdown(3661), '01:01:01')
  assert.equal(clock.formatCountdown(0), '00:00:00')
})
test('fresh fetch after refresh/restart resynchronizes from server deadline rather than cached elapsed time', () => {
  const fetched = clock.stampLabTime({ ...lab, server_time: '2026-10-11T10:30:00Z', clock_received_at: undefined })
  assert.equal(clock.remainingSeconds(fetched, 100), 1800)
  assert.equal(clock.remainingSeconds({ ...lab, server_time: undefined }, 100), null)
  assert.equal(clock.remainingSeconds({ ...lab, expires_at: 'invalid' }, 100), null)
})
test('extension eligibility enforces the window, remaining quota and lifecycle state', () => {
  for (const seconds of [1801, 3600, 0, null]) assert.equal(clock.canExtendLab(lab, seconds), false)
  for (const seconds of [1800, 300, 1]) assert.equal(clock.canExtendLab(lab, seconds), true)
  assert.equal(clock.canExtendLab({ ...lab, extensions_used: 1 }, 1800), true)
  assert.equal(clock.canExtendLab({ ...lab, extensions_used: 2 }, 1800), false)
  for (const status of ['stopping','stopped','failed','pending','provisioning']) assert.equal(clock.canExtendLab({ ...lab, status }, 1800), false)
  assert.equal(clock.canExtendLab({ ...lab, expired_at: lab.expires_at }, 1800), false)
  assert.equal(clock.canExtendLab({ ...lab, session_started_at: null }, 1800), false)
})
test('extension requests pin session ID and quota and never transmit a client deadline', async () => {
  const calls=[]
  const api=load('src/api/labs.ts', { '../lib/labClock':clock, './axios':{default:{post:async(...args)=>{calls.push(args);return {data:lab}}}} })
  const result=await api.extendLab(42,lab)
  assert.equal(calls[0][0],'/rooms/42/lab/extend')
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][1])),{lab_id:9,expected_extensions_used:0})
  assert.equal(result.clock_received_at,100)
})
const Timer=load('src/components/LabTimer.tsx',{react:React,'react/jsx-runtime':jsx,'../lib/labClock':clock}).default
const render=(seconds,extra={})=>renderToStaticMarkup(jsx.jsx(Timer,{lab:{...lab,...extra},seconds,onExtend:async()=>lab}))
test('timer has accessible extension controls and does not announce every second', () => {
  const html=render(1800)
  assert.match(html,/00:30:00/);assert.match(html,/2 extensions remaining/);assert.match(html,/Extend \+1 Hour/)
  assert.match(html,/role="timer" aria-live="off"/)
  assert.ok(!render(1801).includes('Extend +1 Hour'))
})
test('ten-minute and five-minute warnings, quota and terminal states are truthful', () => {
  assert.match(render(600),/10 minutes or less remain/)
  assert.match(render(300),/5 minutes or less remain/)
  assert.match(render(300,{extensions_used:2}),/0 extensions remaining/)
  assert.ok(!render(300,{extensions_used:2}).includes('Extend +1 Hour'))
  assert.match(render(0),/Access expired/)
  assert.match(render(500,{status:'stopped'}),/Session ended/)
  assert.match(render(null),/Syncing time/)
})
