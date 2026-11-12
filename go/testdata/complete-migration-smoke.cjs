// Run only against a freshly created, empty synthetic_go_bootstrap_complete_* loopback database.
// Requires BOARD_GO_PG_DSN, local Playwright and optionally BOARD_GO_EXE / CHROME_EXE.
const { chromium } = require('playwright');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const { randomUUID } = require('node:crypto');
const { mkdirSync, mkdtempSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

const dsn = new URL(process.env.BOARD_GO_PG_DSN || '');
assert.equal(dsn.hostname, '127.0.0.1');
assert.match(dsn.pathname, /^\/synthetic_go_bootstrap_complete_[a-z0-9_]+$/);
const workspace = path.resolve(__dirname, '..');
const binary = path.resolve(process.env.BOARD_GO_EXE || path.join(workspace, '.local', 'board-go.exe'));
const local = path.join(workspace, '.local');
mkdirSync(local, { recursive: true });
const backupDirectory = mkdtempSync(path.join(local, 'complete-migration-backups-'));
const setupKey = randomUUID();
const ownerPassword = 'FixtureOwner-' + randomUUID();
const workerPassword = 'FixtureWorker-' + randomUUID();
const env = { ...process.env, BOARD_GO_SETUP_KEY: setupKey, BOARD_GO_BACKUP_DIR: backupDirectory };
const ownerUsername = 'complete-owner';
const workerUsername = 'complete-worker';

function start() {
  const child = spawn(binary, ['-listen', '127.0.0.1:0'], { env, cwd: tmpdir(), stdio: ['ignore', 'ignore', 'pipe'] });
  const online = new Promise((resolve, reject) => {
    let captured = '';
    const timeout = setTimeout(() => reject(Error('Go startup did not announce a loopback listener')), 15000);
    child.stderr.on('data', block => {
      captured = (captured + block).slice(-8192);
      const address = captured.match(/address=(127\.0\.0\.1:\d+)/)?.[1];
      if (address) {
        clearTimeout(timeout);
        resolve('http://' + address);
      }
    });
    child.once('error', error => { clearTimeout(timeout); reject(error); });
    child.once('exit', code => {
      clearTimeout(timeout);
      reject(Error('Go exited before readiness (' + code + '): ' + captured.trim()));
    });
  });
  return { child, online };
}

async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, 'exit');
  child.kill();
  await exited;
}

function api(context, base, csrf = '') {
  return async (route, data, expected = 200) => {
    const response = data === undefined
      ? await context.request.get(base + route)
      : await context.request.post(base + route, { headers: { Origin: base, 'X-CSRF-Token': csrf }, data });
    const value = await response.json();
    assert.equal(response.status(), expected, `${route} ${value.code || value.error || ''}`);
    return value;
  };
}

function socketOpened(page) {
  return page.waitForEvent('websocket', { predicate: socket => socket.url().includes('/api/go/live?'), timeout: 12000 });
}

async function ownerLogin(context, base, initialize) {
  const page = await context.newPage();
  await page.goto(base + '/login.html');
  await page.getByRole('heading', { name: initialize ? '创建你的总管理员账号' : '登录工作台', exact: true }).waitFor();
  if (initialize) {
    await page.locator('#loginSetupKey').fill(setupKey);
    await page.locator('#loginName').fill('合成总管理员');
    await page.locator('#loginConfirm').fill(ownerPassword);
  }
  await page.locator('#loginUsername').fill(ownerUsername);
  await page.locator('#loginPassword').fill(ownerPassword);
  const socket = socketOpened(page);
  await page.locator('#loginSubmit').click();
  await page.waitForURL('**/workspace.html');
  await socket;
  const { user } = await api(context, base)('/api/auth/me');
  assert.equal(user.role, 'owner');
  assert.equal(user.username, ownerUsername);
  return user;
}

async function confirmEmployee(page) {
  const dialog = page.locator('#employeeActionDialog');
  await dialog.locator('#employeePreview').click();
  await dialog.locator('#employeeApply').waitFor();
  await dialog.locator('#employeeApply').click();
  await dialog.waitFor({ state: 'hidden' });
}

