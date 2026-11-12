// Run against a fresh, disposable standalone fixture. See standalone.sql.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const { randomUUID } = require('node:crypto');
const { tmpdir } = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

const password = process.env.SYNTHETIC_FIXTURE_PASSWORD;
const dsn = new URL(process.env.BOARD_GO_PG_DSN || '');
assert.equal(dsn.hostname, '127.0.0.1');
assert.match(dsn.pathname, /^\/synthetic_go_standalone_[a-z0-9_]+$/);
assert.ok(password, 'Missing disposable fixture password');

async function main() {
  const app = spawn(path.resolve(process.env.BOARD_GO_EXE || '.local/board-go.exe'), ['-listen', '127.0.0.1:0'], {
    cwd: tmpdir(),
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let browser;
  try {
    let base;
    const lines = createInterface({ input: app.stderr });
    for await (const line of lines) {
      const address = line.match(/address=(127\.0\.0\.1:\d+)/)?.[1];
      if (address) {
        base = 'http://' + address;
        break;
      }
    }
    assert.ok(base, 'Standalone Go service did not start');
    const health = await (await fetch(base + '/api/go/health')).json();
    assert.equal(health.standalone, true);
    assert.equal(health.websocket, true);
    assert.equal((await fetch(base + '/api/board')).status, 401);
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXE || undefined, headless: true });
    const reader = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const writer = await browser.newContext();
    const owner = await browser.newContext();
    const login = async (context, user, suppliedPassword = password) => {
      const response = await context.request.post(base + '/api/auth/login', {
        headers: { Origin: base },
        data: { username: user, password: suppliedPassword },
      });
      assert.equal(response.status(), 200, 'fixture login failed');
      const me = await (await context.request.get(base + '/api/auth/me')).json();
      return me.user.csrf;
    };
    const readerCSRF = await login(reader, 'go-daily-employee');
    const writerCSRF = await login(writer, 'go-daily-owner');
    const ownerCSRF = await login(owner, 'go-daily-owner');
    const request = async (context, pathname, data, csrf, status = 200) => {
      const response = data === undefined
        ? await context.request.get(base + pathname)
        : await context.request.post(base + pathname, { headers: { Origin: base, 'X-CSRF-Token': csrf }, data });
      const result = await response.json();
      assert.equal(response.status(), status, pathname + ' ' + (result.error || result.code || ''));
      return result;
    };
    const get = (pathname, status) => request(reader, pathname, undefined, undefined, status);
    const post = (pathname, data, status) => request(reader, pathname, data, readerCSRF, status);
    const admin = (pathname, data, status) => request(owner, pathname, data, ownerCSRF, status);
    const day = '2026-11-09';
    const boardPath = '/api/board?date=' + day + '&version_scope=day';
    const board = await get(boardPath);
    assert.deepEqual(board.accounts.map(row => row.id).sort(), ['go-east-account', 'go-east-missing']);
    assert.ok(board.publishing_layout.cells.every(cell => cell.computer_id === 'go-east-pc'));
    await get('/api/account/inline?id=go-west-account', 403);

    const page = await reader.newPage();
    let hints = 0;
    page.on('websocket', socket => {
      if (socket.url().includes('/api/go/live?')) socket.on('framereceived', ({ payload }) => {
        if (JSON.parse(payload).changed) hints++;
      });
    });
    const socket = page.waitForEvent('websocket', { predicate: socket => socket.url().includes('/api/go/live?') });
    await page.goto(base + '/?page=accounts', { waitUntil: 'domcontentloaded' });
    await socket;
    await page.locator('[data-inline-account="go-east-account"][data-inline-field="name"]').first().waitFor();
    await page.waitForFunction(() => window.boardLiveSync?.getState().phase === 'synced');
    const name = 'Go 原生协作 ' + randomUUID();
    const old = await get('/api/account/inline?id=go-east-account');
    const data = { id: 'go-east-account', changes: { name }, return_account: true, request_id: randomUUID(),
      _expected_versions: { 'account:go-east-account': old.record_version } };
    const started = Date.now();
    const changed = await request(writer, '/api/account/patch', data, writerCSRF);
    assert.equal(changed.account.name, name);
    await page.locator('#accountsBody').getByText(name, { exact: true }).first().waitFor({ timeout: 12000 });
    const syncMs = Date.now() - started;
    assert.ok(hints > 0);
    assert.deepEqual(await request(writer, '/api/account/patch', data, writerCSRF), changed);
    await post('/api/account/patch', { ...data, id: 'go-west-account', request_id: randomUUID() }, 403);
    await post('/api/account/patch', { ...data, request_id: randomUUID() }, 409);

    // Use the real inline editor to save a second change.
    await page.locator('[data-inline-account="go-east-account"][data-inline-field="name"]').dblclick();
    await page.waitForFunction(() => window.AccountInline?.getState()?.phase === 'editing');
    const secondName = name + ' 行内';
    await page.locator('#accountInlineInput').fill(secondName);
    await page.locator('[data-inline-action="save"]').click();
    await page.locator('#accountsBody').getByText(secondName, { exact: true }).first().waitFor();
    assert.equal((await get('/api/account/inline?id=go-east-account')).account.name, secondName);
    await page.locator('[data-account-pilot-detail="go-east-account"]').click();
    await page.locator('#accountPilotDetailEdit').click();
    await page.locator('#editor').waitFor();
    const fullName = secondName + ' 完整编辑';
    await page.locator('#editName').fill(fullName);
    await page.locator('#editForm button[type="submit"]').click();
    await page.locator('#accountsBody').getByText(fullName, { exact: true }).first().waitFor();

    const phone = '100' + String(Date.now()).slice(-8);
    const card = await post('/api/phone-card', { phone, label: '合成卡', office_ids: ['go-east'], request_id: randomUUID() });
    assert.ok(card.id);
    const number = String(200000 + Date.now() % 300000);
    const device = await post('/api/phone-device/create', { number, name: '合成手机', state: '待确认',
      office_ids: ['go-east'], request_id: randomUUID() });
    assert.ok(device.id);
    await post('/api/phone-device/create', { number, name: '异办公室', state: '待确认',
      office_ids: ['go-west'], request_id: randomUUID() }, 403);
    const boardAfter = await get(boardPath);
    assert.ok(boardAfter.phone_cards.cards.some(row => String(row.id) === String(card.id)));
    assert.ok(boardAfter.phone_devices.devices.some(row => row.id === device.id));
    const cards = await reader.newPage();
    await cards.goto(base + '/?page=cards');
    await cards.locator('#cardsBody').getByText(phone, { exact: false }).first().waitFor();
    const devices = await reader.newPage();
    await devices.goto(base + '/?page=phones');
    await devices.locator('#phonesBody').getByText('合成手机', { exact: false }).first().waitFor();

    const layout = { batch: 'synthetic-go-layout', computer_id: 'go-east-pc', slot: 1,
      expected_account_id: null, action: 'link', account_id: 'go-east-account' };
    await post('/api/publishing/link', { ...layout, account_id: 'go-west-account', request_id: randomUUID() }, 403);
    const linked = await post('/api/publishing/link', { ...layout, request_id: randomUUID() });
    assert.equal(linked.account_id, 'go-east-account');
    await post('/api/publishing/link', { ...layout, slot: 3, request_id: randomUUID() }, 409);
    const keys = ['layout:synthetic-go-layout:go-east-pc:1', 'layout:synthetic-go-layout:go-east-pc:2'];
    const versions = await get('/api/record-versions?keys=' + encodeURIComponent(keys.join(',')));
    await post('/api/publishing/assignment', { batch: layout.batch, computer_id: layout.computer_id, slot: 1,
      action: 'move', target_computer: 'go-east-pc', target_slot: 2, _expected_versions: versions.record_versions, request_id: randomUUID() });
    const moved = (await get(boardPath)).publishing_layout.cells.find(cell => cell.slot === 2);
    assert.equal(moved.account_id, 'go-east-account');

    const file = (name, content) => ({ filename: name, content: Buffer.from(content).toString('base64') });
    const handle = '000' + String(Date.now());
    const review = await post('/api/workspace/import-preview', { kind: 'accounts', office_id: 'go-east',
      ...file('synthetic.csv', '抖音号,昵称,备注\n' + handle + ',合成导入,导入备注\n0099,禁止写入,\n') });
    assert.equal(review.rows[0].action, 'create');
    assert.equal(review.rows[1].action, 'error');
    assert.deepEqual(review.rows[1].changes, []);
    const applyData = { preview_id: review.preview_id, rows: [review.rows[0].row], request_id: randomUUID() };
    const applied = await post('/api/workspace/import-apply', applyData);
    assert.equal(applied.imported, 1);
    assert.deepEqual(await post('/api/workspace/import-apply', applyData), applied);
    const afterImport = await get(boardPath);
    assert.equal(afterImport.accounts.find(row => row.douyin_id === handle).name, '合成导入');
    const exported = await post('/api/workspace/export', { kind: 'accounts', format: 'xlsx', date: day });
    assert.equal(Buffer.from(exported.content, 'base64').subarray(0, 2).toString(), 'PK');
    assert.equal(exported.count, 3);
    const exchange = await reader.newPage();
    await exchange.goto(base + '/workspace.html?tab=exchange');
    await exchange.locator('#importFile').setInputFiles({
      name: 'synthetic-update.csv', mimeType: 'text/csv',
      buffer: Buffer.from('抖音号,备注\n' + handle + ',页面确认后的备注\n'),
    });
    await exchange.locator('#previewImport').click();
    const importedRow = exchange.locator('[data-import-row]');
    await importedRow.waitFor();
    assert.equal(await importedRow.isChecked(), false);
    await importedRow.check();
    await exchange.locator('#applyImport').click();
    await exchange.locator('#importResult').getByText('已完成 1 行导入', { exact: false }).waitFor();
    assert.equal((await get(boardPath)).accounts.find(row => row.douyin_id === handle).note, '页面确认后的备注');

    const history = await get('/api/workspace/history?entity=account:go-east-account');
    assert.ok(history.entries.length > 0);
    await get('/api/workspace/history?entity=account:go-west-account', 403);
    if (process.env.TEST_OFFICE_THREE === '1') {
      await verifyOfficeThree(admin, file, boardPath);
    }
    if (process.env.TEST_TABLE_SYNC === '1') {
      await verifyTableSync(admin, file, owner, base);
    }
    const stale = await browser.newContext();
    await login(stale, 'go-daily-employee');
    const newPassword = 'Fixture-' + randomUUID();
    await post('/api/auth/username', { username: 'not-allowed', current_password: password }, 403);
    await post('/api/auth/password', { old_password: password, password: newPassword });
    assert.equal((await (await stale.request.get(base + '/api/auth/me')).json()).user, null);
    assert.ok((await get('/api/auth/me')).user);
    await login(stale, 'go-daily-employee', newPassword);
    await get('/api/auth/users', 403);
    const people = await admin('/api/auth/users');
    const employee = people.users.find(row => row.username === 'go-daily-employee');
    assert.ok(employee && !('password_hash' in employee));
    await admin('/api/auth/users', { id: employee.id, name: employee.name, role: employee.role,
      active: true, permissions: ['publish'], office_ids: ['go-west'], request_id: randomUUID() });
    assert.equal((await (await stale.request.get(base + '/api/auth/me')).json()).user, null);
    await login(stale, 'go-daily-employee', newPassword);
    const reassigned = await (await stale.request.get(base + boardPath)).json();
    assert.deepEqual(reassigned.accounts.map(account => account.id), ['go-west-account']);
    const newUsername = 'go-owner-' + randomUUID().slice(0, 8);
    await admin('/api/auth/username', { username: newUsername, current_password: password });
    assert.equal((await (await writer.request.get(base + '/api/auth/me')).json()).user, null);
    await login(owner, newUsername);
    console.log(JSON.stringify({ standalone: true, browser: 'Chromium', syncMs, crossOfficeDenied: true,
      accountUI: true, inventory: true, layout: true, importAndReplay: true, history: true,
      authSessionRevocation: true, officeThree: process.env.TEST_OFFICE_THREE === '1',
      tableSync: process.env.TEST_TABLE_SYNC === '1' }));
  } finally {
    if (browser) await browser.close();
    app.kill();
  }
}

