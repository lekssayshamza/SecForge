const test=require('node:test'),assert=require('node:assert/strict'),React=require('react'),jsx=require('react/jsx-runtime'),{renderToStaticMarkup:render}=require('react-dom/server')
const {load}=require('./batch-a-helpers.cjs')
function fixture({user=null,isLoading=false,relationship,error=false,pending=false,username='visitor'}={}){
 const calls=[],redirects=[]
 const c=load('src/pages/CanonicalProfile.tsx',{
  react:React,'react/jsx-runtime':jsx,'react-router-dom':{useParams:()=>({username}),Navigate:({to,replace})=>{redirects.push({to,replace});return null}},
  '@tanstack/react-query':{useQuery:o=>{calls.push(o);return {data:relationship,isPending:pending,isError:error,refetch(){}}}},
  '../context/AuthContext':{useAuth:()=>({user,isLoading})},'../api/social':{getRelationship:async()=>relationship},
  './Profile':{default:()=>jsx.jsx('div',{children:'Private owner controls'})},'./Leaderboard':{PublicLearnerProfile:()=>jsx.jsx('div',{children:'Safe public identity'})},
 });return {html:render(jsx.jsx(c.default,{})),alias:()=>render(jsx.jsx(c.ProfileAlias,{})),calls,redirects}
}
test('guest canonical profile renders public view without calling private ownership API',()=>{const r=fixture();assert.match(r.html,/Safe public/);assert.doesNotMatch(r.html,/Private owner/);assert.equal(r.calls[0].enabled,false)})
test('matching client username alone cannot reveal owner controls',()=>{const r=fixture({user:{id:7,username:'visitor'},relationship:{is_self:false}});assert.match(r.html,/Safe public/);assert.doesNotMatch(r.html,/Private owner/)})
test('server-confirmed ownership reuses full existing editable profile',()=>{const r=fixture({user:{id:7,username:'owner'},relationship:{is_self:true}});assert.match(r.html,/Private owner controls/);assert.doesNotMatch(r.html,/Safe public/);assert.deepEqual(Array.from(r.calls[0].queryKey),['profile-ownership',7,'visitor'])})
test('stale local auth waits for session validation and failed ownership never grants editing',()=>{assert.match(fixture({user:{id:7},isLoading:true,relationship:{is_self:true}}).html,/Opening profile/);assert.match(fixture({user:{id:7},pending:true}).html,/Opening profile/);const r=fixture({user:{id:7},error:true});assert.match(r.html,/Private controls are unavailable/);assert.match(r.html,/Safe public/);assert.doesNotMatch(r.html,/Private owner controls/)})
test('profile alias uses authenticated username, encodes it and replaces history',()=>{const r=fixture({user:{id:7,username:'owner.name'}});r.alias();assert.deepEqual(r.redirects,[{to:'/learners/owner.name',replace:true}]);const guest=fixture();guest.alias();assert.equal(guest.redirects[0].to,'/login');const loading=fixture({isLoading:true});assert.match(loading.alias(),/Opening profile/);assert.equal(loading.redirects.length,0)})
