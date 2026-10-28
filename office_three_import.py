"""Owner-only, additive dual-sheet import for the explicitly scoped third office.

Only allowlisted source cells reach a preview. Existing business rows, scope
mappings, manual publishing links and the active batch are never updated.
The HTTP caller owns the admitted transaction, CSRF and idempotency boundary.
"""
import base64
from collections import Counter, defaultdict
from datetime import date, timedelta
import re
import uuid

import auth
import data_exchange
import device_workflow
import office_scope
import operations
import phone_devices


OFFICE_ID = 'three'
KIND = 'office-three-dual-import'
CONFIRMATION = '导入办公室三'
SCOPE_CONFIRMATION = '仅分配电脑到办公室三'
SOURCE_URL = 'https://docs.qq.com/sheet/DRWxWc1huUWFsVkZp?tab=nh0fs2'
ACCOUNTS_HEADERS = ['来源行', '注册手机号', '昵称', '登录手机序号', '抖音号', '小号说明',
                    '门头', '负责人', '登记电脑序号', '来源类型', '来源账号状态', '违规备注', '使用状态', '注册日期']
PROGRESS_HEADERS = ['来源行', '电脑序号', '负责人', '临时负责人', '类别'] + ['发布设备' + str(i) for i in range(1, 11)]
ACCOUNT_COLUMNS = (2, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17, 18)
PROGRESS_COLUMNS = (0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17)
EXTRA_TABLES = ('offices', 'account_offices', 'computer_offices', 'phone_card_offices',
                'phone_device_offices', 'publishing_layout_state', 'publishing_layout_links')


def _require(db, actor, body):
    office_scope.admitted(db)
    if not getattr(db, 'scope_enabled', False):
        raise ValueError('双表导入需要启用明确的办公室范围')
    actor = office_scope.current_user(db, actor)
    auth.require(actor, 'owner')
    if body.get('office_id') != OFFICE_ID:
        raise ValueError('此双表导入仅允许办公室三，不能导入其他办公室或待分配区')
    office_scope.validated_offices(db, [OFFICE_ID])
    return actor


def _snapshot(db):
    return dict(records=operations.snapshot(db), extra={
        table: sorted((dict(row) for row in db.execute('SELECT * FROM ' + table)), key=operations.encoded)
        for table in EXTRA_TABLES})


def _text(value):
    value = str(value).strip()
    if len(value) > 2000:
        raise ValueError('可导入资料单元格超过2000字，请先整理表格')
    if value.startswith('='):
        raise ValueError('导入不接受公式，请先粘贴为值')
    return value


def _source_row(value, fallback):
    if not value:
        return fallback
    if not re.fullmatch(r'[1-9][0-9]{0,5}', value):
        raise ValueError('来源行号格式不正确')
    return int(value)


