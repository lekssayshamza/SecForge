const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript')
const React = require('react'), jsx = require('react/jsx-runtime'), {renderToStaticMarkup: render} = require('react-dom/server')
const H = require('./discovery-helpers.cjs'), nil = {default:()=>null}

function editor({value='', legacy=false, loading=false, failed=false, locked=false, fresh=false}={}) {
  const states=[value,value,loading,legacy,failed,0,false,'',false]
  const react={...React,useState:initial=>[states.length?states.shift():initial,()=>{}],useRef:initial=>({current:initial}),useEffect(){}}
  const source=fs.readFileSync(path.join(__dirname,'../src/components/ChallengeQuestions.tsx'),'utf8').replace('function QuestionEditor(','export function QuestionEditor(')
  const module={exports:{}}, dependencies={react,'react/jsx-runtime':jsx,'../lib/permissions':require('./permissions-helper.cjs'),'@tanstack/react-query':{},'../context/AuthContext':{},'../api/challenges':{},'../lib/presentation':{},'../lib/format':{},'./TaskEditor':nil,'./SafeMarkdown':nil,'./Icon':nil,'./ContextHelp':nil,'./ConfirmProvider':{},'../hooks/useUnsavedChanges':{useUnsavedChanges(){},confirmDiscard:()=>true},'./Dialog':{default:({children})=>jsx.jsx('section',{children})}}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{module,exports:module.exports,require:name=>{assert.ok(name in dependencies,name);return dependencies[name]}})
  return render(jsx.jsx(module.exports.QuestionEditor,{roomId:42,question:fresh?null:{id:11,question_text:'Find proof',hint:'Inspect HTTP',points:50},locked,tasks:[],onClose(){},onSaved:async()=>{}}))
}
test('recoverable correct answer is a normal prefilled text field without replacement choices',()=>{
  const html=editor({value:'syntheticflag'})
  assert.match(html,/for="question-answer">Correct answer/)
  assert.match(html,/id="question-answer"[^>]*type="text"[^>]*value="syntheticflag"/)
  assert.doesNotMatch(html,/Keep saved answer|Replace answer|Saved answer configured|one-way|legacy-answer-help/)
})
test('only genuine legacy answers use optional one-time replacement guidance',()=>{
  const html=editor({legacy:true})
  assert.match(html,/This older answer cannot be recovered/)
  const input=html.match(/<input id="question-answer"[^>]*>/)[0]
  assert.doesNotMatch(input,/required=""/);assert.match(input,/value=""/)
})
test('loading or unavailable encryption keeps answers disabled and prevents accidental saving',()=>{
  for(const values of [{loading:true},{failed:true}]){
    const html=editor(values)
    assert.match(html,/id="question-answer"[^>]*disabled=""/)
    assert.match(html,/type="submit"[^>]*disabled="">Save question/)
    assert.doesNotMatch(html,/legacy-answer-help/)
  }
})
test('legacy API grading locks remain enforced in the editor',()=>{
  const html=editor({value:'syntheticflag',locked:true})
  assert.match(html,/id="question-answer"[^>]*disabled=""/)
  assert.match(html,/id="question-points"[^>]*disabled=""/)
})
test('new questions require a correct answer',()=>{
  const html=editor({fresh:true})
  assert.match(html,/id="question-answer"[^>]*required=""/)
  assert.doesNotMatch(html,/Opening saved answer|legacy-answer-help/)
})
test('answer reads use explicit private authoring endpoint with cancellation and revision',async()=>{
  const calls=[],api=H.load('src/api/challenges.ts',{'./axios':{default:{get:async(...args)=>(calls.push(args),{data:{answer:'syntheticflag',legacy:false}})}}}),signal=new AbortController().signal
  assert.equal((await api.getEditableAnswer(42,-2,true,8,signal)).answer,'syntheticflag')
  assert.equal(calls[0][0],'/rooms/42/workspace/questions/-2/answer')
  assert.equal(calls[0][1].params.revision,8);assert.equal(calls[0][1].signal,signal)
  await api.getEditableAnswer(42,11,false,undefined,signal)
  assert.equal(calls[1][1].params.draft,false);assert.equal(calls[1][1].params.revision,undefined)
})