async function main() {
  const initialized = spawnSync(binary, ['-init-db'], { env, cwd: tmpdir(), encoding: 'utf8', timeout: 20000 });
  assert.equal(initialized.status, 0, initialized.error?.message || initialized.stderr);
  let service = start();
  let browser;
  try {
    let base = await service.online;
    assert.deepEqual(await (await fetch(base + '/api/ready')).json(), { ready: true });
    const health = await (await fetch(base + '/api/go/health')).json();
    assert.equal(health.standalone, true);
    assert.equal(health.websocket, true);
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXE || undefined, headless: true });
    let owner = await browser.newContext();
    let identity = await ownerLogin(owner, base, true);
    let admin = api(owner, base, identity.csrf);
    assert.deepEqual((await admin('/api/board')).accounts, []);

    const east = await admin('/api/auth/offices', { name: '合成一办公室', request_id: randomUUID() });
    const west = await admin('/api/auth/offices', { name: '合成二办公室', request_id: randomUUID() });
    const reader = await admin('/api/auth/users', { username: workerUsername, name: '合成协作员工', role: 'employee', password: workerPassword,
      permissions: ['publish'], office_ids: [east.id], request_id: randomUUID() });

    const employeeName = '合成实名员工';
    const phone = '19900000009';
    const card = await admin('/api/phone-card', { phone, label: '合成员工原卡', office_ids: [east.id], request_id: randomUUID() });
    const employeePage = await owner.newPage();
    await employeePage.goto(base + '/employee-cards.html');
    await employeePage.locator('#employeeCreate').click();
    await employeePage.locator('#ecName').fill(employeeName);
    await employeePage.locator('#ecNumber').fill('SYNTH-0001');
    await employeePage.locator('#ecOffice').selectOption(east.id);
    await confirmEmployee(employeePage);
    await employeePage.locator('#employeeRows').getByText(employeeName, { exact: true }).waitFor();
    const employee = (await admin('/api/employee-cards?view=cards')).employees.find(item => item.employee_no === 'SYNTH-0001');
    assert.ok(employee?.id);
    await employeePage.locator(`[data-employee-detail="${employee.id}"]`).first().click();
    await employeePage.locator('#employeeDetailDialog [data-employee-action="card_link"]').click();
    await employeePage.locator('#ecOperatorId').selectOption('minsheng');
    await employeePage.locator('#ecCard').selectOption(String(card.id));
    await employeePage.locator('#ecSource').fill('合成实名核对依据');
    await confirmEmployee(employeePage);
    const employeeURL = '/api/employee-cards?view=cards&employee_id=' + encodeURIComponent(employee.id);
    const linked = await admin(employeeURL);
    assert.ok(linked.units.some(unit => unit.current_card_id === card.id && unit.operator_id === 'minsheng'));
    assert.ok(linked.linked_cards.some(item => item.phone === phone));

    const handle = 'synthetic-complete-review';
    const originalName = '合成恢复前账号';
    const account = await admin('/api/account/create', { name: originalName, status: '正常', note: '合成原备注', douyin_id: handle,
      computer_id: 'unassigned', slot: 1, office_id: east.id, request_id: randomUUID(), _expected_versions: {} });
    assert.ok(account.id);
    const firstReview = await admin('/api/publish-review');
    assert.equal(firstReview.can_manage, true);
    assert.ok(firstReview.rows.some(row => row.account_id === account.id));
    const reviewPage = await owner.newPage();
    await reviewPage.goto(base + '/publish-review.html');
    await reviewPage.locator(`[data-review-target="${account.id}"]`).click();
    await reviewPage.locator('#reviewTaskTarget').fill('4');
    await reviewPage.locator('#reviewTaskComputer').selectOption('');
    await reviewPage.locator('#reviewTaskReason').fill('合成当日目标');
    const taskResponse = reviewPage.waitForResponse(response => response.url() === base + '/api/publish-review/task');
    await reviewPage.locator('#reviewTaskSave').click();
    const task = await taskResponse;
    assert.equal(task.status(), 200, (await task.json()).error || 'publish-review task failed');
    await reviewPage.locator('#reviewTaskDialog').waitFor({ state: 'hidden' });
    const overview = await admin('/api/publish-review');
    const day = overview.date;
    const savedTask = overview.rows.find(item => item.account_id === account.id);
    assert.equal(savedTask?.target, 4);
    assert.equal(savedTask.revision, 1);
    const yesterday = new Date(Date.parse(day + 'T00:00:00+08:00') - 3600000).toISOString();
    const evidence = { account_id: account.id, matched_douyin_id: handle, sampled_at: yesterday, total: 100, request_id: randomUUID() };
    assert.deepEqual(await admin('/api/publish-review/sample', evidence), { ok: true, duplicate: false });
    const latest = { ...evidence, sampled_at: new Date().toISOString(), total: 107, request_id: randomUUID() };
    const sampled = await admin('/api/publish-review/sample', latest);
    assert.deepEqual(await admin('/api/publish-review/sample', latest), sampled);
    const afterSamples = (await admin('/api/publish-review')).rows.find(item => item.account_id === account.id);
    assert.equal(afterSamples.net, 7);
    assert.equal(afterSamples.baseline_total, 100);
    assert.equal(afterSamples.current_total, 107);
    assert.equal((await admin('/api/collection/status')).collection.enabled, false);

    const backup = await admin('/api/workspace/backup-now', {});
    assert.ok((await admin('/api/workspace/backups')).backups.some(item => item.id === backup.name));
    const renamed = async name => {
      const inline = await admin('/api/account/inline?id=' + encodeURIComponent(account.id));
      const requestID = randomUUID();
      const changed = await admin('/api/account/patch', { id: account.id, changes: { name }, return_account: true, request_id: requestID,
        _expected_versions: { ['account:' + account.id]: inline.record_version } });
      assert.equal(changed.account.name, name);
      return requestID;
    };
    const retiredRequest = await renamed('合成备份后改名');
    await admin('/api/publish-review/task', { account_id: account.id, day, target: 99, task_state: 'active', computer_id: '',
      reason: '合成备份后目标', expected_revision: 1, request_id: randomUUID() });
    const extra = await admin('/api/phone-card', { phone: '19900000008', label: '合成备份后卡片', office_ids: [east.id], request_id: randomUUID() });
    await admin('/api/auth/users', { id: reader.id, role: 'employee', name: '合成协作员工', permissions: ['publish', 'publishing_manage'],
      office_ids: [east.id, west.id], request_id: randomUUID() });

    const backupPage = await owner.newPage();
    await backupPage.goto(base + '/workspace.html?tab=backups');
    await backupPage.getByRole('row').filter({ hasText: backup.name }).getByRole('button', { name: '检查并预览恢复' }).click();
    await backupPage.locator('#restoreConfirm').fill('恢复业务数据');
    await backupPage.locator('#restorePassword').fill(ownerPassword);
    const restoreResponse = backupPage.waitForResponse(response => response.url() === base + '/api/workspace/restore-apply');
    await backupPage.getByRole('button', { name: '确认恢复所选备份' }).click();
    const applied = await restoreResponse;
    const restored = await applied.json();
    assert.equal(applied.status(), 200, restored.error || 'restore failed');
    assert.equal(restored.restart_required, true);
    assert.ok(restored.backup_before_restore?.startsWith('before-restore-'));
    const retired = await fetch(base + '/api/ready');
    assert.equal(retired.status, 503);
    assert.deepEqual(await retired.json(), { ready: false, code: 'NOT_READY' });
    const receiptURL = '/api/idempotency/result?request_id=' + retiredRequest;
    assert.equal((await admin(receiptURL, undefined, 409)).code, 'RESTORE_GENERATION_EXPIRED');

    await owner.close();
    await stop(service.child);
    service = start();
    base = await service.online;
    assert.deepEqual(await (await fetch(base + '/api/ready')).json(), { ready: true });
    owner = await browser.newContext();
    const previousID = identity.id;
    identity = await ownerLogin(owner, base, false);
    assert.equal(identity.id, previousID);
    admin = api(owner, base, identity.csrf);
    assert.equal((await admin(receiptURL, undefined, 409)).code, 'RESTORE_GENERATION_EXPIRED');
    const restoredBoard = await admin('/api/board');
    assert.equal(restoredBoard.accounts.find(row => row.id === account.id)?.name, originalName);
    assert.ok(restoredBoard.phone_cards.cards.some(row => row.id === card.id));
    assert.ok(!restoredBoard.phone_cards.cards.some(row => row.id === extra.id));
    const restoredLink = await admin(employeeURL);
    assert.ok(restoredLink.units.some(unit => unit.current_card_id === card.id && unit.operator_id === 'minsheng'));
    const restoredReview = (await admin('/api/publish-review')).rows.find(row => row.account_id === account.id);
    assert.equal(restoredReview.target, 4);
    assert.equal(restoredReview.net, 7);
    assert.equal((await admin('/api/collection/status')).collection.enabled, false);

    const worker = await browser.newContext();
    await api(worker, base)('/api/auth/login', { username: workerUsername, password: workerPassword });
    const { user } = await api(worker, base)('/api/auth/me');
    assert.equal(user.id, reader.id);
    assert.deepEqual(user.permissions, ['publish', 'publishing_manage']);
    assert.deepEqual([...user.office_ids].sort(), [east.id, west.id].sort());
    const readerPage = await worker.newPage();
    const socket = socketOpened(readerPage);
    await readerPage.goto(base + '/?page=accounts');
    const liveSocket = await socket;
    let changeHints = 0;
    liveSocket.on('framereceived', ({ payload }) => {
      if (JSON.parse(payload.toString()).changed === true) changeHints++;
    });
    await readerPage.locator('#accountsBody').getByText(originalName, { exact: true }).waitFor();
    await readerPage.waitForFunction(() => window.boardLiveSync?.getState().phase === 'synced');
    const started = Date.now();
    const lastName = '合成恢复后同步';
    await renamed(lastName);
    await readerPage.locator('#accountsBody').getByText(lastName, { exact: true }).waitFor({ timeout: 12000 });
    assert.ok(changeHints > 0, 'No WebSocket change hint after restart');
    console.log(JSON.stringify({ bootstrapOwner: true, employeeLink: true, publishReview: true, backupRestore: true,
      restartRequired: true, oldReceiptRetired: true, permissionsRetained: true, websocketAfterRestore: true, syncMs: Date.now() - started }));
  } finally {
    if (browser) await browser.close();
    await stop(service.child);
  }
}

main().catch(error => {
  let message = error.message;
  for (const secret of [setupKey, ownerPassword, workerPassword]) message = message.replaceAll(secret, '[synthetic redacted]');
  console.error(message);
  process.exitCode = 1;
});
