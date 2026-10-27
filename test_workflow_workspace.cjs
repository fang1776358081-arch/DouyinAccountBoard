// Synthetic workspace integration checks; no browser, network, server or database.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const versionSource = fs.readFileSync(path.join(__dirname, 'static/edit-versions.js'), 'utf8');
const workspaceSource = fs.readFileSync(path.join(__dirname, 'static/workspace.js'), 'utf8');
// Drive initial loading explicitly so every response is controlled by the test.
const controlledSource = workspaceSource.replace(/^reload\(\)\.then\(.*$/m, '');
assert.notEqual(controlledSource, workspaceSource, 'workspace bootstrap must be isolated');

function boardFixture() {
  return {
    date: '2026-10-26', today: '2026-10-26',
    user: {name: '合成员工', role: 'staff', permissions: []}, offices: [], accounts: [],
    phone_devices: {devices: []}, phone_cards: {cards: []},
    device_workflow: {enabled: true, units: [], access: [], placements: [], cases: []},
    record_versions: {},
  };
}

function harness(search = '?tab=todo') {
  const listeners = [], elements = new Map(), calls = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'workDate' ? '2026-10-26' : '', hidden: false, textContent: '', open: false,
    });
    return elements.get(id);
  };
  const context = {
    URLSearchParams, console, setTimeout, crypto: require('node:crypto').webcrypto,
    location: {search, replace(value) { context.redirect = value; }},
    history: {replaceState(...args) { context.historyPath = args[2]; }},
    document: {
      getElementById: element,
      addEventListener(type, fn, capture) { listeners.push({type, fn, capture}); },
      querySelectorAll() { return []; },
    },
    workflowOpen: false, renderCount: 0, nextBoard: boardFixture(),
  };
  context.window = context;
  context.fetch = async (url, options) => {
    calls.push({url, options});
    const result = structuredClone(context.nextBoard);
    const view = new URL(url, 'http://synthetic').searchParams.get('view');
    if (view) result.workspace_view = view === 'work' ? 'summary' : 'detail';
    return {ok: true, status: 200, json: async () => result};
  };
  context.DeviceWorkflow = {isOpen: () => context.workflowOpen};
  // These public-entry stubs expose the snapshot DeviceWorkflow takes at open.
  // Real version expansion, request preparation, loading and event handlers run below.
  for (const kind of ['Device', 'Card', 'Account', 'Case']) {
    context.DeviceWorkflow['open' + kind] = id => {
      context.opened = {kind, id};
      context.snapshot = vm.runInContext('({...data.record_versions})', context);
      context.workflowOpen = true;
    };
  }
  vm.createContext(context);
  vm.runInContext(versionSource, context);
  vm.runInContext(controlledSource, context);
  vm.runInContext('render=()=>{renderCount++;};', context);
  return {context, calls, element, listeners, run: source => vm.runInContext(source, context)};
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const requestUrl = request => new URL(request.url, 'http://synthetic');