def _read_sources(body):
    files = []
    size = 0
    for name in ('accounts_file', 'progress_file'):
        item = body.get(name)
        if not isinstance(item, dict) or not isinstance(item.get('filename'), str) or not isinstance(item.get('content'), str):
            raise ValueError('请分别选择账号情况表和工作进度表')
        # Enforce one combined limit, before parsing either file. No filename or
        # excluded source header is retained in preview/history/error output.
        if len(item['content']) > 12 * 1024 * 1024:
            raise ValueError('两张表文件合计不能超过8MB')
        try:
            size += len(base64.b64decode(item['content'], validate=True))
        except Exception:
            raise ValueError('文件内容无效') from None
        files.append(item)
    if size > 8 * 1024 * 1024:
        raise ValueError('两张表文件合计不能超过8MB')
    result = []
    for kind, item, safe_headers, columns in zip(('accounts', 'progress'), files,
                                                 (ACCOUNTS_HEADERS, PROGRESS_HEADERS),
                                                 (ACCOUNT_COLUMNS, PROGRESS_COLUMNS)):
        rows = data_exchange.read_file(item['filename'], item['content'])
        header = [str(value).strip() for value in rows[0]]
        # A safe-template export may still have trailing private/unknown
        # columns. Recognize only the exact safe prefix and discard that tail;
        # neither its headers nor values are ever reported or stored.
        compact = header[:len(safe_headers)] == safe_headers
        if not compact:
            if kind == 'accounts':
                valid = len(header) >= 19 and all(header[index] == title for index, title in
                    ((2, '注册抖音用的手机号'), (5, '抖音名称'), (6, '抖音在哪个手机上'), (7, '抖音号'), (11, '对应电脑')))
            else:
                valid = len(header) >= 18 and all('发布' in header[index] and '设备' in header[index]
                                                 for index in (7, 8, 9, 10, 11, 12, 13, 14, 15, 17))
            if not valid:
                raise ValueError(('账号情况表' if kind == 'accounts' else '工作进度表') + '列结构不匹配，请使用安全模板或原始双表')
        clean = []
        for number, raw in enumerate(rows[1:], 2):
            # Drop all disallowed columns before text validation, persistence or
            # result construction. In particular account col13 and matrix1..3
            # are never copied to any plan, warning, label or response.
            selected = raw[1:len(safe_headers)] if compact else [raw[index] if index < len(raw) else '' for index in columns]
            selected = [_text(value) for value in selected]
            if not any(selected):
                continue
            selected += [''] * (len(safe_headers) - 1 - len(selected))
            source_row = _source_row(_text(raw[0]) if compact and raw else '', number)
            clean.append(dict(zip(safe_headers, [source_row, *selected])))
        result.append(clean)
    return result


def _digits(value, computer=False):
    pattern = r'([1-9][0-9]{0,5})(?:号(?:电脑|设备))?' if computer else r'([1-9][0-9]{0,5})(?:号手机|多开(?:抖音)?|小号)?'
    match = re.fullmatch(pattern, re.sub(r'\s+', '', value), flags=re.ASCII)
    return str(int(match[1])) if match else None


def _normal(value):
    return re.sub(r'[\s\u200b\ufeff]+', '', value)


def _label_identity(label):
    handle = re.fullmatch(r'(?:抖音号|douyin_id)\s*[:：]\s*([A-Za-z0-9_.-]{2,100})', label)
    if handle:
        return handle[1], None, None
    match = re.fullmatch(r'[.。]?\s*([1-9][0-9]{0,5}(?:号手机)?)\s*[-—－–]\s*(.+)', label)
    if not match:
        match = re.fullmatch(r'([1-9][0-9]{0,5})(?:号(?:手机)?)?\s+(.+)', label)
    if not match:
        return None, None, None
    # Parenthetical names and main/auxiliary-account qualifiers are identity
    # information, not disposable notes. Never collapse them to a main account.
    return None, _digits(match[1]), _normal(match[2])


def _registered(value, day):
    if not value:
        return ''
    if re.fullmatch(r'[0-9]{1,6}(?:\.0+)?', value):
        found = date(1899, 12, 30) + timedelta(days=int(float(value)))
    else:
        value = re.sub(r'(\d{4})年(\d{1,2})月(\d{1,2})日', lambda m: '-'.join((m[1], m[2].zfill(2), m[3].zfill(2))), value)
        found = date.fromisoformat(value)
    if found.isoformat() > day:
        raise ValueError('注册日期晚于今天')
    return found.isoformat()


def _item(kind, row, action, label, reason=''):
    return dict(kind=kind, row=row, action=action, label=label, reason=reason)


def _scope_candidates(db, progress_rows):
    """Offer existing, unassigned source PCs; names/prefixes grant no scope."""
    counts = Counter(_digits(row['电脑序号'], True) for row in progress_rows)
    result = []
    for row in progress_rows:
        number = _digits(row['电脑序号'], True)
        if not number or counts[number] != 1:
            continue
        if any(len(row[field]) > 100 for field in ('负责人', '临时负责人', '类别')):
            continue
        ident = 'office3:' + number
        if db.execute('SELECT 1 FROM computers WHERE id=?', (ident,)).fetchone() and not office_scope.entity_offices(db, 'computers', ident):
            result.append(dict(id=ident))
    return sorted(result, key=lambda item: int(item['id'].split(':')[1]))


