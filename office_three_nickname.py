"""Owner-confirmed, names-only repair through the existing dual-import routes.

Source and database handles are exact strings. No phone/name guessing, new
accounts, moves or empty-name overwrites are performed. The HTTP caller owns
admission, CSRF and persistent request replay, and must not finish twice.
"""
import base64
from collections import Counter, defaultdict
import csv
import io
import re
from types import SimpleNamespace

import account_service
import auth
import idempotency
import nurture
import office_scope
import office_three_import as source
import operations
from sql_dialect import integrity_errors

OFFICE_ID = 'three'
MODE = 'fill_missing_nicknames'
KIND = 'office-three-nickname-fill'
CONFIRMATION = '仅补全办公室三缺失昵称'
APPLY_PATH = '/api/workspace/office-three-import-apply'
AUDIT_ACTION = '办公室三缺失昵称来源'
SOURCE_URL = 'https://docs.qq.com/sheet/DRWxWc1huUWFsVkZp?tab=BB08J2'
SOURCE_SHEET = 'BB08J2'


def _require(db, actor, body):
    actor = source._require(db, actor, body)
    if body.get('mode') != MODE:
        raise ValueError('昵称补全模式不正确，请重新预览')
    return actor


def _read_accounts(body):
    # Keep the reviewed raw-column/header allowlist. The work-progress file is
    # deliberately internal and empty: its labels cannot establish identity.
    stream = io.StringIO(newline='')
    csv.writer(stream).writerow(source.PROGRESS_HEADERS)
    empty = dict(filename='empty-progress.csv',
                 content=base64.b64encode(stream.getvalue().encode('utf-8')).decode('ascii'))
    accounts, _ = source._read_sources(dict(accounts_file=body.get('accounts_file'), progress_file=empty))
    return [row for row in accounts if row['昵称'] or row['抖音号']]


def _missing(value):
    return value is None or value == '' or (isinstance(value, str) and
        re.fullmatch(r'昵称待补充(?:（手机[1-9][0-9]{0,5}）)?', value) is not None)


def _entry(row, action, reason='', old=None):
    return dict(row=row['来源行'], account_id=old['id'] if old else '',
        douyin_id=row['抖音号'], current_name=old['name'] if old else '',
        source_name=row['昵称'], reason=reason, action=action)


def _verify(db, before, plan):
    after = source._snapshot(db)
    expected = dict(before['records'])
    seen = set()
    for item in plan:
        ident = item['account_id']
        key = 'account:' + ident
        old = expected.get(key)
        if (ident in seen or old is None or old.get('office_ids') != [OFFICE_ID]
                or old['douyin_id'] != item['douyin_id'] or old['name'] != item['before_name']
                or not _missing(old['name']) or not item['after_name']
                or len(item['after_name']) > 100 or item['after_name'].startswith('昵称待补充')):
            raise operations.ConflictError('补名计划身份、归属或名称不正确，整批已撤销')
        seen.add(ident)
        expected[key] = {**old, 'name': item['after_name']}
    if after['records'] != expected or after['extra'] != before['extra']:
        raise operations.ConflictError('补名保留校验失败：名称之外的资料、关系或历史发生变化，整批已撤销')
    return dict(only_name_fields_changed=True, other_offices_unchanged=True,
                associations_preserved=True, updated_accounts=len(plan))


def _patch(db, actor, item, day):
    if office_scope.entity_offices(db, 'accounts', item['account_id']) != {OFFICE_ID}:
        raise operations.ConflictError('账号归属已变化，整批已撤销')
    old = db.execute('SELECT name,douyin_id FROM accounts WHERE id=?', (item['account_id'],)).fetchone()
    if old is None or old['douyin_id'] != item['douyin_id'] or old['name'] != item['before_name']:
        raise operations.ConflictError('账号身份或名称已变化，请重新预览')
    account_service.patch(db, dict(id=item['account_id'], changes={'name': item['after_name']}), actor, day)


