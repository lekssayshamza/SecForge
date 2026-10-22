const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript')
const React = require('react'), jsx = require('react/jsx-runtime'), router = require('react-router-dom')
const { renderToStaticMarkup } = require('react-dom/server')
function load(file, dependencies={}, globals={}) {
  const module={exports:{}}; const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'..',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText
  vm.runInNewContext(code,{module,exports:module.exports,URLSearchParams,...globals,require(name){assert.ok(Object.hasOwn(dependencies,name),`Unexpected dependency ${name}`);return dependencies[name]}})
  return module.exports
}
function returns() {
  const data=new Map();return {data,...load('src/lib/returnTo.ts',{}, {sessionStorage:{setItem:(k,v)=>data.set(k,v),getItem:k=>data.get(k),removeItem:k=>data.delete(k)}})}
}
const R=returns(), base={react:React,'react/jsx-runtime':jsx}
const ROOM={id:42,name:'Published Linux',description:'A safe public briefing.',category:'Linux',difficulty:'easy'}
const empty={items:[],categories:[],total:0,page:1,page_size:24}
function page(data,detail=false,error=false) {
  const queries=[]
  const component=load('src/pages/PublicRooms.tsx',{...base,'react-router-dom':{...router,useParams:()=>({roomId:'42'})},'@tanstack/react-query':{useQuery:o=>{queries.push(o);return {data,isPending:false,isError:error}}},'../components/DiscoveryCatalog':require('./discovery-helpers.cjs').discovery(null,data,queries,error),'../components/BookmarkButton':{default:()=>null},'../lib/categories':require('./discovery-helpers.cjs').categories,'../hooks/useDiscoveryState':require('./discovery-helpers.cjs').state,'../api/rooms':{getPublicRoom(){},listPublicRooms(){}},'../components/Icon':{default:()=>null},'../lib/topics':{roomTopic:()=> 'system',topicIcon:()=> 'terminal'},'../lib/returnTo':R})
  return {html:renderToStaticMarkup(jsx.jsx(router.MemoryRouter,{children:jsx.jsx(detail?component.PublicRoomDetail:component.PublicCatalog,{})})),queries}
}
test('empty guest catalog is welcoming without fabricated rooms or required login',()=> {
  const {html}=page(empty);assert.match(html,/New cybersecurity rooms are coming soon/);assert.match(html,/href="\/register"/);assert.doesNotMatch(html,/Launch|private_ip|ami-/)
})
test('public cards and filters use safe metadata and actual public routes',()=> {
  const {html,queries}=page({...empty,items:[ROOM],total:1,categories:['Linux']})
  assert.match(html,/href="\/rooms\/42"/);assert.match(html,/Published Linux/);assert.match(html,/Search rooms/);assert.match(html,/Filter by difficulty/);assert.match(html,/Filter by category/);assert.match(html,/maxLength="100"/)
  assert.equal(queries[0].queryKey[0],'discovery');assert.equal(queries[0].queryKey[1],'rooms');assert.equal(queries[0].queryKey[2],null)
})
test('public detail exposes a briefing and login prompts, no tasks, VPN or lab mutation controls',()=> {
  const {html}=page(ROOM,true)
  assert.match(html,/safe public briefing/);assert.match(html,/Log in to start this room/)
  assert.match(html,/href="\/login\?returnTo=%2Frooms%2F42"/);assert.match(html,/href="\/register\?returnTo=%2Frooms%2F42"/)
  assert.doesNotMatch(html,/Submit answer|Download VPN|Stop lab|private_ip|ami-|answer_digest/)
})
test('missing or unpublished guest detail has no login-to-launch action',()=> {
  const {html}=page(undefined,true,true);assert.match(html,/Room unavailable/);assert.doesNotMatch(html,/Log in to start|returnTo=/)
})
test('safe destinations reject external URLs, encoded bypasses, loops and oversized IDs',()=> {
  for (const value of ['https://evil.invalid','//evil.invalid','/\\evil.invalid','/rooms/42?next=https://evil.invalid','/%2f%2fevil.invalid','/rooms/0','/rooms/2147483648','/auth/google','/login','javascript:alert(1)',42,null]) assert.equal(R.safeReturnTo(value),null)
  assert.equal(R.safeReturnTo('/rooms/42'),'/rooms/42');assert.equal(R.authDestination('?returnTo=https://evil.invalid'),'/')
})
test('OAuth return destination survives provider navigation and is consumed once',()=> {
  const r=returns();r.rememberReturnTo('/rooms/42');assert.equal(r.consumeReturnTo(),'/rooms/42');assert.equal(r.consumeReturnTo(),'/')
  r.data.set('secforge.auth.returnTo','//evil.invalid');assert.equal(r.consumeReturnTo(),'/')
})
function findForm(element) {
  if (!element || typeof element!=='object') return null
  if(element.type==='form')return element
  for(const child of [element.props?.children].flat(Infinity)){const match=findForm(child);if(match)return match}
}
function authDeps(r,navigate,user,states) {
  return {'react':{...React,useState:initial=>[states?.length?states.shift():initial,()=>{}]},'react/jsx-runtime':jsx,'react-router-dom':{...router,useNavigate:()=>navigate,useLocation:()=>({search:'?returnTo=%2Frooms%2F42'})},'../context/AuthContext':{useAuth:()=>({login:async()=>user,register:async()=>user,acceptToken:async()=>user,user})},'../lib/returnTo':r,'../components/AuthArtwork':{default:()=>null},'../components/PasswordField':{default:()=>null},'../components/GoogleButton':{default:()=>null},'../lib/presentation':{actionError:()=> 'Safe error'}}
}
test('password login returns to the selected room without launching anything',async()=> {
  const r=returns(),nav=[];const C=load('src/pages/Login.tsx',authDeps(r,(...args)=>nav.push(args),{role:'learner'},[])).default
  await findForm(C()).props.onSubmit({preventDefault(){}});assert.equal(nav[0][0],'/rooms/42');assert.equal(r.consumeReturnTo(),'/')
})
test('registration returns to the selected published room',async()=> {
  const r=returns(),nav=[],states=['synthetic','synthetic@example.com','synthetic-test-password','synthetic-test-password','',false]
  const C=load('src/pages/Register.tsx',authDeps(r,(...args)=>nav.push(args),{role:'learner'},states)).default
  await findForm(C()).props.onSubmit({preventDefault(){}});assert.equal(nav[0][0],'/rooms/42')
})
test('temporary password change preserves the selected room until completed',async()=> {
  const r=returns(),nav=[];const C=load('src/pages/Login.tsx',authDeps(r,(...args)=>nav.push(args),{must_change_password:true},[])).default
  await findForm(C()).props.onSubmit({preventDefault(){}});assert.equal(nav[0][0],'/password');assert.equal(r.consumeReturnTo(),'/rooms/42')
})
test('explicit Google linking keeps a confirmation and returns to account settings',async()=> {
  const r=returns(),nav=[];r.rememberReturnTo('/rooms/42')
  const deps=authDeps(r,(...args)=>nav.push(args),{role:'learner'},[])
  deps.react={...deps.react,useRef:()=>({current:false}),useEffect(){},useCallback:fn=>fn}
  const C=load('src/pages/GoogleComplete.tsx',{...deps,'@tanstack/react-query':{useQuery:()=>({data:{choose_username:false,linking:true},isPending:false,isFetchedAfterMount:true,isFetching:false})},'../api/auth':{getGoogleStatus(){},completeGoogle:async()=>({access_token:'synthetic'})}}).default
  await findForm(C()).props.onSubmit({preventDefault(){}});await new Promise(resolve=>setImmediate(resolve));assert.equal(nav[0][0],'/settings');assert.equal(r.consumeReturnTo(),'/')
})

test('protected account and creator destinations use exact safe application routes',()=>{for(const value of ['/settings','/paths/mine','/paths/42/edit','/creator/analytics/rooms/42','/admin/infrastructure'])assert.equal(R.safeReturnTo(value),value);for(const value of ['/admin/unknown','/paths/42/edit?next=//evil.invalid','/creator/analytics/rooms/2147483648','/paths/42/edit/extra'])assert.equal(R.safeReturnTo(value),null)})
