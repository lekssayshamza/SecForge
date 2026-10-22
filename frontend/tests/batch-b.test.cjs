const test=require('node:test'),assert=require('node:assert/strict'),React=require('react'),jsx=require('react/jsx-runtime'),router=require('react-router-dom'),{renderToStaticMarkup:render}=require('react-dom/server'),H=require('./discovery-helpers.cjs')
const nil={default:()=>null}

test('working copy transport sends revision and publishes without manual image selection',async()=>{
 const calls=[],api=Object.fromEntries(['get','patch','post'].map(method=>[method,async(...args)=>(calls.push([method,...args]),{data:{revision:4}})]))
 const W=H.load('src/api/workspace.ts',{'./axios':{default:api}})
 await W.getWorkspace(42);await W.writeWorkspace(42,3,'question_update',{id:8,hint:'Clarification'});await W.publishWorkspace(42,4)
 assert.equal(calls[0][1],'/rooms/42/workspace');assert.equal(calls[1][2].revision,3);assert.equal(calls[2][1],'/rooms/42/workspace/publish')
 assert.deepEqual(Object.keys(calls[2][2]),['revision']);assert.equal(calls[2][2].revision,4)
})

test('workspace errors propagate for stale edits and failed publication',async()=>{
 const failure={response:{status:409}},W=H.load('src/api/workspace.ts',{'./axios':{default:{patch:async()=>{throw failure},post:async()=>{throw failure}}}})
 await assert.rejects(W.writeWorkspace(42,0,'metadata',{name:'Stale'}),e=>e===failure)
 await assert.rejects(W.publishWorkspace(42,0),e=>e===failure)
})

for(const [role,capability,content] of [['learner',false,false],['learner',true,false],['creator',false,true],['admin',false,true],['admin',true,true]])test(`account creator navigation preserves ${role}/${capability} authority`,()=>{
 const C=require('./navigation-helpers.cjs').header({username:'fixture',role,creator_access:capability})
 const html=render(jsx.jsx(router.MemoryRouter,{children:jsx.jsx(C,{})}))
 assert.equal(html.includes('My rooms'),content);assert.doesNotMatch(html,/href="\/admin/)
 if(content){assert.match(html,/href="\/creator"/);assert.match(html,/href="\/paths\/mine"/);assert.match(html,/href="\/creator\/analytics"/)}
})

test('saved answer editor opens a normal protected field without keep/replace controls',()=>{
 const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript')
 const source=fs.readFileSync(path.join(__dirname,'../src/components/ChallengeQuestions.tsx'),'utf8').replace('function QuestionEditor(','export function QuestionEditor(')
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,module={exports:{}}
 const deps={...H.base,'../lib/permissions':require('./permissions-helper.cjs'),'@tanstack/react-query':{},'../context/AuthContext':{},'../api/challenges':{},'../lib/presentation':{},'../lib/format':{},'./Icon':nil,'./ConfirmProvider':{},'./TaskEditor':nil,'./SafeMarkdown':nil,'../hooks/useUnsavedChanges':{useUnsavedChanges(){},confirmDiscard:()=>true},'./Dialog':{default:({children})=>jsx.jsx('section',{children})}}
 vm.runInNewContext(code,{exports:module.exports,module,require:n=>{if(n.endsWith('/ContextHelp'))return require('./context-help-helper.cjs');if(n.endsWith('/SocialConnections'))return {default:()=>null};assert.ok(n in deps,n);return deps[n]}})
 const C=module.exports.QuestionEditor
 const saved=render(jsx.jsx(C,{roomId:42,question:{id:8,question_text:'Find the flag',hint:null,points:25},locked:false,tasks:[],onClose(){},onSaved:async()=>{}}))
 assert.match(saved,/Correct answer/);assert.match(saved,/Opening saved answer/);assert.doesNotMatch(saved,/Keep saved answer|Replace answer|answer_digest|answer_salt/)
 const fresh=render(jsx.jsx(C,{roomId:42,question:null,locked:false,onClose(){},onSaved:async()=>{}}))
 assert.match(fresh,/id="question-answer"[^>]*type="text"[^>]*required=""/)
})

test('guest SSH panel is explicit, key-first and contains no shared credential',()=>{
 const C=H.load('src/components/BuilderSshAuth.tsx',{...H.base,'@tanstack/react-query':{useQueryClient:()=>({})},'../api/axios':{},'../lib/presentation':{},'../hooks/useUnsavedChanges':{useUnsavedChanges(){}}}).default
 const html=render(jsx.jsx(C,{builder:{id:3,room_id:42,private_ip:'10.0.1.50',ssh_auth_mode:'key'},onSaved(){}}))
 assert.match(html,/SSH key \(recommended\)/);assert.match(html,/unprivileged/);assert.match(html,/authorized VPN network/)
 assert.doesNotMatch(html,/id="guest-ssh-password"|creator\.1234|default password/)
 assert.match(html,/Training account password/)
})

test('password SSH exposes explicit account creation without manual sshd editing',()=>{
 const C=H.load('src/components/BuilderSshAuth.tsx',{...H.base,'@tanstack/react-query':{useQueryClient:()=>({})},'../api/axios':{},'../lib/presentation':{},'../hooks/useUnsavedChanges':{useUnsavedChanges(){}}}).default
 const html=render(jsx.jsx(C,{builder:{id:3,room_id:42,ssh_auth_mode:'password'},onSaved(){}}))
 assert.match(html,/Create this unprivileged account if it does not exist/)
 assert.match(html,/type="checkbox" checked=""/)
 assert.match(html,/id="guest-ssh-password"[^>]*type="password"/)
 assert.match(html,/only to this training account/)
 assert.doesNotMatch(html,/<details|sudo adduser|PasswordAuthentication yes/)
})
