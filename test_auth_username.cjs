// Actual auth-ui.js in a controlled DOM: synthetic identities and deferred API
// promises only. No app bootstrap, network, browser, database or saved secrets.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, 'static', 'auth-ui.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'static', 'index.html'), 'utf8');
const tests = [];
const test = (name, run) => tests.push({name, run});
const fieldsByForm = {
  usernameForm: ['currentUsername', 'newUsername', 'usernamePassword'],
  passwordForm: ['oldPassword', 'newPassword', 'confirmPassword'],
};
const syntheticPassword = 'synthetic-current-confirmation';

function harness(role = 'owner') {
  const fields = new Map(), callbacks = [], requests = [], events = [];
  function field(id) {
    if (!fields.has(id)) {
      const listeners = new Map();
      const item = {id, value: '', textContent: '', hidden: true, disabled: false, open: false,
        querySelector(selector) {
          assert.equal(selector, 'button[type="submit"]');
          return field(id + 'Submit');
        },
        addEventListener(type, callback) {
          if (!listeners.has(type)) listeners.set(type, []);
          listeners.get(type).push(callback);
        },
        emit(type) {
          const event = {defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}};
          for (const callback of listeners.get(type) || []) callback(event);
          return event;
        },
        reset() {for (const name of fieldsByForm[id] || []) field(name).value = '';},
        showModal() {this.open = true;},
        close() {this.open = false;this.emit('close');},
        cancel() {const event = this.emit('cancel');if (!event.defaultPrevented) this.close();return event;},
      };
      fields.set(id, item);
    }
    return fields.get(id);
  }
  const context = {
    $: field,
    data: {user: {id: 7001, username: 'synthetic_owner', name: '合成总管理员', role,
      permissions: []}, phone_cards: {importable: false}, collection: {enabled: false}},
    document: {
      querySelectorAll() {return [];},
      addEventListener(type, callback) {assert.equal(type, 'DOMContentLoaded');callbacks.push(callback);},
    },
    api(url, body) {
      return new Promise((resolve, reject) => requests.push({url, body, resolve, reject}));
    },
    clearBoardCache() {events.push('clear-cache');},
    location: {replace(url) {events.push('redirect:' + url);}},
    async load() {events.push('load');},
    toast(message) {events.push('toast:' + message);},
  };
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'static/auth-ui.js'});
  for (const callback of callbacks) callback();
  const run = command => vm.runInContext(command, context);
  return {context, field, requests, events, run,
    open: () => field('changeOwnUsername').onclick(),
    submit: form => field(form).onsubmit({preventDefault() {}}),
    fill(username = 'synthetic-renamed') {
      field('newUsername').value = username;
      field('usernamePassword').value = syntheticPassword;
    },
  };
}

function input(id) {
  const match = html.match(new RegExp('<input\\b[^>]*\\bid="' + id + '"[^>]*>'));
  assert.ok(match, 'missing input #' + id);
  return match[0];
}

test('markup defaults to owner-hidden action and immutable current login name', () => {
  assert.match(html, /<button\b[^>]*id="changeOwnUsername"[^>]*\bhidden(?:\s|>)/);
  assert.match(html, /<dialog\b[^>]*id="usernameDialog"[^>]*aria-labelledby="usernameTitle"/);
  assert.match(input('currentUsername'), /\breadonly(?:\s|>)/);
  assert.match(input('newUsername'), /\brequired(?:\s|>)/);
  assert.match(input('newUsername'), /\bminlength="3"/);
  assert.match(input('newUsername'), /\bmaxlength="40"/);
  const pattern = input('newUsername').match(/\bpattern="([^"]+)"/)[1];
  assert.equal(pattern, '[A-Za-z0-9_.\\-]+');
  // Modern HTML compiles pattern attributes with Unicode-set ('v') semantics.
  // Escape the literal hyphen there; JS's explicit username validation is kept.
  const htmlPattern = new RegExp('^(?:' + pattern + ')$', 'v');
  for (const value of ['synthetic-owner', 'synthetic_owner', 'synthetic.owner', 'User123'])
    assert.equal(htmlPattern.test(value), true);
  for (const value of ['has space', '中文', 'bad/name']) assert.equal(htmlPattern.test(value), false);
  assert.match(input('usernamePassword'), /\btype="password"/);
  assert.match(input('usernamePassword'), /\bautocomplete="current-password"/);
  assert.doesNotMatch(input('usernamePassword'), /\bvalue=/);
  assert.match(html, /不修改密码、管理员身份、权限或账号、手机卡、设备等业务资料/);
  assert.match(html, /所有设备的登录都会失效/);
  assert.match(html, /新登录名和原密码重新登录/);
  assert.doesNotMatch(source + html, /fangzheng/);
  assert.match(html, /auth-ui\.js\?v=20261028-own-username/);
});