def _plan(db, account_rows, progress_rows, day):
    state = db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()
    batch = state[0] if state else None
    plan = dict(accounts=[], computers=[], devices=[], layouts=[], batch=batch)
    items = []
    warnings = ['仅新增安全资料；已有账号、设备、电脑、发布格子和手工关联一律保留，不导入密码、远控信息或每日发布历史。',
                '来源手机编号只作资料登记，登录状态与实物手机均待确认，不视为已登录或已换机。']
    existing_accounts = defaultdict(list)
    for raw in db.execute('SELECT * FROM accounts'):
        existing_accounts[raw['douyin_id']].append(dict(raw))
    handle_counts = Counter(row['抖音号'] for row in account_rows if row['抖音号'])
    computer_counts = Counter(_digits(row['电脑序号'], True) for row in progress_rows)
    source_computers = {}
    computer_available = {}
    device_available = {}

    def asset(kind, ident, row, values):
        cache = computer_available if kind == 'computers' else device_available
        if ident in cache:
            return cache[ident]
        table = 'computers' if kind == 'computers' else 'phone_devices'
        old = db.execute('SELECT 1 FROM ' + table + ' WHERE id=?', (ident,)).fetchone()
        if old and office_scope.entity_offices(db, kind, ident) != {OFFICE_ID}:
            items.append(_item('computer' if kind == 'computers' else 'device', row, 'conflict', ident,
                               '编号已存在，但并非明确且仅属于办公室三；保留原资料'))
            cache[ident] = False
            return False
        cache[ident] = True
        if not old:
            plan[kind].append(dict(id=ident, **values))
            items.append(_item('computer' if kind == 'computers' else 'device', row, 'create', values['name']))
        return True

    for row in progress_rows:
        number = _digits(row['电脑序号'], True)
        if not number:
            if any(row['发布设备' + str(slot)] for slot in range(1, 11)):
                items.append(_item('computer', row['来源行'], 'conflict', '电脑编号待核对', '未识别到明确电脑编号，整行发布安排不导入'))
            continue
        if computer_counts[number] != 1:
            items.append(_item('computer', row['来源行'], 'conflict', '电脑' + number, '工作进度表电脑编号重复，整行保留待核对'))
            continue
        ident = 'office3:' + number
        if len(row['负责人']) > 100 or len(row['临时负责人']) > 100 or len(row['类别']) > 100:
            items.append(_item('computer', row['来源行'], 'conflict', ident, '电脑负责人或类别内容过长'))
            computer_available[ident] = False
            continue
        source_computers[ident] = row
        asset('computers', ident, row['来源行'], dict(name='办公室三 · ' + number + '号电脑',
            operator=row['临时负责人'] or row['负责人'], category=row['类别']))

    candidates = []
    slots = {row[0]: row[1] or 0 for row in db.execute('SELECT computer_id,MAX(slot) FROM accounts GROUP BY computer_id')}
    for row in account_rows:
        number = row['来源行']
        handle = row['抖音号']
        label = row['昵称'] or handle or '账号资料待补充'
        # A phone-only registration placeholder is not an identified account.
        if not row['昵称'] and not handle:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]{2,100}', handle) or handle_counts[handle] != 1:
            items.append(_item('account', number, 'conflict', label, '真实抖音号缺失、不明确或在来源表重复；不新增猜测身份'))
            continue
        old = existing_accounts.get(handle, [])
        if len(old) > 1 or old and office_scope.entity_offices(db, 'accounts', old[0]['id']) != {OFFICE_ID}:
            items.append(_item('account', number, 'conflict', label, '同抖音号存在多条资料，或属于其他办公室/待分配区；不移动、不覆盖'))
            continue
        device = _digits(row['登录手机序号'])
        pair_device = device if not re.search(r'多开|小号', row['登录手机序号']) else None
        if old:
            candidate = dict(row=number, account_id=old[0]['id'], handle=handle, name=row['昵称'], device=pair_device,
                             old_device=old[0]['phone_device_id'], reused=True)
            candidates.append(candidate)
            items.append(_item('account', number, 'reuse', label, '仅复用真实抖音号对应的办公室三账号，原字段全部保留'))
            continue
        if not device:
            items.append(_item('account', number, 'conflict', label, '来源登录手机编号不明确，保留待核对'))
            continue
        computer = _digits(row['登记电脑序号'], True) if row['登记电脑序号'] else None
        computer_id = 'office3:' + computer if computer else 'office3:unassigned'
        if row['登记电脑序号'] and not computer or computer and computer_id not in source_computers:
            items.append(_item('account', number, 'conflict', label, '登记电脑编号不明确或工作进度表缺少唯一对应电脑'))
            continue
        if computer_id == 'office3:unassigned':
            asset('computers', computer_id, number, dict(name='办公室三 · 未分配电脑', operator='', category=''))
        if not computer_available.get(computer_id, False):
            items.append(_item('account', number, 'conflict', label, '登记电脑存在归属冲突'))
            continue
        name = row['昵称'] or '昵称待补充（手机' + device + '）'
        if len(name) > 100:
            items.append(_item('account', number, 'conflict', label, '昵称超过100字'))
            continue
        next_slot = slots.get(computer_id, 0) + 1
        if next_slot > 100:
            items.append(_item('account', number, 'conflict', label, '登记电脑账号位置超过100，需先核对安排'))
            continue
        device_id = 'office3:' + device
        if not asset('devices', device_id, number, dict(name='办公室三 · ' + device + '号手机')):
            items.append(_item('account', number, 'conflict', label, '手机编号存在归属冲突'))
            continue
        registered = ''
        try:
            registered = _registered(row['注册日期'], day)
        except (ValueError, OverflowError):
            warnings.append('账号情况表第' + str(number) + '行注册日期无效或晚于今天，未导入该日期。')
        phone = row['注册手机号'] if re.fullmatch(r'1[0-9]{10}', row['注册手机号']) else ''
        if row['注册手机号'] and not phone:
            warnings.append('账号情况表第' + str(number) + '行注册手机号不完整，未导入该号码。')
        if not row['昵称']:
            warnings.append('账号情况表第' + str(number) + '行昵称缺失，使用明确的待补充占位名。')
        status = {'正常': '正常', '未使用': '停用', '账号永久封禁': '封禁',
                  '账号短期封禁': '短期封禁', '养号7天': '养号7天'}.get(row['来源账号状态'], '待确认')
        note = '\n'.join(filter(None, [
            '腾讯文档账号情况表，第' + str(number) + '行；办公室三双表导入',
            '原账号状态：' + row['来源账号状态'] if row['来源账号状态'] else '',
            '使用情况：' + row['使用状态'] if row['使用状态'] else '',
            '负责人：' + row['负责人'] if row['负责人'] else '',
            '类型说明：' + row['来源类型'] if row['来源类型'] else '',
            '违规/处理备注：' + row['违规备注'] if row['违规备注'] else '',
            '门头：' + row['门头'] if row['门头'] else '',
        ]))[:2000]
        account_id = 'account-' + uuid.uuid4().hex
        plan['accounts'].append(dict(id=account_id, name=name, douyin_id=handle, computer_id=computer_id,
            slot=next_slot, status=status, note=note, registered_on=registered,
            registration_phone=phone, phone_device_id=device_id,
            lifecycle_note=('原小号说明：' + row['小号说明'] + '；类型及关联关系待核对')[:1000] if row['小号说明'] else '来源未确认大小号类型和关联关系，待核对',
            phone_device_note='来源表第' + str(number) + '行手机编号，实物与登录待核对'))
        slots[computer_id] = next_slot
        candidates.append(dict(row=number, account_id=account_id, handle=handle, name=row['昵称'], device=pair_device,
                               old_device=device_id, reused=False))
        items.append(_item('account', number, 'create', name))

    by_pair = defaultdict(list)
    by_handle = defaultdict(list)
    for candidate in candidates:
        by_handle[candidate['handle']].append(candidate)
        if candidate['device'] and candidate['name']:
            by_pair[(candidate['device'], _normal(candidate['name']))].append(candidate)
    proposed = []
    for computer_id, row in source_computers.items():
        for slot in range(1, 11):
            label = row['发布设备' + str(slot)]
            if not label or label in ('.', '。', '-', '—'):
                continue
            reason = ''
            if not batch:
                reason = '当前没有启用的发布批次；不会创建或切换全局批次'
            elif not computer_available.get(computer_id, False):
                reason = '发布电脑归属冲突'
            handle, device, name = _label_identity(label)
            matches = by_handle.get(handle, []) if handle else by_pair.get((device, name), [])
            candidate = matches[0] if len(matches) == 1 else None
            if not reason and not candidate:
                reason = '不能按真实抖音号或手机编号与昵称唯一匹配，保留待核对'
            if not reason and candidate['reused'] and (
                    not candidate['device'] or candidate['old_device'] != 'office3:' + candidate['device']):
                reason = '已有账号的登记手机与来源不同或不明确，保留旧资料且不猜测新位置'
            if not reason and candidate['reused'] and office_scope.entity_offices(db, 'devices', candidate['old_device']) != {OFFICE_ID}:
                reason = '已有账号手机未明确且仅归属办公室三，保留待核对'
            if reason:
                items.append(_item('layout', row['来源行'], 'conflict', label, reason))
                continue
            existing = db.execute('SELECT * FROM publishing_layout WHERE batch=? AND computer_id=? AND slot=?',
                                   (batch, computer_id, slot)).fetchone()
            if existing:
                if existing['account_id'] == candidate['account_id'] and existing['label'] == label:
                    items.append(_item('layout', row['来源行'], 'reuse', label, '原发布位置与账号一致，保留原行及手工关联'))
                else:
                    items.append(_item('layout', row['来源行'], 'conflict', label, '该坐标已有发布行（含空白或人工处理），不覆盖'))
                continue
            placed = db.execute('SELECT 1 FROM publishing_layout WHERE batch=? AND account_id=?',
                                 (batch, candidate['account_id'])).fetchone()
            if placed:
                items.append(_item('layout', row['来源行'], 'conflict', label, '账号已有发布位置，不移动、不重复安排'))
                continue
            proposed.append(dict(batch=batch, computer_id=computer_id, slot=slot, account_id=candidate['account_id'],
                                 label=label, office=OFFICE_ID, source_cell='导入行' + str(row['来源行']) + '/设备' + str(slot),
                                 source_url=SOURCE_URL, reason='', row=row['来源行']))
    placements = Counter(item['account_id'] for item in proposed)
    for item in proposed:
        if placements[item['account_id']] != 1:
            items.append(_item('layout', item['row'], 'conflict', item['label'], '来源表同一账号有多个新发布位置，全部保留待核对'))
        else:
            items.append(_item('layout', item['row'], 'create', item['label']))
            plan['layouts'].append({key: value for key, value in item.items() if key != 'row'})
    summary = dict(new_accounts=len(plan['accounts']), reused_accounts=sum(item['kind'] == 'account' and item['action'] == 'reuse' for item in items),
        new_computers=len(plan['computers']), new_devices=len(plan['devices']), new_layouts=len(plan['layouts']),
        conflicts=sum(item['action'] == 'conflict' for item in items),
        source_accounts=sum(bool(row['昵称'] or row['抖音号']) for row in account_rows),
        source_computers=len(source_computers))
    return plan, summary, items, warnings


