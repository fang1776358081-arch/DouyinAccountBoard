// AB/BA real first-screen JS CPU/HTML lower bound. No HTTP, browser layout,
// production data or DB; one selected host still carries all 1600 source rows.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const {harness,fixture,root,scripts}=require('./test_publish_first_screen_frontend.cjs');
const baseline=path.join(root,'.synthetic-tests','publish-firstscreen-before-20261029-r1');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const hashes=directory=>Object.fromEntries([...new Set(scripts)].map(name=>[name,hash(fs.readFileSync(path.join(directory,'static',name)))]));
const sourceHashes={before:hashes(baseline),after:hashes(root)};
const harnessHash=hash(fs.readFileSync(path.join(root,'test_publish_first_screen_frontend.cjs')));
const sum=values=>(values||[]).reduce((total,value)=>total+value,0);
const percentile=(rows,key,percent)=>[...rows].map(row=>row[key]).sort((a,b)=>a-b)[Math.ceil(rows.length*percent)-1];
async function sample(directory,unmatched){
 const board=fixture(unmatched),page=harness(directory,{timing:true});page.selectHost(board);
 page.run('data=null;globalThis.data=null;');page.clearMetrics();
 const boardResponse=page.response(board),meResponse=page.response({user:board.user});let responseBytes=0;
 page.transport=async url=>{const response=url==='/api/auth/me'?meResponse:/^\/api\/board\?/.test(url)?boardResponse:null;assert.ok(response,'all other requests are blocked: '+url);responseBytes+=response.bytes;return response;};
 const begin=performance.now();await page.run('openBoard()');const totalMs=performance.now()-begin;
 const phases=Object.fromEntries(['index','render','matrixHTML','boardReady','cacheStringify','responseJsonParse','cacheWriteTotal'].map(key=>[key+'Ms',sum(page.metrics[key])]));
 const requestOrder=page.requests.map(request=>request.url),cacheWrites=page.writes.length;
 const matrix=page.element('matrixBody').innerHTML,pending=page.element('pendingReviewRows').innerHTML;
 // Separately profile a valid warm-cache parse. It is NOT part of cold totalMs.
 page.cache(board);page.metrics.cacheParse=[];page.run('readBoardCache()');
 return {totalMs,...phases,warmCacheParseMs:sum(page.metrics.cacheParse),requestOrder,responseBytes,cacheWrites,
  visibleHosts:(matrix.match(/class="host"/g)||[]).length,matrixCharacters:matrix.length,matrixPublishButtons:(matrix.match(/data-publish=/g)||[]).length,hiddenPendingCharacters:pending.length,
  toggleText:page.element('pendingReviewToggle').textContent,fixtureHash:hash(JSON.stringify(board))};
}
(async()=>{
 const reports=[];
 for(const unmatched of [0,50,150]){
  const rows={before:[],after:[]};await sample(baseline,unmatched);await sample(root,unmatched);
  for(let round=0;round<5;round++)for(const side of round%2?['after','before']:['before','after'])rows[side].push(await sample(side==='before'?baseline:root,unmatched));
  const report={accounts:1600,authorizedHosts:160,visibleHosts:1,nameOnlyUnmatched:unmatched,fixtureHash:rows.before[0].fixtureHash};
  assert.equal(rows.before[0].fixtureHash,rows.after[0].fixtureHash);assert.equal(rows.before[0].matrixCharacters,rows.after[0].matrixCharacters);assert.equal(rows.before[0].toggleText,rows.after[0].toggleText);
  for(const side of ['before','after']){const metrics={};for(const key of Object.keys(rows[side][0]).filter(key=>key.endsWith('Ms')))metrics[key]={p50:+percentile(rows[side],key,.5).toFixed(3),p95:+percentile(rows[side],key,.95).toFixed(3)};const {requestOrder,responseBytes,cacheWrites,visibleHosts,matrixCharacters,matrixPublishButtons,hiddenPendingCharacters,toggleText}=rows[side][0];report[side]={metrics,requestOrder,responseBytes,cacheWrites,visibleHosts,matrixCharacters,matrixPublishButtons,hiddenPendingCharacters,toggleText};}
  reports.push(report);
 }
 assert.deepEqual({before:hashes(baseline),after:hashes(root)},sourceHashes,'a measured script changed; rerun the benchmark');assert.equal(hash(fs.readFileSync(path.join(root,'test_publish_first_screen_frontend.cjs'))),harnessHash,'timing harness changed during the benchmark');
 console.log(JSON.stringify({kind:'synthetic-real-first-screen-js-cpu-html-lower-bound',node:process.version,samples:5,order:'AB/BA alternating, one warmup per side/scenario',p95:'nearest rank: maximum of five, not a production percentile',
  phaseNotes:{totalMs:'controlled cold openBoard response JSON parse + showBoard/render + all actual board-ready listeners + cache stringify; excludes server/HTTP/TLS',renderMs:'includes indexMs and matrixHTMLMs; do not sum nested phases',matrixHTMLMs:'unchanged real hosts.map RHS string generation, not layout/paint',boardReadyMs:'includes actual publishing-pending/workspace-shortcuts callbacks',warmCacheParseMs:'separate valid-cache parse, excluded from cold totalMs'},
  excluded:['real DOM/layout/paint','HTTP/network/server/database','ScopeUI and DeviceWorkflow implementation (shared existing harness stubs)','new browser profile and production state'],harnessHash,sourceHashes,reports},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
