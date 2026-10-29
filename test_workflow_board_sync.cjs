// Runs the real frontend scripts against synthetic DOM/storage/fetch only.
// No browser profile, HTTP server, database or external network is used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const read = name => fs.readFileSync(path.join(__dirname, 'static', name), 'utf8');
const appSource = read('app.js');
const controlledApp = appSource.replace(';openBoard();', ';');
assert.notEqual(controlledApp, appSource, 'isolate startup to control all responses');

function fixture(cursor = 10) {
  return {
    cursor, date: '2026-10-27', today: '2026-10-27',
    user: {id: 1, name: '合成管理员', csrf: 'synthetic', permissions: ['device_manage']},
    computers: [{id: 'host', name: '电脑'}], statuses: ['正常'],
    accounts: [{id: 'a', name: '账号', note: 'opened note', status: '正常', published: false, phone_device_id: 'd'}],
    phone_devices: {devices: [{id: 'd', name: '手机1'}]}, phone_cards: {cards: []},
    device_workflow: {
      enabled: true, units: [{id: 'u', device_id: 'd', active: true, status: '在用'}],
      access: [{account_id: 'a', last_unit_id: 'u', login_state: '登录受阻'}],
      placements: [], cases: [], events: [],
    },
    record_versions: {'account:a': 'opened-account', 'access:a': 'opened-access', 'unit:u': 'opened-unit', 'device:d': 'opened-device'},
  };
}

function response(payload, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => structuredClone(payload)};
}

function harness(search = '?page=phones') {
  const elements = new Map(), storage = new Map(), calls = [], windowEvents = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', hidden: false, open: false, innerHTML: '',
      classList: {add() {}, remove() {}, toggle() {}}, addEventListener() {},
      showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return null; },
    });
    return elements.get(id);
  };
  const context = {
    console, URLSearchParams, setTimeout, clearTimeout, crypto: require('node:crypto').webcrypto,
    location: {search, replace(value) { context.redirect = value; }},
    history: {replaceState() {}}, currentUser: null, accountSearchFields: [],
    document: {
      getElementById: element, addEventListener() {}, dispatchEvent() {},
      createElement() { return {}; }, body: {appendChild() {}},
    },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key),
    },
    renderVerificationFilter() {}, addEventListener: (type, handler) => windowEvents.set(type, handler),
    renders: 0, serverBoard: fixture(), updatePayload: {cursor: 10, changes: []}, updateStatus: 200,
    dailyFlags: {date: '2026-10-26', published: {a: true}},
  };
  context.window = context;
  context.respond = (url, options) => {
    if (url === '/api/auth/me') return response({user: {id: 1}});
    if (url.startsWith('/api/board-updates?')) return response(context.updatePayload, context.updateStatus);
    if (url.startsWith('/api/daily-flags?')) return response(context.dailyFlags);
    if (url.startsWith('/api/board')) return response(context.serverBoard);
    if (options?.method === 'POST') { context.savedPayload = JSON.parse(options.body); return response({ok: true}); }
    throw Error('Unexpected synthetic request: ' + url);
  };
  context.fetch = async (url, options) => {
    calls.push({url, options});
    return context.respond(url, options);
  };
  vm.createContext(context);
  for (const source of [read('edit-versions.js'), read('device-workflow.js'), controlledApp, read('board-updates.js')]) {
    vm.runInContext(source, context);
  }
  const run = source => vm.runInContext(source, context);
  run('render=()=>{renders++;};');
  return {context, element, storage, calls, windowEvents, run};
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const fullLoads = calls => calls.filter(call => /^\/api\/board(?:\?|$)/.test(call.url)).length;
const cache = page => JSON.parse(page.storage.get('douyin-board-view')).board;