def preview(db, actor, body, day):
    actor = _require(db, actor, body)
    account_rows, progress_rows = _read_sources(body)
    before = _snapshot(db)
    plan, summary, items, warnings = _plan(db, account_rows, progress_rows, day)
    scope_candidates = _scope_candidates(db, progress_rows)
    payload = dict(office_id=OFFICE_ID, guard=operations.token(before), plan=plan, summary=summary,
                   scope_candidates=scope_candidates)
    preview_id = operations.preview(db, actor, KIND, payload)
    return dict(preview_id=preview_id, office_id=OFFICE_ID, summary=summary, rows=items,
                warnings=warnings, confirmation=CONFIRMATION, scope_candidates=scope_candidates)


def _verify_computer_scopes(db, before, requested):
    after = _snapshot(db)
    if before['records'].keys() != after['records'].keys():
        raise operations.ConflictError('电脑归属保留校验失败：业务记录数量变化，整次操作已撤销')
    for key, old in before['records'].items():
        old_fields = {field: value for field, value in old.items() if field != 'office_ids'}
        new_fields = {field: value for field, value in after['records'][key].items() if field != 'office_ids'}
        if old_fields != new_fields:
            raise operations.ConflictError('电脑归属保留校验失败：原业务字段变化，整次操作已撤销')
    for table, old in before['extra'].items():
        expected = old
        if table == 'computer_offices':
            expected = sorted([*old, *(dict(computer_id=ident, office_id=OFFICE_ID) for ident in requested)],
                              key=operations.encoded)
        if after['extra'][table] != expected:
            raise operations.ConflictError('电脑归属保留校验失败：并非仅分配所选电脑，整次操作已撤销')
    return dict(business_fields_preserved=True, only_requested_computers_assigned=True,
                other_entity_scopes_preserved=True)


