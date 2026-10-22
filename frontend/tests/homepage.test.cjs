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

function load(relative, dependencies, globals = {}) {
  const filename = path.join(__dirname, '..', relative)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    fileName: filename, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, { ...globals, module, exports: module.exports, require(name) {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
    return dependencies[name]
  } }, { filename })
  return module.exports.default
}
const base = { react: React, 'react/jsx-runtime': jsx }
const CyberHero = load('src/components/CyberHero.tsx', base)
const render = Component => renderToStaticMarkup(jsx.jsx(Component, {}))
const Logo = load('src/components/Logo.tsx', base)
const Icon = load('src/components/Icon.tsx', base)
const Landing = load('src/pages/Landing.tsx', { ...base, 'react-router-dom': router,
  '../components/Logo': { default: Logo }, '../components/Icon': { default: Icon },
  '../components/CyberHero': { default: CyberHero }, './Landing.css': {},
  '../components/ThemeToggle': { default: () => jsx.jsx('button', { children: 'Theme' }) },
})

function homepage() {
  return renderToStaticMarkup(jsx.jsx(router.MemoryRouter, { children: jsx.jsx(Landing, {}) }))
}

test('homepage offers public room navigation without fetching the catalog in the hero', () => {
  const html = homepage()
  assert.match(html, /href="\/register"[^>]*>Start learning/)
  assert.match(html, /href="\/rooms"/)
  assert.match(html, /Explore freely/)
  assert.match(html, /href="\/login"/)
  assert.doesNotMatch(html, /href="#how-it-works"/)
  assert.ok(!html.includes('landing-lab-card'))
  assert.match(html, /Learn cybersecurity/)
})

test('illustration has an accessible name and identifies itself as illustrative, not live telemetry', () => {
  const html = render(CyberHero)
  assert.match(html, /role="img" aria-labelledby="[^"]+"/)
  assert.match(html, /The SecForge practice network/)
  assert.match(html, /not a live lab/)
  assert.match(html, /Pause illustration animation/)
  assert.match(html, /aria-pressed="false"/)
  assert.ok(!html.includes('<foreignObject') && !html.includes('<image'))
})

test('reduced-motion visitors receive a static illustration without a misleading play control', () => {
  const ReducedHero = load('src/components/CyberHero.tsx', base, { window: { matchMedia: () => ({ matches: true }) } })
  const html = render(ReducedHero)
  assert.match(html, /cyber-hero is-paused/)
  assert.match(html, /Reduced motion enabled/)
  assert.ok(!html.includes('<button'))
  assert.match(html, /The SecForge practice network/)
})

test('missing matchMedia support leaves usable static markup and pause control', () => {
  const FallbackHero = load('src/components/CyberHero.tsx', base, { window: {} })
  assert.match(render(FallbackHero), /Pause illustration animation/)
})

test('multiple scenes use unique SVG descriptions and gradient references', () => {
  const html = renderToStaticMarkup(jsx.jsxs('div', { children: [jsx.jsx(CyberHero, {}), jsx.jsx(CyberHero, {})] }))
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1])
  assert.equal(new Set(ids).size, ids.length)
  for (const ref of html.matchAll(/url\(#([^\)]+)\)/g)) assert.ok(ids.includes(ref[1]))
})

test('learning copy describes existing SSH/VPN/question workflows without unimplemented features or fabricated results', () => {
  const html = homepage()
  for (const text of ['Connect through VPN', 'Find the answers', 'Earn your points', 'SSH', 'Saved progress']) assert.ok(html.includes(text))
  for (const text of ['learning paths', 'certification', 'Google', 'million learners']) assert.ok(!html.includes(text))
  assert.equal((html.match(/<h1\b/g) || []).length, 1)
})
