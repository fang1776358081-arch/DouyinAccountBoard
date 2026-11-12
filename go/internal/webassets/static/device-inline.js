/* Edit descriptive device fields without changing physical state or relations. */
window.DeviceInline = (() => {
 const labels = {name: '手机名称', owner: '负责人', location: '设备位置'};
 const limits = {name: 100, owner: 100, location: 200};
 let draft = null;

 function allowed(device, field) {
  return Object.hasOwn(labels, field) && hasPermission('device_manage') &&
   (data.user?.scope_mode === 'all' || !!device.office_ids?.length &&
    device.office_ids.every(office => data.user?.office_ids?.includes(office)));
 }

 function cellId(id, field) {
  return 'deviceInline-' + field + '-' + encodeURIComponent(id);
 }

 function cell(device, field, fallback) {
  if (!allowed(device, field)) return fallback;
  return `<button type="button" id="${esc(cellId(device.id, field))}" class="device-inline-cell" data-device-inline-id="${esc(device.id)}" data-device-inline-field="${field}" title="双击或按 Enter 编辑" aria-label="编辑${labels[field]}，当前值 ${esc(device[field] || '未登记')}">${fallback}</button>`;
 }

 function hint(message) {
  const target = $('deviceInlineHint');
  if (target) target.textContent = message || '双击名称、负责人或位置直接修改，Enter 保存，Esc 取消。';
 }

 function announceBlocked() {
  hint('请先保存或取消当前手机编辑。');
 }

 function paint(focus = false) {
  const entry = draft;
  const locked = ['saving', 'unknown'].includes(entry.phase);
  entry.host.innerHTML = `<div class="device-inline-editor" data-device-inline-editor><input id="deviceInlineInput" type="text" maxlength="${limits[entry.field]}" aria-label="${labels[entry.field]}" value="${esc(entry.value)}" ${locked ? 'disabled' : ''}><div class="device-inline-actions"><button type="button" class="text-button" data-device-inline-save ${['saving', 'conflict'].includes(entry.phase) ? 'disabled' : ''}>${entry.phase === 'unknown' ? '重试核对' : '保存'}</button><button type="button" class="text-button" data-device-inline-cancel ${locked ? 'disabled' : ''}>取消</button></div></div>`;
  if (focus && !locked) $('deviceInlineInput').focus();
  hint(entry.message);
 }

 function begin(button) {
  if (draft) return announceBlocked();
  if (window.AccountInline?.hasDraft() || window.CardInline?.hasDraft() || window.EmployeeCardLink?.hasDraft()) return;
  if (typeof dailyViewLoading !== 'undefined' && dailyViewLoading || typeof boardWrites !== 'undefined' && boardWrites > 0) return hint('页面正在更新，请稍后编辑。');
  if (document.querySelector('dialog[open]')) return;
  const {deviceInlineId: id, deviceInlineField: field} = button.dataset;
  const device = data.phone_devices?.devices.find(row => row.id === id);
  if (!device || !allowed(device, field)) return;
  const version = data.record_versions?.['device:' + id];
  if (!version) return hint('手机资料尚未同步，请刷新后再编辑。');
  draft = {id, field, device, version, value: String(device[field] || ''), host: button.parentElement, phase: 'editing'};
  if (typeof boardSyncInteraction === 'function') boardSyncInteraction();
  paint(true);
 }

 function end(entry, message) {
  draft = null;
  render();
  $(cellId(entry.id, entry.field))?.focus();
  hint(message);
  window.boardLiveSync?.poke();
 }

 async function save() {
  const entry = draft;
  if (!entry || ['saving', 'conflict'].includes(entry.phase)) return;
  if (entry.phase !== 'unknown') {
   const value = $('deviceInlineInput').value.trim();
   if (entry.field === 'name' && !value) return hint('手机名称不能为空。');
   if (value === String(entry.device[entry.field] || '')) return end(entry, '没有需要保存的修改。');
   entry.value = value;
   entry.payload = {id: entry.id, changes: {[entry.field]: value}, _expected_versions: {['device:' + entry.id]: entry.version}, request_id: newRequestId()};
  }
  entry.phase = 'saving';
  entry.message = '正在保存手机资料…';
  paint();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
   const writing = api('/api/phone-device', entry.payload, {fixedPayload: true, applyVersions: false, signal: controller.signal});
   const result = await boardReadWithSignal(writing, controller.signal);
   if (result?.ok !== true && result?.code !== 'REQUEST_SUCCEEDED') throw Error('保存结果待核对');
  } catch (error) {
   const uncertain = error.status === undefined || error.status >= 500 || error.status === 408 || error.code === 'REQUEST_PENDING';
   entry.phase = uncertain ? 'unknown' : error.status === 409 ? 'conflict' : 'editing';
   entry.message = entry.phase === 'conflict' ? '这台手机已被修改。输入已保留，请取消并刷新后再编辑。' :
    entry.phase === 'unknown' ? '保存结果待确认，点击「重试核对」会核对原请求。' : error.message;
   paint(!uncertain);
   return;
  } finally {
   clearTimeout(timeout);
  }
  draft = null;
  const refreshed = await load();
  if (!draft && !refreshed) render();
  $(cellId(entry.id, entry.field))?.focus();
  if (!draft) hint(refreshed ? '手机资料已保存。' : '手机资料已保存，请刷新查看最新值。');
 }

 function cancel() {
  if (!draft || ['saving', 'unknown'].includes(draft.phase)) return;
  const entry = draft;
  end(entry, '已取消手机编辑。');
  if (entry.phase === 'conflict') void load();
 }

 document.addEventListener('DOMContentLoaded', () => {
  const body = $('phonesBody');
  body.addEventListener('dblclick', event => {
   const button = event.target.closest('[data-device-inline-field]');
   if (button) begin(button);
  });
  body.addEventListener('keydown', event => {
   if (event.isComposing || event.keyCode === 229) return;
   const button = event.target.closest('[data-device-inline-field]');
   if (button && ['Enter', ' ', 'F2'].includes(event.key)) {
    event.preventDefault();
    begin(button);
   } else if (draft && event.target.closest('[data-device-inline-editor]')) {
    if (event.key === 'Escape') {event.preventDefault(); cancel();}
    else if (event.key === 'Enter' && event.target.id === 'deviceInlineInput') {event.preventDefault(); void save();}
   }
  });
  body.addEventListener('click', event => {
   if (event.target.closest('[data-device-inline-save]')) void save();
   if (event.target.closest('[data-device-inline-cancel]')) cancel();
  });
  document.addEventListener('click', event => {
   if (draft && !event.target.closest('[data-device-inline-editor]') && event.target.closest('button,a,summary,input,select,textarea,label')) {
    event.preventDefault();
    event.stopImmediatePropagation();
    announceBlocked();
   }
  }, true);
 });
 window.addEventListener('beforeunload', event => {
  if (!draft) return;
  event.preventDefault(); event.returnValue = '';
 });
 return Object.freeze({cell, hasDraft: () => !!draft, announceBlocked});
})();