def assign_computer_scopes(db, actor, body, day):
    """Confirm a chosen subset of the actor-bound preview's unassigned PCs.

    This never imports business data. Success consumes the old plan so callers
    must request a new preview against the newly confirmed explicit scopes.
    """
    actor = _require(db, actor, body)
    if body.get('confirmation') != SCOPE_CONFIRMATION:
        raise ValueError('请明确输入“仅分配电脑到办公室三”后确认')
    selected = body.get('computer_ids')
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 100 or
            any(type(value) is not str or not re.fullmatch(r'office3:[1-9][0-9]{0,5}', value) for value in selected) or
            len(set(selected)) != len(selected)):
        raise ValueError('请选择1至100台不重复的来源电脑')
    ident = body.get('preview_id')
    if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise ValueError('预览编号不正确，请重新预览')
    payload = operations.take_preview(db, actor, ident, KIND)
    available = {item['id'] for item in payload.get('scope_candidates', [])}
    if not set(selected) <= available:
        raise ValueError('只能确认本次预览列出的尚未分配来源电脑，请重新预览')
    before = _snapshot(db)
    if payload.get('office_id') != OFFICE_ID or payload['guard'] != operations.token(before):
        raise operations.ConflictError('预览后资料或归属已变化，请重新预览；未保存任何归属修改')
    for computer_id in selected:
        if office_scope.entity_offices(db, 'computers', computer_id):
            raise operations.ConflictError('所选电脑已经有办公室归属，请重新预览；未移动任何电脑')
    selected = sorted(selected)
    db.execute('SAVEPOINT office_three_computer_scopes')
    try:
        for computer_id in selected:
            office_scope.assign_entity(db, actor, 'computers', computer_id, [OFFICE_ID])
        verification = _verify_computer_scopes(db, before, selected)
        db.execute('DELETE FROM operations_previews WHERE id=?', (ident,))
    except Exception:
        db.execute('ROLLBACK TO office_three_computer_scopes')
        db.execute('RELEASE office_three_computer_scopes')
        raise
    db.execute('RELEASE office_three_computer_scopes')
    return dict(ok=True, office_id=OFFICE_ID, assigned_computer_ids=selected,
                verification=verification, requires_new_preview=True)


