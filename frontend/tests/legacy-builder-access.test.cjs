const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),React=require('react'),jsx=require('react/jsx-runtime'),router=require('react-router-dom'),{renderToStaticMarkup:render}=require('react-dom/server')
const {load}=require('./batch-a-helpers.cjs')
test('settings no longer presents registration, rotation or shared private-key delivery',()=>{
 const C=load('src/pages/AccountSettings.tsx',{react:React,'react/jsx-runtime':jsx,'react-router-dom':router,'../components/SignInMethods':{default:()=>jsx.jsx('section',{children:'Secure sign-in settings'})},'../components/Icon':{default:()=>null}}).default
 const html=render(jsx.jsx(router.MemoryRouter,{children:jsx.jsx(C,{})}))
 assert.match(html,/Secure sign-in settings/);assert.doesNotMatch(html,/creator-ssh|Personal builder SSH key|Register a personal|OpenSSH public key/)
 for(const file of ['AccountSettings.tsx','RoomDetail.tsx','Administration.tsx'])assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../src/pages',file),'utf8'),/CreatorSshSettings|BuilderCreatorKey|AdminCreatorKey/)
})
test('builder PEM instructions use the server username/IP with no registration requirement',()=>{
 const C=load('src/components/ConnectionGuide.tsx',{react:React,'react/jsx-runtime':jsx,'./Icon':{default:()=>null}}).default
 const html=render(jsx.jsx(C,{address:'10.0.1.158',sshUsername:'ubuntu',builder:true,download:null}))
 assert.match(html,/ssh -i ~\/sf\/CyberRange-Key.pem ubuntu@10.0.1.158/);assert.match(html,/Port: <code>22/);assert.match(html,/Connect the creator VPN/)
 assert.doesNotMatch(html,/Register a personal|register only|secforge_creator|BEGIN.*PRIVATE KEY|download=.*pem/)
 const invalid=render(jsx.jsx(C,{address:'10.0.1.158;whoami',sshUsername:'ubuntu',builder:true,download:null}));assert.match(invalid,/SSH connection details are unavailable/);assert.doesNotMatch(invalid,/ssh -i/)
})