const headers = '来源行,注册手机号,昵称,登录手机序号,抖音号,小号说明,门头,负责人,登记电脑序号,来源类型,来源账号状态,违规备注,使用状态,注册日期';
const progressHeaders = '来源行,电脑序号,负责人,临时负责人,类别,' + Array.from({ length: 10 }, (_, i) => '发布设备' + (i + 1)).join(',');

async function verifyOfficeThree(admin, file, boardPath) {
  const mode = 'fill_missing_nicknames';
  const previewPath = '/api/workspace/office-three-import-preview';
  const applyPath = '/api/workspace/office-three-import-apply';
  const names = await admin(previewPath, { mode, office_id: 'three',
    accounts_file: file('names.csv', headers + '\n2,,合成补名,,006767,,,,,,,,,\n') });
  assert.equal(names.summary.updated_accounts, 1);
  const filled = await admin(applyPath, { mode, office_id: 'three', preview_id: names.preview_id,
    confirmation: '仅补全办公室三缺失昵称', request_id: randomUUID() });
  assert.equal(filled.updated_accounts, 1);
  const handle = '000' + String(Date.now()) + '3';
  const rows = await admin(previewPath, { office_id: 'three',
    accounts_file: file('account.csv', headers + '\n2,,合成双表账号,72,' + handle + ',,,,71,,正常,,,\n'),
    progress_file: file('progress.csv', progressHeaders + '\n2,71,,,,抖音号:' + handle + '\n') });
  assert.equal(rows.summary.new_accounts, 1);
  assert.equal(rows.summary.new_layouts, 1);
  const applied = await admin(applyPath, { office_id: 'three', preview_id: rows.preview_id,
    confirmation: '导入办公室三', request_id: randomUUID() });
  assert.equal(applied.verification.original_records_preserved, true);
  const board = await admin(boardPath);
  assert.ok(board.accounts.some(row => row.douyin_id === handle));
}

