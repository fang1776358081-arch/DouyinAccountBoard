/* Full device information stays behind a detail view of the authorized board. */
window.DeviceDetails = (() => {
 let node = null, selected = null, openedUser = null;
 const text = value => value === undefined || value === null || value === '' ? '未登记' : String(value);
 const field = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(text(value))}</dd></div>`;
 const section = (title, content) => `<section><h3>${esc(title)}</h3>${content}</section>`;
 const record = id => data?.phone_devices?.devices.find(device => device.id === id);

 function dialog() {
  if (node) return node;
  node = document.createElement('dialog');
  node.id = 'deviceDetailsDialog';
  node.className = 'device-details-drawer';
  node.setAttribute('aria-labelledby', 'deviceDetailsTitle');
  document.body.appendChild(node);
  node.addEventListener('close', () => {selected = null; openedUser = null; node.innerHTML = '';});
  return node;
 }

 function close() {
  if (node?.open) node.close();
  selected = null; openedUser = null;
  if (node) node.innerHTML = '';
 }

 function render() {
  const device = record(selected);
  if (!device || String(data.user?.id) !== openedUser) {close(); return false;}
  const own = deviceAccounts(device.id), cards = deviceCards(device);
  const workflow = data.device_workflow?.enabled, unit = device.current_unit;
  const inScope = data.user?.scope_mode === 'all' || !!device.office_ids?.length && device.office_ids.every(office => data.user?.office_ids?.includes(office));
  const actions = `${inScope && hasPermission('device_manage') ? '<button type="button" class="button" data-device-details-edit>修改详细资料</button>' : ''}${workflow ? '<button type="button" class="button" data-device-details-workflow>故障 / 换机 / 旧机记录</button>' : ''}`;
  const identity = `<dl class="device-details-grid">${field('手机序号',deviceNumber(device))}${field('办公室',deviceOfficeText(device,data))}${field('手机名称',device.name)}${field('负责人',device.owner)}${field('设备位置',device.location)}${field('实物状态',workflow?unit?.status:device.state)}${field('现用实物',workflow?(unit?'第 '+unit.generation+' 台':'待核对'):'尚未建立实物记录')}${field('安排状态',device.state)}</dl><p class="device-details-muted">${esc(device.attention || (device.state_source==='auto'?'安排状态按当前账号判断。':'安排状态为人工登记。'))}</p>`;
  const accounts = own.length ? `<ul>${own.map(account=>`<li><strong>${esc(accountDisplay(account))}</strong><small>${esc(account.status)}${account.device_access?' · '+esc(account.device_access.login_state):''}</small>${workflow?`<button type="button" class="text-button" data-device-detail-account="${esc(account.id)}">登录恢复 / 处理记录</button>`:''}</li>`).join('')}</ul>` : `<p>${esc(emptyPhoneText(device))}</p>`;
  const cardList = cards.length ? `<ul>${cards.map(card=>`<li><button type="button" class="text-button" data-device-detail-card="${card.id}">${esc(card.phone)}</button><small>${esc(card.owner||'卡主待核对')} · ${esc(card.status)}${workflow?' · 卡槽'+esc(card.placement?.slot||'待确认'):''}</small></li>`).join('')}</ul>` : '<p>暂无已核对插卡记录。</p>';
  dialog().innerHTML = `<div class="device-details-head"><div><h2 id="deviceDetailsTitle">手机设备详情</h2><p>${esc(deviceNumber(device))}号手机 · ${esc(deviceOfficeText(device,data))}</p></div><button type="button" class="text-button" data-device-details-close aria-label="关闭手机设备详情">关闭</button></div><div class="device-details-content"><div class="device-details-actions">${actions}</div>${section('设备资料',identity)}${section('当前关联账号（'+own.length+'）',accounts)}${section('当前插卡（'+cards.length+'）',cardList)}${section('完整备注','<p class="device-details-note">'+esc(device.note||'暂无备注')+'</p>')}</div>`;
  node.querySelector('[data-device-details-close]').onclick = close;
  const edit = node.querySelector('[data-device-details-edit]');
  if (edit) edit.onclick = () => {close(); editPhoneDevice(device.id);};
  const history = node.querySelector('[data-device-details-workflow]');
  if (history) history.onclick = () => {close(); window.DeviceWorkflow?.openDevice(device.id);};
  node.querySelectorAll('[data-device-detail-account]').forEach(button => button.onclick = () => {const id = button.dataset.deviceDetailAccount; close(); window.DeviceWorkflow?.openAccount(id);});
  node.querySelectorAll('[data-device-detail-card]').forEach(button => button.onclick = () => {const id = button.dataset.deviceDetailCard; close(); window.CardDetails?.open(id);});
  return true;
 }

 function open(id) {
  if (window.DeviceInline?.hasDraft()) {DeviceInline.announceBlocked(); return false;}
  if (window.AccountInline?.hasDraft()) {AccountInline.announceBlocked(); return false;}
  if (window.CardInline?.hasDraft()) {CardInline.announceBlocked(); return false;}
  if (!record(id) || document.querySelector('dialog[open]')) return false;
  selected = id; openedUser = String(data.user?.id);
  if (!render()) return false;
  dialog().showModal();
  return true;
 }
 document.addEventListener('click', event => {
  const button = event.target.closest('[data-device-details]');
  if (button) {event.preventDefault(); open(button.dataset.deviceDetails);}
 });
 document.addEventListener('board-ready', () => {if (node?.open) render();});
 return Object.freeze({open, render, close, isOpen:()=>!!node?.open});
})();
