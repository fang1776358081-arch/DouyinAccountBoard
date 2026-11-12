/* A read-only list, refreshed with the board's authorized snapshot and updates. */
window.BanImpact = (() => {
 const bannedStatuses = new Set(['封禁', '短期封禁']);
 const stateLabels = {
  cancel_review: '待确认是否注销', in_use: '仍有账号使用', review: '待核对',
  cancel_pending: '待注销', cancelled: '已注销', exception: '发布异常'
 };
 const missingLabels = {card:'注册卡', phone:'登录手机', slot:'发布格'};
 const relatedVersion = /^(account|card|device|unit|access|layout):/;
 let panel, countNode, statusNode, contentNode, retryButton, focusedLabel, resetFocus;
 let source = null, authorization = '', bannedCount = 0, pending = null, focused = null;

 function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value != null) node.textContent = String(value);
  return node;
 }

 function mount() {
  if (panel) return true;
  const anchor = document.getElementById('accountWorkspace');
  if (!anchor) return false;
  panel = element('details', 'ban-impact');
  panel.id = 'banImpact';
  panel.innerHTML = `<summary><strong>封禁影响清单</strong> <span data-ban-count aria-live="polite"></span></summary>
   <div class="ban-impact-body">
    <p class="ban-impact-note">按当前授权范围查看。账号状态和发布位置以当前资料为准。</p>
    <div class="ban-impact-focus" data-ban-focus hidden><strong data-ban-focused-label></strong><button type="button" class="text-button" data-ban-reset>查看全部</button></div>
    <p data-ban-status role="status"></p>
    <button type="button" class="text-button" data-ban-retry hidden>重试读取</button>
    <div class="ban-impact-groups" data-ban-content></div>
   </div>`;
  countNode = panel.querySelector('[data-ban-count]');
  statusNode = panel.querySelector('[data-ban-status]');
  contentNode = panel.querySelector('[data-ban-content]');
  retryButton = panel.querySelector('[data-ban-retry]');
  focusedLabel = panel.querySelector('[data-ban-focused-label]');
  resetFocus = panel.querySelector('[data-ban-reset]');
  resetFocus.addEventListener('click', () => {focused = null; void refresh();});
  retryButton.addEventListener('click', () => void refresh());
  panel.addEventListener('toggle', () => {
   if (!panel.open) focused = null;
   if (!panel.hidden && (!panel.open || !pending)) void refresh();
  });
  anchor.before(panel);
  return true;
 }

 function setStatus(message, retry = false) {
  statusNode.textContent = message;
  statusNode.hidden = !message;
  retryButton.hidden = !retry;
 }

 function accountLabel(account) {
  if (!account) return '账号待核对';
  return [account.name || '昵称待补充', account.douyin_id && '抖音号 ' + account.douyin_id].filter(Boolean).join(' · ');
 }

 function accountNames(accounts) {
  return accounts.map(account => accountLabel(account) + (account?.status ? `（${account.status}）` : '')).join('、');
 }

 function group(title, rows, empty, makeRow) {
  const section = element('section', 'ban-impact-group');
  const heading = element('h3', '', title);
  heading.append(' ', element('span', 'ban-impact-number', rows.length));
  section.append(heading);
  if (!rows.length) {
   section.append(element('p', 'ban-impact-note', empty));
   return section;
  }
  const list = element('ul');
  for (const row of rows) list.append(makeRow(row));
  section.append(list);
  return section;
 }

 function affectedRow(title, state, reason, banned, others, action) {
  const item = element('li', 'ban-impact-item');
  const heading = element('div', 'ban-impact-item-heading');
  heading.append(element('strong', '', title), element('span', 'ban-impact-state', state));
  if (action) heading.append(action);
  item.append(heading, element('p', '', reason));
  if (banned.length) item.append(element('p', 'ban-impact-note', '相关封禁账号：' + accountNames(banned)));
  if (others.length) item.append(element('p', 'ban-impact-note', '其他关联账号：' + accountNames(others)));
  return item;
 }

 function button(label, key, id) {
  const node = element('button', 'text-button ban-impact-link', label);
  node.type = 'button';
  node.dataset[key] = String(id);
  return node;
 }

 function paint(result) {
  const accounts = focused ? result.accounts.filter(account => String(account.id) === focused.id) : result.accounts;
  const byId = new Map(accounts.map(account => [String(account.id), account]));
  const related = ids => (ids || []).map(id => byId.get(String(id))).filter(Boolean);
  countNode.textContent = `${accounts.length} 个账号`;
  if (!accounts.length) {
   setStatus(focused ? '该账号已不在可核对的封禁清单，请查看当前发布格。' : '当前授权范围内没有封禁账号。');
   return;
  }
  setStatus('');
  const missing = accounts.flatMap(account => (account.missing || []).map(item => ({account, ...item})));
  const matchesSlot = slot => focused && slot.computer_id === focused.computer && String(slot.slot) === focused.slot;
  const slots = result.slots.filter(slot => byId.has(String(slot.account_id)));
  if (focused) slots.sort((left, right) => Number(matchesSlot(right)) - Number(matchesSlot(left)));
  const sections = [
   group('受影响的手机卡', result.cards.filter(card => related(card.account_ids).length), '暂无可确认的关联手机卡。', card =>
    affectedRow(card.phone || '手机号待核对', stateLabels[card.state] || '待核对', card.reason,
     related(card.account_ids), card.other_accounts || [], card.id == null ? null : button('查看卡片', 'cardDetails', card.id))),
   group('受影响的手机', result.phones.filter(phone => related(phone.account_ids).length), '暂无可确认的登录手机。', phone =>
    affectedRow(phone.label || '手机待核对', '待人工核对', phone.reason,
     related(phone.account_ids), phone.other_accounts || [], window.DeviceWorkflow?.enabled() && phone.device_id
      ? button('查看手机', 'workflowDevice', phone.device_id) : null)),
   group('受影响的发布格', slots, '暂无已关联的发布格。', slot =>
    affectedRow(`${slot.computer_name || '电脑待核对'} · 发布设备 ${slot.slot}${slot.source_cell ? ' · ' + slot.source_cell : ''}`,
     matchesSlot(slot) ? '当前发布格' : stateLabels[slot.state] || '发布异常', slot.reason, related([slot.account_id]), [], null))
  ];
  if (missing.length) sections.push(group('关联待核对', missing, '', item =>
   affectedRow(accountLabel(item.account), missingLabels[item.kind] || '待核对', item.reason, [], [], null)));
  contentNode.append(...sections);
 }

 function permissionKey(user) {
  return JSON.stringify([user.id, user.authorization_version, user.scope_mode,
   [...(user.office_ids || [])].sort(), [...(user.permissions || [])].sort()]);
 }

 function versionsChanged(before, after) {
  return before !== after && Object.keys(after || {}).some(key => relatedVersion.test(key) && before?.[key] !== after[key]);
 }

 async function refresh() {
  pending?.abort();
  pending = null;
  contentNode.replaceChildren();
  focusedLabel.parentElement.hidden = !focused;
  focusedLabel.textContent = focused ? '正在核对 ' + focused.name : '';
  if (!bannedCount) {
   countNode.textContent = '0 个账号';
   setStatus(focused ? '该账号已不在可核对的封禁清单，请查看当前发布格。' : '当前授权范围内没有封禁账号。');
   return;
  }
  if (!panel.open) {
   countNode.textContent = '查看关联';
   setStatus('');
   return;
  }
  const request = new AbortController();
  pending = request;
  countNode.textContent = '正在核对…';
  setStatus('正在核对关联资料…');
  try {
   const result = await api('/api/ban-impact', undefined, {applyVersions:false, signal:request.signal});
   if (pending !== request || !panel.open) return;
   if (!['accounts', 'cards', 'phones', 'slots'].every(key => Array.isArray(result?.[key]))) throw Error('invalid impact response');
   paint(result);
  } catch (error) {
   if (pending === request && panel.open) {
    countNode.textContent = '读取失败';
    setStatus('封禁影响清单读取失败。', error?.status !== 401);
   }
  } finally {
   if (pending === request) pending = null;
  }
 }

 function sync(board, view) {
  const visible = board?.user?.id != null && ['board', 'accounts', 'cards', 'phones'].includes(view);
  if (!visible) {
   pending?.abort();
   pending = null;
   source = null;
   authorization = '';
   focused = null;
   if (panel) {
    panel.hidden = true;
    panel.open = false;
    contentNode.replaceChildren();
   }
   return;
  }
  if (!mount()) return;
  panel.hidden = false;
  const scope = permissionKey(board.user);
  const leaveFocusedBoard = view !== 'board' && !!focused;
  const changed = leaveFocusedBoard || scope !== authorization || source?.board !== board || source?.cursor !== board.cursor ||
   versionsChanged(source?.versions, board.record_versions);
  if (!changed) {
   source.versions = board.record_versions;
   return;
  }
  if (leaveFocusedBoard || scope !== authorization) {
   panel.open = false;
   focused = null;
  }
  authorization = scope;
  source = {board, cursor:board.cursor, versions:board.record_versions};
  bannedCount = (board.accounts || []).filter(account => bannedStatuses.has(account.status)).length;
  void refresh();
 }

 document.addEventListener('click', event => {
  const button = event.target.closest?.('[data-board-ban-account]');
  if (!button || !source || panel.hidden) return;
  const id = button.dataset.boardBanAccount;
  const account = source.board.accounts.find(row => String(row.id) === id && bannedStatuses.has(row.status));
  const cell = button.closest('[data-publish-computer][data-publish-slot]');
  if (!account || !cell) return;
  focused = {id, name: accountLabel(account), computer: cell.dataset.publishComputer, slot: cell.dataset.publishSlot};
  panel.open = true;
  void refresh();
  panel.querySelector('summary').focus();
 });

 return Object.freeze({sync});
})();