test('owner sees rename action while other roles cannot open or submit it', async () => {
  const owner = harness();
  owner.run('applyPermissions()');
  assert.equal(owner.field('changeOwnUsername').hidden, false);
  for (const role of ['admin', 'employee', 'viewer']) {
    const page = harness(role);
    page.run('applyPermissions()');
    assert.equal(page.field('changeOwnUsername').hidden, true);
    page.open();
    assert.equal(page.field('usernameDialog').open, false);
    page.fill();
    await page.submit('usernameForm');
    assert.equal(page.requests.length, 0);
    assert.match(page.field('usernameError').textContent, /仅总管理员/);
  }
});

test('opening shows current login name and clears stale inputs without defaults', () => {
  const page = harness();
  page.fill();
  page.field('usernameError').textContent = 'stale synthetic error';
  page.open();
  assert.equal(page.field('currentUsername').value, 'synthetic_owner');
  assert.equal(page.field('newUsername').value, '');
  assert.equal(page.field('usernamePassword').value, '');
  assert.equal(page.field('usernameError').textContent, '');
  assert.equal(page.field('usernameDialog').open, true);
});

for (const username of ['ab', 'a'.repeat(41), 'has space', '<script>', '中文账号', ''])
  test('invalid username is blocked before a request: ' + (username.length || 'empty'), async () => {
    const page = harness();
    page.open();page.fill(username);
    await page.submit('usernameForm');
    assert.equal(page.requests.length, 0);
    assert.match(page.field('usernameError').textContent, /3至40位/);
  });

test('unchanged name and missing password are blocked before a request', async () => {
  const page = harness();
  page.open();page.fill('synthetic_owner');
  await page.submit('usernameForm');
  assert.match(page.field('usernameError').textContent, /相同/);
  page.fill();page.field('usernamePassword').value = '';
  await page.submit('usernameForm');
  assert.match(page.field('usernameError').textContent, /当前密码/);
  assert.equal(page.requests.length, 0);
});

test('successful rename sends only current password and name, clears forms/cache and redirects', async () => {
  const page = harness();
  page.open();page.fill(' synthetic.New-owner_1 ');
  page.field('oldPassword').value = syntheticPassword;
  page.field('newPassword').value = 'synthetic-never-change-password';
  page.field('confirmPassword').value = 'synthetic-never-change-password';
  const submit = page.submit('usernameForm');
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, '/api/auth/username');
  assert.deepEqual(JSON.parse(JSON.stringify(page.requests[0].body)), {
    username: 'synthetic.New-owner_1', current_password: syntheticPassword,
  });
  assert.equal(page.events.length, 0);
  page.requests[0].resolve({ok: true});await submit;
  for (const form of Object.values(fieldsByForm)) for (const id of form) assert.equal(page.field(id).value, '');
  assert.equal(page.field('usernameDialog').open, false);
  assert.deepEqual(page.events, ['clear-cache', 'redirect:/login.html']);
  assert.equal(page.field('usernameFormSubmit').disabled, false);
});

test('pending rename rejects repeat and cross-credential submits and cannot close/cancel', async () => {
  const page = harness();
  page.open();page.fill();
  const pending = page.submit('usernameForm');
  for (const id of ['usernameFormSubmit', 'passwordFormSubmit', 'closeUsername', 'closePassword', 'changeOwnUsername', 'changeOwnPassword'])
    assert.equal(page.field(id).disabled, true);
  await page.submit('usernameForm');await page.submit('passwordForm');
  assert.equal(page.requests.length, 1);
  page.field('closeUsername').onclick();
  assert.equal(page.field('usernameDialog').open, true);
  assert.equal(page.field('usernameDialog').cancel().defaultPrevented, true);
  assert.equal(page.field('usernamePassword').value, syntheticPassword);
  page.requests[0].reject(Error('合成请求失败'));await pending;
  assert.equal(page.field('usernamePassword').value, '');
  assert.equal(page.field('closeUsername').disabled, false);
});

