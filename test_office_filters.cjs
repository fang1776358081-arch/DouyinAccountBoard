// Actual frontend functions with a synthetic DOM/fetch only. No production data,
// browser profile, HTTP server, database or external network is accessed.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const read = name => fs.readFileSync(path.join(__dirname, 'static', name), 'utf8');
const appSource = read('app.js');
const controlledApp = appSource.replace(';openBoard();', ';');
assert.notEqual(controlledApp, appSource, 'startup must remain under test control');

const decode = value => String(value).replace(/&(amp|lt|gt|quot|#39);/g, (_, name) => ({amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"}[name]));
const selectIds = new Set(['computer', 'loginPhoneFilter', 'status', 'bulkStatus', 'editComputer', 'editPhoneDevice', 'editStatus']);
function mockElement(id) {
  let html = '', value = '', selectedIndex = -1, options = [];
  const select = selectIds.has(id);
  return {
    id, hidden: false, textContent: '', open: false, dataset: {}, checked: false,
    classList: {add() {}, remove() {}, toggle() {}}, addEventListener() {}, after() {},
    showModal() { this.open = true; }, close() { this.open = false; },
    querySelector() { return null; },
    get innerHTML() { return html; },
    set innerHTML(next) {
      html = String(next);
      if (select) {
        options = [...html.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map(match => ({
          value: decode(match[1].match(/\bvalue="([^"]*)"/)?.[1] ?? match[2].replace(/<[^>]*>/g, '')),
          textContent: decode(match[2].replace(/<[^>]*>/g, '')),
        }));
        selectedIndex = options.length ? 0 : -1;
      }
    },
    get value() { return select ? options[selectedIndex]?.value ?? '' : value; },
    set value(next) {
      if (select) selectedIndex = options.findIndex(option => option.value === String(next));
      else value = String(next);
    },
    get options() { return options; },
    get selectedIndex() { return selectedIndex; },
  };
}

function fixture() {
  const account = (id, office, phone, computer) => ({
    id, office_ids: [office], name: id, phone_device_id: phone,
    computer_id: computer, slot: 1, status: '正常', account_type: '大号', published: false,
  });
  return {
    cursor: 10, date: '2026-10-28', today: '2026-10-28', statuses: ['正常'],
    user: {id: 'synthetic-owner', scope_mode: 'all'},
    offices: [{id: 'one', name: '办公室一'}, {id: 'two', name: '办公室二'}, {id: 'three', name: '办公室三'}],
    accounts: [
      account('office-three', 'three', 'office3:1', 'pc-three'),
      account('cross-reference', 'three', 'pending-device', 'pc-pending'),
      account('unassigned-three', 'three', null, 'unassigned'),
      account('office-one', 'one', '1', 'pc-one'),
      {...account('pending-account', 'pending', 'pending-device', 'pc-pending'), office_ids: []},
    ],
    phone_devices: {devices: [
      {id: 'office3:1', name: '办公室三·1号手机', office_ids: ['three']},
      {id: 'office3:2', name: '三办空设备', office_ids: ['three']},
      {id: 'shared', name: '共享手机', office_ids: ['one', 'three']},
      {id: '1', name: '1号手机', office_ids: ['one']},
      {id: '301', name: '301号手机', office_ids: ['two']},
      {id: 'office3:900', name: '前缀不能覆盖真实归属', office_ids: ['one']},
      {id: 'pending-device', name: '待分配手机', office_ids: []},
      {id: 'missing-map', name: '缺少明确映射'},
      {id: 'safe<&"', name: '合成 <img src=x onerror=bad()> & "', office_ids: ['three']},
    ]},
    computers: [
      {id: 'pc-three', name: '三办电脑', office_ids: ['three']},
      {id: 'pc-empty', name: '三办空电脑', office_ids: ['three']},
      {id: 'pc-shared', name: '共享电脑', office_ids: ['one', 'three']},
      {id: 'pc-one', name: '一办电脑', office_ids: ['one']},
      {id: 'pc-pending', name: '待分配电脑', office_ids: []},
      {id: 'pc-missing', name: '缺少映射电脑'},
      {id: 'unassigned', name: '未分配电脑', office_ids: []},
    ],
    phone_cards: {cards: []}, device_workflow: {enabled: false},
    publishing_layout: {enabled: false, cells: []}, collection: {},
  };
}

function harness() {
  const elements = new Map(), listeners = new Map(), calls = [], scripts = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, mockElement(id));
    return elements.get(id);
  };
  const context = {
    console, URLSearchParams, location: {search: '?page=accounts&office=three'}, history: {replaceState() {}},
    accountSearchFields: [], activeLifecyclePreset: '', currentUser: null,
    clearAccountSearch() {}, lifecycleSummary() {}, lifecyclePresetMatches() { return true; },
    accountSearchMatches() { return true; }, verificationMatches() { return true; },
    renderVerificationFilter() {}, verificationMarkup() { return ''; }, lifecycleDetails() { return ''; },
    applyPermissions() {}, hasPermission() { return false; }, lifecycleEdit() {}, readinessEdit() {},
    captureVersions() {}, applyAccountPermissions() {}, lifecyclePayload() { return {}; }, readinessPayload() { return {}; },
    ScopeUI: {mount() {}, payload() { return {}; }},
    setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; },
    document: {
      getElementById: element, querySelector: selector => element(selector), querySelectorAll() { return []; },
      addEventListener(type, listener) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(listener);
      },
      dispatchEvent() {}, createElement() { return {}; },
      body: {classList: {toggle() {}}, appendChild(script) { scripts.push(script.src); }},
    },
    addEventListener() {}, updatePayload: {cursor: 11, changes: []},
    async fetch(url) {
      calls.push(url);
      assert.match(url, /^\/api\/board-updates\?cursor=\d+$/, 'all unexpected requests are blocked');
      return {ok: true, status: 200, json: async () => structuredClone(context.updatePayload)};
    },
  };
  context.window = context;
  vm.createContext(context);
  for (const file of ['offices.js', 'phone-devices.js', 'publishing-layout.js']) {
    vm.runInContext(read(file), context, {filename: file});
  }
  vm.runInContext(controlledApp, context, {filename: 'app.js'});
  vm.runInContext(read('board-updates.js'), context, {filename: 'board-updates.js'});
  const run = code => vm.runInContext(code, context);
  // Non-target management pages have independent controls; exercise top filters
  // with the real render() without claiming to test their table renderers.
  run('renderPhones=()=>{};renderCards=()=>{};');
  assert.deepEqual(calls, [], 'neither app startup nor script insertion can make a request');
  assert.deepEqual(scripts, ['/board-updates.js']);
  return {
    context, run, element, calls,
    show(next = fixture()) { context.nextBoard = next; run('showBoard(nextBoard)'); },
    values(id) { return element(id).options.map(option => option.value); },
    clickOffice(id) {
      const button = {dataset: {office: id}};
      for (const listener of listeners.get('click') || []) listener({target: {closest: selector => selector === '[data-office]' ? button : null}});
    },
  };
}

