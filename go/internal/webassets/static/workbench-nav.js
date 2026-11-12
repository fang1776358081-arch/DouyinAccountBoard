/* Shared navigation for all authenticated workbench pages. */
window.WorkbenchNav = (() => {
 const host = document.querySelector('.side-nav');
 if (!host) return {};
 const params = new URLSearchParams(location.search);
 const workspace = location.pathname === '/workspace.html';
 const mainBoard = ['/', '/index.html'].includes(location.pathname);
 const groups = [
  ['日常工作', [
   ['todo', '今日待办', '/workspace.html', 'tab'],
   ['board', '每日发布', '/', 'page'],
   ['publish-review', '今日发布核对', '/publish-review.html'],
   ['nurture', '7日养号', '/workspace.html', 'tab'],
   ['collection', '作品采集', '/collection.html', null, 'collection_manage'],
  ]],
  ['账号与关联', [
   ['accounts', '账号资料', '/', 'page'],
   ['account-overview', '账号关联总览', '/workspace.html', 'tab', null, 'accounts'],
   ['positions', '发布位置', '/workspace.html', 'tab'],
   ['review', '资料待核对', '/workspace.html', 'tab'],
  ]],
  ['手机与卡', [
   ['phones', '手机设备', '/', 'page'],
   ['cards', '手机卡', '/', 'page'],
   ['employee-cards', '员工办卡', '/employee-cards.html'],
  ]],
  ['导入与同步', [
   ['exchange', '导入 / 导出', '/workspace.html', 'tab', 'exchange'],
   ['table-sync', '腾讯文档同步', '/table-sync.html', null, 'table-sync'],
  ]],
  ['系统管理', [
   ['history', '操作历史', '/workspace.html', 'tab', 'history_view'],
   ['users', '子账号与权限', '/users.html', null, 'owner'],
   ['backups', '备份与恢复', '/workspace.html', 'tab', 'owner'],
  ]],
 ];
 const workspaceView = tab => tab === 'accounts' ? 'account-overview' : tab;
 let view = mainBoard ? params.get('page') || 'board' : workspace ? workspaceView(params.get('tab') || 'todo') : location.pathname.slice(1, -5);
 let office = params.get('office') || (view === 'table-sync' ? 'three' : '');
 let actor = null, pilotEnabled = false, pilotActor = '';
 let sessionRequest;
 const nodes = [];
 const labels = new Map();
 for (const [title, entries] of groups) {
  const group = document.createElement('details');
  group.className = 'nav-group';
  group.name = 'workbench-navigation';
  const heading = document.createElement('summary');
  heading.textContent = title;
  const list = document.createElement('div');
  list.className = 'nav-submenu';
  group.append(heading, list);
  host.append(group);
  const links = [];
  for (const [key, label, path, query, permission, value = key] of entries) {
   const link = document.createElement('a');
   link.textContent = label;
   link.className = 'nav-item';
   if (mainBoard && path === '/') link.dataset.page = value;
   if (workspace && path === '/workspace.html') link.dataset.workTab = value;
   labels.set(key, title);
   list.append(link);
   links.push({key, path, query, permission, value, link});
  }
  nodes.push({group, links});
 }

 function allowed(permission) {
  if (!permission) return true;
  if (permission === 'owner') return actor?.role === 'owner';
  if (permission === 'table-sync') return actor?.role === 'owner' || office === 'three' && pilotEnabled;
  if (permission === 'exchange') return actor?.permissions?.some(key => key === 'data_export' || key === 'data_import') === true;
  return actor?.permissions?.includes(permission) === true;
 }

 function render(openActive = false) {
  for (const {group, links} of nodes) {
   for (const {key, path, query, permission, value, link} of links) {
    const current = key === view;
    const search = new URLSearchParams();
    if (query) search.set(query, value);
    if (key === 'table-sync') search.set('office_id', 'three');
    else if (office && (path === '/' || path === '/workspace.html' || key === 'publish-review')) search.set('office', office);
    const href = path + (search.size ? '?' + search : '');
    if (link.getAttribute('href') !== href) link.setAttribute('href', href);
    link.hidden = !allowed(permission);
    link.classList.toggle('active', current);
    if (current) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
   }
   group.hidden = links.every(({link}) => link.hidden);
   if (openActive && links.some(({key, link}) => key === view && !link.hidden)) group.open = true;
  }
  const section = document.querySelector('[data-nav-section]');
  if (section) section.textContent = labels.get(view) || '工作台';
 }

 function update(context = {}) {
  const previousView = view, previousActor = actor?.id;
  if ('user' in context) actor = context.user;
  if ('page' in context) view = context.page;
  if ('tab' in context) view = workspaceView(context.tab);
  if ('office' in context) office = context.office || '';
  if ('pilot' in context) pilotEnabled = context.pilot === 'enabled';
  else if (previousActor !== actor?.id) pilotEnabled = false;
  render(previousView !== view || previousActor !== actor?.id);
  if (office !== 'three' || !actor || actor.role === 'owner' || 'pilot' in context || pilotActor === String(actor.id)) return;
  const id = String(actor.id);
  pilotActor = id;
  fetch('/api/table-sync?office_id=three', {cache: 'no-store'})
   .then(response => response.ok ? response.json() : null)
   .then(result => {
    if (String(actor?.id) !== id) return;
    pilotEnabled = result?.pilot?.state === 'enabled';
    render();
   }).catch(() => { if (pilotActor === id) pilotActor = ''; });
 }

 function loadSession() {
  if (actor || sessionRequest) return sessionRequest;
  sessionRequest = fetch('/api/auth/me', {cache: 'no-store'})
   .then(response => response.ok ? response.json() : null)
   .then(result => { if (!actor && result?.user) update({user: result.user}); })
   .catch(() => {}).finally(() => { sessionRequest = null; });
  return sessionRequest;
 }

 render(true);
 return Object.freeze({update, loadSession});
})();
