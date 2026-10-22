const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const React = require('react')
const jsx = require('react/jsx-runtime')
const { renderToStaticMarkup: render } = require('react-dom/server')

function imageSection(machine) {
  const source = fs.readFileSync(require('node:path').join(__dirname, '../src/pages/RoomDetail.tsx'), 'utf8')
    .replace('function CreatorChallengeSection(', 'export function CreatorChallengeSection(')
    .replace('function LabSection(', 'function ActualLabSection(')
    + '\nfunction LabSection() { return null }\n'
  const module = { exports: {} }
  const dependencies = {
    react: React,
    'react/jsx-runtime': jsx,
    '@tanstack/react-query': {
      useQueryClient: () => ({}),
      useQuery: () => ({ data: [{ id: 22, machines: [machine], is_published: false }] }),
      useMutation: () => ({}),
    },
    '../hooks/useLab': { useCreatorTestLab: () => ({}) },
    '../components/ConfirmProvider': { useConfirm: () => async () => true },
    '../lib/presentation': { isImageReady: rows => rows[0].status === 'available' && !!rows[0].ami_id },
    '../lib/format': { formatDateTime: () => 'Today' },
  }
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, {
    module, exports: module.exports,
    require: name => dependencies[name] || { default: () => null },
  })
  return render(jsx.jsx(module.exports.CreatorChallengeSection, {
    roomId: 3187, creatorId: 1, canCapture: true, draft: { data: { image_template_id: 22 } },
  }))
}

test('failed saved image shows escaped backend diagnostics after reopening the page', () => {
  const html = imageSection({ status: 'failed', error_message: 'Console identity <missing> & unavailable' })
  assert.match(html, /Capture details: Console identity &lt;missing&gt; &amp; unavailable/)
  assert.match(html, /role="alert"/)
  assert.match(html, /Test captured image/)
  assert.match(html, /disabled=""/)
})

test('available image stays ready without a stale error panel', () => {
  const html = imageSection({ status: 'available', ami_id: 'ami-offline', error_message: null })
  assert.match(html, /Ready/)
  assert.doesNotMatch(html, /Capture details:/)
})
