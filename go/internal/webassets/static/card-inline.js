/* Use one card record version for each single-field edit. */
window.CardInline = (() => {
 const labels = {owner: '卡主 / 提供人', status: '卡状态'};
 const permissions = {owner: 'card_manage', status: 'card_tasks'};
 let draft = null;

 function allowed(card, field) {
  return !!permissions[field] && hasPermission(permissions[field]) &&
   (data.user?.scope_mode === 'all' || !!card.office_ids?.length &&
    card.office_ids.every(office => data.user?.office_ids?.includes(office)));
 }

 function cellId(id, field) {
  return 'cardInline-' + id + '-' + field;
 }

 function cell(card, field, content) {
  if (!allowed(card, field)) return content;
  return `<button type="button" id="${cellId(card.id, field)}" class="card-inline-cell" data-card-inline-id="${card.id}" data-card-inline-field="${field}" title="双击或按 Enter 编辑" aria-label="编辑${labels[field]}，当前值 ${esc(card[field] || '未登记')}">${content}</button>`;
 }

 function hint(message) {
  const target = $('cardInlineHint');
  if (target) target.textContent = message || '双击卡主或卡状态直接修改，Enter 保存，Esc 取消。';
 }

 function announceBlocked() {
  hint('请先保存或取消当前手机卡编辑。');
 }

 function paint(focus = false) {
  const {host, field, phase, value, message} = draft;
  const disabled = phase === 'saving' || phase === 'unknown' ? ' disabled' : '';
  const input = field === 'owner'
   ? `<input id="cardInlineInput" type="text" maxlength="100" aria-label="${labels[field]}" value="${esc(value)}"${disabled}>`
   : `<select id="cardInlineInput" aria-label="${labels[field]}"${disabled}>${(data.phone_cards?.statuses || []).map(status => `<option value="${esc(status)}">${esc(status)}</option>`).join('')}</select>`;
  host.innerHTML = `<div class="card-inline-editor" data-card-inline-editor>${input}<div class="card-inline-actions"><button type="button" class="text-button" data-card-inline-save ${phase === 'saving' || phase === 'conflict' ? 'disabled' : ''}>${phase === 'unknown' ? '重试核对' : '保存'}</button><button type="button" class="text-button" data-card-inline-cancel${disabled}>取消</button></div></div>`;
  const element = $('cardInlineInput');
  element.value = value;
  if (focus && !disabled) element.focus();
  hint(message);
 }

 function begin(button) {
  if (draft) return announceBlocked();
  if ((typeof dailyViewLoading !== 'undefined' && dailyViewLoading) ||
      (typeof boardWrites !== 'undefined' && boardWrites > 0)) {
   return hint('页面正在刷新或保存，请稍后编辑。');
  }
  const {cardInlineId: id, cardInlineField: field} = button.dataset;
  const card = data.phone_cards?.cards.find(row => String(row.id) === id);
  if (!card || !allowed(card, field) || document.querySelector('dialog[open]')) return;
  const version = data.record_versions?.['card:' + id];
  if (!version) return hint('手机卡资料尚未同步，请刷新后再编辑。');
  draft = {id, field, card, version, value: String(card[field] || ''), phase: 'editing', host: button.parentElement};
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
  if (!entry || entry.phase === 'saving' || entry.phase === 'conflict') return;
  if (entry.phase !== 'unknown') {
   const value = $('cardInlineInput').value.trim();
   if (entry.field === 'status' && !(data.phone_cards?.statuses || []).includes(value)) {
    return hint('请从下拉列表选择一个卡状态。');
   }
   if (value === String(entry.card[entry.field] || '')) {
    end(entry, '没有需要保存的修改。');
    return;
   }
   entry.value = value;
   entry.payload = {
    id: Number(entry.id),
    changes: {[entry.field]: value},
    _expected_versions: {['card:' + entry.id]: entry.version},
    request_id: newRequestId(),
   };
  }
  entry.phase = 'saving';
  entry.message = '正在保存手机卡，请稍候。';
  paint();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
   const writing = api('/api/phone-card', entry.payload, {
    fixedPayload: true, applyVersions: false, signal: controller.signal,
   });
   const result = await boardReadWithSignal(writing, controller.signal);
   if (result?.ok !== true && result?.code !== 'REQUEST_SUCCEEDED') throw Error('保存结果待核对');
  } catch (error) {
   const uncertain = error.status === undefined || error.status >= 500 || error.status === 408 || error.code === 'REQUEST_PENDING';
   entry.phase = uncertain ? 'unknown' : error.status === 409 ? 'conflict' : 'editing';
   entry.message = entry.phase === 'conflict'
    ? '这张卡已被修改。输入已保留，请取消并刷新后再编辑。'
    : entry.phase === 'unknown'
     ? '保存结果待确认，点击「重试核对」会核对原请求。'
     : error.message;
   paint(entry.phase !== 'unknown');
   return;
  } finally {
   clearTimeout(timeout);
  }
  draft = null;
  const refreshed = await load();
  if (!draft && !refreshed) render();
  $(cellId(entry.id, entry.field))?.focus();
  if (!draft) hint(refreshed ? '手机卡已保存。' : '手机卡已保存，请刷新查看最新值。');
 }

 function cancel() {
  if (!draft || draft.phase === 'saving' || draft.phase === 'unknown') return;
  const entry = draft;
  end(entry, '已取消手机卡编辑。');
  if (entry.phase === 'conflict') void load();
 }

 document.addEventListener('DOMContentLoaded', () => {
  const body = $('cardsBody');
  body.addEventListener('dblclick', event => {
   const button = event.target.closest('td')?.querySelector('[data-card-inline-field]');
   if (button) begin(button);
  });
  body.addEventListener('keydown', event => {
   if (event.isComposing || event.keyCode === 229) return;
   const button = event.target.closest('[data-card-inline-field]');
   if (button && ['Enter', ' ', 'F2'].includes(event.key)) {
    event.preventDefault();
    begin(button);
   } else if (draft && event.target.closest('[data-card-inline-editor]')) {
    if (event.key === 'Escape') {
     event.preventDefault();
     cancel();
    } else if (event.key === 'Enter' && event.target.id === 'cardInlineInput') {
     event.preventDefault();
     void save();
    }
   }
  });
  body.addEventListener('click', event => {
   if (event.target.closest('[data-card-inline-save]')) void save();
   if (event.target.closest('[data-card-inline-cancel]')) cancel();
  });
  document.addEventListener('click', event => {
   if (draft && !event.target.closest('[data-card-inline-editor]') && event.target.closest('button, a, summary, input, select, textarea, label')) {
    event.preventDefault();
    event.stopImmediatePropagation();
    announceBlocked();
   }
  }, true);
 });
 window.addEventListener('beforeunload', event => {
  if (!draft) return;
  event.preventDefault();
  event.returnValue = '';
 });
 return Object.freeze({cell, hasDraft: () => !!draft, announceBlocked});
})();
