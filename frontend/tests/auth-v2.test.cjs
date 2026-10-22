const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript')
const React = require('react'), jsx = require('react/jsx-runtime'), router = require('react-router-dom')
const { renderToStaticMarkup } = require('react-dom/server')
function load(file, dependencies) {
  const filename = path.join(__dirname, '..', file), module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(filename,'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  vm.runInNewContext(code,{ module,exports:module.exports,URLSearchParams,require(name) { assert.ok(Object.hasOwn(dependencies,name),name);return dependencies[name] } },{filename})
  return module.exports
}
const returns = load('src/lib/returnTo.ts', {})
const base = {react:React,'react/jsx-runtime':jsx}
const Password = load('src/components/PasswordField.tsx',base).default
const artwork = {default:()=>jsx.jsx('div',{'data-art':'cyber'})}
function render(component) { return renderToStaticMarkup(jsx.jsx(router.MemoryRouter,{initialEntries:['/login'],children:jsx.jsx(component,{})})) }
test('password controls default to masked, have accessible toggle and password manager metadata',()=> {
  const html=renderToStaticMarkup(jsx.jsx(Password,{id:'password',label:'Password',value:'',onChange(){},newPassword:true}))
  assert.match(html,/type="password"/);assert.match(html,/Show password/);assert.match(html,/aria-pressed="false"/)
  assert.match(html,/autoComplete="new-password"/);assert.match(html,/minLength="12"/);assert.match(html,/maxLength="128"/)
})
test('login supports username or email, real Google control and no unsupported recovery link',()=> {
  const component=load('src/pages/Login.tsx',{...base,'react-router-dom':router,'../context/AuthContext':{useAuth:()=>({login(){}})},'../components/AuthArtwork':artwork,'../components/PasswordField':{default:Password},'../components/GoogleButton':{default:()=>jsx.jsx('button',{children:'Continue with Google'})},'../lib/returnTo': returns,'../lib/presentation':{actionError(){}}}).default
  const html=render(component);assert.match(html,/Username or email/);assert.match(html,/Continue with Google/);assert.match(html,/href="\/register"/);assert.doesNotMatch(html,/Forgot password/)
})
test('registration has confirmation, password guidance without intrusive role messaging',()=> {
  const component=load('src/pages/Register.tsx',{...base,'react-router-dom':router,'../context/AuthContext':{useAuth:()=>({register(){}})},'../components/AuthArtwork':artwork,'../components/PasswordField':{default:Password},'../components/GoogleButton':{default:()=>null},'../lib/returnTo': returns,'../lib/presentation':{actionError(){}}}).default
  const html=render(component);assert.match(html,/Email address/);assert.match(html,/Confirm password/);assert.match(html,/12–128/);assert.doesNotMatch(html,/New accounts are learners/);assert.doesNotMatch(html,/<select/)
})
test('Google button fails closed without configured OAuth and uses the backend redirect',()=> {
  for (const enabled of [false,true]) {
    const component=load('src/components/GoogleButton.tsx',{...base,'@tanstack/react-query':{useQuery:()=>({data:{google_enabled:enabled},isPending:false,isError:false})},'../lib/returnTo': returns,'../api/auth':{getAuthOptions(){},googleStartUrl(){return '/api/auth/google/start'}}}).default
    const html=render(component);assert.equal(html.includes('disabled=""'),!enabled);assert.match(html,/Continue with Google/)
  }
})
test('password change route exposes only authenticated password forms',()=> {
  const component=load('src/pages/PasswordChange.tsx',{...base,'react-router-dom':router,'../context/AuthContext':{useAuth:()=>({user:{must_change_password:true},acceptToken(){},logout(){}})},'../components/AuthArtwork':artwork,'../components/PasswordField':{default:Password},'../api/auth':{changePassword(){}},'../lib/returnTo': returns,'../lib/presentation':{actionError(){}}}).default
  const html=render(component);assert.match(html,/Replace your temporary password/);assert.match(html,/Current password/);assert.match(html,/New password/);assert.match(html,/Confirm password/)
})
test('external fonts never block interactive authentication startup',()=> {
  const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8')
  const fonts=html.match(/<link\s+href="https:\/\/fonts.googleapis.com[^>]+>/)[0]
  assert.match(fonts,/media="print"/);assert.match(fonts,/data-secforge-fonts/);assert.doesNotMatch(html,/onload=/);assert.match(html,/src="\/theme-init.js"/)
})