(async () => {
  for (const name of ['board', 'accounts', 'cards', 'phones', 'invalid']) {
    const page = harness('?page=' + name);
    assert.equal(page.run('page'), name === 'invalid' ? 'board' : name);
  }

  const old = harness();
  const oldBoard = fixture();
  delete oldBoard.device_workflow;
  old.storage.set('douyin-board-view', JSON.stringify({userId: 1, board: oldBoard}));
  assert.equal(old.run('readBoardCache()'), null, 'pre-workflow cache must be rejected');
  await old.run('openBoard()');
  assert.equal(fullLoads(old.calls), 1, 'old cache requires a full feature payload');
  assert.equal(old.calls.some(call => call.url.startsWith('/api/board-updates?')), false);
  assert.equal(old.context.requestSerial, old.run('requestSerial'));
  assert.equal(old.context.data, old.run('data'));
  assert.equal(cache(old).device_workflow.enabled, true);

  const cached = harness();
  cached.storage.set('douyin-board-view', JSON.stringify({userId: 1, board: fixture()}));
  cached.context.updatePayload = {cursor: 11, changes: [{entity: 'account:a', after: {id: 'a', note: 'caught up'}, record_version: 'remote-account'}]};
  await cached.run('openBoard()');
  assert.equal(fullLoads(cached.calls), 0, 'valid cache should retain incremental startup');
  assert.equal(cached.context.data.accounts[0].note, 'caught up');
  assert.equal(cache(cached).cursor, 11);
  assert.equal(cache(cached).accounts[0].note, 'caught up');

  const frozen = harness();
  await frozen.run('load()');
  frozen.context.DeviceWorkflow.openAccount('a');
  const dialogHtml = frozen.element('deviceWorkflowDialog').innerHTML;
  const serial = frozen.context.requestSerial;
  const initialLoads = fullLoads(frozen.calls);
  await frozen.run('load()');
  await frozen.run('load({type:"click"})');
  assert.equal(fullLoads(frozen.calls), initialLoads, 'ordinary refresh must skip an open workflow');
  assert.equal(frozen.context.requestSerial, serial, 'skipped loads must not invalidate incremental responses');
  frozen.context.updatePayload = {cursor: 11, changes: [{entity: 'account:a', after: {id: 'a', note: 'remote note'}, record_version: 'remote-account'}]};
  await frozen.windowEvents.get('focus')();
  assert.equal(fullLoads(frozen.calls), initialLoads, 'focus keeps the incremental path');
  assert.equal(frozen.context.data.accounts[0].note, 'remote note');
  assert.equal(frozen.context.data.record_versions['account:a'], 'remote-account');
  assert.equal(frozen.run("activeEditVersions()['account:a']"), 'opened-account');
  assert.equal(frozen.element('deviceWorkflowDialog').innerHTML, dialogHtml);
  await frozen.run("api('/api/device-workflow',{action:'account_recovery',account_id:'a',recovery_state:'等待协助'})");
  assert.equal(frozen.context.savedPayload._expected_versions['account:a'], 'opened-account');
  assert.equal(frozen.context.savedPayload._expected_versions['unit:u'], 'opened-unit');
  await frozen.run('load(true)');
  assert.equal(fullLoads(frozen.calls), initialLoads + 1, 'successful workflow save can force a full reload');

  const pending = harness();
  await pending.run('load()');
  const before = pending.context.data, savedCache = pending.storage.get('douyin-board-view');
  let resolveBoard;
  pending.context.respond = () => new Promise(resolve => { resolveBoard = resolve; });
  const inFlight = pending.run('load()');
  await flush();
  pending.context.DeviceWorkflow.openAccount('a');
  resolveBoard(response({...fixture(20), date: '2026-10-28'}));
  await inFlight;
  assert.equal(pending.context.data, before, 'in-flight full load must preserve the opened workflow state');
  assert.equal(pending.storage.get('douyin-board-view'), savedCache);

  const daily = harness();
  await daily.run('load()');
  let resolveStale;
  const respond = daily.context.respond;
  daily.context.respond = (url, options) => /^\/api\/board(?:\?|$)/.test(url)
    ? new Promise(resolve => { resolveStale = resolve; }) : respond(url, options);
  const staleLoad = daily.run('load()');
  await flush();
  await daily.run("loadPublished('2026-10-26')");
  resolveStale(response({...fixture(99), date: '2026-10-29'}));
  await staleLoad;
  assert.equal(daily.context.data.date, '2026-10-26');
  assert.equal(daily.context.data.accounts[0].published, true);
  assert.equal(daily.context.data.cursor, 10, 'daily flags must not replace unrelated board state');
  assert.equal(daily.context.requestSerial, daily.run('requestSerial'));
  assert.equal(cache(daily).date, '2026-10-26');
  assert.equal(daily.calls.some(call => call.url === '/api/daily-flags?date=2026-10-26&versions=1'), true);

  for (const prefix of ['unit', 'placement', 'access', 'case', 'workflow_event']) {
    const page = harness();
    await page.run('load()');
    page.context.DeviceWorkflow.openAccount('a');
    page.context.updatePayload = {cursor: 12, changes: [
      {entity: 'account:a', after: {id: 'a', note: 'must wait'}, record_version: 'remote-account'},
      {entity: prefix + ':new', after: {id: 'new'}, record_version: 'remote-workflow'},
    ]};
    await page.run('catchUpBoard()');
    assert.equal(fullLoads(page.calls), 1, prefix + ' reload waits for the editor');
    assert.equal(page.context.data.cursor, 10, prefix + ' update must remain retryable');
    assert.equal(page.context.data.accounts[0].note, 'opened note', 'mixed payload must not partially apply');
    assert.equal(page.run("activeEditVersions()['account:a']"), 'opened-account');
    page.element('deviceWorkflowDialog').close();
    page.context.serverBoard = fixture(12);
    await page.run('catchUpBoard()');
    assert.equal(fullLoads(page.calls), 2, prefix + ' change must fetch complete workflow relations');
    assert.equal(page.context.data.cursor, 12);
  }

  for (const mode of ['reload', '404']) {
    const page = harness();
    await page.run('load()');
    page.context.DeviceWorkflow.openAccount('a');
    page.context.updatePayload = {reload: true, cursor: 12};
    page.context.updateStatus = mode === '404' ? 404 : 200;
    await page.run('catchUpBoard()');
    assert.equal(fullLoads(page.calls), 1, mode + ' fallback must protect workflows');
    assert.equal(page.context.data.cursor, 10);
    page.element('deviceWorkflowDialog').close();
    await page.run('catchUpBoard()');
    assert.equal(fullLoads(page.calls), 2);
  }

  const editor = harness();
  await editor.run('load()');
  editor.element('editor').open = true;
  editor.context.updatePayload = {cursor: 12, changes: [{entity: 'unit:new', after: {id: 'new'}}]};
  await editor.run('catchUpBoard()');
  assert.equal(fullLoads(editor.calls), 1, 'existing editors also defer workflow-triggered full reloads');
  assert.equal(editor.context.data.cursor, 10);

  console.log('PASS: workflow-safe cached/incremental board sync, frozen edit versions, full-load races, daily flags, workflow entity reloads and focus/404 guards (synthetic VM)');
})().catch(error => { console.error(error); process.exitCode = 1; });
