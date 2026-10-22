const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const React = require('react'), jsx = require('react/jsx-runtime'), router = require('react-router-dom'), { renderToStaticMarkup: render } = require('react-dom/server')
const H = require('./discovery-helpers.cjs'), { header } = require('./navigation-helpers.cjs')
const markup = node => render(jsx.jsx(router.MemoryRouter, { children: node }))
for (const role of ['learner', 'creator', 'admin']) test(`account menu preserves ${role} access without any administration links`, () => {
  const Header = header({ id: 1, username: 'fixture', role }), html = markup(jsx.jsx(Header, {}))
  for (const href of ['/profile', '/saved', '/settings']) assert.ok(html.includes(`href="${href}"`))
  for (const href of ['/creator', '/paths/mine', '/creator/analytics']) assert.equal(html.includes(`href="${href}"`), role !== 'learner')
  assert.doesNotMatch(html, /href="\/admin(?:["/?#])|Admin Workspace|Platform administration/)
  assert.match(html, /Sign Out/)
  if (role !== 'learner') {
    assert.match(html, /My rooms/); assert.match(html, /My learning paths/); assert.match(html, /Creator Analytics/)
    assert.equal((html.match(/href="\/creator"/g) || []).length, 1)
  }
})
test('workspace shell renders children directly with no navigation or reserved column', () => {
  const Shell = H.load('src/components/WorkspaceShell.tsx', H.base).default
  for (const className of ['catalog-page', 'paths-page', 'creator-room', 'creator-analytics', 'administration-page', 'learner-room']) {
    const tree = Shell({ className, children: jsx.jsx('main', { children: 'Page content' }) })
    assert.equal(tree.type, 'div'); assert.equal(tree.props.className, `workspace ${className}`)
    assert.equal(tree.props.children.type, 'main')
    assert.doesNotMatch(render(tree), /<aside|<nav|workspace-layout|workspace-content|workspace-menu-toggle/)
  }
})
test('obsolete sidebar component and CSS are removed while the existing centered workspace remains', () => {
  assert.equal(fs.existsSync(path.join(__dirname, '../src/components/WorkspaceNav.tsx')), false)
  const css = fs.readFileSync(path.join(__dirname, '../src/App.css'), 'utf8')
  assert.doesNotMatch(css, /workspace-(?:sidebar|navigation|layout|content|menu-toggle)/)
  assert.match(css, /\.workspace\s*\{[^}]*max-width:\s*1248px;[^}]*margin:\s*0 auto/)
})