async function verifyTableSync(admin, file, context, base) {
  const prefix = '/api/table-sync';
  const status = await admin(prefix);
  assert.equal(status.available, true);
  const write = (action, values) => admin(prefix + '/' + action, { office_id: 'three', request_id: randomUUID(), ...values });
  await write('pilot', { state: 'enabled', expected_revision: status.pilot.revision, confirmation: '开启办公室三试点' });
  const table = await context.newPage();
  await table.goto(base + '/table-sync.html');
  await table.waitForFunction(() => document.querySelector('#syncPilotState')?.textContent.includes('进行中'));
  const current = await admin('/api/account/inline?id=go-three-existing');
  const preview = await write('preview', { mode: 'update', source_round: '合成浏览器核对',
    accounts_file: file('v7-account.csv', headers + '\n2,,同步新名称,,006767,,,,71,,正常,,,\n'),
    progress_file: file('v7-progress.csv', progressHeaders + '\n2,71,,,,抖音号:006767\n') });
  const selection = value => value.rows.filter(row => row.selected).map(row => row.row_key);
  assert.equal(selection(preview).length, 1);
  const applied = await write('apply', { preview_id: preview.preview_id, selected_rows: selection(preview), confirmation: preview.confirmation });
  assert.equal((await admin('/api/account/inline?id=go-three-existing')).account.name, '同步新名称');
  const receipt = await admin(prefix + '/batch?id=' + applied.batch_id);
  assert.equal(receipt.batch_id, applied.batch_id);
  const reversal = await write('rollback-preview', { batch_id: applied.batch_id });
  assert.equal(selection(reversal).length, 1);
  await write('rollback-apply', { preview_id: reversal.preview_id, selected_rows: selection(reversal), confirmation: reversal.confirmation });
  assert.equal((await admin('/api/account/inline?id=go-three-existing')).account.name, current.account.name);
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