let checks = 0;
function check(name, work) { work(); checks++; }
const resourceIds = (page, kind) => Array.from(page.run(`officeFilterResources(${kind === 'devices' ? 'data.phone_devices.devices' : 'data.computers'},${JSON.stringify(kind)})`), row => row.id);

(async () => {
  const page = harness();
  const board = fixture(), unchanged = JSON.stringify(board);
  page.show(board);
  check('scoped devices use explicit ownership and retain empty/shared resources', () => {
    assert.deepEqual(resourceIds(page, 'devices'), ['office3:1', 'office3:2', 'shared', 'safe<&"']);
    assert.deepEqual(page.values('loginPhoneFilter'), ['', 'missing', 'review', 'device:office3:1', 'device:office3:2', 'device:shared', 'device:safe<&"']);
    assert.equal(page.element('loginPhoneFilter').selectedIndex, 0);
    assert.equal(page.element('loginPhoneFilter').innerHTML.includes('<img'), false);
    assert.ok(page.element('loginPhoneFilter').innerHTML.includes('&lt;img'));
  });
  check('computer scope also excludes cross-references and keeps virtual unassigned', () => {
    assert.deepEqual(resourceIds(page, 'computers'), ['pc-three', 'pc-empty', 'pc-shared', 'unassigned']);
    assert.deepEqual(page.values('computer'), ['', 'pc-three', 'pc-empty', 'pc-shared', 'unassigned']);
  });
  check('render does not change any business record', () => assert.equal(JSON.stringify(board), unchanged));
  check('valid selections persist on actual render and showBoard reload', () => {
    page.element('loginPhoneFilter').value = 'device:office3:1';
    page.element('computer').value = 'pc-three';
    page.run('render()');
    page.show(structuredClone(board));
    assert.equal(page.element('loginPhoneFilter').value, 'device:office3:1');
    assert.equal(page.element('computer').value, 'pc-three');
    assert.equal(page.run('filtered().length'), 1);
  });
  check('changed office scope resets both old selections before filtering rows', () => {
    page.run('activeOffice="one";render()');
    assert.equal(page.element('loginPhoneFilter').value, '');
    assert.equal(page.element('loginPhoneFilter').selectedIndex, 0);
    assert.equal(page.element('computer').value, '');
    assert.equal(page.element('computer').selectedIndex, 0);
    assert.equal(page.run('filtered().length'), 1);
    assert.deepEqual(page.values('computer'), ['', 'pc-shared', 'pc-one']);
    assert.deepEqual(resourceIds(page, 'devices'), ['shared', '1', 'office3:900']);
  });
  check('office tab click rebuilds options and keeps account/board behavior aligned', () => {
    page.clickOffice('three');
    assert.equal(page.run('activeOffice'), 'three');
    assert.equal(page.run('filtered().length'), 3);
    page.run('page="board";render()');
    assert.deepEqual(page.values('computer'), ['', 'pc-three', 'pc-empty', 'pc-shared', 'unassigned']);
    assert.equal(page.values('loginPhoneFilter').includes('device:1'), false);
    page.run('page="accounts";render()');
  });
  check('virtual unassigned disappears when current office has no such account', () => {
    page.run('data.accounts=data.accounts.filter(a=>a.id!=="unassigned-three");render()');
    assert.equal(page.values('computer').includes('unassigned'), false);
  });
  check('special missing/review device filters survive repaint', () => {
    for (const value of ['missing', 'review']) {
      page.element('loginPhoneFilter').value = value;
      page.run('render()');
      assert.equal(page.element('loginPhoneFilter').value, value);
      assert.notEqual(page.element('loginPhoneFilter').selectedIndex, -1);
    }
    page.element('loginPhoneFilter').value = '';
  });
  check('all authorized resources retain original order without guessing ownership', () => {
    page.run('activeOffice="all";render()');
    assert.deepEqual(resourceIds(page, 'devices'), board.phone_devices.devices.map(row => row.id));
    assert.deepEqual(resourceIds(page, 'computers'), board.computers.map(row => row.id));
  });
  check('pending means explicit empty scope, not missing mapping', () => {
    page.run('activeOffice="pending";render()');
    assert.deepEqual(resourceIds(page, 'devices'), ['pending-device']);
    assert.deepEqual(resourceIds(page, 'computers'), ['pc-pending']);
  });
  check('removed/changed resources clear stale values on full board reload', () => {
    page.run('activeOffice="three";render()');
    page.element('computer').value = 'pc-three';
    page.element('loginPhoneFilter').value = 'device:office3:1';
    const next = fixture();
    next.computers.find(row => row.id === 'pc-three').office_ids = ['one'];
    next.phone_devices.devices = next.phone_devices.devices.filter(row => row.id !== 'office3:1');
    page.show(next);
    assert.equal(page.element('computer').value, '');
    assert.equal(page.element('computer').selectedIndex, 0);
    assert.equal(page.element('loginPhoneFilter').value, '');
    assert.equal(page.element('loginPhoneFilter').selectedIndex, 0);
    assert.equal(page.run('filtered().length'), 3);
  });
  check('account editors retain full authorized associations despite top filter', () => {
    page.show(fixture());
    page.run('edit("cross-reference")');
    assert.deepEqual(page.values('editPhoneDevice'), ['', ...fixture().phone_devices.devices.map(row => row.id)]);
    assert.deepEqual(page.values('editComputer'), fixture().computers.map(row => row.id));
    assert.equal(page.element('editPhoneDevice').value, 'pending-device');
    assert.equal(page.element('editComputer').value, 'pc-pending');
    page.element('editor').close();
  });
  check('device management drilldown rebuilds options before selecting another office', () => {
    page.run('page="phones";render();showPhoneAccounts("1")');
    assert.equal(page.run('activeOffice'), 'all');
    assert.equal(page.run('page'), 'accounts');
    assert.equal(page.element('loginPhoneFilter').value, 'device:1');
    assert.equal(page.run('filtered().length'), 1);
    assert.equal(page.run('filtered()[0].id'), 'office-one');
  });
  check('invalid current office falls back before choices are built', () => {
    const assigned = fixture();
    assigned.user.scope_mode = 'assigned'; assigned.offices = [{id: 'one', name: '一办'}];
    page.run('activeOffice="three"'); page.show(assigned);
    assert.equal(page.run('activeOffice'), 'all');
    assert.deepEqual(page.values('computer'), ['', ...assigned.computers.map(row => row.id)]);
  });

  const incremental = harness();
  incremental.show(); incremental.element('loginPhoneFilter').value = 'device:office3:1';
  incremental.context.updatePayload = {cursor: 11, changes: [{entity: 'device:office3:1', after: {id: 'office3:1', office_ids: ['one']}}]};
  await incremental.run('catchUpBoard()');
  check('real incremental path re-renders changed device ownership and clears stale filter', () => {
    assert.deepEqual(incremental.calls, ['/api/board-updates?cursor=10']);
    assert.equal(incremental.element('loginPhoneFilter').value, '');
    assert.equal(incremental.element('loginPhoneFilter').selectedIndex, 0);
    assert.equal(incremental.values('loginPhoneFilter').includes('device:office3:1'), false);
    assert.equal(incremental.run('filtered().length'), 3);
  });

  const legacy = harness();
  const legacyBoard = fixture();
  legacyBoard.user = {}; legacyBoard.accounts = [
    {id: 'legacy-three', phone_device_id: 'office3:1', computer_id: 'pc-three', slot: 1},
    {id: 'legacy-one', phone_device_id: '1', computer_id: 'pc-one', slot: 1},
    {id: 'legacy-two', phone_device_id: '301', computer_id: 'pc-two', slot: 1},
    {id: 'legacy-pending', phone_device_id: null, computer_id: 'unassigned', slot: 1},
  ];
  legacyBoard.phone_devices.devices = ['1', '300', '301', '700', '701', 'office3:1', 'bad'].map(id => ({id, name: id}));
  legacyBoard.computers = ['pc-three', 'pc-one', 'pc-two', 'pc-layout', 'pc-other-layout', 'unassigned', 'pc-unused'].map(id => ({id, name: id}));
  legacyBoard.publishing_layout.cells = [
    {computer_id: 'pc-layout', office: 'three', slot: 1, label: '合成待匹配'},
    {computer_id: 'pc-other-layout', office: 'one', slot: 1, label: '另一办公室'},
  ];
  legacy.show(legacyBoard);
  check('legacy devices retain established numbering rules', () => {
    assert.deepEqual(resourceIds(legacy, 'devices'), ['office3:1']);
    legacy.run('activeOffice="one";render()');
    assert.deepEqual(resourceIds(legacy, 'devices'), ['1', '300', '701']);
    legacy.run('activeOffice="two";render()');
    assert.deepEqual(resourceIds(legacy, 'devices'), ['301', '700']);
    legacy.run('activeOffice="pending";render()');
    assert.deepEqual(resourceIds(legacy, 'devices'), ['bad']);
  });
  check('legacy computers follow office account/layout associations only', () => {
    legacy.run('activeOffice="three";render()');
    assert.deepEqual(resourceIds(legacy, 'computers'), ['pc-three', 'pc-layout']);
    legacy.run('activeOffice="pending";render()');
    assert.deepEqual(resourceIds(legacy, 'computers'), ['unassigned']);
    legacy.run('activeOffice="all";render()');
    assert.deepEqual(resourceIds(legacy, 'computers'), legacyBoard.computers.map(row => row.id));
  });
  console.log(`PASS: ${checks} office-filter regression groups (actual frontend functions, synthetic select/DOM/fetch only)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
