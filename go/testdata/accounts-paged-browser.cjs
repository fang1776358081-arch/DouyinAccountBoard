// Windows loopback browser acceptance. Requires local Go, PostgreSQL and Chromium.
// Example (from go/): node testdata/accounts-paged-browser.cjs
// Override GO_EXE, PG_BIN, or CHROME_EXE for other local installations.
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const { createHash, randomBytes, randomUUID } = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');

const goDir = path.resolve(__dirname, '..');
const pgBin = process.env.PG_BIN || 'E:/Tools/PostgreSQL-16.15/pgsql/bin';
const goExe = process.env.GO_EXE || 'E:/Tools/go1.26.10/bin/go.exe';
const chromeExe = process.env.CHROME_EXE || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const sampleCount = Number(process.env.ACCOUNT_BROWSER_SAMPLES || 20);
assert.ok(Number.isInteger(sampleCount) && sampleCount > 0 && sampleCount <= 100);

async function poll(check, label, timeout = 8000) {
  const deadline = Date.now() + timeout;
  do {
    const result = await check();
    if (result) return result;
    await delay(20);
  } while (Date.now() < deadline);
  throw Error('等待超时：' + label);
}

function run(exe, args, options = {}) {
  const child = spawnSync(exe, args, { windowsHide: true, encoding: 'utf8', timeout: 55_000, ...options });
  if (child.error) throw child.error;
  assert.equal(child.status, 0, path.basename(exe) + ' failed: ' + (child.stderr || ''));
  return child.stdout?.trim();
}

async function loopbackPort() {
  const socket = net.createServer();
  await new Promise((resolve, reject) => socket.once('error', reject).listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  return port;
}

function openCDP(url) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const pending = new Map(), listeners = new Map();
    let next = 0;
    socket.addEventListener('error', () => reject(Error('Chromium 调试连接失败')), { once: true });
    socket.addEventListener('open', () => resolve({
      send(method, params = {}) {
        return new Promise((accept, fail) => {
          const id = ++next;
          const timer = setTimeout(() => { pending.delete(id); fail(Error(method + ' 超时')); }, 12_000);
          pending.set(id, { accept, fail, timer });
          socket.send(JSON.stringify({ id, method, params }));
        });
      },
      on(method, listener) { listeners.set(method, listener); },
      close() { socket.close(); },
    }), { once: true });
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      const entry = pending.get(message.id);
      if (entry) {
        clearTimeout(entry.timer);
        pending.delete(message.id);
        if (message.error) entry.fail(Error(message.error.message));
        else entry.accept(message.result);
      } else listeners.get(message.method)?.(message.params);
    });
  });
}

function measureFromEvent({ selector, type, predicate }) {
  // The first rAF observes the UI before paint. The second samples a frame that
  // can already display the updated UI, so this includes one render interval.
  window.accountProbeResult = null;
  document.addEventListener(type, function observe(event) {
    if (!event.target.closest(selector)) return;
    document.removeEventListener(type, observe, true);
    const start = event.timeStamp;
    const ready = Function('return (' + predicate + ');');
    const measure = stamp => {
      if (ready()) requestAnimationFrame(paint => { window.accountProbeResult = paint - start; });
      else if (stamp - start < 6000) requestAnimationFrame(measure);
      else window.accountProbeResult = -1;
    };
    requestAnimationFrame(measure);
  }, true);
}

function summarize(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const percentile = p => +sorted[Math.ceil(p * sorted.length) - 1].toFixed(1);
  return { n: sorted.length, p50_ms: percentile(0.5), p95_ms: percentile(0.95) };
}

