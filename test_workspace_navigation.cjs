// Real workspace rendering with controlled DOM/fetch; no network or database.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const read = file => fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
const workspace = read('workspace.js');
// Start reload explicitly, with the same error handling as the page bootstrap.
const controlledWorkspace = workspace.replace(/^reload\(\)\.then\(.*$/m, '');
assert.notEqual(controlledWorkspace, workspace, 'isolate the automatic bootstrap');

function board(view = 'summary', date = '2026-10-27') {
  const nurture = {records: [], today: date};
  if (view === 'detail') nurture.verification_states = ['待检查'];
  return {
    date, today: date, workspace_view: view,
    user: {id: 1, name: '合成员工', role: 'staff', permissions: []}, offices: [],
    computers: [], accounts: [], phone_cards: {cards: []}, phone_devices: {devices: []},
    publishing_layout: {enabled: false, cells: []}, nurture,
    device_workflow: {enabled: false, units: [], todos: []}, record_versions: {},
  };
}

function harness(search = '?tab=todo') {
  const fields = new Map(), listeners = [], requests = [], renders = [];
  let content = '<p>正在读取共享资料…</p>';
  const field = id => {
    if (!fields.has(id)) {
      const item = {
        value: id === 'workDate' ? '2026-10-27' : id.endsWith('Office') ? 'all' : '',
        textContent: '', hidden: false, open: false, innerHTML: '',
        querySelectorAll() { return []; },
      };
      if (id === 'workspaceBody') Object.defineProperty(item, 'innerHTML', {
        get() { return content; },
        set(value) { content = value; renders.push(value); },
      });
      fields.set(id, item);
    }
    return fields.get(id);
  };
  const context = {
    console, URLSearchParams,
    location: {search, replace(value) { context.redirect = value; }},
    history: {replaceState(...args) { context.lastUrl = args[2]; }},
    document: {
      getElementById: field, querySelectorAll() { return []; },
      addEventListener(type, callback, capture) { listeners.push({type, callback, capture}); },
    },
    fetch(url) {
      return new Promise((resolve, reject) => requests.push({url, resolve, reject, settled: false}));
    },
  };
  context.window = context;
  vm.createContext(context);
  for (const [name, source] of [
    ['workspace-model.js', read('workspace-model.js')],
    ['nurture.js', read('nurture.js')],
    ['workspace.js', controlledWorkspace],
  ]) vm.runInContext(source, context, {filename: 'static/' + name});
  const run = source => vm.runInContext(source, context);
  const navigation = listeners.find(item => item.type === 'click' && !item.capture
    && item.callback.toString().includes('[data-work-tab]')).callback;
  const navigate = tab => navigation({target: {closest(selector) {
    return selector === '[data-work-tab]' ? {dataset: {workTab: tab}} : null;
  }}});
  const respondRaw = (index, value) => {
    const request = requests[index];
    assert.ok(request && !request.settled, 'expected an outstanding synthetic request ' + index);
    request.settled = true;
    request.resolve(value);
  };
  const respond = (index, value) => {
    const request = requests[index];
    assert.ok(request && !request.settled, 'expected an outstanding synthetic request ' + index);
    const params = new URL(request.url, 'http://synthetic').searchParams;
    respondRaw(index, {ok: true, status: 200, json: async () => value
      || board(params.get('view') === 'detail' ? 'detail' : 'summary', params.get('date') || '2026-10-27')});
  };
  const reject = (index, message) => {
    const request = requests[index];
    assert.ok(request && !request.settled, 'expected an outstanding synthetic request ' + index);
    request.settled = true;
    request.reject(Error(message));
  };
  return {context, field, requests, renders, run, navigate, respond, respondRaw, reject,
    start: () => run('reload().catch(fail)')};
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const error = page => page.field('workspaceError').textContent;
const body = page => page.field('workspaceBody').innerHTML;
const noRuntimeError = page => assert.doesNotMatch(error(page), /Cannot read properties|TypeError/);
const tests = [];
const test = (name, run) => tests.push({name, run});

test('pending initial load can switch to todo without dereferencing null', async () => {
  const page = harness('?tab=nurture');
  const initial = page.start();
  assert.equal(page.run('data'), null);
  page.navigate('todo');
  await flush();
  assert.equal(error(page), '', 'switching before the first response must preserve the loading state');
  // A navigation may reuse startup or issue its own summary read. Resolve the
  // newest request first, then let the original startup response arrive late.
  for (let index = page.requests.length - 1; index >= 0; index--) {
    page.respond(index);
    await flush();
  }
  await initial;
  await flush();
  assert.match(body(page), /工作待办/);
  noRuntimeError(page);
});

test('failed initial load keeps navigation safe and refresh can recover', async () => {
  const page = harness('?tab=nurture');
  const initial = page.start();
  page.reject(0, 'synthetic initial failure');
  await initial;
  assert.match(error(page), /synthetic initial failure/);
  page.navigate('todo');
  await flush();
  noRuntimeError(page);
  const requestsBeforeRetry = page.requests.length;
  const retry = page.field('reloadWorkspace').onclick();
  assert.equal(page.requests.length, requestsBeforeRetry + 1);
  page.respond(page.requests.length - 1);
  await retry;
  for (let index = 1; index < page.requests.length; index++) {
    if (!page.requests[index].settled) page.respond(index);
  }
  await flush();
  assert.equal(error(page), '');
  assert.match(body(page), /工作待办/);
});

test('late summary response cannot replace newer detail or break nurture rendering', async () => {
  const page = harness();
  const initial = page.start();
  page.navigate('nurture');
  assert.equal(page.requests.length, 2);
  page.respond(1);
  await flush();
  assert.equal(page.run('data.workspace_view'), 'detail');
  assert.match(body(page), /7日养号/);
  page.respond(0);
  await initial;
  await flush();
  assert.equal(error(page), '');
  assert.equal(page.run('data.workspace_view'), 'detail');
  assert.match(body(page), /7日养号/);
});

test('failure of an older request must not overwrite a newer successful page', async () => {
  const page = harness();
  const initial = page.start();
  page.navigate('nurture');
  page.respond(1);
  await flush();
  const successfulBody = body(page);
  page.reject(0, 'synthetic stale failure');
  await initial;
  await flush();
  assert.equal(error(page), '');
  assert.equal(body(page), successfulBody);
  assert.equal(page.run('data.workspace_view'), 'detail');
});

for (const status of [500, 401]) test('stale HTTP ' + status + ' cannot replace success or redirect', async () => {
  const page = harness();
  const initial = page.start();
  page.navigate('nurture');
  page.respond(1);
  await flush();
  page.respondRaw(0, {ok: false, status});
  await initial;
  assert.equal(error(page), '');
  assert.equal(page.context.redirect, undefined);
  assert.equal(page.run('data.workspace_view'), 'detail');
  assert.match(body(page), /7日养号/);
});

test('JSON rejection becoming stale during parsing cannot overwrite newer success', async () => {
  const page = harness();
  const initial = page.start();
  let rejectJson, parsing = false;
  page.respondRaw(0, {ok: true, status: 200, json() {
    parsing = true;
    return new Promise((resolve, reject) => { rejectJson = reject; });
  }});
  await flush();
  assert.equal(parsing, true, 'older response must reach await json before it becomes stale');
  page.navigate('nurture');
  page.respond(1);
  await flush();
  rejectJson(Error('synthetic stale JSON failure'));
  await initial;
  assert.equal(error(page), '');
  assert.equal(page.run('data.workspace_view'), 'detail');
  assert.match(body(page), /7日养号/);
});

test('a current request failure remains visible instead of being swallowed', async () => {
  const page = harness();
  const initial = page.start();
  page.reject(0, 'synthetic current failure');
  await initial;
  assert.match(error(page), /synthetic current failure/);
  assert.equal(page.run('data'), null);
});

test('nurture renderer waits for detail instead of consuming a summary shape', async () => {
  const page = harness();
  const initial = page.start();
  page.respond(0);
  await initial;
  assert.match(body(page), /工作待办/);
  // Exercise the shared render boundary as well as the navigation await path.
  const renderJob = page.run("tab='nurture';render()");
  await flush();
  assert.doesNotMatch(body(page), /<h2>7日养号/);
  noRuntimeError(page);
  const detail = page.requests.findIndex((request, index) => index > 0 && !request.settled);
  assert.notEqual(detail, -1);
  page.respond(detail);
  await renderJob;
  await flush();
  assert.equal(error(page), '');
  assert.equal(page.run('data.workspace_view'), 'detail');
  assert.match(body(page), /7日养号/);
});

test('date refresh returning summary automatically reloads detail for the current nurture tab', async () => {
  const page = harness('?tab=nurture');
  const initial = page.start();
  page.respond(0);
  await initial;
  assert.equal(page.run('data.workspace_view'), 'detail');
  page.navigate('todo');
  await flush();
  assert.match(body(page), /工作待办/);
  page.field('workDate').value = '2026-10-28';
  const refreshing = page.field('workDate').onchange();
  const summary = page.requests.length - 1;
  assert.equal(new URL(page.requests[summary].url, 'http://synthetic').searchParams.get('view'), 'work');
  page.navigate('nurture');
  await flush();
  page.respond(summary);
  await flush();
  const detail = page.requests.findIndex((request, index) => index > summary && !request.settled);
  assert.notEqual(detail, -1, 'current detail tab needs a fresh detail read after date summary arrives');
  const params = new URL(page.requests[detail].url, 'http://synthetic').searchParams;
  assert.equal(params.get('view'), 'detail');
  assert.equal(params.get('date'), '2026-10-28');
  page.respond(detail);
  await refreshing;
  await flush();
  assert.equal(error(page), '');
  assert.equal(page.run('data.date'), '2026-10-28');
  assert.equal(page.run('data.workspace_view'), 'detail');
  assert.match(body(page), /7日养号/);
});

(async () => {
  let failed = 0, completed = 0;
  for (const item of tests) {
    let timeout;
    try {
      await Promise.race([item.run(), new Promise((resolve, reject) => {
        timeout = setTimeout(() => reject(Error('test did not settle within 3000 ms: ' + item.name)), 3000);
      })]);
      completed++;
      console.log('PASS: ' + item.name);
    }
    catch (failure) { failed++; console.error('FAIL: ' + item.name + '\n' + failure.stack); }
    finally { clearTimeout(timeout); }
  }
  if (failed) process.exitCode = 1;
  else {
    assert.equal(completed, tests.length);
    console.log('PASS: ' + completed + '/' + tests.length + ' workspace navigation regressions (real renderers; synthetic DOM/fetch only)');
  }
})();
