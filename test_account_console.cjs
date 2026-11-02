// Exercise the actual presentation helper with a minimal movable DOM.
// No business state, fetch, database, browser profile or permissions are supplied.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'static/account-workspace.js'),'utf8');
let checks=0;
const check=(name,run)=>{run();checks++;console.log('ok '+name);};
function fixture(){
 const nodes=new Map();
 function node(id){
  const classes=new Set(),listeners=[];
  const item={id,children:[],parentElement:null,hidden:false,disabled:false,open:false,value:'original-'+id,listeners,
   classList:{toggle(name,active){if(active)classes.add(name);else classes.delete(name);},contains(name){return classes.has(name);}},
   append(child){if(child.parentElement){const old=child.parentElement.children;old.splice(old.indexOf(child),1);}this.children.push(child);child.parentElement=this;},
   addEventListener(type,listener){listeners.push([type,listener]);},
  };
  nodes.set(id,item);return item;
 }
 for(const id of ['body','newAccount','refresh','exportVisibleAccounts','lifecycleSummary','collectionBar','accountsHeaderActions','accountCreateHome','accountListActions','accountToolsHome','accountSupplementBody','accountSupplementHome','accountSupplement'])node(id);
 const get=id=>nodes.get(id),home=get('accountSupplementHome');
 home.append(get('collectionBar'));home.append(get('lifecycleSummary'));
 get('accountCreateHome').append(get('newAccount'));for(const id of ['refresh','exportVisibleAccounts'])get('accountToolsHome').append(get(id));
 const context=vm.createContext({document:{body:get('body'),addEventListener(){}},$:get});
 vm.runInContext(source,context,{filename:'account-workspace.js'});
 return {get,nodes,activate:active=>vm.runInContext('activateAccountConsole('+active+')',context)};
}
const f=fixture(),controls=['newAccount','refresh','exportVisibleAccounts','collectionBar','lifecycleSummary'],original=new Map(controls.map(id=>[id,f.get(id)]));
for(const [i,id]of controls.entries()){const node=f.get(id);node.hidden=i%2===0;node.disabled=i%2===1;node.addEventListener('click',()=>id);node.onclick=()=>id;}
const snapshot=new Map(controls.map(id=>{const node=f.get(id);return[id,{hidden:node.hidden,disabled:node.disabled,value:node.value,listeners:node.listeners,onclick:node.onclick}];}));
check('activating moves the original nodes into header, list and auxiliary disclosure',()=>{
 f.activate(true);assert.equal(f.get('body').classList.contains('accounts-console'),true);
 assert.equal(f.get('newAccount').parentElement,f.get('accountsHeaderActions'));assert.equal(f.get('refresh').parentElement,f.get('accountListActions'));assert.equal(f.get('exportVisibleAccounts').parentElement,f.get('accountListActions'));
 assert.equal(f.get('collectionBar').parentElement,f.get('accountSupplementBody'));assert.equal(f.get('lifecycleSummary').parentElement,f.get('accountSupplementBody'));
 for(const id of controls)assert.equal(f.get(id),original.get(id));
});
check('moving controls preserves hidden, disabled, values and both listener forms',()=>{
 for(const id of controls){const node=f.get(id),before=snapshot.get(id);for(const key of ['hidden','disabled','value','listeners','onclick'])assert.equal(node[key],before[key],id+':'+key);assert.equal(node.listeners[0][1](),id);assert.equal(node.onclick(),id);}
});
check('accounts-only hosts are visible without auto-opening the auxiliary disclosure',()=>{
 for(const id of ['accountsHeaderActions','accountListActions','accountSupplement'])assert.equal(f.get(id).hidden,false);
 assert.equal(f.get('accountSupplement').open,false);assert.equal(f.get('newAccount').classList.contains('primary'),true);
});
check('repeated renders do not duplicate or reorder existing controls',()=>{
 f.activate(true);f.activate(true);
 assert.deepEqual(f.get('accountsHeaderActions').children.map(node=>node.id),['newAccount']);
 assert.deepEqual(f.get('accountListActions').children.map(node=>node.id),['refresh','exportVisibleAccounts']);
 assert.deepEqual(f.get('accountSupplementBody').children.map(node=>node.id),['collectionBar','lifecycleSummary']);
});
check('leaving accounts restores the original home order and hides account-only hosts',()=>{
 f.activate(false);assert.equal(f.get('body').classList.contains('accounts-console'),false);
 assert.deepEqual(f.get('accountSupplementHome').children.map(node=>node.id),['collectionBar','lifecycleSummary']);
 assert.deepEqual(f.get('accountToolsHome').children.map(node=>node.id),['refresh','exportVisibleAccounts']);
 assert.deepEqual(f.get('accountCreateHome').children.map(node=>node.id),['newAccount']);
 for(const id of ['accountsHeaderActions','accountListActions','accountSupplement'])assert.equal(f.get(id).hidden,true);
 assert.equal(f.get('newAccount').classList.contains('primary'),false);
 for(const id of controls){const node=f.get(id),before=snapshot.get(id);for(const key of ['hidden','disabled','value','listeners','onclick'])assert.equal(node[key],before[key],id+':'+key);}
});
check('page transitions retain unrelated body classes and a disclosure chosen by the user',()=>{
 f.get('body').classList.toggle('board-fit',true);f.get('accountSupplement').open=true;
 f.activate(true);f.activate(false);f.activate(false);f.activate(true);
 assert.equal(f.get('body').classList.contains('board-fit'),true);assert.equal(f.get('accountSupplement').open,true);
 assert.deepEqual(f.get('accountListActions').children.map(node=>node.id),['refresh','exportVisibleAccounts']);
});
check('incomplete optional markup does not reset controls or throw during cleanup',()=>{
 const partial=fixture();partial.nodes.delete('accountsHeaderActions');partial.nodes.delete('accountSupplement');partial.nodes.delete('lifecycleSummary');
 assert.doesNotThrow(()=>{partial.activate(true);partial.activate(false);});assert.equal(partial.get('newAccount').parentElement,partial.get('accountCreateHome'));
});
console.log(JSON.stringify({passed:true,checks,kind:'actual-account-console-movable-synthetic-dom-no-business-state'}));
