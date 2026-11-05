// Actual pilot shell and update adapter with synthetic DOM/fetch only.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=name=>fs.readFileSync(__dirname+'/static/'+name,'utf8');
class Node {
 constructor(id=''){this.id=id;this.childNodes=[];this.parentElement=null;this.hidden=false;this.inert=false;this.attributes={};this.value='';this.classList={toggle(){}};}
 append(...nodes){for(const node of nodes){if(node.parentElement)node.parentElement.childNodes=node.parentElement.childNodes.filter(item=>item!==node);this.childNodes.push(node);node.parentElement=this;}}
 replaceChildren(...nodes){for(const node of this.childNodes)node.parentElement=null;this.childNodes=[];this.append(...nodes);}
 set innerHTML(value){this.html=value;this.replaceChildren();}get innerHTML(){return this.html||'';}
 setAttribute(key,value){this.attributes[key]=value;}
 remove(){if(this.parentElement)this.parentElement.childNodes=this.parentElement.childNodes.filter(item=>item!==this);this.parentElement=null;}
 querySelector(){return null;}closest(){return null;}
}
let checks=0;
function shell(office='three',role='owner'){
 const nav=new Node(),header=new Node(),legacy=new Node('permissionsNav');legacy.value='original-node';nav.append(legacy);
 const collectionBar=new Node('collectionBar'),collectionActions=new Node();collectionActions.hidden=true;collectionBar.querySelector=selector=>selector==='.collection-actions'?collectionActions:null;
 const nodes=new Map([['date',new Node('date')],['today',new Node('today')],['collectionBar',collectionBar]]),reads=[];
 const find=(node,id)=>node.id===id?node:node.childNodes.map(child=>find(child,id)).find(Boolean);
 const context={console,URL,URLSearchParams,location:{search:'?page=accounts&office='+office,origin:'http://synthetic'},activeOffice:office,page:'accounts',data:{user:{id:1,role,permissions:['history_view'],scope_mode:'all'},offices:[{id:'three'}],collection:{enabled:false}},fetch:async url=>{reads.push(url);return {ok:true,json:async()=>({pilot:{state:'enabled'}})};},document:{body:new Node(),getElementById:id=>nodes.get(id)||find(nav,id)||find(header,id)||null,querySelector:selector=>selector==='.side-nav'?nav:selector==='main > header'?header:null,createElement:()=>new Node(),addEventListener(){}}};context.window=context;vm.createContext(context);vm.runInContext(source('pilot-console.js'),context);return {context,nav,header,legacy,reads,collectionBar,collectionActions,run:code=>vm.runInContext(code,context)};
}
async function main(){
 const other=shell('one');other.run('PilotConsole.refresh()');assert.equal(other.reads.length,0);assert.equal(other.nav.childNodes[0],other.legacy);checks++;
 const owner=shell();owner.run('PilotConsole.refresh()');assert.match(owner.nav.innerHTML,/今日工作/);assert.match(owner.nav.innerHTML,/账号中心/);assert.match(owner.nav.innerHTML,/表格同步/);assert.match(owner.nav.innerHTML,/资料与设备/);assert.match(owner.nav.innerHTML,/管理工具/);assert.equal(owner.run('document.getElementById("permissionsNav").value'),'original-node');assert.equal(owner.legacy.parentElement.inert,true);checks++;
 assert.equal(owner.collectionBar.hidden,false);assert.equal(owner.collectionActions.hidden,false);assert.equal(owner.context.data.collection.enabled,false);owner.run('page="cards";PilotConsole.refresh()');assert.equal(owner.collectionBar.hidden,true);owner.run('page="board";PilotConsole.refresh()');assert.equal(owner.collectionBar.hidden,false);assert.equal(owner.collectionActions.hidden,false);owner.run('page="accounts";PilotConsole.refresh()');checks++;
 owner.run('PilotConsole.refresh()');assert.equal(owner.reads.length,1);owner.run('activeOffice="one";PilotConsole.refresh()');assert.equal(owner.nav.childNodes[0],owner.legacy);assert.equal(owner.header.childNodes.length,0);checks++;
 const staff=shell('three','employee');staff.run('PilotConsole.refresh()');assert.doesNotMatch(staff.nav.innerHTML,/表格同步/);await new Promise(resolve=>setImmediate(resolve));assert.match(staff.nav.innerHTML,/表格同步/);checks++;
 // Source-only notices advance the cursor and read only the narrow metadata.
 const reads=[],state={date:'2026-11-04',cursor:1,accounts:[{id:'synthetic-id',name:'合成名称'}],record_versions:{'account:synthetic-id':'original-version'}};
 let narrow=0,renders=0;
 const context={console,AbortController,URLSearchParams,data:state,page:'accounts',activeOffice:'three',location:{search:'?office=three'},document:{activeElement:null,querySelector(){return null;},getElementById(){return null;}},AccountPilot:{async refreshSources(){narrow++;}},fetch:async url=>{reads.push(url);return {ok:true,status:200,json:async()=>({cursor:2,changes:[{entity:'account-source:synthetic-id',after:{account_id:'synthetic-id',revision:2}}]})};},render(){renders++;}};context.window=context;vm.createContext(context);vm.runInContext(source('board-updates.js'),context);const result=await vm.runInContext('catchUpBoard()',context);assert.equal(result.nextDelayMs,12000);assert.equal(reads.length,1);assert.doesNotMatch(reads[0],/wait=5/);assert.equal(narrow,1);assert.equal(renders,0);assert.equal(state.cursor,2);assert.equal(state.record_versions['account:synthetic-id'],'original-version');checks++;
 context.AccountInline={hasDraft:()=>true};assert.equal((await vm.runInContext('catchUpBoard()',context)).status,'deferred');assert.equal(reads.length,1);checks++;
 context.AccountInline.hasDraft=()=>false;context.AccountPilot.enabled=()=>true;context.AccountPilot.refreshSources=async()=>false;
 await assert.rejects(vm.runInContext('catchUpBoard()',context),/来源字段同步失败/);assert.equal(state.cursor,2);assert.equal(renders,0);checks++;
 console.log(JSON.stringify({passed:true,checks,kind:'actual-pilot-shell-and-source-delta-synthetic-dom-only'}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