def preview(db, actor, body, day):
    actor = _require(db, actor, body)
    account_rows = _read_accounts(body)
    before = source._snapshot(db)
    existing = defaultdict(list)
    for row in db.execute('SELECT id,name,douyin_id FROM accounts'):
        existing[row['douyin_id']].append(dict(row))
    counts = Counter(row['抖音号'] for row in account_rows if row['抖音号'])
    rows, plan = [], []
    matched = 0
    db.execute('SAVEPOINT nickname_fill_preview')
    try:
        for row in account_rows:
            handle, name = row['抖音号'], row['昵称']
            found = existing.get(handle, [])
            if not re.fullmatch(r'[A-Za-z0-9_.-]{2,100}', handle):
                rows.append(_entry(row, 'conflict', '真实抖音号缺失或格式不明确；不按名称、手机号或手机编号猜测'))
            elif counts[handle] != 1:
                rows.append(_entry(row, 'conflict', '来源有重复抖音号，整组保留待核对'))
            elif not found:
                rows.append(_entry(row, 'unmatched', '工作台没有此抖音号；补名入口不新增账号'))
            elif len(found) != 1:
                rows.append(_entry(row, 'conflict', '工作台存在重复抖音号，整组保留待核对'))
            elif office_scope.entity_offices(db, 'accounts', found[0]['id']) != {OFFICE_ID}:
                # Do not expose another office's stored name/internal identity.
                rows.append(_entry(row, 'conflict', '账号并非明确且仅属于办公室三；不移动或覆盖'))
            else:
                old = found[0]
                matched += 1
                if not name:
                    rows.append(_entry(row, 'skip', '来源昵称为空，保留系统原值', old))
                elif len(name) > 100 or name.startswith('昵称待补充'):
                    rows.append(_entry(row, 'conflict', '来源昵称过长或仍为待补充标记，保留原值', old))
                elif old['name'] == name:
                    rows.append(_entry(row, 'same', '昵称已经一致，不重复修改', old))
                elif not _missing(old['name']):
                    rows.append(_entry(row, 'conflict', '两边已有不同真实名称；本入口仅补空白或占位名称', old))
                else:
                    item = dict(account_id=old['id'], douyin_id=handle, before_name=old['name'],
                        after_name=name, source_row=row['来源行'], source_url=SOURCE_URL, source_sheet=SOURCE_SHEET)
                    db.execute('SAVEPOINT nickname_fill_row')
                    try:
                        _patch(db, actor, item, day)
                        nurture.sync(db, day, {old['id']})
                        _verify(db, before, plan + [item])
                    except (*integrity_errors(db), ValueError, TypeError, KeyError):
                        db.execute('ROLLBACK TO nickname_fill_row')
                        rows.append(_entry(row, 'conflict', '补名会触发额外资料变化或校验冲突，整行保留', old))
                    else:
                        plan.append(item)
                        rows.append(_entry(row, 'update', '唯一抖音号匹配；仅补全缺失昵称', old))
                    finally:
                        db.execute('RELEASE nickname_fill_row')
        _verify(db, before, plan)
    finally:
        db.execute('ROLLBACK TO nickname_fill_preview')
        db.execute('RELEASE nickname_fill_preview')
    summary = dict(source_rows=len(account_rows), matched_accounts=matched, updated_accounts=len(plan),
        conflicts=sum(row['action'] == 'conflict' for row in rows),
        unmatched=sum(row['action'] == 'unmatched' for row in rows),
        unchanged=sum(row['action'] == 'same' for row in rows), skipped=sum(row['action'] == 'skip' for row in rows))
    payload = dict(mode=MODE, office_id=OFFICE_ID, day=day, guard=operations.token(before),
                   plan=plan, summary=summary)
    ident = operations.preview(db, actor, KIND, payload)
    return dict(mode=MODE, preview_id=ident, office_id=OFFICE_ID, confirmation=CONFIRMATION,
        summary=summary, rows=rows, source_url=SOURCE_URL, source_sheet=SOURCE_SHEET,
        warnings=['仅补全办公室三唯一账号的空白或严格占位昵称；不新增账号，不修改电脑、手机、卡或其他业务字段。',
                  '源空白保留；真实名称不一致、重复标识、未匹配或归属不明确的记录均不修改。'])