def _verify_preserved(db, before):
    after = _snapshot(db)
    for key, value in before['records'].items():
        if after['records'].get(key) != value:
            raise operations.ConflictError('导入保留校验失败：原资料发生变化，整次导入已撤销')
    for table, rows in before['extra'].items():
        current = {operations.encoded(row) for row in after['extra'][table]}
        if any(operations.encoded(row) not in current for row in rows):
            raise operations.ConflictError('导入保留校验失败：原归属或手工发布资料变化，整次导入已撤销')
    if after['extra']['publishing_layout_state'] != before['extra']['publishing_layout_state']:
        raise operations.ConflictError('发布批次发生变化，整次导入已撤销')
    if after['extra']['offices'] != before['extra']['offices']:
        raise operations.ConflictError('办公室定义发生变化，整次导入已撤销')
    for key in after['records'].keys() - before['records'].keys():
        if after['records'][key].get('office_ids') != [OFFICE_ID]:
            raise operations.ConflictError('新增资料不属于办公室三，整次导入已撤销')
    for table in ('account_offices', 'computer_offices', 'phone_card_offices', 'phone_device_offices'):
        previous = {operations.encoded(row) for row in before['extra'][table]}
        if any(row['office_id'] != OFFICE_ID for row in after['extra'][table] if operations.encoded(row) not in previous):
            raise operations.ConflictError('其他办公室归属发生变化，整次导入已撤销')
    if after['extra']['publishing_layout_links'] != before['extra']['publishing_layout_links']:
        raise operations.ConflictError('原手工发布关联发生变化，整次导入已撤销')
    return dict(original_records_preserved=True, other_offices_unchanged=True, active_batch_unchanged=True,
                original_record_count=len(before['records']))