async function main() {
  const parent = path.join(goDir, '..', '.synthetic-tests');
  fs.mkdirSync(parent, { recursive: true });
  const root = fs.mkdtempSync(path.join(parent, 'accounts-browser-'));
  const cluster = path.join(root, 'pg');
  const pg = name => path.join(pgBin, name + '.exe');
  const port = await loopbackPort();
  const database = 'synthetic_accounts_browser';
  const board = path.join(root, 'board-go.exe');
  const profile = path.join(root, 'chromium');
  const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(PG|BOARD_GO_|DOUYIN_BOARD_)/i.test(key)));
  const pgEnv = { ...cleanEnv, PGCLIENTENCODING: 'UTF8', PGSSLMODE: 'disable' };
  const appEnv = {
    ...cleanEnv,
    BOARD_GO_PG_DSN: `postgres://synthetic_owner@127.0.0.1:${port}/${database}?sslmode=disable`,
    BOARD_GO_BACKUP_DIR: path.join(root, 'backups'),
  };
  const cookie = randomBytes(32).toString('hex');
  const csrf = randomBytes(32).toString('hex');
  const tokenHash = createHash('sha256').update(cookie).digest('hex');
  const officeThreeSession = randomBytes(32).toString('hex');
  const otherOfficeSession = randomBytes(32).toString('hex');
  let pgStarted = false, app, browser, cdp;
  try {
    run(goExe, ['build', '-mod=readonly', '-o', board, './cmd/board-go'], {
      cwd: goDir, env: { ...cleanEnv, GOPROXY: 'off', GOSUMDB: 'off', GOTOOLCHAIN: 'local' },
    });
    run(pg('initdb'), ['-D', cluster, '-U', 'synthetic_owner', '--encoding=UTF8', '--locale=C', '--auth=trust'], { env: pgEnv });
    const pgOutput = fs.openSync(path.join(root, 'pg-ctl.log'), 'w');
    try {
      run(pg('pg_ctl'), ['-D', cluster, '-l', path.join(root, 'pg.log'), '-w', '-t', '20', '-o', `-h 127.0.0.1 -p ${port}`, 'start'], {
        env: pgEnv, stdio: ['ignore', pgOutput, pgOutput],
      });
      pgStarted = true;
    } finally { fs.closeSync(pgOutput); }
    const sql = (statement, db = database) => run(pg('psql'), ['-X', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-p', String(port), '-U', 'synthetic_owner', '-d', db, '-q'], { env: pgEnv, input: statement });
    sql(`CREATE DATABASE ${database};`, 'postgres');
    run(board, ['-init-db'], { cwd: root, env: appEnv });
    sql(`BEGIN;
      SET LOCAL search_path=board_candidate,pg_catalog;
      CREATE SCHEMA board_app;
      CREATE TABLE board_app.environment(kind text PRIMARY KEY CHECK(kind='synthetic-only'));
      INSERT INTO board_app.environment VALUES ('synthetic-only');
      INSERT INTO offices(id,name) VALUES ('three','办公室三'),('elsewhere','另一办公室');
      INSERT INTO computers(id,name) VALUES ('office3:71','合成电脑'),('other','另一电脑');
      INSERT INTO computer_offices(computer_id,office_id) VALUES ('office3:71','three'),('other','elsewhere');
      INSERT INTO accounts(id,computer_id,slot,name,status,douyin_id,account_type,note)
        SELECT 'probe-' || n,'office3:71',n,'测试账号 ' || lpad(n::text,3,'0'),'正常','dy-probe-' || n,
          CASE WHEN n % 2 = 0 THEN '大号' ELSE '小号' END,'隔离性能测试' FROM generate_series(1,175) n;
      INSERT INTO accounts(id,computer_id,slot,name,status,douyin_id,note)
        SELECT 'elsewhere-' || n,'other',n,'其他办公室合成数据','正常','dy-other-' || n,
          repeat('隔离性能测试',100) FROM generate_series(1,1200) n;
      INSERT INTO account_offices(account_id,office_id)
        SELECT id,CASE WHEN computer_id='other' THEN 'elsewhere' ELSE 'three' END FROM accounts;
      INSERT INTO auth_users(id,username,name,role,permissions,password_hash,created_at)
        VALUES(9101,'synthetic-browser-owner','合成管理员','owner','[]','test-only',0),
          (9102,'synthetic-browser-three','合成试点员工','employee','["publish"]','test-only',0),
          (9103,'synthetic-browser-other','合成其他员工','employee','[]','test-only',0);
      INSERT INTO user_office_grants(user_id,office_id) VALUES (9102,'three'),(9103,'elsewhere');
      INSERT INTO auth_sessions(token_hash,user_id,csrf,expires_at)
        VALUES('${tokenHash}',9101,'${csrf}',extract(epoch from now())::bigint+3600),
          ('${createHash('sha256').update(officeThreeSession).digest('hex')}',9102,'${randomBytes(32).toString('hex')}',extract(epoch from now())::bigint+3600),
          ('${createHash('sha256').update(otherOfficeSession).digest('hex')}',9103,'${randomBytes(32).toString('hex')}',extract(epoch from now())::bigint+3600);
      COMMIT;`);

    let base;
    app = spawn(board, ['-listen', '127.0.0.1:0'], { cwd: root, env: appEnv, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
    app.stderr.on('data', chunk => {
      const address = String(chunk).match(/address=(127\.0\.0\.1:\d+)/)?.[1];
      if (address) base = 'http://' + address;
    });
    await poll(() => base, 'Go 站点启动');
    assert.equal((await fetch(base + '/api/accounts?office_id=three')).status, 401);
    browser = spawn(chromeExe, [
      '--headless', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
      '--user-data-dir=' + profile, '--window-size=1440,900', '--no-first-run',
      '--disable-background-networking', '--disable-component-update', '--disable-sync',
      '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', 'about:blank',
    ], { windowsHide: true, stdio: 'ignore' });
    const devToolsPort = await poll(() => {
      try { return Number(fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]); }
      catch { return 0; }
    }, 'Chromium 启动');
    const tabs = await (await fetch(`http://127.0.0.1:${devToolsPort}/json/list`)).json();
    cdp = await openCDP(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    await cdp.send('Network.enable');
    const errors = [], requests = [], pausedErrors = [];
    let exportedCount = null, exportStatus = null, dailyStatus = null, pagedStatus = null;
    const dailyRecovery = [];
    cdp.on('Runtime.exceptionThrown', ({ exceptionDetails }) => errors.push(exceptionDetails.exception?.description || exceptionDetails.text));
    cdp.on('Network.requestWillBeSent', ({ request }) => {
      const url = new URL(request.url);
      if (url.origin !== base) return;
      requests.push(url.pathname);
      if (url.pathname === '/api/workspace/export') exportedCount = JSON.parse(request.postData).ids?.length;
      if (url.pathname === '/api/daily-flags') dailyRecovery.push(url.searchParams);
    });
    cdp.on('Network.responseReceived', ({ response }) => {
      if (response.url === base + '/api/workspace/export') exportStatus = response.status;
      if (response.url === base + '/api/daily') dailyStatus = response.status;
      if (response.url.startsWith(base + '/api/accounts?')) pagedStatus = response.status;
    });
    await cdp.send('Network.setCookie', { name: 'board_session', value: cookie, url: base, httpOnly: true });
    await cdp.send('Browser.setDownloadBehavior', { behavior: 'deny' });
    let dragNetwork = false, staleProbe = false, failPagedRead = false, rejectDaily = false;
    cdp.on('Fetch.requestPaused', async ({ requestId, request }) => {
      const url = new URL(request.url);
      try {
        if (dragNetwork) await delay(260);
        if (staleProbe && url.searchParams.get('query') === '001') await delay(500);
        if (failPagedRead && url.pathname === '/api/accounts') {
          await cdp.send('Fetch.fulfillRequest', { requestId, responseCode: 503, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }], body: Buffer.from('{"error":"synthetic 503"}').toString('base64') });
          return;
        }
        if (rejectDaily && url.pathname === '/api/daily') {
          await cdp.send('Fetch.fulfillRequest', {
            requestId, responseCode: 409, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from(JSON.stringify({ error: '合成发布冲突' })).toString('base64'),
          });
          return;
        }
        await cdp.send('Fetch.continueRequest', { requestId });
      } catch (error) {
        // Aborting a fetch while its request is held may cancel the CDP request.
        if (!/Invalid InterceptionId|Invalid parameters/.test(error.message)) pausedErrors.push(error.message);
      }
    });
    await cdp.send('Fetch.enable', { patterns: ['/api/accounts?*', '/api/account/inline?*', '/api/account/patch', '/api/daily'].map(endpoint => ({ urlPattern: base + endpoint })) });

    const evaluate = async expression => {
      const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
      return result.result.value;
    };
    const until = (condition, label) => poll(() => evaluate(`Boolean(${condition})`), label);
    const rowCount = "document.querySelectorAll('#accountsBody [data-account-pilot-detail]').length";
    const hasRow = id => `!!document.querySelector('[data-account-pilot-detail="probe-${id}"]')`;
    const pending = "getComputedStyle(document.querySelector('#accountActiveFilters'), '::after').content.includes('正在更新')";
    const click = selector => evaluate(`(() => { const element = document.querySelector(${JSON.stringify(selector)}); if (!element || element.disabled) throw Error('不可点击'); element.click(); })()`);
    const input = async (selector, value) => {
      await evaluate(`(() => { const element = document.querySelector(${JSON.stringify(selector)}); element.focus(); element.select(); })()`);
      await cdp.send('Input.insertText', { text: value });
    };
    const observed = async (selector, type, predicate, action) => {
      await evaluate(`(${measureFromEvent.toString()})(${JSON.stringify({ selector, type, predicate })})`);
      await action();
      const result = await poll(async () => {
        const value = await evaluate('window.accountProbeResult');
        return typeof value === 'number' ? { value } : null;
      }, `${type} ${selector}`);
      assert.ok(result.value >= 0, '可见反馈未出现：' + selector);
      return result.value;
    };

    const stats = { cold_open: [], warm_open: [], filter_feedback: [], filter_results: [], next_feedback: [], next_results: [], detail: [], select_all: [], inline_open: [], inline_save: [], daily_feedback: [], daily_confirmation: [] };
    const open = async () => {
      const prior = requests.length;
      await cdp.send('Page.navigate', { url: base + '/?page=accounts&office=three' });
      await until(`${hasRow(1)} && ${rowCount} === 50 && window.AccountPaged.canAct()`, '首屏 50 条可操作');
      const firstPaint = await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(stamp => resolve(stamp))))');
      const paths = requests.slice(prior);
      assert.equal(paths.filter(route => route === '/api/accounts').length, 1, '进入默认首屏仅需一次分页请求');
      assert.ok(!paths.includes('/api/board'), '账号首屏读取了完整工作台');
      return firstPaint;
    };
    for (let i = 0; i < sampleCount; i++) {
      await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
      stats.cold_open.push(await open());
      await cdp.send('Network.setCacheDisabled', { cacheDisabled: false });
      stats.warm_open.push(await open());
    }
    const dailyButton = '[data-publish="probe-1"]';
    const dailyDay = await evaluate("document.querySelector('#date').value");
    const dailyKey = 'daily:probe-1:' + dailyDay;
    const displayedDaily = published =>
      `document.querySelector('${dailyButton}')?.getAttribute('aria-pressed') === '${published}' && document.querySelector('${dailyButton}')?.textContent.includes('${published ? '已发' : '未发'}')`;
    const dailyVersion = () => evaluate(`data.record_versions[${JSON.stringify(dailyKey)}]`);
    const readDaily = async () => {
      const params = new URLSearchParams({ office_id: 'three', date: dailyDay, account_id: 'probe-1' });
      const response = await fetch(base + '/api/accounts?' + params, { headers: { Cookie: 'board_session=' + cookie } });
      assert.equal(response.status, 200);
      const result = await response.json();
      return { published: Boolean(result.accounts[0].published), version: result.record_versions[dailyKey] };
    };
    dragNetwork = true;
    for (let i = 0; i < sampleCount; i++) {
      const selector = i % 2 ? '#accountDouyinQuery' : '#query';
      const term = i % 2 ? 'dy-probe-1' : '001';
      const started = Date.now();
      stats.filter_feedback.push(await observed(selector, 'input', pending, () => input(selector, term)));
      await until(`${hasRow(1)} && ${rowCount} === 1`, '精确筛选结果');
      stats.filter_results.push(Date.now() - started);
      await click('#clear');
      await until(`${hasRow(1)} && ${rowCount} === 50`, '恢复首屏');
      const nextStarted = Date.now();
      stats.next_feedback.push(await observed('#accountsNext', 'click', pending, () => click('#accountsNext')));
      await until(`${hasRow(51)} && !${hasRow(1)}`, '第二页');
      stats.next_results.push(Date.now() - nextStarted);
      await click('#accountsPrevious');
      await until(hasRow(1), '上一页');
      stats.detail.push(await observed('[data-account-pilot-detail="probe-1"]', 'click', "document.querySelector('#accountPilotDetailDialog')?.open", () => click('[data-account-pilot-detail="probe-1"]')));
      await click('#accountPilotDetailClose');
      stats.select_all.push(await observed('#selectAll', 'click', "document.querySelector('#accountsBody [data-select-account]:checked, #accountsBody input:checked')?.getClientRects().length > 0", () => click('#selectAll')));
      assert.ok(await evaluate("document.querySelector('#selectedCount').textContent.includes('175')"), '全选应覆盖 175 个匹配账号');
      await click('#selectAll');
      const cell = '[data-inline-account="probe-1"][data-inline-field="name"]';
      stats.inline_open.push(await observed(cell, 'dblclick', "document.querySelector('#accountInlineStatus')?.getClientRects().length > 0", () => evaluate(`document.querySelector('${cell}').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`)));
      await until("window.AccountInline.getState()?.phase === 'editing'", '行内可编辑');
      const name = '测试账号 001 修改 ' + i;
      await input('#accountInlineInput', name);
      stats.inline_save.push(await observed('[data-inline-action="save"]', 'click', "document.querySelector('#accountInlineStatus')?.textContent.includes('保存')", () => click('[data-inline-action="save"]')));
      await until(`!window.AccountInline.hasDraft() && document.querySelector('${cell}').textContent.includes(${JSON.stringify(name)})`, '行内保存与重绘');
      await until('window.AccountPaged.canAct()', '行内编辑后列表可操作');
      const before = Boolean(i % 2);
      const prior = requests.length, versionBefore = await dailyVersion();
      assert.ok(await evaluate(displayedDaily(before)), '点击前发布状态');
      dailyStatus = null;
      const saving = `document.querySelector('${dailyButton}')?.textContent.includes('保存中') && document.querySelector('${dailyButton}')?.getAttribute('aria-pressed') === '${before}' && document.querySelector('${dailyButton}')?.disabled`;
      const startedDaily = Date.now();
      stats.daily_feedback.push(await observed(dailyButton, 'click', saving, () => click(dailyButton)));
      await until(displayedDaily(!before), '服务端确认后更新发布状态');
      stats.daily_confirmation.push(Date.now() - startedDaily);
      assert.equal(dailyStatus, 200, '发布状态必须等到真实写入返回成功');
      await until('window.AccountPaged.canAct()', '发布保存后列表可操作');
      const stored = await readDaily();
      assert.equal(stored.published, !before, '发布状态已持久化');
      assert.equal(await dailyVersion(), stored.version, '列表采用服务端新版本');
      assert.notEqual(stored.version, versionBefore, '发布写入必须更换版本');
      assert.deepEqual(requests.slice(prior).filter(route => route === '/api/daily'), ['/api/daily']);
      assert.ok(!requests.slice(prior).includes('/api/board'), '日常发布成功不需要读取完整看板');
    }
    dragNetwork = false;
    const storedBeforeReject = await readDaily();
    const recoveryIndex = dailyRecovery.length, beforeReject = requests.length;
    dailyStatus = null;
    rejectDaily = true;
    await click(dailyButton);
    await until("document.querySelector('#toast')?.textContent.includes('合成发布冲突') && window.AccountPaged.canAct()", '发布冲突后的反馈和列表恢复');
    rejectDaily = false;
    assert.equal(dailyStatus, 409, '发布冲突由明确拒绝触发');
    assert.ok(await evaluate(displayedDaily(storedBeforeReject.published)), '被拒绝的发布写入保留原状态');
    assert.equal(await dailyVersion(), storedBeforeReject.version, '被拒绝的发布写入保留原版本');
    assert.deepEqual(await readDaily(), storedBeforeReject, '被拒绝的发布写入没有更改数据库');
    assert.equal(dailyRecovery.length, recoveryIndex + 1, '发布冲突使用一次定向核对');
    assert.equal(dailyRecovery.at(-1).get('id'), 'probe-1', '发布冲突仅核对当前账号');
    assert.ok(!requests.slice(beforeReject).includes('/api/board'), '发布冲突不加载完整看板');
    staleProbe = true;
    await input('#query', '001');
    await delay(180);
    await input('#query', '002');
    await until(`${hasRow(2)} && ${rowCount} === 1`, '后发查询结果');
    await delay(600);
    assert.ok(await evaluate(hasRow(2)), '迟到结果覆盖了更新后的筛选');
    staleProbe = false;
    await click('#clear');
    await until(`${hasRow(1)} && ${rowCount} === 50`, '恢复全部结果');
    await click('#exportVisibleAccounts');
    await poll(() => exportStatus, '导出 API 响应');
    assert.equal(exportStatus, 200, '跨页导出失败');
    assert.equal(exportedCount, 175, '导出范围应覆盖全部匹配账号');
    failPagedRead = true;
    await input('#query', '002');
    await until("!document.querySelector('#loadError').hidden", '分页失败反馈');
    assert.ok(await evaluate(`${hasRow(1)} && ${rowCount} === 50`), '分页失败应保留上次成功的列表');
    assert.equal(await evaluate('window.AccountPaged.canAct()'), false, '失败的查询不能使用旧范围进行导出或批量操作');
    failPagedRead = false;
    await click('#refresh');
    await until(`${hasRow(2)} && ${rowCount} === 1`, '查询重试');
    await click('#clear');
    await until(`${hasRow(1)} && ${rowCount} === 50`, '冲突测试准备');
    const cell = '[data-inline-account="probe-1"][data-inline-field="name"]';
    await evaluate(`document.querySelector('${cell}').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
    await until("window.AccountInline.getState()?.phase === 'editing'", '冲突测试编辑器');
    await input('#accountInlineInput', '本地冲突草稿');
    assert.equal(await evaluate('window.AccountPaged.load(2)'), false, '分页刷新不能替换编辑草稿');
    const headers = { Cookie: 'board_session=' + cookie, Origin: base, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' };
    const fresh = await (await fetch(base + '/api/account/inline?id=probe-1', { headers })).json();
    const externalUpdate = await fetch(base + '/api/account/patch', {
      method: 'POST', headers,
      body: JSON.stringify({ id: 'probe-1', changes: { name: '测试账号 001 并发更新' }, request_id: randomUUID(), return_account: true, _expected_versions: { 'account:probe-1': fresh.record_version } }),
    });
    assert.equal(externalUpdate.status, 200, '合成并发修改失败');
    await click('[data-inline-action="save"]');
    await until("window.AccountInline.getState()?.phase === 'conflict'", '版本冲突反馈');
    assert.equal(await evaluate("document.querySelector('#accountInlineInput').value"), '本地冲突草稿');
    await click('[data-inline-action="cancel"]');
    await until('!window.AccountInline.hasDraft()', '取消冲突草稿');
    const current = await (await fetch(base + '/api/account/inline?id=probe-1', { headers })).json();
    assert.equal(current.account.name, '测试账号 001 并发更新');
    await click('#accountsNext');
    await until(`${hasRow(51)} && !${hasRow(1)}`, '批量选择第二页');
    await click('[data-batch-all="accounts"][data-batch-page]');
    assert.ok(await evaluate("selected.size === 50 && Array.from({length:50}, (_, i) => 'probe-' + (i + 51)).every(id => selected.has(id))"), '选中本页的范围应为第二页 50 条记录');
    await click('[data-batch-all="accounts"]:not([data-batch-page])');
    assert.equal(await evaluate('selected.size'), 175, '批量全选的范围应为全部 175 条记录');
    await click('[data-batch-open="accounts"][data-batch-source="accounts"]');
    await until("document.querySelector('#batchEditor')?.open && document.querySelector('#batchEditor h2').textContent.includes('175')", '跨页批量编辑范围');
    await click('#closeBatchEditor');
    await click('#clear');
    await until(`${hasRow(1)} && ${rowCount} === 50 && window.AccountPaged.active()`, '批量回退后恢复分页模式');
    await click('[data-account-pilot-detail="probe-1"]');
    await click('#accountPilotDeviceWorkflow');
    await until("document.querySelector('#deviceWorkflowDialog')?.open", '分页详情按需打开登录恢复');
    await click('#deviceWorkflowDialog [data-workflow-close]');
    await click('#clear');
    await until(`${hasRow(1)} && ${rowCount} === 50 && window.AccountPaged.active()`, '登录恢复后回到分页模式');
    const previous = requests.length;
    await evaluate("const field = document.querySelector('#accountType'); field.value = '大号'; field.dispatchEvent(new Event('change', {bubbles:true}));");
    await until(`${hasRow(2)} && !${hasRow(1)}`, '高级筛选完整回退');
    assert.ok(requests.slice(previous).includes('/api/board'));
    const switchSession = async sessionToken => {
      await cdp.send('Page.navigate', { url: 'about:blank' });
      await until("location.href === 'about:blank'", '重建浏览器页面');
      await cdp.send('Network.setCookie', { name: 'board_session', value: sessionToken, url: base, httpOnly: true });
      pagedStatus = null;
    };

    await switchSession(officeThreeSession);
    await open();
    assert.equal(pagedStatus, 200, '办公室三员工读取试点分页');
    assert.ok(await evaluate("data.user.role === 'employee' && JSON.stringify(data.user.office_ids) === '[\"three\"]'"));
    assert.deepEqual(await evaluate("[...document.querySelectorAll('#officeTabs [data-office]')].map(node => node.dataset.office)"), ['all', 'three']);
    assert.ok(await evaluate("data.accounts.every(row => row.id.startsWith('probe-')) && data.computers.every(row => row.id !== 'other')"), '办公室三员工仅收到本办公室资料');
    assert.equal(await evaluate("document.querySelector('#newAccount').hidden && document.querySelector('#exportVisibleAccounts').disabled && !document.querySelector('[data-inline-account]')"), true, '员工界面不得授予未配置的新增、导出或编辑权限');
    assert.equal(await evaluate(`document.querySelector('${dailyButton}').disabled`), false, '员工保留已授予的发布权限');
    await click('#accountsNext');
    await until(`${hasRow(51)} && !${hasRow(1)}`, '员工翻到第二页');
    await click('#selectAll');
    assert.ok(await evaluate("selected.size === 175 && [...selected].every(id => /^probe-\\d+$/.test(id))"), '员工全选跨页且仅覆盖办公室三');
    dailyStatus = null;
    await click(dailyButton.replace('probe-1', 'probe-51'));
    await until("document.querySelector('[data-publish=\"probe-51\"]')?.getAttribute('aria-pressed') === 'true' && window.AccountPaged.canAct()", '办公室三员工成功记录发布');
    assert.equal(dailyStatus, 200, '发布权限在服务端通过校验');
    assert.equal(await evaluate('selected.size'), 175, '保存并刷新后保留员工跨页选择');
    const restoreStart = requests.length, formerPageOrigin = await evaluate('performance.timeOrigin');
    await cdp.send('Page.reload');
    await until(`performance.timeOrigin !== ${formerPageOrigin} && ${hasRow(51)} && !${hasRow(1)} && window.AccountPaged.canAct()`, '重新打开恢复员工第二页');
    assert.ok(requests.slice(restoreStart).includes('/api/accounts'));
    assert.ok(!requests.slice(restoreStart).includes('/api/board'), '恢复分页无需读取完整看板');
    await click('#selectAll');
    assert.equal(await evaluate('selected.size'), 175, '员工恢复第二页后全选范围仍然正确');
    await click('[data-account-pilot-detail="probe-51"]');
    await until("document.querySelector('#accountPilotDetailDialog')?.open", '员工打开本办公室详情');

    // Keep this synthetic session alive to exercise the same-document 403 path.
    await click('#accountPilotDetailClose');
    sql("BEGIN; DELETE FROM board_candidate.user_office_grants WHERE user_id=9102 AND office_id='three'; UPDATE board_candidate.auth_users SET authorization_version=authorization_version+1 WHERE id=9102; COMMIT;");
    pagedStatus = null;
    await click('#refresh');
    await until("!document.querySelector('#loadError').hidden && document.querySelector('#accountPilotDetailDialog')?.childElementCount === 0", '撤销办公室授权后丢弃关掉的详情');
    assert.equal(pagedStatus, 403, '原员工失去办公室三读取权限');
    assert.ok(await evaluate(`${rowCount} === 0 && !document.querySelector('#accountPilotDetailDialog')?.childElementCount && selected.size === 0 && data === null`), '403 后清空原列表、详情缓存和跨页选中项');

    await switchSession(otherOfficeSession);
    const deniedStart = requests.length;
    await cdp.send('Page.navigate', { url: base + '/?page=accounts&office=three' });
    await until("!!document.querySelector('#loadError') && !document.querySelector('#loadError').hidden", '其他办公室员工被拒绝进入试点范围');
    assert.equal(pagedStatus, 403);
    assert.ok(await evaluate(`${rowCount} === 0 && data === null`));
    assert.ok(!requests.slice(deniedStart).includes('/api/board'), '拒绝时不得回退加载完整看板');

    assert.deepEqual(errors, []);
    assert.deepEqual(pausedErrors, []);
    const timings = Object.fromEntries(Object.entries(stats).map(([key, values]) => [key, summarize(values)]));
    console.log(JSON.stringify({
      fixture: '175 Office 3 accounts, 1200 cross-office; real Go + PostgreSQL + Chromium',
      viewport: '1440x900',
      measured_role: 'owner',
      employee_scope_checks: 'passed',
      artificial_request_delay_ms: 260,
      timings,
    }, null, 2));
    for (const key of ['filter_feedback', 'next_feedback', 'detail', 'select_all', 'inline_open', 'inline_save', 'daily_feedback']) assert.ok(timings[key].p95_ms <= 200, key + ' P95 超过 200ms');
    assert.ok(timings.warm_open.p95_ms <= 1000, '暖开 P95 超过 1 秒');
    assert.ok(timings.cold_open.p95_ms <= 2000, '冷开 P95 超过 2 秒');
  } finally {
    cdp?.close();
    for (const process of [browser, app]) {
      if (process && process.exitCode === null) {
        process.kill();
        await poll(() => process.exitCode !== null || process.signalCode !== null, '关闭合成进程', 3000).catch(() => {});
      }
    }
    if (pgStarted || fs.existsSync(path.join(cluster, 'postmaster.pid'))) run(pg('pg_ctl'), ['-D', cluster, '-w', '-t', '15', '-m', 'fast', 'stop'], { env: pgEnv });
    assert.equal(path.dirname(root), parent);
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
