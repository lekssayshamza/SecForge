const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const React = require('react')
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup } = require('react-dom/server')

function load(relative, dependencies) {
  const filename = path.join(__dirname, '..', relative)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, { module, exports: module.exports, require(name) { if(name.endsWith('/CategoryMultiSelect')) return require('./discovery-helpers.cjs').CategoryMultiSelect; if (name.endsWith('/ContextHelp')) return require('./context-help-helper.cjs'); if (name.endsWith('/SocialConnections')) return {default:()=>null};  if (name.endsWith('/lib/permissions')) return require('./permissions-helper.cjs'); 
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
    return dependencies[name]
  } }, { filename })
  return module.exports
}

test('question authoring, ordering and submission use only the intended room endpoints', async () => {
  const calls = []
  const transport = Object.fromEntries(['get', 'post', 'patch', 'put', 'delete'].map(method => [method, async (...args) => {
    calls.push([method, ...args]); return { data: { accepted: true } }
  }]))
  const api = load('src/api/challenges.ts', { './axios': { default: transport } })
  await api.listQuestions(42)
  await api.createQuestion(42, { question_text: 'Find the flag', answer: 'synthetic-only', points: 25 })
  await api.updateQuestion(42, 8, { hint: 'Look closely' })
  await api.orderQuestions(42, [9, 8])
  await api.deleteQuestion(42, 9)
  await api.getProgress(42)
  await api.submitAnswer(42, 8, '  Synthetic-Only  ')
  assert.deepEqual(calls.map(c => c.slice(0, 2)), [
    ['get', '/rooms/42/questions'], ['post', '/rooms/42/questions'], ['patch', '/rooms/42/questions/8'],
    ['put', '/rooms/42/questions/order'], ['delete', '/rooms/42/questions/9'], ['get', '/rooms/42/progress'],
    ['post', '/rooms/42/questions/8/submit'],
  ])
  assert.equal(calls.at(-1)[2].answer, '  Synthetic-Only  ', 'normalization belongs to the server')
  assert.deepEqual(Array.from(calls[3][2].question_ids), [9, 8])
})

test('submission failures propagate without inventing success or points', async () => {
  const failure = { response: { status: 429 } }
  const api = load('src/api/challenges.ts', { './axios': { default: { post: async () => { throw failure } } } })
  await assert.rejects(api.submitAnswer(42, 8, 'synthetic-only'), e => e === failure)
})

function view(role, options = {}) {
  const queries = []
  const questions = options.questions ?? [{ id: 8, question_text: 'Find the service', hint: 'Inspect HTTP', points: 100, display_order: 1 }]
  const progress = { room_id: 42, solved_question_ids: options.solved ? [8] : [], questions_solved: options.solved ? 1 : 0,
    questions_total: questions.length, points_earned: options.solved ? 100 : 0, points_total: 100,
    completed: !!options.solved, completed_at: options.solved ? '2026-10-11T10:00:00' : null }
  const Icon = () => null
  const Component = load('src/components/ChallengeQuestions.tsx', {
    react: React, 'react/jsx-runtime': jsx,
    '@tanstack/react-query': { useQueryClient: () => ({}), useQuery: q => {
      queries.push(q)
      return q.queryKey[0] === 'questions' ? { data: { questions, authoring_locked: !!options.locked }, error: options.error }
        : { data: progress }
    } },
    '../context/AuthContext': { useAuth: () => ({ user: { id: role === 'creator' ? 7 : 9, role } }) },
    '../api/challenges': {}, '../lib/presentation': { actionError: () => 'Service unavailable. Retry.' },
    '../lib/format': { formatDateTime: () => '11 Oct 2026' }, './TaskEditor': { default: () => null }, './SafeMarkdown': { default: ({text}) => jsx.jsx('div',{children:text}) }, '../hooks/useUnsavedChanges': { useUnsavedChanges() {}, confirmDiscard: () => true }, './Dialog': { default: () => null },
    './Icon': { default: Icon }, './ConfirmProvider': { useConfirm: () => async () => true },
  }).default
  const html = renderToStaticMarkup(jsx.jsx(Component, { room: { id: 42, creator_id: 7, status: options.draft ? 'draft' : 'published' } }))
  return { html, queries }
}

test('learner questions render independently of any AWS lab and do not enable creator requests', () => {
  const { html, queries } = view('learner')
  assert.match(html, /Answer for question 1/)
  assert.match(html, /Submit answer/)
  assert.match(html, /<details/)
  assert.match(html, /role="status"/)
  assert.ok(!html.includes('Edit question') && !html.includes('Add question'))
  assert.ok(queries.every(q => ['questions', 'progress', 'room-tasks'].includes(q.queryKey[0])))
})

test('solved questions retain a disabled input instead of a resubmittable form', () => {
  const { html } = view('learner', { solved: true })
  assert.match(html, /Solved/)
  assert.match(html, /name="answer"[^>]*disabled=""/)
  assert.doesNotMatch(html, /Submit answer|task-navigation/)
})

test('creator can author drafts without learner progress requests', () => {
  const { html, queries } = view('creator', { draft: true })
  assert.match(html, /Add question/)
  assert.match(html, /Edit question/)
  assert.match(html, /Move question 1 up/)
  assert.equal(queries[1].enabled, false)
  assert.ok(!html.includes('Submit answer'))
})

test('earned credit locks structural controls but keeps text edits available', () => {
  const { html } = view('creator', { locked: true })
  assert.match(html, /Answers, points and the question set are locked/)
  assert.match(html, /disabled="">Delete question/)
  assert.match(html, /Edit question/)
})

test('question failure is an accessible alert and legacy empty rooms stay usable', () => {
  assert.match(view('learner', { error: new Error('private detail') }).html, /role="alert"/)
  assert.ok(!view('learner', { error: new Error('private detail') }).html.includes('private detail'))
  assert.match(view('learner', { questions: [] }).html, /No questions have been added/)
})


test('learning conflicts explain stale working copies without infrastructure detail', () => {
  const { html } = view('learner', { error: { response: { status: 409 } } })
  assert.match(html, /room changed/)
  assert.ok(!html.includes('another environment'))
})

test('learner normalization help uses keyboard and touch accessible disclosure', () => {
  const { html } = view('learner')
  assert.match(html, /aria-label="Answer format" aria-expanded="false"/)
  assert.doesNotMatch(html, /case-sensitive/)
  assert.match(html, /class="help-button"/)
})


test('authoring shows and enforces the per-room question limit', () => {
  const questions = Array.from({ length: 100 }, (_, i) => ({ id: i + 1, question_text: `Task ${i}`, hint: null, points: 1, display_order: i + 1 }))
  const { html } = view('creator', { questions })
  assert.match(html, /limit of 100 questions/)
  assert.match(html, /disabled=""> Add question/)
})
