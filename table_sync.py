"""Office-scoped dual-sheet pilot with durable receipts and conservative reversal.

The HTTP caller authenticates/CSRF-checks and admits one transaction. This module
owns request replay, history and protective verification; do not call the generic
operations.finish/nurture.sync around it. Reads never create configuration/data.
"""
import base64
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

import auth
import data_exchange
import idempotency
import office_scope
import office_three_import as legacy_source
import operations
from sql_dialect import integrity_errors

TABLES = ('table_sync_pilots', 'table_sync_account_sources', 'table_sync_batches',
          'table_sync_review_items', 'table_sync_events', 'table_sync_requests')
SOURCE_FIELDS = ('responsible', 'usage', 'violation', 'business_type', 'storefront')
ACCOUNT_FIELDS = ('name', 'registration_phone', 'registered_on', 'status', 'computer_id', 'slot')
SOURCE_COLUMNS = dict(responsible='负责人', usage='使用状态', violation='违规备注',
                      business_type='来源类型', storefront='门头')
STATES = {'正常': '正常', '未使用': '停用', '停用': '停用', '账号永久封禁': '封禁',
          '封禁': '封禁', '账号短期封禁': '短期封禁', '短期封禁': '短期封禁',
          '养号7天': '养号7天', '发不了': '发不了', '待确认': '待确认'}
CONFIRMATION = '同步办公室三资料'
REVERSE_CONFIRMATION = '回退办公室三批次'
PREVIEW_KIND = 'table-sync-pilot'
PILOT_OFFICES = frozenset({'three'})
COMPUTER_ID_PREFIXES = {'three': 'office3:'}  # Existing source IDs, never an authorization rule.
PROTECTED = ('computers', 'phone_cards', 'phone_devices', 'daily', 'nurture_records', 'nurture_images',
             'phone_units', 'phone_card_placements', 'phone_account_access', 'device_cases', 'device_workflow_events',
             'publishing_layout', 'publishing_layout_state', 'publishing_layout_links', 'computer_offices',
             'phone_card_offices', 'phone_device_offices', 'offices', 'user_office_grants', 'auth_users',
             'collection_jobs', 'collection_settings', 'collection_worker')


def _json(value):
    return operations.encoded(value)


def _id():
    return str(uuid.uuid4())


