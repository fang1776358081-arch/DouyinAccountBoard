// CPU/HTML-generation lower bound, NOT browser layout, network or production
// latency. Deterministic synthetic rows only; resets data/selection/filter/page
// for every sample, and alternates frozen/current ordering.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const assert=require('node:assert/strict'),{performance}=require('node:perf_hooks');
const {harness,fixture,scripts,root}=require('./test_performance_ui.cjs');
const baseline=path.join(root,'.synthetic-tests','performance-source-baseline-20261029');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const sourceHashes=directory=>Object.fromEntries(scripts.map(name=>[name,hash(fs.readFileSync(path.join(directory,'static',name)))]));
const measuredSources={before:sourceHashes(baseline),after:sourceHashes(root)};
const measuredHarnessHash=hash(fs.readFileSync(path.join(root,'test_performance_ui.cjs')));
const percentile=(values,ratio)=>[...values].sort((a,b)=>a-b)[Math.ceil(values.length*ratio)-1];
const outputIds=['accountsBody','matrixBody','cardsBody','phonesBody'];
const outputKey={accounts:'accountsBody',board:'matrixBody',cards:'cardsBody',phones:'phonesBody'};
const describe=(page,kind)=>{
 const visible=page.element(outputKey[kind]).innerHTML;
 return {visibleCharacters:visible.length,visibleBytes:Buffer.byteLength(visible),visibleRows:(visible.match(/<tr>/g)||[]).length,visiblePublishButtons:(visible.match(/data-publish=/g)||[]).length,
  hiddenCharacters:outputIds.filter(id=>id!==outputKey[kind]).reduce((sum,id)=>sum+page.element(id).innerHTML.length,0),
  generatedCharacters:outputIds.reduce((sum,id)=>sum+page.element(id).innerHTML.length,0)};
};
const reports=[];
for(const count of [1600,5000]){
 const before=harness(baseline),after=harness(root),template=fixture(count),fixtureHash=hash(JSON.stringify(template));
 for(const kind of ['accounts','board','cards','phones']){
  const pages={before,after},samples={before:[],after:[]},html={};
  for(const label of ['before','after']){pages[label].reset(structuredClone(template),kind);pages[label].run('render()');}
  for(let round=0;round<5;round++)for(const label of round%2?['after','before']:['before','after']){
   const page=pages[label];page.reset(structuredClone(template),kind);
   const start=performance.now();page.run('render()');samples[label].push(performance.now()-start);
   if(round===0)html[label]=page.element(outputKey[kind]).innerHTML;
  }
  if(kind==='accounts')assert.equal(html.after,html.before.split('</tr>').slice(0,50).join('</tr>')+'</tr>','visible first page must preserve the same row markup');
  else assert.equal(html.after,html.before,'non-account visible markup must remain byte-identical');
  reports.push({accounts:count,resources:template.phone_devices.devices.length,computers:template.computers.length,workflowEnabled:template.device_workflow.enabled,page:kind,fixtureHash,
   before:{p50ms:+percentile(samples.before,.5).toFixed(3),p95ms:+percentile(samples.before,.95).toFixed(3),...describe(before,kind)},
   after:{p50ms:+percentile(samples.after,.5).toFixed(3),p95ms:+percentile(samples.after,.95).toFixed(3),...describe(after,kind)}});
 }
}
assert.deepEqual({before:sourceHashes(baseline),after:sourceHashes(root)},measuredSources,'source changed during the benchmark; rerun against one stable revision');
assert.equal(hash(fs.readFileSync(path.join(root,'test_performance_ui.cjs'))),measuredHarnessHash,'harness changed during benchmark');
console.log(JSON.stringify({kind:'synthetic-vm-real-render-cpu-html-only',node:process.version,harnessSha256:measuredHarnessHash,fixture:'deterministic-formula-v1-no-randomness',samplesPerScenario:5,warmups:1,p95Method:'nearest rank; 5 samples means the maximum sample, not a production percentile',
 excluded:['browser layout/paint','real DOM allocation','permission/scope UI DOM generation (shared stubs)','network','database','production latency','workflow-enabled performance (covered by semantic regression only)'],sourceHashes:measuredSources,reports},null,2));