def apply(db, actor, body, day):
    actor = _require(db, actor, body)
    if body.get('confirmation') != CONFIRMATION:
        raise ValueError('请明确输入“导入办公室三”后确认，仅新增安全资料')
    ident = body.get('preview_id')
    if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise ValueError('预览编号不正确，请重新预览')
    payload = operations.take_preview(db, actor, ident, KIND)
    before = _snapshot(db)
    if payload.get('office_id') != OFFICE_ID or payload['guard'] != operations.token(before):
        raise operations.ConflictError('预览后资料或归属已变化，请重新预览；未保存任何导入修改')
    plan = payload['plan']
    db.execute('SAVEPOINT office_three_apply')
    try:
        for item in plan['computers']:
            db.execute('INSERT INTO computers(id,name,shadow_account,ip,operator,category) VALUES(?,?,\'\',\'\',?,?)',
                       (item['id'], item['name'], item['operator'], item['category']))
            office_scope.assign_entity(db, actor, 'computers', item['id'], [OFFICE_ID], newly_created=True)
        for item in plan['devices']:
            phone_devices.save(db, dict(id=item['id'], name=item['name'], state='待确认', office_ids=[OFFICE_ID],
                                        note='办公室三来源表编号，实物与登录情况待核对'), create=True, actor=actor)
        for item in plan['accounts']:
            db.execute('''INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,
                registered_on,registration_phone,lifecycle_note,phone_device_id,phone_device_source,phone_device_note,office_group)
                VALUES(?,?,?,?,?,?,?,'','',?,?,?,?,?,?,'three')''',
                (item['id'], item['computer_id'], item['slot'], item['name'], item['status'], item['note'], item['douyin_id'],
                 item['registered_on'], item['registration_phone'], item['lifecycle_note'], item['phone_device_id'],
                 'tencent-office3-preview', item['phone_device_note']))
            office_scope.assign_entity(db, actor, 'accounts', item['id'], [OFFICE_ID], newly_created=True)
            if device_workflow.available(db):
                # No inference about the live physical unit or successful login,
                # and no update of any existing phone/unit/access row.
                db.execute('INSERT INTO phone_account_access(account_id,reason) VALUES(?,?)',
                           (item['id'], '来源表手机编号仅为资料登记，实物与登录待核对'))
        for item in plan['layouts']:
            db.execute('INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES(?,?,?,?,?,?,?,?,?)',
                       tuple(item[key] for key in ('batch', 'computer_id', 'slot', 'account_id', 'label', 'office', 'source_cell', 'source_url', 'reason')))
        verification = _verify_preserved(db, before)
        db.execute('DELETE FROM operations_previews WHERE id=?', (ident,))
    except Exception:
        db.execute('ROLLBACK TO office_three_apply')
        db.execute('RELEASE office_three_apply')
        raise
    db.execute('RELEASE office_three_apply')
    return dict(ok=True, office_id=OFFICE_ID, summary=payload['summary'], verification=verification,
                imported=sum(len(plan[kind]) for kind in ('accounts', 'computers', 'devices', 'layouts')))
