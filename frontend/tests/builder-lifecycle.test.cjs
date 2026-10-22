const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

// Execute the actual TS modules with only their external transports/hooks fake.
// No API, browser, database, or infrastructure connection is possible here.
function load(relative, dependencies) {
  const filename = path.join(__dirname, '..', relative)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, {
    module, exports: module.exports,
    require(name) { if (name.endsWith('/lib/permissions')) return require('./permissions-helper.cjs'); 
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
      return dependencies[name]
    },
  }, { filename })
  return module.exports
}

test('builder Stop uses the owned room lifecycle endpoint and returns its state', async () => {
  const calls = []
  const stopped = { id: 14, room_id: 28, status: 'stopped' }
  const api = load('src/api/builders.ts', { './axios': { default: {
    post: async (...args) => { calls.push(args); return { data: stopped } },
  } } })
  assert.equal(await api.stopBuilder(28), stopped)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], '/rooms/28/builder/stop')
  assert.equal(calls[0].length, 1)
})

test('failed Stop propagates instead of reporting stopped', async () => {
  const failure = { response: { status: 409 } }
  const api = load('src/api/builders.ts', { './axios': { default: {
    post: async () => { throw failure },
  } } })
  await assert.rejects(api.stopBuilder(28), error => error === failure)
})

function hook(role = 'creator', pending = false) {
  const mutations = [], writes = [], invalidations = [], stops = [], queries = []
  const queryClient = {
    setQueryData: (...args) => writes.push(args),
    invalidateQueries: (...args) => { invalidations.push(args); return Promise.resolve() },
  }
  const builder = { id: 14, room_id: 28, status: 'running' }
  const exports = load('src/hooks/useBuilder.ts', {
    '../api/builders': {
      getBuilder: async () => builder,
      startBuilder: async () => builder,
      stopBuilder: async id => { stops.push(id); return { ...builder, status: 'stopping' } },
    },
    '../context/AuthContext': { useAuth: () => ({ user: { role } }) },
    '@tanstack/react-query': {
      useQueryClient: () => queryClient,
      useQuery: options => { queries.push(options); return { data: builder, isLoading: false, refetch() {} } },
      useMutation: options => {
        mutations.push(options)
        return { mutate() {}, mutateAsync: options.mutationFn, isPending: mutations.length === 2 && pending }
      },
    },
  })
  return { result: exports.useBuilder(28), mutations, queries, writes, invalidations, stops }
}

test('hook Stop preserves returned stopping state and refreshes only its builder', async () => {
  const h = hook()
  const state = await h.result.stopBuilderAsync()
  assert.deepEqual(h.stops, [28])
  await h.mutations[1].onSuccess(state)
  assert.equal(h.writes.length, 1)
  assert.equal(JSON.stringify(h.writes[0][0]), JSON.stringify(['builder', 28]))
  assert.equal(h.writes[0][1].status, 'stopping')
  assert.equal(JSON.stringify(h.invalidations), JSON.stringify([[{ queryKey: ['builder', 28] }]]))
})

test('Stop pending state is exposed for disabling duplicate controls', () => {
  assert.equal(hook('creator', true).result.isStopping, true)
  assert.equal(hook().result.isStopping, false)
})

test('stopping builders keep polling until a terminal state', () => {
  const q = hook().queries[0]
  assert.equal(q.refetchInterval({ state: { data: { status: 'stopping' } } }), 5000)
  assert.equal(q.refetchInterval({ state: { data: { status: 'stopped' } } }), false)
  assert.equal(q.refetchInterval({ state: { data: { status: 'failed' } } }), false)
})

test('learner role still does not inspect creator builders', () => {
  assert.equal(hook('learner').queries[0].enabled, false)
  assert.equal(hook('creator').queries[0].enabled, true)
})
