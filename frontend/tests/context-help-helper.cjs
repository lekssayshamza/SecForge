const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),jsx=require('react/jsx-runtime')
const moduleBody={exports:{}}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/components/ContextHelp.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{module:moduleBody,exports:moduleBody.exports,require:n=>({'react':React,'react/jsx-runtime':jsx}[n])})
module.exports=moduleBody.exports