for (const {message, status} of [
  {message: '当前密码不正确', status: 400},
  {message: '登录名已存在', status: 400},
  {message: '页面已过期，请刷新后重试', status: 403},
])
  test('HTTP ' + status + ' failure is visible without clearing cache or changing account: ' + message, async () => {
    const page = harness();
    page.open();page.fill();
    const pending = page.submit('usernameForm');
    page.requests[0].reject(Object.assign(Error(message), {status}));await pending;
    assert.equal(page.field('usernameError').textContent, message);
    assert.equal(page.field('usernamePassword').value, '');
    assert.equal(page.field('newUsername').value, 'synthetic-renamed');
    assert.equal(page.field('usernameDialog').open, true);
    assert.equal(page.field('usernameFormSubmit').disabled, false);
    assert.equal(page.context.data.user.username, 'synthetic_owner');
    assert.deepEqual(page.events, []);
  });

test('lost network response reports an unconfirmed result and directs fresh login instead of duplicate save', async () => {
  const page = harness();
  page.open();page.fill();
  const pending = page.submit('usernameForm');
  page.requests[0].reject(Error('synthetic lost response'));await pending;
  assert.equal(page.field('usernameError').textContent,
    '请求结果未确认：请先尝试用新登录名和原密码重新登录；不要重复保存。');
  assert.equal(page.field('usernamePassword').value, '');
  assert.equal(page.field('newUsername').value, 'synthetic-renamed');
  assert.equal(page.field('usernameDialog').open, true);
  assert.equal(page.requests.length, 1);
  assert.deepEqual(page.events, []);
});

for (const close of ['button', 'cancel', 'native-close']) test('rename ' + close + ' clears sensitive inputs and errors', () => {
  const page = harness();
  page.open();page.fill();page.field('usernameError').textContent = 'synthetic error';
  if (close === 'button') page.field('closeUsername').onclick();
  else if (close === 'cancel') page.field('usernameDialog').cancel();
  else page.field('usernameDialog').close();
  assert.equal(page.field('usernamePassword').value, '');
  assert.equal(page.field('newUsername').value, '');
  assert.equal(page.field('usernameError').textContent, '');
  assert.equal(page.field('usernameDialog').open, false);
  assert.equal(page.requests.length, 0);
});

for (const close of ['button', 'cancel', 'native-close']) test('password ' + close + ' clears all password inputs', () => {
  const page = harness();
  page.field('changeOwnPassword').onclick();
  for (const id of fieldsByForm.passwordForm) page.field(id).value = syntheticPassword;
  page.field('passwordError').textContent = 'synthetic error';
  if (close === 'button') page.field('closePassword').onclick();
  else if (close === 'cancel') page.field('passwordDialog').cancel();
  else page.field('passwordDialog').close();
  for (const id of fieldsByForm.passwordForm) assert.equal(page.field(id).value, '');
  assert.equal(page.field('passwordError').textContent, '');
  assert.equal(page.field('passwordDialog').open, false);
});

test('existing password flow rejects mismatch and duplicate submit without submitter dependency', async () => {
  const page = harness();
  page.field('changeOwnPassword').onclick();
  page.field('newPassword').value = 'synthetic-new-secret';
  page.field('confirmPassword').value = 'synthetic-mismatch';
  await page.submit('passwordForm');
  assert.match(page.field('passwordError').textContent, /不一致/);
  assert.equal(page.requests.length, 0);
  page.field('oldPassword').value = syntheticPassword;
  page.field('confirmPassword').value = 'synthetic-new-secret';
  const pending = page.submit('passwordForm');
  await page.submit('passwordForm');
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, '/api/auth/password');
  assert.equal(page.field('passwordDialog').cancel().defaultPrevented, true);
  page.requests[0].resolve({ok: true});await pending;
  for (const id of fieldsByForm.passwordForm) assert.equal(page.field(id).value, '');
  assert.equal(page.field('passwordDialog').open, false);
  assert.equal(page.events[0], 'load');
  assert.match(page.events[1], /密码已更新/);
  assert.equal(page.field('passwordFormSubmit').disabled, false);
});

test('a changed role at submit time is rejected even after owner opened dialog', async () => {
  const page = harness();
  page.open();page.fill();page.context.data.user.role = 'admin';
  await page.submit('usernameForm');
  assert.equal(page.requests.length, 0);
  assert.match(page.field('usernameError').textContent, /仅总管理员/);
});

(async () => {
  let failed = 0;
  for (const item of tests) {
    try {await item.run();console.log('PASS: ' + item.name);}
    catch (error) {failed++;console.error('FAIL: ' + item.name + '\n' + error.stack);}
  }
  if (failed) process.exitCode = 1;
  else console.log('PASS: ' + tests.length + ' auth username/password UI contracts (synthetic DOM/API only)');
})();