def apply(db, actor, body, day):
    actor = _require(db, actor, body)
    if not getattr(db, 'restore_generation', None):
        raise ValueError('当前环境未启用持久幂等，禁止执行昵称补全')
    allowed = {'mode', 'office_id', 'preview_id', 'confirmation', 'request_id', '_expected_versions'}
    if set(body) - allowed or body.get('_expected_versions', {}) != {}:
        raise ValueError('确认请求不能包含客户端修改计划或整行资料')
    idempotency.request_uuid(body.get('request_id'))
    if body.get('confirmation') != CONFIRMATION:
        raise ValueError('请明确输入“' + CONFIRMATION + '”确认')
    ident = body.get('preview_id')
    if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise ValueError('预览编号不正确，请重新预览')
    payload = operations.take_preview(db, actor, ident, KIND)
    before = source._snapshot(db)
    if (payload.get('mode') != MODE or payload.get('office_id') != OFFICE_ID or payload.get('day') != day
            or payload.get('guard') != operations.token(before)):
        raise operations.ConflictError('预览后资料、归属或日期已变化，请重新预览；未保存任何补名')
    plan = payload['plan']
    history_before = {row['id']: dict(row) for row in db.execute('SELECT * FROM operations_history')}
    db.execute('SAVEPOINT nickname_fill_apply')
    try:
        state = operations.before(db, APPLY_PATH, body)
        for item in plan:
            _patch(db, actor, item, day)
        handler = SimpleNamespace()
        operations.finish(handler, db, actor, APPLY_PATH, state)
        history = [dict(row) for row in db.execute('SELECT * FROM operations_history')]
        if any(row != history_before[row['id']] for row in history if row['id'] in history_before):
            raise operations.ConflictError('原有修改历史发生变化，整批已撤销')
        if not history_before.keys() <= {row['id'] for row in history}:
            raise operations.ConflictError('原有修改历史缺失，整批已撤销')
        added_history = [row for row in history if row['id'] not in history_before]
        references = {row['entity']: row['id'] for row in added_history}
        if (len(added_history) != len(plan) or any(row['action'] != APPLY_PATH for row in added_history)
                or set(references) != {'account:' + item['account_id'] for item in plan}):
            raise operations.ConflictError('补名产生计划外历史记录，整批已撤销')
        changes = []
        for item in plan:
            target = operations.encoded(dict(source=SOURCE_URL, row=item['source_row'],
                history_id=references['account:' + item['account_id']], preview=ident))
            if len(target) > 200:
                raise operations.ConflictError('来源审计引用过长，整批已撤销')
            auth.audit(db, actor, AUDIT_ACTION, target)
            changes.append({**item, 'after_version': handler._version_delta['account:' + item['account_id']],
                            'history_id': references['account:' + item['account_id']]})
        db.execute('DELETE FROM operations_previews WHERE id=?', (ident,))
        verification = _verify(db, before, plan)
        if {row['id']: dict(row) for row in db.execute('SELECT * FROM operations_history')} != {
                row['id']: row for row in history}:
            raise operations.ConflictError('审计后修改历史发生变化，整批已撤销')
        verification['history_preserved'] = True
    except Exception:
        db.execute('ROLLBACK TO nickname_fill_apply')
        db.execute('RELEASE nickname_fill_apply')
        raise
    db.execute('RELEASE nickname_fill_apply')
    return dict(ok=True, mode=MODE, office_id=OFFICE_ID, summary=payload['summary'],
        updated_accounts=len(plan), changes=changes, verification=verification,
        requires_refresh=True, note='仅保存已预览昵称；来源及修改前后值已记录，其他业务资料保持。')
