const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const React = require('react')
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup } = require('react-dom/server')

const filename = path.join(__dirname, '..', 'src/components/ConnectionGuide.tsx')
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  fileName: filename,
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText
const dependencies = { react: React, 'react/jsx-runtime': jsx, './Icon': { default: () => null } }
const moduleObject = { exports: {} }
vm.runInNewContext(code, {
  module: moduleObject, exports: moduleObject.exports,
  require(name) { assert.ok(Object.hasOwn(dependencies, name)); return dependencies[name] },
}, { filename })
const ConnectionGuide = moduleObject.exports.default
const render = builder => renderToStaticMarkup(jsx.jsx(ConnectionGuide, {
  address: '10.0.1.42', builder, sshUsername: 'ubuntu', download: jsx.jsx('button', { children: 'Download VPN Profile' }),
}))

test('learner SSH guidance uses the room login rather than assuming Ubuntu admin', () => {
  const html = render(false)
  assert.match(html, /ssh ROOM_USERNAME@10\.0\.1\.42/)
  assert.match(html, /user named in the room briefing/)
  assert.ok(!html.includes('ssh ubuntu@'))
  assert.match(html, /Download VPN Profile/)
  assert.match(html, /href="http:\/\/10\.0\.1\.42"/)
})

test('creator builder SSH guidance retains the configured Ubuntu key workflow', () => {
  const html = render(true)
  assert.match(html, /ssh -i ~\/sf\/CyberRange-Key\.pem ubuntu@10\.0\.1\.42/)
  assert.match(html, /Use your existing CyberRange-Key.pem file/)
  assert.match(html, /Keep the private file on your computer/)
  assert.match(html, /Copy builder SSH command/)
  assert.match(html, /Permission denied \(publickey\)/)
  assert.ok(!html.includes('ROOM_USERNAME'))
})

test('both roles receive an actionable dual-VPN command while ordinary profiles stay supported', () => {
  for (const builder of [false, true]) {
    const html = render(builder)
    assert.match(html, /sudo \/usr\/local\/libexec\/secforge\/learner_vpn/)
    assert.match(html, /Copy development VPN command/)
    assert.match(html, /Keep the management VPN connected/)
    assert.match(html, /Disconnect only your previous user VPN/)
    assert.match(html, /profile for the account that owns this machine/)
    assert.match(html, /sudo openvpn --config/)
    assert.ok(!html.includes('tun0') && !html.includes('tun1'))
    assert.ok(html.includes('<details') && html.includes('<summary'))
  }
})

test('personalized VPN filenames are quoted safely in ordinary and development commands', () => {
  const html = renderToStaticMarkup(jsx.jsx(ConnectionGuide, {
    address: '10.0.1.42', profileFilename: "secforge-user's (2).ovpn", download: null,
  }))
  const text = html.replace(/&#x27;/g, "'").replace(/&quot;/g, '"')
  assert.ok(text.includes(`sudo openvpn --config './secforge-user'"'"'s (2).ovpn'`))
  assert.ok(text.includes(`sudo /usr/local/libexec/secforge/learner_vpn './secforge-user'"'"'s (2).ovpn'`))
})

for (const username of ['challenge_admin', undefined, 'ubuntu; echo unsafe', 'ubuntu$']) {
  test(`builder SSH metadata is honored or rejected safely: ${username}`, () => {
    const html = renderToStaticMarkup(jsx.jsx(ConnectionGuide, { address: '10.0.1.42', builder: true, sshUsername: username, download: null }))
    if (username === 'challenge_admin') assert.match(html, /challenge_admin@10\.0\.1\.42/)
    else { assert.match(html, /SSH connection details are unavailable/); assert.ok(!html.includes('Copy builder SSH command')); assert.ok(!html.includes('unsafe')) }
  })
}
