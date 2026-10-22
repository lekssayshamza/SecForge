const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),jsx=require('react/jsx-runtime')
function load(file,deps,globals={}) { const module={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'..',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText,{module,exports:module.exports,...globals,require:n=>{if(n.endsWith('/ContextHelp'))return require('./context-help-helper.cjs');if(n.endsWith('/SocialConnections'))return {default:()=>null};if(!(n in deps))throw Error('Unexpected dependency '+n);return deps[n]}});return module.exports }
const base={react:React,'react/jsx-runtime':jsx},icon={default:()=>null}
const Avatar=load('src/components/Avatar.tsx',{...base,'./Icon':icon,'./AvatarImageContext':{AvatarImageContext:React.createContext(null)}})
const identity=load('src/components/ProfileIdentity.tsx',{...base,'./Avatar':Avatar})
module.exports={load,base,icon,Avatar,identity}