(async () => {
  for (const tab of ['todo', 'nurture', 'accounts', 'positions', 'history', 'exchange', 'backups', 'unknown']) {
    const page = harness('?tab=' + tab);
    assert.equal(page.run('tab'), tab === 'unknown' ? 'todo' : tab);
    await page.run('reload()');
    const detail = ['nurture', 'accounts', 'positions', 'history', 'exchange'].includes(tab);
    assert.equal(requestUrl(page.calls.at(-1)).searchParams.get('view'), detail ? 'detail' : 'work');
    assert.equal(page.element('workspaceBackupNav').hidden, true);
  }

  const {context, calls, element, listeners, run} = harness();
  await run('reload()');
  const navigate = listeners.find(item => item.type === 'click' && !item.capture).fn;
  navigate({target: {closest(selector) {
    return selector === '[data-work-tab]' ? {dataset: {workTab: 'accounts'}} : null;
  }}});
  await flush();
  assert.equal(requestUrl(calls.at(-1)).searchParams.get('view'), 'detail');
  assert.equal(context.historyPath, '?tab=accounts');
  assert.equal(run('data.workspace_view'), 'detail');

  const board = boardFixture();
  board.accounts = [{id: 'a', phone_device_id: 'd'}];
  board.phone_devices.devices = [{id: 'd', name: '手机1'}];
  board.phone_cards.cards = [{id: 1}];
  board.device_workflow.units = [{id: 'u', device_id: 'd', original_device_id: 'd', active: true}];
  board.device_workflow.access = [{account_id: 'a', last_unit_id: 'u'}];
  board.device_workflow.placements = [{card_id: 1, unit_id: 'u'}];
  board.device_workflow.cases = [{id: 'c', unit_id: 'u', account_ids: ['a']}];
  for (const key of ['device:d', 'unit:u', 'account:a', 'access:a', 'card:1', 'placement:1', 'case:c']) {
    board.record_versions[key] = 'opened-' + key;
  }
  context.nextBoard = board;
  const open = listeners.find(item => item.type === 'click' && item.capture).fn;
  for (const [kind, id] of [['Device', 'd'], ['Card', '1'], ['Account', 'a'], ['Case', 'c']]) {
    context.workflowOpen = false;
    let stopped = false;
    open({
      target: {closest() { return {dataset: {['workflow' + kind]: id}}; }},
      stopPropagation() { stopped = true; },
    });
    await flush();
    assert.equal(stopped, true);
    assert.equal(requestUrl(calls.at(-1)).searchParams.has('view'), false);
    assert.deepEqual(context.opened, {kind, id});
    assert.equal(context.snapshot['unit:u'], 'opened-unit:u');
    assert.equal(run('data.workspace_view'), 'full');
  }

  const loadedCalls = calls.length;
  const rendered = context.renderCount;
  await run('reload()');
  await run('ensureDetail()');
  assert.equal(calls.length, loadedCalls, 'open workflow must prevent ordinary reload');
  assert.equal(context.renderCount, rendered);
  open({target: {closest() { return {dataset: {workflowAccount: 'a'}}; }}, stopPropagation() {
    throw Error('internal workflow navigation must retain its existing snapshot');
  }});
  await flush();
  assert.equal(calls.length, loadedCalls);

  context.fetch = async (url, options) => {
    calls.push({url, options});
    assert.equal(options?.method, 'POST', 'frozen versions must not be fetched again');
    context.savedPayload = JSON.parse(options.body);
    return {ok: true, status: 200, json: async () => ({ok: true})};
  };
  run("data.record_versions['unit:u']='changed-after-open'");
  await run("send('/api/device-workflow',{action:'unit_status',unit_id:'u',status:'送修'},snapshot)");
  assert.equal(context.savedPayload._expected_versions['unit:u'], 'opened-unit:u');
  assert.equal(context.savedPayload._expected_versions['access:a'], 'opened-access:a');
  assert.equal(context.savedPayload._expected_versions['placement:1'], 'opened-placement:1');
  assert.equal(context.savedPayload._expected_versions['case:c'], 'opened-case:c');

  context.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok: true, status: 200, json: async () => structuredClone(board)};
  };
  await run('reload(true)');
  assert.equal(requestUrl(calls.at(-1)).searchParams.has('view'), false);
  assert.equal(run('data.workspace_view'), 'full');
  assert.equal(context.renderCount, rendered + 1);

  context.workflowOpen = false;
  let resolveJson;
  context.fetch = async () => ({
    ok: true, status: 200, json: () => new Promise(resolve => { resolveJson = resolve; }),
  });
  const pending = run('loadBoard(false)');
  await flush();
  context.workflowOpen = true;
  resolveJson({...boardFixture(), date: '2026-10-27', workspace_view: 'summary'});
  assert.equal(await pending, false, 'in-flight reload must not replace an opened editor');
  assert.equal(run('data.date'), '2026-10-26');

  context.workflowOpen = false;
  run('data.record_versions={}');
  context.batchSnapshot = {'account:0': 'frozen'};
  const batches = [];
  context.fetch = async url => {
    const keys = requestUrl({url}).searchParams.get('keys').split(',');
    batches.push(keys);
    return {ok: true, status: 200, json: async () => ({
      record_versions: Object.fromEntries(keys.map(key => [key, 'latest-' + key])),
    })};
  };
  await run("ensureVersions('/api/bulk-status',{ids:Array.from({length:162},(_,i)=>String(i))},batchSnapshot)");
  assert.deepEqual(batches.map(keys => keys.length), [80, 80, 1]);
  assert.equal(context.batchSnapshot['account:0'], 'frozen');
  assert.equal(Object.keys(context.batchSnapshot).length, 162);

  context.fetch = async () => ({ok: false, status: 401});
  assert.equal(await run('loadBoard(false)'), false);
  assert.equal(context.redirect, '/login.html');
  assert.equal(element('workspaceError').textContent, '');
  console.log('PASS: workspace work/detail/full routing, frozen workflow versions, relation keys, refresh guards, 80-key batches and 401 redirect (synthetic VM)');
})().catch(error => { console.error(error); process.exitCode = 1; });
