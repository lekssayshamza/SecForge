const test=require('node:test'),assert=require('node:assert/strict'),React=require('react'),jsx=require('react/jsx-runtime'),router=require('react-router-dom'),{renderToStaticMarkup:render}=require('react-dom/server'),H=require('./discovery-helpers.cjs')
const {hasCreatorAccess,hasLearningAccess}=require('./permissions-helper.cjs')
for(const role of ['learner','creator','admin'])test(`${role} has explicit learning capability without learner privilege escalation`,()=>{assert.equal(hasLearningAccess({role}),true);assert.equal(hasCreatorAccess({role}),role!=='learner')})
test('unknown role and absent account have no learning capability',()=>{assert.equal(hasLearningAccess(null),false);assert.equal(hasLearningAccess({role:'system'}),false)})
test('social transport encodes username, bounds lists and uses desired idempotent state',async()=>{
 const calls=[],api={get:async(...v)=>(calls.push(['get',...v]),{data:{}}),request:async v=>(calls.push(['request',v]),{data:{}})}
 const C=H.load('src/api/social.ts',{'./axios':{default:api}})
 await C.getConnections('realuser');await C.getRelationship('realuser');await C.listConnections('realuser','followers',2);await C.setRelationship('../bad','follow',true);await C.setRelationship('realuser','follow',false);await C.setRelationship('realuser','block',true)
 assert.equal(calls[0][1],'/public/learners/realuser/connections');assert.equal(calls[2][2].params.page_size,20)
 assert.equal(calls[3][1].url,'/social/users/..%2Fbad/follow');assert.equal(calls[3][1].method,'PUT');assert.equal(calls[4][1].method,'DELETE');assert.equal(calls[5][1].url,'/social/users/realuser/block')
})
test('hint disclosure is a named keyboard button, collapsed initially and contains no hidden answer',()=>{
 const C=require('./context-help-helper.cjs').default,html=render(jsx.jsx(C,{label:'Hint for question 1',text:'Inspect permissions.'}))
 assert.match(html,/type="button"/);assert.match(html,/aria-label="Hint for question 1" aria-expanded="false"/);assert.match(html,/aria-controls=/);assert.doesNotMatch(html,/Inspect permissions|answer_digest|answer_salt/)
})
// Render the actual social component; only transport/query state is isolated.
function social(user,relationship){const calls=[];const C=H.load('src/components/SocialConnections.tsx',{...H.base,'react-router-dom':router,'../context/AuthContext':{useAuth:()=>({user})},'../api/social':{},'../lib/returnTo':H.load('src/lib/returnTo.ts',{}),'../lib/presentation':{actionError:()=> 'Unable to update'},'./Avatar':{default:()=>null},'./Dialog':{default:()=>null},'./ConfirmProvider':{useConfirm:()=>async()=>true},'@tanstack/react-query':{useQueryClient:()=>({}),useIsMutating:()=>0,useMutation:()=>({mutate:v=>calls.push(v)}),useQuery:q=>({data:q.queryKey[0]==='social'?relationship:{followers:2,following:1}})}}).default;return render(jsx.jsx(router.MemoryRouter,{children:jsx.jsx(C,{username:'realuser'})}))}
test('guest social profile has honest counts and safe login destination',()=>{const s=social(null);assert.match(s,/2<\/strong> Followers/);assert.match(s,/returnTo=%2Flearners%2Frealuser/);assert.doesNotMatch(s,/>Block<|email|password/)})
test('self profile provides lists without follow or block controls',()=>{const s=social({id:1,username:'realuser'},{followers:2,following:1,is_self:true});assert.match(s,/Followers/);assert.doesNotMatch(s,/>Follow<|>Block</)})
test('followed profile offers unfollow; blocked profile offers only unblock',()=>{const followed=social({id:2,username:'visitor'},{followers:2,following:1,following_user:true,can_follow:true});assert.match(followed,/aria-pressed="true"/);assert.match(followed,/>Unfollow</);const blocked=social({id:2,username:'visitor'},{followers:0,following:0,blocked:true,can_follow:false});assert.match(blocked,/>Unblock</);assert.doesNotMatch(blocked,/>Follow<|>Unfollow</)})
