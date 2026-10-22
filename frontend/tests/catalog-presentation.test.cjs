const test = require('node:test'), assert = require('node:assert/strict')
const React = require('react'), jsx = require('react/jsx-runtime'), router = require('react-router-dom')
const { renderToStaticMarkup: render } = require('react-dom/server'), H = require('./discovery-helpers.cjs')
const ROOM = { id: 42, name: 'Web to Root', description: 'Investigate the company website.', category: 'web-security', categories: ['web-security', 'linux', 'privilege-escalation'], difficulty: 'easy', room_type: 'PRACTICAL', points_total: 150, progress: 'completed', bookmarked: true }
const page = (items = [ROOM]) => ({ items, total: items.length, page: 1, page_size: 24, categories: [] })
const html = node => render(jsx.jsx(router.MemoryRouter, { children: node }))
function find(node, predicate) {
  if (!node || typeof node !== 'object') return
  if (predicate(node)) return node
  return React.Children.toArray(node.props?.children).map(n => find(n, predicate)).find(Boolean)
}
test('shared cards separate room type, two topic previews, points and actual progress', () => {
  const result = html(jsx.jsx(H.CatalogRoomCard.default, { room: ROOM, learner: true, returnTo: '/rooms?category=linux' }))
  assert.match(result, /room-type-label">Practical Lab/)
  assert.match(result, /diff-easy">easy/)
  assert.match(result, /title="Web Security">Web Security/)
  assert.match(result, /title="Linux">Linux/)
  assert.match(result, /aria-label="All topics for Web to Root"[^>]*>\+1/)
  assert.doesNotMatch(result, /title="Privilege Escalation"/)
  assert.match(result, /catalog-room-metadata"><span class="room-points">150 points<\/span><span class="room-progress progress-completed">Completed/)
  assert.match(result, /href="\/rooms\/42"/)
})
test('one/two topics, theory type, all difficulties and missing progress keep honest metadata', () => {
  for (const difficulty of ['easy', 'medium', 'hard']) for (const topics of [['linux'], ['linux', 'web-security']]) {
    const result = html(jsx.jsx(H.CatalogRoomCard.default, { room: { ...ROOM, categories: topics, room_type: 'THEORY', difficulty, points_total: 0, progress: undefined }, learner: false, returnTo: '/rooms' }))
    assert.match(result, /Theory Room/); assert.ok(result.includes(`diff-${difficulty}`)); assert.match(result, /0 points/)
    assert.doesNotMatch(result, /more-topics|Completed|In progress|Not started/)
  }
})
test('legacy primary category, long text and distinct progress values remain accessible', () => {
  for (const [progress, label] of [['not-started', 'Not started'], ['in-progress', 'In progress']]) {
    const name = 'Long room title '.repeat(20), description = 'Detailed briefing. '.repeat(60)
    const result = html(jsx.jsx(H.CatalogRoomCard.default, { room: { ...ROOM, name, description, categories: undefined, category: 'linux', progress }, learner: true, returnTo: '/saved' }))
    assert.ok(result.includes(`title="${name}"`)); assert.ok(result.includes(`title="${description}"`))
    assert.match(result, /title="Linux">Linux/); assert.ok(result.includes(label))
  }
})
test('topic disclosure reveals every full category without nesting buttons inside room links', () => {
  const tree = H.CatalogRoomCard.default({ room: ROOM, learner: true, returnTo: '/rooms' })
  const disclosure = find(tree, n => n.type === H.AnchoredPopover.default)
  assert.ok(disclosure)
  const expanded = render(disclosure.props.children(() => {}))
  for (const label of ['Web Security', 'Linux', 'Privilege Escalation']) assert.ok(expanded.includes(`<li>${label}</li>`))
  assert.match(expanded, /Done/)
  const briefing = find(tree, n => n.props.className === 'catalog-room-briefing')
  assert.equal(find(briefing, n => n.type === 'button'), undefined)
  const open = find(tree, n => n.props.className === 'card-open')
  assert.equal(open.props.to, '/rooms/42'); assert.equal(open.props.state.discovery, '/rooms')
})
test('Explore and Saved compose the identical shared card and preserve known bookmark state', () => {
  const Catalog = H.discovery({ id: 7, role: 'learner' }, page()).default
  const explore = html(jsx.jsx(Catalog, {})), saved = html(jsx.jsx(Catalog, { saved: true }))
  const card = markup => markup.slice(markup.indexOf('<article'), markup.indexOf('</article>') + 10)
  assert.equal(card(explore), card(saved)); assert.match(saved, /Saved rooms/); assert.match(saved, /Saved learning paths/)
  assert.doesNotMatch(saved, /category-filter-trigger/)
  const tree = H.CatalogRoomCard.default({ room: ROOM, learner: true, returnTo: '/saved' })
  const bookmark = find(tree, n => n.props.kind === 'rooms')
  assert.equal(bookmark.props.id, 42); assert.equal(bookmark.props.known, true)
})
test('empty saved rooms, failure, saved paths and learner authorization retain their behavior', () => {
  const Empty = H.discovery({ id: 7, role: 'learner' }, page([])).default
  assert.match(html(jsx.jsx(Empty, { saved: true })), /No saved rooms yet/)
  const ErrorPage = H.discovery({ id: 7, role: 'learner' }, undefined, [], true).default
  assert.match(html(jsx.jsx(ErrorPage, {})), /role="alert"/)
  const Guest = H.discovery(null, page()).default
  assert.match(html(jsx.jsx(Guest, { saved: true })), /Saved content is for learners/)
  const Paths = H.discovery({ id: 7, role: 'learner' }, page([{ id: 8, title: 'Learning Linux', category: 'linux', difficulty: 'easy', room_count: 3, rooms_completed: 1, percent: 33 }])).default
  const result = html(jsx.jsx(Paths, { saved: true, kind: 'paths' }))
  assert.match(result, /3 published rooms/); assert.match(result, /1\/3 rooms completed/); assert.match(result, /Open path/)
  assert.doesNotMatch(result, /catalog-room-card/)
})
test('compact category trigger counts selections without inline chips and keeps selection limit in URL state', () => {
  const result = render(jsx.jsx(H.CategoryFilterPopover.default, { values: ['linux', 'web-security'], onChange() {} }))
  assert.match(result, /Categories \(2\)/); assert.match(result, /aria-haspopup="dialog" aria-expanded="false"/)
  assert.doesNotMatch(result, /category-chip|checkbox/)
  const values = H.categories.categories.slice(0, 10).map(c => c.slug)
  assert.equal(H.state.readDiscovery('?category=' + values.join(',')).category.split(',').length, 8)
})
test('pending catalogs retain labeled controls and loading state', () => {
  const Pending = H.discovery({ id: 7, role: 'learner' }, undefined, [], false, true).default
  const result = html(jsx.jsx(Pending, {}))
  assert.match(result, /aria-label="Loading content"/)
  assert.match(result, /Finding content/)
  assert.match(result, /category-filter-trigger/)
  assert.doesNotMatch(result, /catalog-room-card/)
})
