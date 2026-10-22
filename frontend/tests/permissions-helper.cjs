// Exercise the actual shared role/capability helper, never a duplicated mock rule.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript')
const moduleBody={exports:{}}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/lib/permissions.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{module:moduleBody,exports:moduleBody.exports})
module.exports=moduleBody.exports
