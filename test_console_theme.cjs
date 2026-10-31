// Source-only presentation contracts: no browser, app bootstrap, network or DB.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const staticRoot = path.join(__dirname, 'static');
const read = name => fs.readFileSync(path.join(staticRoot, name), 'utf8');
const withoutComments = source => source.replace(/\/\*[\s\S]*?\*\//g, '');
const words = source => source.trim().split(/\s+/);
let completed = 0;
function check(name, run) {
  run();
  completed++;
  console.log('PASS: ' + name);
}

// These pages contain no embedded templates. Parse quoted tag attributes only;
// this is deliberately a static contract check, not a browser/CSS implementation.
function tags(html) {
  return [...html.matchAll(/<([a-z][\w:-]*)\b((?:"[^"]*"|'[^']*'|[^'">])*)>/gi)].map(match => {
    const attributes = {};
    for (const item of match[2].matchAll(/([^\s=/'">]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
      const key = item[1].toLowerCase();
      assert.ok(!Object.hasOwn(attributes, key), 'duplicate attribute: ' + key);
      attributes[key] = item[2] ?? item[3] ?? item[4] ?? '';
    }
    return {name: match[1].toLowerCase(), attributes};
  });
}
const has = (tag, attribute) => Object.hasOwn(tag.attributes, attribute);
const hasClass = (tag, value) => words(tag.attributes.class || '').includes(value);
function page(name) {
  const html = read(name), elements = tags(html), ids = new Map();
  for (const tag of elements.filter(tag => has(tag, 'id'))) {
    assert.ok(!ids.has(tag.attributes.id), name + ': duplicate ID ' + tag.attributes.id);
    ids.set(tag.attributes.id, tag);
  }
  return {name, html, elements, ids,
    styles: elements.filter(tag => tag.name === 'link' && tag.attributes.rel === 'stylesheet'),
    scripts: elements.filter(tag => tag.name === 'script')};
}
function asset(url) {
  assert.match(url, /^\/(?!\/)[A-Za-z0-9._/-]+(?:\?[^#\s]*)?$/, 'resource must be local: ' + url);
  const relative = url.split('?')[0].slice(1);
  assert.ok(!relative.split('/').includes('..'), 'resource must stay in static/');
  const target = path.join(staticRoot, relative);
  assert.equal(fs.statSync(target).isFile(), true, 'missing static resource: ' + relative);
  return relative;
}
function element(current, id, name) {
  const found = current.ids.get(id);
  assert.ok(found, current.name + ': preserve #' + id);
  if (name) assert.equal(found.name, name, id + ': preserve element type');
  return found;
}
function attributes(current, id, expected) {
  const found = element(current, id);
  for (const [key, value] of Object.entries(expected)) {
    if (value === true || value === false) assert.equal(has(found, key), value, id + ': ' + key);
    else assert.equal(found.attributes[key], value, id + ': ' + key);
  }
}

const pages = ['index.html', 'workspace.html', 'users.html', 'login.html'].map(page);
const consoles = pages.slice(0, 3), login = pages[3];
for (const current of pages) check(current.name + ': local assets and versioned theme with page overrides', () => {
  const expected = current === login ? 'login-theme.css' : 'console-theme.css';
  const styleAssets = current.styles.map(tag => asset(tag.attributes.href));
  assert.equal(styleAssets.at(-1), current === pages[0] ? 'card-filters.css' : expected);
  assert.equal(styleAssets.filter(name => name === expected).length, 1);
  assert.match(current.styles.at(-1).attributes.href, /\?v=\d{8}/);
  for (const script of current.scripts) {
    assert.ok(has(script, 'src'), current.name + ': no new inline scripts');
    asset(script.attributes.src);
  }
  assert.ok(current.elements.find(tag => tag.name === 'meta' && tag.attributes.name === 'viewport'));
  assert.ok(current.elements.find(tag => tag.name === 'html' && tag.attributes.lang === 'zh-CN'));
  for (const tag of current.elements) {
    assert.ok(!Object.keys(tag.attributes).some(key => /^on[a-z]+$/.test(key)), 'no inline event handlers');
  }
});

const expectedScripts = {
  'index.html': words(`device-workflow.js scope-ui.js edit-versions.js identity-labels.js
    field-search.js lifecycle.js card-filters.js phone-cards.js phone-devices.js offices.js publishing-layout.js
    publishing-match.js publishing-pending.js workspace-shortcuts.js auth-ui.js verification.js batch-edit.js account-workspace.js app.js`),
  'workspace.html': words(`device-workflow.js scope-ui.js identity-labels.js field-search.js
    edit-versions.js workspace-model.js account-review.js nurture.js batch-edit.js office-three-import.js workspace.js collection-panel.js`),
  'users.html': ['users.js'],
  'login.html': ['login.js'],
};
for (const current of pages) check(current.name + ': existing script order preserved', () => {
  assert.deepEqual(current.scripts.map(tag => tag.attributes.src.split('?')[0].slice(1)), expectedScripts[current.name]);
});

for (const current of consoles) check(current.name + ': three exclusive navigation groups', () => {
  assert.ok(hasClass(current.elements.find(tag => tag.name === 'body'), 'console-page'));
  const navigation = current.elements.find(tag => tag.name === 'nav' && hasClass(tag, 'side-nav'));
  assert.equal(navigation.attributes['aria-label'], '主导航');
  const groups = current.elements.filter(tag => tag.name === 'details' && hasClass(tag, 'nav-group'));
  assert.equal(groups.length, 3);
  assert.equal(groups.filter(tag => has(tag, 'open')).length, 1);
  for (const group of groups) assert.equal(group.attributes.name, 'workspace-navigation');
  const blocks = [...current.html.matchAll(/<details\b[^>]*class="nav-group"[^>]*>([\s\S]*?)<\/details>/g)];
  assert.equal(blocks.length, 3);
  blocks.forEach((block, index) => {
    assert.match(block[1], new RegExp('<summary><span>' + ['日常工作', '资料管理', '管理工具'][index] + '</span>'));
    assert.match(block[1], /class="nav-submenu"/);
  });
  assert.doesNotMatch(current.html, /class="workspace-tabs"/);
});

check('owner-only controls and collection-disabled defaults remain hidden', () => {
  for (const id of ['backupNav', 'permissionsNav', 'manageUsers', 'resumeCollect', 'pendingReviewPanel'])
    attributes(pages[0], id, {hidden: true});
  for (const id of ['workspaceBackupNav', 'peopleLink']) attributes(pages[1], id, {hidden: true});
  attributes(pages[1], 'workCollectionPause', {disabled: true});
  attributes(pages[1], 'workCollectionResume', {hidden: true, disabled: true});
  for (const id of ['ownerContent', 'officeManagement', 'scopeAssignment', 'scopeForm', 'auditSection'])
    attributes(pages[2], id, {hidden: true});
});

check('console form and rendering entry points remain present', () => {
  for (const id of words(`date currentUser title breadcrumb subtitle loadError backupError officeWorkspace
    officeTabs stats accountWorkspace boardView matrixHead matrixBody accountsView accountsBody
    cardsWorkspace cardsBody phonesWorkspace phonesBody phoneDeviceEditor phoneDeviceForm deviceId
    cardEditor cardForm cardPhone cardEditCarrier editor editForm editId editName editPhoneDevice
    passwordDialog passwordForm oldPassword newPassword confirmPassword publishingMatchDialog
    publishingMatchForm matchCandidates matchError confirmPublishingMatch toast`)) element(pages[0], id);
  for (const id of words(`workspaceUser workDate reloadWorkspace workspaceError workspaceBody
    overviewDialog overviewTitle overviewBody positionDialog positionForm positionSource positionTarget
    positionError removeAssignment`)) element(pages[1], id);
  for (const id of words(`pageError officeForm officeName scopeKind scopeEntity scopeOffices scopeMessage
    userSearch userFilter addPerson refreshPeople peopleBody peopleAudit personDialog personForm
    personUsername personName personRole personActive personPassword personConfirm personGrants
    personError permissionGuide`)) element(pages[2], id);
  attributes(pages[0], 'deviceId', {required: true, pattern: '[0-9]{1,6}', inputmode: 'numeric', maxlength: '6'});
  attributes(pages[0], 'cardPhone', {required: true, pattern: '1[0-9]{10}', inputmode: 'numeric', maxlength: '11'});
  attributes(pages[0], 'oldPassword', {type: 'password', required: true, autocomplete: 'current-password'});
  for (const id of ['newPassword', 'confirmPassword'])
    attributes(pages[0], id, {type: 'password', required: true, minlength: '10', maxlength: '128', autocomplete: 'new-password'});
  attributes(pages[2], 'personUsername', {required: true, minlength: '3', maxlength: '40', pattern: '[A-Za-z0-9_.-]+'});
  for (const id of ['personPassword', 'personConfirm'])
    attributes(pages[2], id, {type: 'password', minlength: '10', maxlength: '128', autocomplete: 'new-password'});
});

check('login and first-owner setup retain their shared form contract', () => {
  const body = login.elements.find(tag => tag.name === 'body');
  for (const name of ['login-page', 'enterprise-login']) assert.ok(hasClass(body, name));
  element(login, 'loginForm', 'form');
  attributes(login, 'loginForm', {action: false});
  attributes(login, 'setupName', {hidden: true, for: 'loginName'});
  attributes(login, 'confirmField', {hidden: true, for: 'loginConfirm'});
  attributes(login, 'loginName', {maxlength: '60', autocomplete: 'name'});
  attributes(login, 'loginUsername', {required: true, minlength: '3', maxlength: '40', pattern: '[A-Za-z0-9_.-]+', autocomplete: 'username'});
  attributes(login, 'loginPassword', {type: 'password', required: true, minlength: '10', maxlength: '128', autocomplete: 'current-password', value: false});
  attributes(login, 'loginConfirm', {type: 'password', required: false, minlength: '10', maxlength: '128', autocomplete: 'new-password', value: false});
  attributes(login, 'loginSubmit', {type: 'submit'});
  attributes(login, 'loginError', {role: 'alert'});
  for (const match of read('login.js').matchAll(/input\('([^']+)'\)/g)) element(login, match[1]);
  for (const id of ['loginUsername', 'loginPassword'])
    assert.ok(login.elements.some(tag => tag.name === 'label' && tag.attributes.for === id));
});

check('login resets legacy grid centering and keeps its layout full width', () => {
  const css = withoutComments(read('login-theme.css'));
  const body = /(?:^|})\s*\.enterprise-login\s*\{([^{}]*)\}/.exec(css);
  const layout = /(?:^|})\s*\.enterprise-login\s+\.login-layout\s*\{([^{}]*)\}/.exec(css);
  assert.ok(body, 'login body needs an explicit presentation reset');
  assert.ok(layout, 'login main needs its own base layout rule');
  assert.match(body[1], /(?:^|;)\s*place-items\s*:\s*normal\s*(?:;|$)/);
  assert.match(layout[1], /(?:^|;)\s*width\s*:\s*100%\s*(?:;|$)/);
  assert.match(layout[1], /(?:^|;)\s*margin\s*:\s*0\s*(?:;|$)/);
});

check('themes preserve hidden semantics and do not force closed dialogs visible', () => {
  for (const current of pages) {
    const loaded = current.styles.map(tag => withoutComments(read(asset(tag.attributes.href)))).join('\n');
    assert.match(loaded, /\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important\s*;?[^}]*\}/i);
  }
  for (const name of ['console-theme.css', 'login-theme.css']) {
    const css = withoutComments(read(name));
    assert.doesNotMatch(css, /@import\b|url\s*\(/i, name + ': no remote or missing visual assets');
    assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const display = /(?:^|;)\s*display\s*:\s*([^;}]+)/i.exec(match[2]);
      if (!display) continue;
      if (match[1].includes('[hidden]')) assert.match(display[1], /^none\s*!important$/i);
      if (/\bdialog\b/.test(match[1]) && !/\[open\]|:modal/.test(match[1]))
        assert.match(display[1], /^none(?:\s*!important)?$/i, 'closed dialog visibility remains browser-controlled');
    }
  }
});

check('source-only package includes both theme stylesheets', () => {
  const source = fs.readFileSync(path.join(__dirname, 'server_package.py'), 'utf8');
  const match = /STATIC_FILES\s*=\s*'''([\s\S]*?)'''\.split\(\)/.exec(source);
  assert.ok(match, 'explicit static allowlist must remain inspectable');
  const allowlist = words(match[1]);
  for (const name of ['console-theme.css', 'login-theme.css']) assert.equal(allowlist.filter(item => item === name).length, 1);
});

console.log('PASS: ' + completed + ' console/login theme contracts (source-only; no browser/network/DB)');
