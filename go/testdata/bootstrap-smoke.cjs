const { chromium } = require('playwright');
const { spawn, spawnSync } = require('node:child_process');
const { createInterface } = require('node:readline');
const { randomUUID } = require('node:crypto');
const assert = require('node:assert/strict');
const path = require('node:path');
const { tmpdir } = require('node:os');

const dsn = new URL(process.env.BOARD_GO_PG_DSN);
assert.equal(dsn.hostname, '127.0.0.1');
assert.match(dsn.pathname, /^\/synthetic_go_bootstrap_[a-z0-9_]+$/);
const binary = path.resolve(process.env.BOARD_GO_EXE || '.local/board-go.exe');
const setupKey = randomUUID();
const password = 'Fixture-' + randomUUID();
const username = 'bootstrap-owner';
const env = { ...process.env, BOARD_GO_SETUP_KEY: setupKey };

async function main() {
  const initialized = spawnSync(binary, ['-init-db'], { env, cwd: tmpdir() });
  assert.equal(initialized.status, 0, initialized.stderr.toString());
  const refused = spawnSync(binary, ['-init-db'], { env, cwd: tmpdir() });
  assert.notEqual(refused.status, 0);
  assert.match(refused.stderr.toString(), /只接受空数据库/);

  const service = spawn(binary, ['-listen', '127.0.0.1:0'], { env, cwd: tmpdir(), stdio: ['ignore', 'ignore', 'pipe'] });
  let browser;
  try {
    let address;
    for await (const line of createInterface({ input: service.stderr })) {
      address = line.match(/address=(127\.0\.0\.1:\d+)/)?.[1];
      if (address) break;
    }
    assert.ok(address);
    const base = 'http://' + address;
    const ready = await (await fetch(base + '/api/ready')).json();
    assert.equal(ready.ready, true);
    const meta = await (await fetch(base + '/api/auth/me')).json();
    assert.equal(meta.initialized, false);
    assert.equal(meta.setup_key_required, true);
    const data = { username, name: '合成总管理员', password };
    const post = (endpoint, body, origin = base, key = '') => fetch(base + endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Origin: origin, 'X-Worker-Token': key },
      body: JSON.stringify(body),
    });
    assert.equal((await post('/api/auth/setup', data)).status, 403);
    assert.equal((await post('/api/auth/desktop-setup', {})).status, 403);
    assert.equal((await post('/api/auth/desktop-setup', {}, 'http://localhost:8765', setupKey)).status, 403);
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXE || undefined, headless: true });
    const user = await browser.newContext();
    const page = await user.newPage();
    await page.goto(base + '/login.html');
    await page.locator('#loginSetupKey').fill(setupKey);
    await page.locator('#loginUsername').fill(username);
    await page.locator('#loginName').fill(data.name);
    await page.locator('#loginPassword').fill(password);
    await page.locator('#loginConfirm').fill(password);
    const socket = page.waitForEvent('websocket', { predicate: value => value.url().includes('/api/go/live?') });
    await page.locator('#loginSubmit').click();
    await page.waitForURL('**/workspace.html');
    await socket;
    const account = await (await user.request.get(base + '/api/auth/me')).json();
    assert.equal(account.user.role, 'owner');
    assert.equal(account.user.username, username);
    assert.equal(account.setup_key_required, false);
    assert.equal((await post('/api/auth/desktop-setup', {}, base, setupKey)).status, 403);
    const board = await (await user.request.get(base + '/api/board')).json();
    assert.deepEqual(board.accounts, []);
    assert.equal(board.user.role, 'owner');
    console.log(JSON.stringify({ freshSchema: true, existingRejected: true, setupOwnerBrowser: true, socketOpened: true }));
  } finally {
    if (browser) await browser.close();
    service.kill();
  }
}
main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