def available(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        import pg_schema
        expected = next((item for item in pg_schema.expected_versions() if item[0] == 7), None)
        row = db.execute('SELECT version,sha256 FROM schema_migrations WHERE version=7').fetchone()
        if not expected or not row or (row[0], row[1]) != expected:
            return False
        row = db.execute('SELECT '+','.join('to_regclass(?)' for _ in TABLES), TABLES).fetchone()
        return bool(row) and all(row[index] is not None for index in range(len(TABLES)))
    return set(TABLES) <= _tables(db)


def migrate(db):
    if getattr(db, 'dialect', 'sqlite') != 'sqlite':
        raise ValueError('表格同步结构只能通过受控PostgreSQL升级创建')
    ddl = Path(__file__).with_name('migrations').joinpath('postgresql/0007_table_sync.sql').read_text(encoding='utf-8')
    db.executescript(ddl.replace('BIGINT', 'INTEGER'))


def _tables(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        return {row[0] for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _text(value, label, maximum=2000, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(label+'格式不正确或过长')
    result = value.strip()
    if required and not result:
        raise ValueError('请填写'+label)
    return result


def _actor(db, actor, office, *, owner=False):
    auth.require(actor)
    if not getattr(db, 'scope_enabled', False):
        raise ValueError('表格同步需要明确的办公室权限范围')
    current = office_scope.current_user(db, actor)
    if 'csrf' in actor:
        current['csrf'] = actor['csrf']
    if owner:
        auth.require(current, 'owner')
    if office not in PILOT_OFFICES:
        raise ValueError('当前仅开放办公室三试点，其他办公室和待分配区不参与写入')
    office_scope.validated_offices(db, [office])
    if not office_scope.global_scope(current) and office not in office_scope.granted_offices(db, current):
        raise auth.AccessError('无权访问此办公室的表格同步', 403)
    return current


def _generation(db):
    value = getattr(db, 'restore_generation', None)
    if value is None and getattr(db, 'dialect', 'sqlite') == 'sqlite':
        return 'sqlite-local'
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError('恢复代次未配置，表格同步暂不可写')
    return value


def _pilot(db, office):
    row = db.execute('SELECT state,revision,updated_at FROM table_sync_pilots WHERE office_id=?', (office,)).fetchone()
    return dict(row) if row else dict(state='disabled', revision=0, updated_at=None)


def _enabled(db, office):
    pilot = _pilot(db, office)
    if pilot['state'] != 'enabled':
        raise ValueError('办公室三表格同步试点未开启或已暂停；未修改任何资料')
    return pilot


def _event(db, actor, office, kind, target, before, after):
    db.execute('INSERT INTO table_sync_events VALUES(?,?,?,?,?,?,?,?)',
               (_id(), office, actor['id'], int(time.time()), kind, target,
                _json(before) if before is not None else None, _json(after)))
    auth.audit(db, actor, '表格同步:'+kind, target)


def _query(query, key, default=''):
    value = query.get(key, [default])
    return value[0] if isinstance(value, list) else value


def _sources(db):
    return {row['account_id']: dict(row) for row in db.execute('SELECT * FROM table_sync_account_sources')}


def get(db, actor, path, query, day):
    office = _query(query, 'office_id', 'three')
    actor = _actor(db, actor, office)
    present = available(db)
    if path == '/api/table-sync/account-sources':
        if not present:
            return dict(available=False, office_id=office, sources=[], version=operations.token([]))
        result = []
        # The refreshed actor has this office grant. Filter by the complete
        # authoritative account scope in SQL, without one lookup per account or
        # loading unrelated devices, layouts and card scope maps.
        for raw in db.execute('SELECT s.* FROM table_sync_account_sources s JOIN accounts a ON a.id=s.account_id '
                              'WHERE s.office_id=? AND s.account_id IN '
                              '(SELECT account_id FROM account_offices GROUP BY account_id HAVING COUNT(*)=1 AND MIN(office_id)=?) '
                              'ORDER BY s.account_id', (office,office)):
            item = dict(raw)
            item['source_data'] = json.loads(item['source_data'])
            result.append(item)
        return dict(available=True, office_id=office, sources=result, version=operations.token(result))
    if path == '/api/table-sync':
        manage = actor['role'] == 'owner'
        recent = []
        if present and manage:
            for row in db.execute('SELECT id,created_at,kind,receipt_json FROM table_sync_batches WHERE office_id=? ORDER BY created_at DESC,id DESC LIMIT 20', (office,)):
                recent.append(dict(id=row['id'], created_at=row['created_at'], kind=row['kind'],
                                   summary=json.loads(row['receipt_json'])['summary']))
        return dict(available=present, user=actor, office_id=office,
                    office_name=db.execute('SELECT name FROM offices WHERE id=?', (office,)).fetchone()[0],
                    pilot=_pilot(db, office) if present else dict(state='disabled', revision=0),
                    can_manage=manage and present, can_export=manage and 'data_export' in actor['permissions'],
                    recent_batches=recent, note='' if present else '表格同步扩展尚未启用；需按项目流程备份并升级0007后才能试点，其他功能可继续使用。')
    auth.require(actor, 'owner')
    if not present:
        raise ValueError('表格同步扩展尚未启用，不能读取批次或执行写入')
    if path == '/api/table-sync/batch':
        return _receipt(db, office, _query(query, 'id'))
    if path == '/api/table-sync/request':
        ident = idempotency.request_uuid(_query(query, 'request_id'))
        row = db.execute('SELECT generation,result_json FROM table_sync_requests WHERE actor_id=? AND request_id=?', (actor['id'], ident)).fetchone()
        if not row:
            raise RequestNotFound()
        if row['generation'] != _generation(db):
            raise idempotency.IdempotencyError('RESTORE_GENERATION_EXPIRED', '请求属于已关闭的恢复代次，不能再次执行')
        return json.loads(row['result_json'])
    raise ValueError('表格同步接口不存在')


class RequestNotFound(ValueError):
    code = 'REQUEST_NOT_FOUND'
    status = 404

    def __init__(self):
        super().__init__('未找到原请求结果；只能核对或重试同一请求编号，不能另建重复请求')


def post(db, actor, path, body, day):
    office_scope.admitted(db)
    if not isinstance(body, dict):
        raise ValueError('表格同步请求格式不正确')
    office = body.get('office_id')
    actor = _actor(db, actor, office, owner=True)
    if not available(db):
        raise ValueError('表格同步扩展尚未启用；未修改任何资料')
    request_id = idempotency.request_uuid(body.get('request_id'))
    generation = _generation(db)
    fingerprint = idempotency.fingerprint(path, body)
    old = db.execute('SELECT generation,fingerprint,result_json FROM table_sync_requests WHERE actor_id=? AND request_id=?', (actor['id'], request_id)).fetchone()
    if old:
        if old['generation'] != generation:
            raise idempotency.IdempotencyError('RESTORE_GENERATION_EXPIRED', '请求属于已关闭的恢复代次，不能再次执行')
        if old['fingerprint'] != fingerprint:
            raise idempotency.IdempotencyError('REQUEST_ID_REUSED', '同一请求编号不能用于不同内容')
        return json.loads(old['result_json'])
    actions = {'/api/table-sync/pilot': _set_pilot, '/api/table-sync/preview': _preview,
               '/api/table-sync/apply': _apply, '/api/table-sync/review': _review,
               '/api/table-sync/rollback-preview': _reverse_preview,
               '/api/table-sync/rollback-apply': lambda *args: _apply(*args, reverse=True)}
    if path not in actions:
        raise ValueError('表格同步接口不存在')
    date.fromisoformat(day)
    db.execute('SAVEPOINT table_sync_action')
    try:
        result = actions[path](db, actor, office, body, day)
        db.execute('INSERT INTO table_sync_requests VALUES(?,?,?,?,?)',
                   (actor['id'], request_id, generation, fingerprint, _json(result)))
        db.execute('RELEASE table_sync_action')
        return result
    except Exception:
        db.execute('ROLLBACK TO table_sync_action')
        db.execute('RELEASE table_sync_action')
        raise


def _fields(body, allowed):
    if set(body) - {'office_id', 'request_id'} - set(allowed):
        raise ValueError('请求包含未开放字段，不能提交客户端修改计划')


def _set_pilot(db, actor, office, body, day):
    _fields(body, {'state', 'expected_revision', 'confirmation'})
    state = body.get('state')
    phrases = {'enabled': '开启办公室三试点', 'paused': '暂停办公室三试点', 'ended': '结束办公室三试点'}
    if state not in phrases or body.get('confirmation') != phrases[state]:
        raise ValueError('请明确确认本次试点状态变更')
    before = _pilot(db, office)
    if type(body.get('expected_revision')) is not int or body['expected_revision'] != before['revision']:
        raise operations.ConflictError('试点状态已变化，请重新读取')
    after = dict(state=state, revision=before['revision']+1, updated_at=int(time.time()))
    db.execute('INSERT INTO table_sync_pilots(office_id,state,revision,updated_at) VALUES(?,?,?,?) '
               'ON CONFLICT(office_id) DO UPDATE SET state=excluded.state,revision=excluded.revision,updated_at=excluded.updated_at',
               (office, state, after['revision'], after['updated_at']))
    _event(db, actor, office, 'pilot', office, before, after)
    return dict(ok=True, office_id=office, pilot=after)


def _read_sources(body):
    source_round = _text(body.get('source_round'), '同轮来源标记', 100, True)
    files, total = [], 0
    metadata = dict(round=source_round)
    for name in ('accounts', 'progress'):
        item = body.get(name+'_file')
        if not isinstance(item, dict) or set(item) != {'filename', 'content'} or not all(isinstance(item.get(key), str) for key in ('filename', 'content')):
            raise ValueError('请同时上传同一轮的账号情况表与工作进度表')
        if len(item['content']) > 12*1024*1024:
            raise ValueError('两份来源合计不能超过8MB')
        try:
            raw = base64.b64decode(item['content'], validate=True)
        except (ValueError, TypeError):
            raise ValueError('来源文件内容无效') from None
        total += len(raw)
        if total > 8*1024*1024:
            raise ValueError('两份来源合计不能超过8MB')
        metadata[name] = dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        files.append(item)
    result = []
    for kind, item, safe, columns in zip(('accounts', 'progress'), files,
            (legacy_source.ACCOUNTS_HEADERS, legacy_source.PROGRESS_HEADERS),
            (legacy_source.ACCOUNT_COLUMNS, legacy_source.PROGRESS_COLUMNS)):
        raw_rows = data_exchange.read_file(item['filename'], item['content'])
        header = [str(value).strip() for value in raw_rows[0]]
        compact = header[:len(safe)] == safe
        if not compact:
            valid = (len(header) >= 19 and all(header[index] == title for index, title in
                     ((2, '注册抖音用的手机号'), (5, '抖音名称'), (6, '抖音在哪个手机上'), (7, '抖音号'), (11, '对应电脑')))) if kind == 'accounts' else (
                     len(header) >= 18 and all('发布' in header[index] and '设备' in header[index] for index in (7,8,9,10,11,12,13,14,15,17)))
            if not valid:
                raise ValueError(('账号情况表' if kind == 'accounts' else '工作进度表')+'列结构不匹配，请使用安全模板或原始双表')
        clean = []
        for number, raw in enumerate(raw_rows[1:], 2):
            values = raw[1:len(safe)] if compact else [raw[index] if index < len(raw) else '' for index in columns]
            values = [str(value) for value in values]+['']*len(safe)
            selected = dict(zip(safe[1:], values))
            if not any(selected.values()):
                continue
            for key, value in selected.items():
                if len(value) > 2000 or value.startswith('=') or any(ord(c)<32 and c not in '\n\t' for c in value):
                    raise ValueError('允许导入的来源单元格过长或含不支持的公式/字符')
                if key != '抖音号':
                    selected[key] = value.strip()
            source_number = str(raw[0]) if compact and raw else ''
            selected['来源行'] = legacy_source._source_row(source_number.strip(), number)
            clean.append(selected)
        result.append(clean)
    for name,rows in zip(('accounts','progress'),result):
        metadata[name]['rows']=len(rows)
    return *result, metadata


def _normalized(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'bytes_sha256': hashlib.sha256(bytes(value)).hexdigest(), 'length': len(value)}
    return value


def _rows(db, table):
    return sorted(({key: _normalized(value) for key,value in dict(raw).items()}
                   for raw in db.execute('SELECT * FROM '+table)), key=_json)


def _snapshot(db):
    tables = _tables(db)
    protected = set(PROTECTED) | {name for name in tables if name.startswith(('employee_card_', 'publish_review_'))}
    return dict(accounts={row['id']: dict(row) for row in db.execute('SELECT * FROM accounts')},
                sources=_sources(db), scopes=_rows(db, 'account_offices'),
                protected={name: operations.token(_rows(db, name)) for name in sorted(protected & tables)},
                history={row['id']: dict(row) for row in db.execute('SELECT * FROM operations_history')},
                audit={row['id']: dict(row) for row in db.execute('SELECT * FROM auth_audit')})


def _guard(before):
    return operations.token(before)


def _issue(code, message):
    return dict(code=code, message=message)


def _source_public(raw):
    if raw is None:
        return None
    return {**raw, 'source_data': json.loads(raw['source_data'])}


def _changes(before, after, old_source, new_source):
    changes = [dict(field=key, before=(before or {}).get(key), after=(after or {}).get(key))
               for key in ACCOUNT_FIELDS if (before or {}).get(key) != (after or {}).get(key)]
    old, new = _source_public(old_source) or {}, _source_public(new_source) or {}
    changes += [dict(field='source.'+key, before=old.get(key), after=new.get(key))
                for key in (*SOURCE_FIELDS, 'source_data') if old.get(key) != new.get(key)]
    return changes


def _new_source(old, ident, office, row, refs, metadata):
    if old and (old['office_id']!=office or old['account_id']!=ident):
        raise ValueError('账号来源记录归属不一致，保留待核对')
    result = dict(old) if old else dict(account_id=ident, office_id=office, revision=1,
                                      **{key: '' for key in SOURCE_FIELDS}, source_data='{}')
    for key, column in SOURCE_COLUMNS.items():
        if row[column]:
            if len(row[column]) > (100 if key in ('responsible', 'business_type') else 1000):
                raise ValueError('结构化来源字段过长')
            result[key] = row[column]
    data = json.loads(result['source_data'])
    for key, column in (('source_phone_number', '登录手机序号'), ('auxiliary_note', '小号说明'), ('original_status', '来源账号状态')):
        if row[column]:
            data[key] = row[column]
    data.update(source_round=metadata['round'], files={key:metadata[key] for key in ('accounts','progress')}, rows=refs)
    result['source_data'] = _json(data)
    if old and {key:value for key,value in result.items() if key != 'revision'} != {key:value for key,value in old.items() if key != 'revision'}:
        result['revision'] = old['revision']+1
    return result


def _computer(db, value, progress, occupied, old, office):
    number = legacy_source._digits(value, True)
    if not number or len(progress.get(number, [])) != 1:
        raise ValueError('资料登记电脑需在双表中明确且唯一，不能猜测编号')
    ident = COMPUTER_ID_PREFIXES[office]+number
    if not db.execute('SELECT 1 FROM computers WHERE id=?', (ident,)).fetchone() or office_scope.entity_offices(db, 'computers', ident) != {office}:
        raise ValueError('登记电脑尚不存在或并非仅属于办公室三；不会创建或移动设备')
    if old and old['computer_id'] == ident:
        return ident, old['slot']
    slot = next((slot for slot in range(1,101) if (ident,slot) not in occupied), None)
    if slot is None:
        raise ValueError('登记电脑没有可用资料位置')
    return ident, slot


def _write_account(db, plan):
    kind, ident, after = plan['action'], plan['account_id'], plan['after_account']
    if kind == 'create':
        columns = ('id', 'computer_id', 'slot', 'name', 'status', 'note', 'douyin_id', 'homepage', 'last_collected', 'registered_on', 'registration_phone')
        db.execute('INSERT INTO accounts('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                   tuple(after[key] for key in columns))
        db.execute('INSERT INTO account_offices VALUES(?,?)', (ident, plan['office_id']))
    elif kind == 'delete':
        db.execute('DELETE FROM table_sync_account_sources WHERE account_id=?', (ident,))
        db.execute('DELETE FROM account_offices WHERE account_id=?', (ident,))
        db.execute('DELETE FROM accounts WHERE id=?', (ident,))
    else:
        changes = {key: after[key] for key in ACCOUNT_FIELDS if plan['before_account'][key] != after[key]}
        if changes:
            db.execute('UPDATE accounts SET '+','.join(key+'=?' for key in changes)+' WHERE id=?', (*changes.values(), ident))
    if kind != 'delete':
        source = plan['after_source']
        if source is None:
            db.execute('DELETE FROM table_sync_account_sources WHERE account_id=?', (ident,))
        elif source != plan['before_source']:
            keys = ('account_id','office_id',*SOURCE_FIELDS,'source_data','revision')
            db.execute('INSERT INTO table_sync_account_sources('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+') '
                       'ON CONFLICT(account_id) DO UPDATE SET '+','.join(key+'=excluded.'+key for key in keys if key!='account_id'),
                       tuple(source[key] for key in keys))


def _safe_preview_row(db, plan, day, actor):
    """Use the real account/lifecycle validators without accepting implicit edits."""
    import account_service
    import nurture
    ident=plan['account_id']
    previous=[dict(row) for row in db.execute('SELECT * FROM nurture_records WHERE account_id=?',(ident,))]
    db.execute('SAVEPOINT table_sync_validate_row')
    try:
        if plan['action']=='update':
            changes={key:plan['after_account'][key] for key in ACCOUNT_FIELDS
                     if plan['before_account'][key]!=plan['after_account'][key]}
            if changes:
                account_service.patch(db,dict(id=ident,changes=changes),actor,day)
                validated=dict(db.execute('SELECT * FROM accounts WHERE id=?',(ident,)).fetchone())
                if validated!=plan['after_account']:
                    raise ValueError('现有账号校验会隐式改变大小号关系或其他非同步字段，整名账号待核对')
        _write_account(db, plan)
        if plan['action'] != 'delete' and (plan['before_account'] is None or any(
                plan['before_account'].get(key)!=plan['after_account'].get(key) for key in ('status','registered_on'))):
            nurture.sync(db, day, {plan['account_id']})
        current=[dict(row) for row in db.execute('SELECT * FROM nurture_records WHERE account_id=?',(ident,))]
        if current!=previous:
            raise ValueError('本行会影响养号或其他受保护资料，需单独人工核对')
        # Capture database defaults for exact apply/rollback protection.
        if plan['action'] == 'create':
            actual=dict(db.execute('SELECT * FROM accounts WHERE id=?', (ident,)).fetchone())
            defaults=dict(work_count=None,last_collected='',collection_state='',collection_error='',last_attempted='',
                          account_type='待确认',parent_account_id=None,auxiliary_state='待确认',lifecycle_note='',
                          card_stability='待确认',password_state='待确认',phone_device_id=None,phone_device_source='',
                          phone_device_note='',office_group='',nurture_started='')
            if any(actual.get(key)!=value for key,value in {**defaults,**plan['after_account']}.items()):
                raise ValueError('新增账号出现计划外身份、状态或关系，保留待核对')
            account_service.patch(db,dict(id=ident,changes={key:actual[key] for key in ACCOUNT_FIELDS}),actor,day)
            if dict(db.execute('SELECT * FROM accounts WHERE id=?',(ident,)).fetchone())!=actual:
                raise ValueError('新增账号校验会隐式改变关系或其他非同步字段，整名账号待核对')
            plan['after_account'] = actual
    finally:
        db.execute('ROLLBACK TO table_sync_validate_row')
        db.execute('RELEASE table_sync_validate_row')


def _validate_bundle(db,before,plans,rows):
    """One complete rollback-only guard per batch, not full-table hashing per row.

    Local row validation handles account constraints/nurture. This additional
    joint simulation catches cross-row constraints and any trigger side effects
    on protected business tables, scopes, history or permissions.
    """
    db.execute('SAVEPOINT table_sync_validate_bundle')
    message=None
    try:
        for plan in plans:_write_account(db,plan)
        _verify(db,before,plans)
    except (*integrity_errors(db),ValueError,KeyError,TypeError):
        message='联合验证存在资料约束或计划外业务变化，整批安全行保留待核对；未修改任何资料'
    finally:
        db.execute('ROLLBACK TO table_sync_validate_bundle')
        db.execute('RELEASE table_sync_validate_bundle')
    if message:
        selected={plan['row_key'] for plan in plans}
        for row in rows:
            if row['row_key'] in selected:
                row.update(action='conflict',selected=False,blocked=True,reason=message,
                           issues=[_issue('batch_validation_conflict',message)])
        return []
    return plans


def _plan(db, office, mode, account_rows, progress_rows, metadata, day, before, actor):
    if mode not in ('add','update'):
        raise ValueError('请选择安全新增或更新已有账号')
    by_handle = defaultdict(list)
    for value in before['accounts'].values():
        by_handle[value['douyin_id']].append(value)
    counts = Counter(row['抖音号'] for row in account_rows if row['抖音号'])
    progress, progress_handles = defaultdict(list), defaultdict(list)
    orphan_rows = []
    for row in progress_rows:
        number = legacy_source._digits(row['电脑序号'], True)
        if number:
            progress[number].append(row)
        for slot in range(1,11):
            label = row['发布设备'+str(slot)]
            if not label or label in ('.','。','-','—'):
                continue
            match = re.fullmatch(r'(?:抖音号|douyin_id)\s*[:：]\s*([A-Za-z0-9_.-]{2,100})', label)
            if match:
                progress_handles[match[1]].append(dict(row=row['来源行'], computer=number))
            else:
                orphan_rows.append(dict(row_key='progress:'+str(row['来源行'])+':'+str(slot), action='skip', selected=False,
                    account_id='', douyin_id='', current_name='', source_name='', source_rows=[dict(sheet='progress',row=row['来源行'],column='发布设备'+str(slot))],
                    changes=[], blocked=True, issues=[_issue('progress_identity_unverified','工作进度表此位置没有明确真实抖音号，只作来源核对，不按昵称或手机编号匹配')]))
    for handle,items in progress_handles.items():
        if handle not in counts:
            orphan_rows.append(dict(row_key='progress-unmatched:'+str(len(orphan_rows)),action='conflict',selected=False,
                blocked=True,account_id='',douyin_id=handle,current_name='',source_name='',changes=[],
                source_rows=[dict(sheet='progress',row=item['row']) for item in items],
                issues=[_issue('progress_unmatched','账号情况表没有此真实抖音号，双表来源不完整；不新增或猜测匹配')]))
    occupied = {(value['computer_id'],value['slot']) for value in before['accounts'].values() if value['slot'] is not None}
    rows, plans = [], []
    for index,row in enumerate(account_rows):
        handle = row['抖音号']
        if not handle and not row['昵称']:
            continue
        refs = [dict(sheet='accounts',row=row['来源行'])]
        matched_progress = progress_handles.get(handle, [])
        refs += [dict(sheet='progress',row=item['row']) for item in matched_progress]
        item = dict(row_key='account:'+str(index)+':'+str(row['来源行']), douyin_id=handle, source_rows=refs,
                    account_id='', current_name='', source_name=row['昵称'], action='conflict', selected=False,
                    blocked=True, changes=[], issues=[])
        old = None
        try:
            if not re.fullmatch(r'[A-Za-z0-9_.-]{2,100}', handle):
                raise ValueError('真实抖音号为空、含空白或格式不明确；不归一化或猜测匹配')
            found = by_handle.get(handle, [])
            if counts[handle] != 1 or len(found)>1 or len(matched_progress)>1:
                raise ValueError('任一来源或系统中存在重复真实抖音号，整名账号保留待核对')
            if found and office_scope.entity_offices(db, 'accounts', found[0]['id']) != {office}:
                raise ValueError('同抖音号属于其他办公室、待分配区或归属不明确；不复制、不移动')
            old = found[0] if found else None
            if old:
                item.update(account_id=old['id'],current_name=old['name'])
            if mode == 'add' and old or mode == 'update' and not old:
                item.update(action='skip',reason='已有账号由更新模式处理' if old else '系统无此账号；更新模式不会新增')
                rows.append(item)
                continue
            if matched_progress and row['登记电脑序号'] and legacy_source._digits(row['登记电脑序号'], True) != matched_progress[0]['computer']:
                raise ValueError('双表对同一真实抖音号的登记电脑存在矛盾；不修改发布安排')
            after = dict(old) if old else dict(id='account-'+uuid.uuid4().hex, computer_id='',slot=None,
                      name='昵称待补充',status='待确认',note='',douyin_id=handle,homepage='',last_collected='',
                      registered_on='',registration_phone='')
            ident = after['id']
            if row['昵称']:
                if len(row['昵称'])>100:
                    raise ValueError('昵称超过100字')
                after['name'] = row['昵称']
            if row['注册手机号']:
                if not re.fullmatch(r'1[0-9]{10}',row['注册手机号']):
                    raise ValueError('注册手机号不完整，整名账号待核对')
                after['registration_phone'] = row['注册手机号']
            if row['注册日期']:
                after['registered_on'] = legacy_source._registered(row['注册日期'],day)
            if row['来源账号状态']:
                if row['来源账号状态'] not in STATES:
                    raise ValueError('来源状态未有明确映射，保留系统原值并列入待核对')
                after['status'] = STATES[row['来源账号状态']]
                if old and after['status']=='养号7天' and old['status']!='养号7天':
                    raise ValueError('此状态更新会改变养号流程，需单独人工处理')
            if row['登记电脑序号'] or not old:
                computer,slot = _computer(db,row['登记电脑序号'],progress,occupied,old,office)
                after.update(computer_id=computer,slot=slot)
            source = _new_source(before['sources'].get(ident),ident,office,row,refs,metadata)
            plan = dict(row_key=item['row_key'],account_id=ident,office_id=office,douyin_id=handle,source_rows=refs,
                        action='update' if old else 'create',before_account=old,after_account=after,
                        before_source=before['sources'].get(ident),after_source=source)
            _safe_preview_row(db,plan,day,actor)
            changes = _changes(old,plan['after_account'],plan['before_source'],source)
            requested={key for key,column in (('name','昵称'),('registration_phone','注册手机号'),
                        ('registered_on','注册日期'),('status','来源账号状态')) if row[column]}
            if row['登记电脑序号']:requested.update(('computer_id','slot'))
            unchanged=sum(old is not None and old[key]==plan['after_account'][key] for key in requested)
            old_source=plan['before_source'] or {}
            unchanged+=sum(bool(row[column]) and old_source.get(key)==source[key] for key,column in SOURCE_COLUMNS.items())
            unchanged+=int(old_source.get('source_data')==source['source_data'])
            item.update(account_id=ident,action=plan['action'] if changes else 'same',selected=bool(changes),
                        blocked=not bool(changes),changes=changes,unchanged_fields=unchanged,
                        reason='仅修改预览的允许字段；不会移动实际发布位置')
            if changes:
                plans.append(plan)
                if old and old['computer_id']!=after['computer_id']:
                    occupied.discard((old['computer_id'],old['slot']))
                occupied.add((after['computer_id'],after['slot']))
        except integrity_errors(db):
            item['issues'] = [_issue('account_conflict','本行不能满足现有资料约束，整名账号保留待核对')]
            item['reason'] = item['issues'][0]['message']
        except (ValueError, OverflowError, KeyError, TypeError) as error:
            item['issues'] = [_issue('account_conflict',str(error))]
            item['reason'] = str(error)
        rows.append(item)
    rows += orphan_rows
    for item in rows:
        item.setdefault('reason','')
    return plans, rows


def _summary(rows, plans):
    handles = {row['douyin_id'] for row in rows if row.get('douyin_id')}
    conflicts = {row['douyin_id'] for row in rows if row.get('douyin_id') and row['action']=='conflict'}
    return dict(source_rows=len(rows),preview_rows=len(rows),unique_accounts=len(handles),safe_accounts=len(plans),
                updated_accounts=sum(plan['action']=='update' for plan in plans),new_accounts=sum(plan['action']=='create' for plan in plans),
                changed_fields=sum(len(row['changes']) for row in rows if row['selected']),
                unchanged_fields=sum(row.get('unchanged_fields',0) for row in rows),conflict_accounts=len(conflicts),
                conflict_items=sum(len(row['issues']) for row in rows),
                skipped_accounts=sum(row['action']=='skip' and bool(row.get('douyin_id')) for row in rows))


def _store_preview(db, actor, office, mode, source, rows, plans, day, before, *, reverses=None):
    expires = int(time.time())+1800
    summary = _summary(rows,plans)
    if mode!='rollback':
        summary['source_rows']=sum(source[name]['rows'] for name in ('accounts','progress'))
    payload = dict(office_id=office,mode=mode,source=source,rows=rows,plans=plans,summary=summary,
                   guard=_guard(before),day=day,generation=_generation(db),pilot_revision=_pilot(db,office)['revision'],
                   reverses_batch_id=reverses,expires_at=expires)
    ident = operations.preview(db,actor,PREVIEW_KIND,payload)
    return dict(preview_id=ident,office_id=office,mode=mode,source=source,rows=rows,summary=summary,expires_at=expires,
                confirmation=REVERSE_CONFIRMATION if mode=='rollback' else CONFIRMATION,
                warnings=['只使用真实抖音号精确匹配；空白不更新；电脑只指资料登记位置，实际发布安排不变。',
                          '预览有效30分钟；相关资料变化须重新预览；未知结果只能核对原请求编号。'])


def _preview(db, actor, office, body, day):
    _fields(body,{'mode','source_round','accounts_file','progress_file'})
    _enabled(db,office)
    accounts,progress,metadata = _read_sources(body)
    before = _snapshot(db)
    plans,rows = _plan(db,office,body.get('mode'),accounts,progress,metadata,day,before,actor)
    plans=_validate_bundle(db,before,plans,rows)
    if _snapshot(db) != before:
        raise operations.ConflictError('预览保护校验失败，未保存任何资料')
    return _store_preview(db,actor,office,body['mode'],metadata,rows,plans,day,before)


def _verify(db, before, plans):
    after = _snapshot(db)
    accounts,sources = dict(before['accounts']),dict(before['sources'])
    scopes = list(before['scopes'])
    for plan in plans:
        ident = plan['account_id']
        if plan['action']=='delete':
            accounts.pop(ident)
            sources.pop(ident,None)
            scopes = [row for row in scopes if row['account_id']!=ident]
        else:
            accounts[ident] = plan['after_account']
            if plan['after_source'] is None:
                sources.pop(ident,None)
            else:
                sources[ident] = plan['after_source']
            if plan['action']=='create':
                scopes.append(dict(account_id=ident,office_id=plan['office_id']))
    if (after['accounts'] != accounts or after['sources'] != sources or after['scopes'] != sorted(scopes,key=_json)
            or after['protected'] != before['protected']):
        raise operations.ConflictError('事务保护失败：出现计划外资料、身份、设备、发布或办公室变化，整批已撤销')
    if after['history'] != before['history'] or after['audit'] != before['audit']:
        raise operations.ConflictError('事务保护失败：原操作历史或审计出现计划外变化，整批已撤销')
    return dict(only_planned_fields_changed=True,other_offices_unchanged=True,identity_preserved=True,
                devices_preserved=True,publishing_preserved=True,nurture_preserved=True)


def _history(db, actor, office, plan, batch_id):
    before,after = plan['before_account'],plan['after_account']
    old = {**before,'office_ids':[office]} if before is not None else None
    new = {**after,'office_ids':[office]} if after is not None else None
    count=0
    if before!=after:
        db.execute('INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices) VALUES(?,?,?,?,?,?,?,?,?)',
                   (batch_id,int(time.time()),actor['id'],actor['name'],'/api/table-sync/apply',
                    'account:'+plan['account_id'],_json(old) if old is not None else None,
                    _json(new) if new is not None else None,_json([office])))
        count+=1
    if plan['before_source']!=plan['after_source']:
        marker=dict(account_id=plan['account_id'],office_ids=[office],revision=(plan['after_source'] or {}).get('revision'))
        db.execute('INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices) VALUES(?,?,?,?,?,?,?,?,?)',
                   (batch_id,int(time.time()),actor['id'],actor['name'],'/api/table-sync/apply',
                    'account-source:'+plan['account_id'],None,_json(marker),_json([office])))
        count+=1
    return count


def _apply(db, actor, office, body, day, *, reverse=False):
    _fields(body,{'preview_id','selected_rows','confirmation'})
    pilot = _enabled(db,office)
    ident = body.get('preview_id')
    if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):
        raise ValueError('预览编号不正确')
    payload = operations.take_preview(db,actor,ident,PREVIEW_KIND)
    if (payload['mode']=='rollback')!=reverse:
        raise ValueError('确认接口与预览类型不一致，未保存任何修改')
    expected_phrase = REVERSE_CONFIRMATION if payload['mode']=='rollback' else CONFIRMATION
    if body.get('confirmation')!=expected_phrase:
        raise ValueError('请明确输入“'+expected_phrase+'”后确认')
    # The endpoint must agree with the actor-bound preview; a client cannot move
    # a normal plan into reversal handling or vice versa.
    selected = body.get('selected_rows')
    if not isinstance(selected,list) or len(selected)>5000 or any(not isinstance(key,str) for key in selected) or len(set(selected))!=len(selected):
        raise ValueError('请选择不重复的安全账号行')
    plans = [plan for plan in payload['plans'] if plan['row_key'] in set(selected)]
    if len(plans)!=len(selected):
        raise ValueError('不能选择冲突、跳过、无变化或不属于本预览的行')
    before = _snapshot(db)
    if (payload['office_id']!=office or payload['day']!=day or payload['generation']!=_generation(db)
            or payload['pilot_revision']!=pilot['revision'] or payload['guard']!=_guard(before)):
        raise operations.ConflictError('预览后资料、权限、日期或试点状态已变化，请重新预览；未保存任何修改')
    if payload['mode']=='rollback' and db.execute('SELECT 1 FROM table_sync_batches WHERE reverts_batch_id=?',(payload['reverses_batch_id'],)).fetchone():
        raise operations.ConflictError('此批次已有回退回执，请先核对原回退结果')
    for plan in plans:
        if plan['before_account'] is not None and office_scope.entity_offices(db,'accounts',plan['account_id'])!={office}:
            raise operations.ConflictError('账号归属已变化，整批未执行')
        _write_account(db,plan)
    verification = _verify(db,before,plans)
    batch_id = _id()
    changes = []
    for plan in plans:
        changes += [dict(account_id=plan['account_id'],douyin_id=plan['douyin_id'],source_rows=plan['source_rows'],**change)
                    for change in _changes(plan['before_account'],plan['after_account'],plan['before_source'],plan['after_source'])]
    rows = [{**row,'applied':row['row_key'] in set(selected),'selected':row['row_key'] in set(selected)} for row in payload['rows']]
    summary = {**payload['summary'],'applied_accounts':len(plans),'changed_fields':len(changes),
               'excluded_safe_accounts':len(payload['plans'])-len(plans)}
    issues = []
    for row in rows:
        for issue in row['issues']:
            issues.append(dict(id=_id(),row_key=row['row_key'],douyin_id=row['douyin_id'],source_rows=row['source_rows'],
                               state='open',note='',**issue))
    receipt = dict(batch_id=batch_id,office_id=office,created_at=int(time.time()),kind=payload['mode'],
                   reverts_batch_id=payload.get('reverses_batch_id'),actor=dict(id=actor['id'],name=actor['name']),
                   source=payload['source'],summary=summary,rows=rows,changes=changes,issues=issues,verification=verification)
    db.execute('INSERT INTO table_sync_batches VALUES(?,?,?,?,?,?,?,?)',
               (batch_id,office,actor['id'],receipt['created_at'],payload['mode'],payload.get('reverses_batch_id'),_json(receipt),_json(plans)))
    for issue in issues:
        db.execute('INSERT INTO table_sync_review_items VALUES(?,?,?,?,?,?,?)',
                   (issue['id'],batch_id,office,'open',_json(issue),'',int(time.time())))
    history_count=sum(_history(db,actor,office,plan,batch_id) for plan in plans)
    _event(db,actor,office,'batch',batch_id,None,dict(kind=payload['mode'],summary=summary))
    db.execute('DELETE FROM operations_previews WHERE id=?',(ident,))
    # Verify that history/audit did not mutate any business row through triggers.
    final = _snapshot(db)
    for key in ('accounts','sources','scopes','protected'):
        if final[key] != _snapshot_after(before,plans)[key]:
            raise operations.ConflictError('历史审计后保护校验失败，整批已撤销')
    if any(final['history'].get(key)!=value for key,value in before['history'].items()) or any(final['audit'].get(key)!=value for key,value in before['audit'].items()):
        raise operations.ConflictError('原历史或审计发生变化，整批已撤销')
    if len(final['history'])-len(before['history'])!=history_count or len(final['audit'])-len(before['audit'])!=1:
        raise operations.ConflictError('产生计划外业务历史，整批已撤销')
    return dict(ok=True,batch_id=batch_id,office_id=office,summary=summary,receipt=receipt,verification=verification)


def _snapshot_after(before, plans):
    expected = {**before,'accounts':dict(before['accounts']),'sources':dict(before['sources']),'scopes':list(before['scopes'])}
    for plan in plans:
        ident = plan['account_id']
        if plan['action']=='delete':
            expected['accounts'].pop(ident)
            expected['sources'].pop(ident,None)
            expected['scopes']=[row for row in expected['scopes'] if row['account_id']!=ident]
        else:
            expected['accounts'][ident]=plan['after_account']
            if plan['after_source'] is None:expected['sources'].pop(ident,None)
            else:expected['sources'][ident]=plan['after_source']
            if plan['action']=='create':expected['scopes'].append(dict(account_id=ident,office_id=plan['office_id']))
    expected['scopes']=sorted(expected['scopes'],key=_json)
    return expected


def _receipt(db,office,ident):
    row = db.execute('SELECT receipt_json FROM table_sync_batches WHERE id=? AND office_id=?',(ident,office)).fetchone()
    if not row:
        raise ValueError('未找到本办公室批次')
    result=json.loads(row['receipt_json'])
    result['issues']=[{**json.loads(item['payload_json']),'state':item['state'],'note':item['note'],'updated_at':item['updated_at']}
                      for item in db.execute('SELECT * FROM table_sync_review_items WHERE batch_id=? AND office_id=? ORDER BY id',(ident,office))]
    return result


def _review(db,actor,office,body,day):
    _fields(body,{'batch_id','issue_id','state','note'})
    state=body.get('state')
    if state not in ('open','confirmed_no_action','corrected'):
        raise ValueError('待核对状态不正确')
    note=_text(body.get('note',''),'核对依据',1000,required=state!='open')
    row=db.execute('SELECT * FROM table_sync_review_items WHERE id=? AND batch_id=? AND office_id=?',
                   (body.get('issue_id'),body.get('batch_id'),office)).fetchone()
    if not row:
        raise ValueError('未找到本批次待核对项')
    before=dict(state=row['state'],note=row['note'])
    after=dict(state=state,note=note)
    db.execute('UPDATE table_sync_review_items SET state=?,note=?,updated_at=? WHERE id=?',
               (state,note,int(time.time()),row['id']))
    _event(db,actor,office,'review',row['id'],before,after)
    return dict(ok=True,batch_id=body['batch_id'],issue_id=row['id'],**after)


def _dependencies(db,ident):
    tables=_tables(db)
    if db.execute('SELECT 1 FROM accounts WHERE parent_account_id=?',(ident,)).fetchone():
        return True
    for table in sorted(tables-set(TABLES)-{'accounts','account_offices','operations_history','operations_previews'}):
        if not re.fullmatch(r'[a-z][a-z0-9_]*',table):continue
        if getattr(db,'dialect','sqlite')=='postgresql':
            columns={row[0] for row in db.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?',(table,))}
        else:
            columns={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
        if 'account_id' in columns and db.execute('SELECT 1 FROM '+table+' WHERE account_id=? LIMIT 1',(ident,)).fetchone():
            return True
    return False


def _reverse_preview(db,actor,office,body,day):
    _fields(body,{'batch_id'})
    _enabled(db,office)
    ident=body.get('batch_id')
    row=db.execute('SELECT kind,plan_json FROM table_sync_batches WHERE id=? AND office_id=?',(ident,office)).fetchone()
    if not row or row['kind']=='rollback':
        raise ValueError('请选择可回退的原始同步批次')
    if db.execute('SELECT 1 FROM table_sync_batches WHERE reverts_batch_id=?',(ident,)).fetchone():
        raise operations.ConflictError('此批次已有回退回执，不可重复回退')
    original=json.loads(row['plan_json'])
    if not original:
        raise ValueError('该批次仅记录核对结果，没有业务变更需要回退')
    before=_snapshot(db)
    rows,plans=[],[]
    for index,old in enumerate(original):
        account_id=old['account_id']
        current=before['accounts'].get(account_id)
        item=dict(row_key='rollback:'+str(index),account_id=account_id,douyin_id=old['douyin_id'],source_rows=old['source_rows'],
                  action='conflict',selected=False,blocked=True,current_name=(current or {}).get('name',''),
                  source_name=(old['before_account'] or {}).get('name',''),changes=[],issues=[],reason='')
        try:
            if current is None or office_scope.entity_offices(db,'accounts',account_id)!={office} or current['douyin_id']!=old['douyin_id']:
                raise ValueError('账号已不存在、身份或办公室变化，不能回退')
            if before['sources'].get(account_id)!=old['after_source']:
                raise ValueError('来源字段已被后续修改，不能覆盖')
            if old['action']=='create':
                if current!=old['after_account'] or _dependencies(db,account_id):
                    raise ValueError('新增账号已有后续修改或依赖，保留账号，不执行删除')
                after=None
                action='delete'
            else:
                changes={key:value for key,value in old['after_account'].items()
                         if key in ACCOUNT_FIELDS and value!=old['before_account'][key]}
                if any(current[key]!=value for key,value in changes.items()):
                    raise ValueError('拟回退字段已被后续人工修改，不能覆盖')
                after={**current,**{key:old['before_account'][key] for key in changes}}
                action='update'
            plan=dict(row_key=item['row_key'],account_id=account_id,office_id=office,douyin_id=old['douyin_id'],source_rows=old['source_rows'],
                      action=action,before_account=current,after_account=after,before_source=before['sources'].get(account_id),
                      after_source=old['before_source'])
            _safe_preview_row(db,plan,day,actor)
            item.update(action='update',selected=True,blocked=False,
                        changes=_changes(current,after,plan['before_source'],plan['after_source']),
                        reason='仅恢复仍等于本批写入值的字段；保留后续无关修改' if action=='update' else '删除尚无后续修改或依赖的本批新增账号')
            plans.append(plan)
        except integrity_errors(db):
            item['issues']=[_issue('rollback_conflict','回退不满足现有资料约束，整名账号保留待核对')]
            item['reason']=item['issues'][0]['message']
        except (ValueError,KeyError,TypeError) as error:
            item['issues']=[_issue('rollback_conflict',str(error))]
            item['reason']=str(error)
        rows.append(item)
    plans=_validate_bundle(db,before,plans,rows)
    if _snapshot(db)!=before:
        raise operations.ConflictError('反向预览保护校验失败')
    return _store_preview(db,actor,office,'rollback',dict(reverts_batch_id=ident),rows,plans,day,before,reverses=ident)
