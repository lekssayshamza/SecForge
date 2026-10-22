const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript')
const React = require('react'), jsx = require('react/jsx-runtime'), {renderToStaticMarkup: render} = require('react-dom/server')
function load(file, dependencies = {}) {
  const module = {exports: {}}
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText, {module, exports: module.exports, require: name => {assert.ok(name in dependencies, name); return dependencies[name]}})
  return module.exports
}
const ports = load('src/lib/networkPorts.ts')
const parse = rows => JSON.parse(JSON.stringify(ports.parsePortInputs(rows)))
test('individual TCP/UDP ports and passive FTP ranges normalize safely', () => {
  assert.deepEqual(parse([{protocol:'udp', start:'53', end:''}, {protocol:'tcp', start:'40000', end:'40100'}, {protocol:'tcp', start:'40050', end:'40200'}, {protocol:'tcp', start:'21', end:''}]), [{protocol:'tcp',from_port:21,to_port:21},{protocol:'tcp',from_port:40000,to_port:40200},{protocol:'udp',from_port:53,to_port:53}])
  assert.deepEqual(parse([]), [])
})
test('malformed and excessive configurations cannot be submitted', () => {
  for (const row of [{protocol:'icmp',start:'1',end:'1'},{protocol:'tcp',start:'0',end:''},{protocol:'tcp',start:'1e2',end:''},{protocol:'tcp',start:'80.5',end:''},{protocol:'udp',start:'65536',end:''},{protocol:'tcp',start:'90',end:'80'},{protocol:'tcp',start:'1',end:'65535'}]) assert.throws(() => parse([row]))
  assert.throws(() => parse(Array(21).fill({protocol:'tcp',start:'80',end:''})))
})
test('creator port section shows only configured VPN service badges', () => {
  const Component = load('src/components/NetworkPorts.tsx', {react:React,'react/jsx-runtime':jsx,'../lib/networkPorts':ports,'../hooks/useUnsavedChanges':{useUnsavedChanges(){},confirmDiscard:()=>true}}).default
  const html = render(jsx.jsx(Component, {draft:{data:{revision:1,room:{exposed_ports:[{protocol:'tcp',from_port:40000,to_port:40100},{protocol:'udp',from_port:53,to_port:53}]}},write(){}}}))
  assert.match(html,/Network &amp; Exposed Ports/); assert.match(html,/Accessible through the SecForge VPN/); assert.doesNotMatch(html,/TCP 22|Publish to apply|SSH management/); assert.match(html,/network-port-badges/); assert.match(html,/TCP 40000–40100/); assert.match(html,/UDP 53/)
})
