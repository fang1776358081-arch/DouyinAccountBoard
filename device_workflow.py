"""Independent physical phones, SIM locations and account recovery records.

Schema changes are explicit: migrate() is SQLite-only; PostgreSQL uses 0004.
Read paths never infer a card location or confirm that an account is logged in.
"""
from datetime import date
import json
import time
import uuid

import auth

UNIT_STATUSES = ['待确认', '在用', '待清理', '可用闲置', '故障', '送修', '返修待验', '回收']
CASE_STATUSES = ['待处理', '处理中', '暂挂', '已结案']
RECOVERY_STATES = ['待处理', '等待协助', '恢复成功', '暂不恢复', '无法恢复']
LOGIN_STATES = ['待确认', '登录受阻', '可操作']
USABLE_UNITS = {'在用', '可用闲置'}
REPLACEABLE_UNITS = {'故障', '送修', '返修待验', '回收'}
RESOLVED_RECOVERY = {'恢复成功', '暂不恢复', '无法恢复'}
TABLE_KEYS = {'phone_units': ['id'], 'phone_card_placements': ['card_id'],
              'phone_account_access': ['account_id'], 'device_cases': ['id'],
              'device_workflow_events': ['id']}
PREFIX = {'phone_units': 'unit', 'phone_card_placements': 'placement',
          'phone_account_access': 'access', 'device_cases': 'case',
          'device_workflow_events': 'workflow_event'}


def available(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        row = db.execute(
            "SELECT to_regclass(?),to_regclass(?),to_regclass(?),to_regclass(?),to_regclass(?)",
            tuple(TABLE_KEYS)).fetchone()
        return bool(row) and all(row[index] is not None for index in range(len(TABLE_KEYS)))
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return set(TABLE_KEYS) <= names


def migrate(db):
    if getattr(db, 'dialect', 'sqlite') != 'sqlite':
        raise ValueError('PostgreSQL设备流程结构只能通过受控迁移创建')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS phone_units(
      id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES phone_devices(id),
      original_device_id TEXT NOT NULL REFERENCES phone_devices(id),
      generation INTEGER NOT NULL CHECK(generation>0),active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      status TEXT NOT NULL DEFAULT '待确认',model TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,UNIQUE(device_id,generation));
    CREATE UNIQUE INDEX IF NOT EXISTS phone_unit_current ON phone_units(device_id) WHERE active=1;
    CREATE TABLE IF NOT EXISTS device_cases(
      id TEXT PRIMARY KEY,unit_id TEXT NOT NULL REFERENCES phone_units(id),status TEXT NOT NULL DEFAULT '待处理',
      reason TEXT NOT NULL,owner TEXT NOT NULL DEFAULT '',followup_on TEXT NOT NULL DEFAULT '',
      note TEXT NOT NULL DEFAULT '',closure_reason TEXT NOT NULL DEFAULT '',created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,closed_at INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,
      account_ids TEXT NOT NULL DEFAULT '[]');
    CREATE INDEX IF NOT EXISTS device_case_unit ON device_cases(unit_id,status);
    CREATE TABLE IF NOT EXISTS phone_card_placements(
      card_id INTEGER PRIMARY KEY REFERENCES phone_cards(id),unit_id TEXT REFERENCES phone_units(id),
      slot TEXT NOT NULL DEFAULT '',storage TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL,
      revision INTEGER NOT NULL DEFAULT 0,
      CHECK((unit_id IS NOT NULL AND slot IN ('1','2','待确认') AND storage='') OR
            (unit_id IS NULL AND slot='' AND storage<>'')));
    CREATE UNIQUE INDEX IF NOT EXISTS phone_unit_card_slot ON phone_card_placements(unit_id,slot) WHERE unit_id IS NOT NULL AND slot IN ('1','2');
    CREATE TABLE IF NOT EXISTS phone_account_access(
      account_id TEXT PRIMARY KEY REFERENCES accounts(id),last_unit_id TEXT REFERENCES phone_units(id),
      target_unit_id TEXT REFERENCES phone_units(id),login_state TEXT NOT NULL DEFAULT '待确认',
      recovery_state TEXT NOT NULL DEFAULT '待处理',reason TEXT NOT NULL DEFAULT '',owner TEXT NOT NULL DEFAULT '',
      followup_on TEXT NOT NULL DEFAULT '',case_id TEXT REFERENCES device_cases(id),
      confirmed_at INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS device_workflow_events(
      id TEXT PRIMARY KEY,case_id TEXT REFERENCES device_cases(id),unit_id TEXT REFERENCES phone_units(id),
      card_id INTEGER REFERENCES phone_cards(id),account_id TEXT REFERENCES accounts(id),
      actor_id INTEGER,actor_name TEXT NOT NULL,at INTEGER NOT NULL,action TEXT NOT NULL,detail TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS device_event_unit ON device_workflow_events(unit_id,at,id);
    CREATE INDEX IF NOT EXISTS device_event_case ON device_workflow_events(case_id,at,id);
    ''')
    now = int(time.time())
    db.execute("""INSERT INTO phone_units(id,device_id,original_device_id,generation,status,note,created_at)
        SELECT 'legacy-unit:'||id,id,id,1,state,'由原设备编号建立，实物资料待核对',?
        FROM phone_devices WHERE NOT EXISTS(SELECT 1 FROM phone_units u WHERE u.device_id=phone_devices.id)""", (now,))
    db.execute("""INSERT INTO phone_account_access(account_id,last_unit_id)
        SELECT a.id,u.id FROM accounts a JOIN phone_units u ON u.device_id=a.phone_device_id AND u.active=1
        WHERE NOT EXISTS(SELECT 1 FROM phone_account_access x WHERE x.account_id=a.id)""")


def _text(body, field, default='', limit=2000, required=False):
    value = body.get(field, default)
    if not isinstance(value, str):
        raise ValueError(field + '内容格式不正确')
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError(field + ('不能为空' if not value else '内容过长'))
    return value


def _followup(body, default=''):
    value = _text(body, 'followup_on', default, 10)
    if value:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            raise ValueError('跟进日期格式不正确') from None
    return value


def _row(db, table, ident, message):
    key = TABLE_KEYS.get(table, ['id'])[0]
    row = db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (ident,)).fetchone()
    if not row:
        raise ValueError(message)
    return dict(row)


def ensure_unit(db, device_id):
    if not available(db):
        raise ValueError('设备流程尚未完成受控迁移')
    device = _row(db, 'phone_devices', str(device_id), '手机编号不存在')
    row = db.execute('SELECT * FROM phone_units WHERE device_id=? AND active=1', (device['id'],)).fetchone()
    if row:
        return dict(row)
    if db.execute('SELECT 1 FROM phone_units WHERE device_id=?', (device['id'],)).fetchone():
        raise ValueError('该编号没有现用实物，请通过新机补号登记')
    ident = 'unit-' + uuid.uuid4().hex
    db.execute('INSERT INTO phone_units(id,device_id,original_device_id,generation,status,created_at) VALUES(?,?,?,1,?,?)',
               (ident, device['id'], device['id'], device['state'], int(time.time())))
    return _row(db, 'phone_units', ident, '实物手机不存在')


def _unit(db, body):
    if body.get('unit_id'):
        return _row(db, 'phone_units', str(body['unit_id']), '实物手机不存在')
    return ensure_unit(db, _text(body, 'device_id', limit=80, required=True))


def _access(db, account_id):
    _row(db, 'accounts', account_id, '账号不存在')
    row = db.execute('SELECT * FROM phone_account_access WHERE account_id=?', (account_id,)).fetchone()
    if row:
        return dict(row)
    account = _row(db, 'accounts', account_id, '账号不存在')
    unit = ensure_unit(db, account['phone_device_id']) if account['phone_device_id'] else None
    db.execute('INSERT INTO phone_account_access(account_id,last_unit_id) VALUES(?,?)',
               (account_id, unit['id'] if unit else None))
    return _row(db, 'phone_account_access', account_id, '账号记录不存在')


def _event(db, actor, action, detail, *, unit_id=None, case_id=None, card_id=None, account_id=None):
    db.execute('''INSERT INTO device_workflow_events
        (id,case_id,unit_id,card_id,account_id,actor_id,actor_name,at,action,detail)
        VALUES(?,?,?,?,?,?,?,?,?,?)''',
        ('event-' + uuid.uuid4().hex, case_id, unit_id, card_id, account_id,
         actor['id'], actor['name'], int(time.time()), action,
         json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _touch(db, *unit_ids):
    for ident in set(unit_ids) - {None, ''}:
        db.execute('UPDATE phone_units SET revision=revision+1 WHERE id=?', (ident,))


def _refs(db, body):
    """Gather every existing reference without mutating, also for optimistic locks."""
    refs = {'devices': set(), 'units': set(), 'accounts': set(), 'cards': set(), 'cases': set()}
    if body.get('device_id'):
        refs['devices'].add(str(body['device_id']))
        refs['units'].update(row[0] for row in db.execute('SELECT id FROM phone_units WHERE device_id=? AND active=1', (str(body['device_id']),)))
    for field in ('unit_id', 'target_unit_id'):
        if body.get(field):
            refs['units'].add(str(body[field]))
    if body.get('case_id'):
        refs['cases'].add(str(body['case_id']))
    if body.get('account_id'):
        refs['accounts'].add(str(body['account_id']))
    if body.get('card_id') is not None:
        if type(body['card_id']) is not int or body['card_id'] < 1:
            raise ValueError('手机卡编号不正确')
        refs['cards'].add(body['card_id'])
    affected = body.get('affected_account_ids', [])
    if not isinstance(affected, list) or len(affected) > 1000 or any(not isinstance(v, str) or not v for v in affected):
        raise ValueError('受影响账号格式不正确')
    refs['accounts'].update(affected)
    for ident in list(refs['accounts']):
        access = db.execute('SELECT * FROM phone_account_access WHERE account_id=?', (ident,)).fetchone()
        if access:
            refs['units'].update(value for value in (access['last_unit_id'], access['target_unit_id']) if value)
            if access['case_id']:
                refs['cases'].add(access['case_id'])
        else:
            account = db.execute('SELECT phone_device_id FROM accounts WHERE id=?', (ident,)).fetchone()
            if account and account[0]:
                refs['devices'].add(account[0])
                refs['units'].update(row[0] for row in db.execute('SELECT id FROM phone_units WHERE device_id=? AND active=1', (account[0],)))
    for ident in refs['cards']:
        row = db.execute('SELECT unit_id FROM phone_card_placements WHERE card_id=?', (ident,)).fetchone()
        if row and row[0]:
            refs['units'].add(row[0])
    for ident in refs['cases']:
        row = _row(db, 'device_cases', ident, '故障处理记录不存在')
        refs['units'].add(row['unit_id'])
        refs['accounts'].update(json.loads(row['account_ids']))
    for ident in refs['units']:
        unit = _row(db, 'phone_units', ident, '实物手机不存在')
        refs['devices'].add(unit['device_id'])
        refs['devices'].add(unit['original_device_id'])
        refs['cards'].update(row[0] for row in db.execute('SELECT card_id FROM phone_card_placements WHERE unit_id=?', (ident,)))
        refs['accounts'].update(row[0] for row in db.execute('SELECT account_id FROM phone_account_access WHERE last_unit_id=? OR target_unit_id=?', (ident, ident)))
    return refs


def authorize(db, actor, body):
    auth.require(actor, 'device_manage')
    if not available(db):
        raise ValueError('设备流程尚未完成受控迁移，请由维护人员完成迁移后使用')
    if body.get('action') == 'place_card':
        auth.require(actor, 'card_manage')
    if body.get('action') == 'reassign_unit':
        auth.require(actor, 'device_create')
        # The new number does not exist yet; it inherits all source offices.
        refs_body = {key: value for key, value in body.items() if key != 'device_id'}
    else:
        refs_body = body
    if body.get('pause_nurture') is True:
        auth.require(actor, 'nurture_manage')
    refs = _refs(db, refs_body)
    if getattr(db, 'scope_enabled', False):
        import office_scope
        for kind in ('devices', 'accounts', 'cards'):
            for ident in refs[kind]:
                office_scope.require_entity(db, actor, kind, ident, write=True)
    return refs


def version_keys(db, body):
    refs = _refs(db, {key: value for key, value in body.items() if key != 'device_id'} if body.get('action') == 'reassign_unit' else body)
    keys = {'device:' + value for value in refs['devices']}
    if body.get('action') == 'reassign_unit' and body.get('device_id') and not getattr(db,'scope_enabled',False):
        keys.add('device:' + str(body['device_id']))
    keys.update('unit:' + value for value in refs['units'])
    keys.update('case:' + value for value in refs['cases'])
    for ident in refs['accounts']:
        keys.update(('account:' + ident, 'access:' + ident))
        if body.get('pause_nurture') is True:
            keys.add('nurture:' + ident)
    for ident in refs['cards']:
        keys.update(('card:' + str(ident), 'placement:' + str(ident)))
    return sorted(keys)


def legacy_version_keys(db, path, body):
    """Legacy editors must also protect the physical identity they may update."""
    if not available(db):return []
    units, devices, keys = set(), set(), set()
    if path == '/api/phone-device':devices.add(str(body.get('id', '')))
    if path == '/api/batch-edit' and body.get('kind') == 'devices' and 'state' in (body.get('patch') or {}):
        devices.update(str(value) for value in body.get('ids', []))
    if path in ('/api/account', '/api/account/create', '/api/account/phone-device') and 'phone_device_id' in body:
        ident = str(body.get('id', ''))
        old = db.execute('SELECT phone_device_id FROM accounts WHERE id=?', (ident,)).fetchone()
        if (old[0] if old else None) != (body['phone_device_id'] or None):
            if ident:
                keys.add('access:' + ident)
                access = db.execute('SELECT last_unit_id,target_unit_id FROM phone_account_access WHERE account_id=?', (ident,)).fetchone()
                if access:units.update(access[index] for index in (0, 1) if access[index])
            devices.update(value for value in (old[0] if old else None, body['phone_device_id']) if value)
    for ident in devices:
        units.update(row[0] for row in db.execute('SELECT id FROM phone_units WHERE device_id=? AND active=1', (ident,)))
    keys.update('unit:' + ident for ident in units)
    return sorted(keys)


def sync_assignment(db, account_id, device_id):
    """An explicit legacy assignment is a record edit, never proof of login."""
    if not available(db):
        return
    unit = ensure_unit(db, device_id) if device_id else None
    access = _access(db, str(account_id))
    if unit and access['last_unit_id'] == unit['id']:
        return
    if access['login_state'] == '登录受阻':
        raise ValueError('账号登录受阻，请通过恢复记录处理，不能改写最后登录实物')
    if access['case_id']:
        case = _row(db, 'device_cases', access['case_id'], '原处理记录不存在')
        if case['status'] != '已结案':
            raise ValueError('账号故障处理尚未结案，请通过恢复记录处理设备变更')
    db.execute("""UPDATE phone_account_access SET last_unit_id=?,target_unit_id=NULL,login_state='待确认',
        recovery_state='待处理',reason='',owner='',followup_on='',case_id=NULL,confirmed_at=0,
        revision=revision+1 WHERE account_id=?""",
        (unit['id'] if unit else None, str(account_id)))
    _touch(db, access['last_unit_id'], unit['id'] if unit else None)


def enrich_scope_maps(db, maps):
    """Add reference-only scope metadata; no names, notes or other business text."""
    maps['workflow_units'] = {row['id']: dict(device_id=row['device_id'], original_device_id=row['original_device_id'])
                              for row in db.execute('SELECT id,device_id,original_device_id FROM phone_units')}
    cases = {row['id']: dict(unit_id=row['unit_id'], account_ids=json.loads(row['account_ids']))
             for row in db.execute('SELECT id,unit_id,account_ids FROM device_cases')}
    maps['workflow_cases'] = cases
    return maps


def _record_references(maps, table, row):
    refs = []
    def unit(ident):
        if ident:
            value = maps.get('workflow_units', {}).get(ident)
            if value:
                refs.extend(('devices', value[field]) for field in ('device_id', 'original_device_id'))
            else:
                refs.append(('devices', ''))
    def case(ident):
        if not ident:
            return
        value = maps.get('workflow_cases', {}).get(ident)
        if not value:
            refs.append(('devices', ''))
            return
        unit(value['unit_id'])
        refs.extend(('accounts', ident) for ident in value['account_ids'])
    if table == 'phone_units':
        refs.append(('devices', row['device_id']))
        refs.append(('devices', row['original_device_id']))
    elif table == 'phone_card_placements':
        refs.append(('cards', row['card_id']))
        unit(row.get('unit_id'))
    elif table == 'phone_account_access':
        refs.append(('accounts', row['account_id']))
        unit(row.get('last_unit_id'))
        unit(row.get('target_unit_id'))
        case(row.get('case_id'))
    elif table == 'device_cases':
        unit(row['unit_id'])
        case(row['id'])
    elif table == 'device_workflow_events':
        unit(row.get('unit_id'))
        case(row.get('case_id'))
        if row.get('card_id'):
            refs.append(('cards', row['card_id']))
        if row.get('account_id'):
            refs.append(('accounts', row['account_id']))
        try:
            detail = json.loads(row['detail'])
        except (ValueError, TypeError):
            return [('devices', '')]
        refs.extend(('accounts', ident) for ident in detail.get('affected_account_ids', []))
        for key in ('old_unit_id', 'new_unit_id', 'replaces_unit_id', 'from_unit_id', 'target_unit_id'):
            unit(detail.get(key))
        for key in ('old_device_id', 'device_id', 'original_device_id'):
            if detail.get(key):
                refs.append(('devices', detail[key]))
        for key in ('before', 'after'):
            if isinstance(detail.get(key), dict):
                unit(detail[key].get('unit_id'))
    return refs


def record_offices(maps, table, row):
    offices = set()
    for kind, ident in _record_references(maps, table, row):
        values = maps[kind].get(ident, [])
        if not values:
            return []
        offices.update(values)
    return sorted(offices)


def record_visible(context, table, row):
    if context['global_access']:
        return True
    refs = _record_references(context['maps'], table, row)
    return bool(refs) and all(ident in context['ids'][kind] for kind, ident in refs)


def _open_case(db, actor, body, today):
    unit = _unit(db, body)
    reason = _text(body, 'reason', required=True)
    if db.execute("SELECT 1 FROM device_cases WHERE unit_id=? AND status<>'已结案'", (unit['id'],)).fetchone():
        raise ValueError('该实物已有进行中的处理记录，请继续原记录')
    affected = list(dict.fromkeys(body.get('affected_account_ids', [])))
    accesses = []
    for ident in affected:
        access = _access(db, ident)
        if access['last_unit_id'] != unit['id']:
            raise ValueError('所选账号不关联这台实物手机，请先核对')
        if access['case_id']:
            old_case = _row(db, 'device_cases', access['case_id'], '原处理记录不存在')
            if old_case['status'] != '已结案':
                raise ValueError('所选账号已有进行中的处理记录')
        accesses.append(access)
    ident = 'case-' + uuid.uuid4().hex
    now = int(time.time())
    owner = _text(body, 'owner', actor['name'], 100)
    followup = _followup(body)
    db.execute('''INSERT INTO device_cases(id,unit_id,reason,owner,followup_on,note,created_at,updated_at,account_ids)
        VALUES(?,?,?,?,?,?,?,?,?)''', (ident, unit['id'], reason, owner, followup, _text(body, 'note'), now, now, json.dumps(affected)))
    db.execute("UPDATE phone_units SET status='故障',revision=revision+1 WHERE id=?", (unit['id'],))
    if unit['active']:
        db.execute("UPDATE phone_devices SET state='故障',state_source='manual' WHERE id=?", (unit['device_id'],))
    for access in accesses:
        db.execute("""UPDATE phone_account_access SET login_state='登录受阻',recovery_state='待处理',
            target_unit_id=NULL,reason=?,owner=?,followup_on=?,case_id=?,revision=revision+1 WHERE account_id=?""",
            (reason, owner, followup, ident, access['account_id']))
        if body.get('pause_nurture') is True:
            record = db.execute('SELECT * FROM nurture_records WHERE account_id=?', (access['account_id'],)).fetchone()
            if record and record['workflow'] == '运行':
                import nurture
                nurture.pause(db, record, today)
        _event(db, actor, '登记账号登录受阻', {'reason': reason}, unit_id=unit['id'], case_id=ident, account_id=access['account_id'])
    _event(db, actor, '登记设备故障', {'reason': reason, 'affected_account_ids': affected}, unit_id=unit['id'], case_id=ident)
    return dict(ok=True, case_id=ident, unit_id=unit['id'])


def _update_case(db, actor, body):
    case = _row(db, 'device_cases', _text(body, 'case_id', required=True), '处理记录不存在')
    status = _text(body, 'status', case['status'], 30)
    if status not in CASE_STATUSES:
        raise ValueError('处理进度不正确')
    if case['status'] == '已结案':
        raise ValueError('已结案记录不可改写，请新建后续处理记录')
    followup = _followup(body, case['followup_on'])
    closure = _text(body, 'closure_reason', case['closure_reason'])
    if status == '暂挂' and not followup:
        raise ValueError('暂挂必须填写下次跟进日期')
    if status == '已结案':
        if not closure:
            raise ValueError('结案必须填写处理结果及未恢复账号的说明')
        affected = json.loads(case['account_ids'])
        pending = {row['account_id']: row for row in db.execute('SELECT account_id,recovery_state FROM phone_account_access WHERE case_id=?', (case['id'],))}
        if any(ident not in pending or pending[ident]['recovery_state'] not in RESOLVED_RECOVERY for ident in affected):
            raise ValueError('仍有账号未明确处理结果，请逐个恢复或明确暂不恢复/无法恢复')
    now = int(time.time())
    db.execute('''UPDATE device_cases SET status=?,reason=?,owner=?,followup_on=?,note=?,closure_reason=?,
        updated_at=?,closed_at=?,revision=revision+1 WHERE id=?''',
        (status, _text(body, 'reason', case['reason'], required=True), _text(body, 'owner', case['owner'], 100),
         followup, _text(body, 'note', case['note']), closure, now, now if status == '已结案' else 0, case['id']))
    _touch(db, case['unit_id'])
    _event(db, actor, '更新故障处理', {'before_status': case['status'], 'status': status, 'closure_reason': closure}, unit_id=case['unit_id'], case_id=case['id'])
    return dict(ok=True, case_id=case['id'])


def _unit_status(db, actor, body):
    unit = _unit(db, body)
    status = _text(body, 'status', limit=30, required=True)
    if status not in UNIT_STATUSES:
        raise ValueError('实物手机状态不正确')
    if not unit['active'] and status in USABLE_UNITS:
        raise ValueError('旧机已被新机补号，请保留返修待验并安排其他空号，不能恢复占用原编号')
    if status == '可用闲置' and (db.execute('SELECT 1 FROM phone_card_placements WHERE unit_id=?', (unit['id'],)).fetchone()
            or db.execute('SELECT 1 FROM phone_account_access WHERE last_unit_id=?', (unit['id'],)).fetchone()):
        raise ValueError('这台实物仍有卡或账号记录，请先核对并移出后再标记空闲')
    db.execute('UPDATE phone_units SET status=?,note=?,revision=revision+1 WHERE id=?',
               (status, _text(body, 'note', unit['note']), unit['id']))
    if unit['active']:
        legacy = status if status in {'待确认', '在用', '待清理', '可用闲置', '故障'} else '故障'
        db.execute("UPDATE phone_devices SET state=?,state_source='manual' WHERE id=?", (legacy, unit['device_id']))
    _event(db, actor, '更新实物状态', {'before_status': unit['status'], 'status': status, 'note': _text(body, 'note', unit['note'])}, unit_id=unit['id'])
    return dict(ok=True, unit_id=unit['id'])


def _replace_unit(db, actor, body):
    old = ensure_unit(db, _text(body, 'device_id', limit=80, required=True))
    if old['status'] not in REPLACEABLE_UNITS:
        raise ValueError('现用手机须先明确登记故障、送修、返修待验或回收，才能补号')
    status = _text(body, 'new_status', '待确认', 30)
    if status not in {'待确认', '可用闲置'}:
        raise ValueError('新机初始状态请选择待确认或可用闲置')
    ident = 'unit-' + uuid.uuid4().hex
    generation = db.execute('SELECT MAX(generation) FROM phone_units WHERE device_id=?', (old['device_id'],)).fetchone()[0] + 1
    db.execute('UPDATE phone_units SET active=0,revision=revision+1 WHERE id=?', (old['id'],))
    db.execute('INSERT INTO phone_units(id,device_id,original_device_id,generation,status,model,note,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (ident, old['device_id'], old['device_id'], generation, status, _text(body, 'model', limit=100), _text(body, 'note'), int(time.time())))
    db.execute("UPDATE phone_devices SET state=?,state_source='manual' WHERE id=?", (status, old['device_id']))
    _event(db, actor, '新机补号', {'old_unit_id': old['id'], 'new_unit_id': ident, 'generation': generation,
                              'cards_and_logins_inherited': False}, unit_id=old['id'])
    _event(db, actor, '新机登记', {'replaces_unit_id': old['id'], 'generation': generation}, unit_id=ident)
    return dict(ok=True, unit_id=ident, old_unit_id=old['id'])


def _place_card(db, actor, body):
    card_id = body.get('card_id')
    _row(db, 'phone_cards', card_id, '手机卡不存在')
    old = db.execute('SELECT * FROM phone_card_placements WHERE card_id=?', (card_id,)).fetchone()
    unit = _row(db, 'phone_units', str(body['unit_id']), '目标实物不存在') if body.get('unit_id') else None
    existing = body.get('confirm_existing') is True
    if unit:
        slot = str(body.get('slot', ''))
        if slot not in (('1', '2', '待确认') if existing else ('1', '2')):
            raise ValueError('请选择卡槽1或卡槽2')
        if not existing and (not unit['active'] or unit['status'] not in USABLE_UNITS):
            raise ValueError('目标手机必须是已确认在用或可用闲置的现用实物')
        if not existing and body.get('confirmed_usable') is not True:
            raise ValueError('请确认目标手机实际可用且手机卡已插入所选卡槽')
        if slot in ('1', '2') and db.execute('SELECT 1 FROM phone_card_placements WHERE unit_id=? AND slot=? AND card_id<>?', (unit['id'], slot, card_id)).fetchone():
            raise ValueError('目标卡槽已有其他手机卡，请先核对位置')
        if not existing:
            db.execute("UPDATE phone_units SET status='在用' WHERE id=?", (unit['id'],))
            db.execute("UPDATE phone_devices SET state='在用',state_source='manual' WHERE id=?", (unit['device_id'],))
        storage = ''
    else:
        slot = ''
        storage = _text(body, 'storage', limit=200, required=True)
    now = int(time.time())
    db.execute('''INSERT INTO phone_card_placements(card_id,unit_id,slot,storage,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET unit_id=excluded.unit_id,slot=excluded.slot,
        storage=excluded.storage,updated_at=excluded.updated_at,revision=phone_card_placements.revision+1''',
        (card_id, unit['id'] if unit else None, slot, storage, now))
    _touch(db, old['unit_id'] if old else None, unit['id'] if unit else None)
    _event(db, actor, '核对手机卡位置' if existing else '移动手机卡', {'before': dict(old) if old else None,
                'after': dict(unit_id=unit['id'] if unit else None, slot=slot, storage=storage)},
           unit_id=unit['id'] if unit else None, card_id=card_id)
    return dict(ok=True, card_id=card_id)


def _reassign_unit(db, actor, body):
    import phone_devices
    unit = _row(db, 'phone_units', _text(body, 'unit_id', required=True), '旧实物不存在')
    if unit['active']:
        raise ValueError('只可为被新机补号后的旧实物重新编号')
    if unit['status'] not in {'返修待验', '故障', '待确认'} or body.get('confirmed_usable') is not True:
        raise ValueError('请先核对旧机返修情况，并确认实际复检可用后重新编号')
    occupied = (db.execute('SELECT 1 FROM phone_card_placements WHERE unit_id=?', (unit['id'],)).fetchone()
                or db.execute('SELECT 1 FROM phone_account_access WHERE last_unit_id=?', (unit['id'],)).fetchone())
    state = '在用' if occupied else '可用闲置'
    if getattr(db, 'scope_enabled', False):
        import office_scope
        scopes = sorted(office_scope.entity_offices(db, 'devices', unit['device_id']))
        if len(scopes)!=1:
            raise ValueError('原实物的办公室归属待核对，请先明确唯一办公室再重新编号')
        # Only the number is supplied. The office is inherited from an explicit
        # source mapping and the stable internal identity is generated here.
        if 'number' not in body:raise ValueError('返修重新编号请填写手机序号')
        payload=dict(number=body['number'],office_ids=scopes,state=state,name=_text(body,'name',limit=100))
    else:
        device_id=phone_devices.device_identity(body.get('device_id',''))
        payload=dict(id=device_id,state=state,name=_text(body,'name',limit=100))
    # Reuse create validation without manufacturing an additional physical
    # phone: the same old unit will become the new number's current identity.
    device_id=phone_devices.save(db,payload,create=True,actor=actor,initialize_unit=False)
    db.execute('UPDATE phone_units SET device_id=?,generation=1,active=1,status=?,revision=revision+1 WHERE id=?',
               (device_id, state, unit['id']))
    # Accounts still refer to this physical identity, so their legacy number follows it.
    db.execute("UPDATE accounts SET phone_device_id=?,phone_device_source='manual' WHERE id IN (SELECT account_id FROM phone_account_access WHERE last_unit_id=?)", (device_id, unit['id']))
    _event(db, actor, '返修旧机重新编号', {'old_device_id': unit['device_id'], 'device_id': device_id,
            'original_device_id': unit['original_device_id'], 'confirmed_usable': True}, unit_id=unit['id'])
    return dict(ok=True, unit_id=unit['id'], device_id=device_id)


def _account_recovery(db, actor, body):
    account_id = _text(body, 'account_id', limit=100, required=True)
    access = _access(db, account_id)
    state = _text(body, 'recovery_state', access['recovery_state'], 30)
    if state not in RECOVERY_STATES:
        raise ValueError('账号恢复进度不正确')
    case_id = body.get('case_id', access['case_id']) or None
    if access['case_id'] and case_id != access['case_id']:
        previous = _row(db, 'device_cases', access['case_id'], '原处理记录不存在')
        if previous['status'] != '已结案':
            raise ValueError('当前故障处理尚未结案，不能移除或改换处理记录')
    if case_id:
        case = _row(db, 'device_cases', str(case_id), '故障处理记录不存在')
        if case['status'] == '已结案':
            raise ValueError('该故障记录已经结案，请建立新的处理记录')
        if access['case_id'] != case_id and case['unit_id'] != access['last_unit_id']:
            raise ValueError('账号与该故障实物不关联')
    target_id = body.get('target_unit_id', access['target_unit_id']) or None
    target = _row(db, 'phone_units', str(target_id), '目标实物不存在') if target_id else None
    reason = _text(body, 'reason', access['reason'])
    if state in {'等待协助', '暂不恢复', '无法恢复'} and not reason:
        raise ValueError('请填写账号受阻原因或处理结果')
    last_id, login_state, confirmed_at = access['last_unit_id'], '登录受阻', access['confirmed_at']
    if state == '恢复成功':
        if body.get('confirmed_login') is not True:
            raise ValueError('只有实际登录成功后才能确认恢复')
        if not target or not target['active'] or target['status'] not in USABLE_UNITS:
            raise ValueError('恢复成功必须选择已确认可用的现用实物手机')
        last_id, login_state, confirmed_at = target['id'], '可操作', int(time.time())
        db.execute("UPDATE accounts SET phone_device_id=?,phone_device_source='manual' WHERE id=?", (target['device_id'], account_id))
        db.execute("UPDATE phone_units SET status='在用',revision=revision+1 WHERE id=?", (target['id'],))
        db.execute("UPDATE phone_devices SET state='在用',state_source='manual' WHERE id=?", (target['device_id'],))
    followup = _followup(body, access['followup_on'])
    db.execute('''UPDATE phone_account_access SET last_unit_id=?,target_unit_id=?,login_state=?,recovery_state=?,
        reason=?,owner=?,followup_on=?,case_id=?,confirmed_at=?,revision=revision+1 WHERE account_id=?''',
        (last_id, target_id, login_state, state, reason, _text(body, 'owner', access['owner'], 100),
         followup, case_id, confirmed_at, account_id))
    _touch(db, access['last_unit_id'], access['target_unit_id'], last_id, target_id)
    _event(db, actor, '更新账号恢复', {'before_state': access['recovery_state'], 'recovery_state': state,
                'from_unit_id': access['last_unit_id'], 'target_unit_id': target_id, 'reason': reason,
                'confirmed_login': state == '恢复成功'}, unit_id=last_id, case_id=case_id, account_id=account_id)
    return dict(ok=True, account_id=account_id)


def post(db, actor, body, today):
    authorize(db, actor, body)
    action = body.get('action')
    if action == 'open_case':
        return _open_case(db, actor, body, today)
    if action == 'update_case':
        return _update_case(db, actor, body)
    if action == 'unit_status':
        return _unit_status(db, actor, body)
    if action == 'replace_unit':
        return _replace_unit(db, actor, body)
    if action == 'place_card':
        return _place_card(db, actor, body)
    if action == 'account_recovery':
        return _account_recovery(db, actor, body)
    if action == 'reassign_unit':
        return _reassign_unit(db, actor, body)
    raise ValueError('设备流程操作不存在')


def overview(db, accounts, devices, cards, actor=None):
    if not available(db):
        return dict(enabled=False, reason='设备流程尚未完成受控迁移', units=[], placements=[], access=[], cases=[], events=[], todos=[])
    account_ids = {row['id'] for row in accounts}
    device_ids = {row['id'] for row in devices}
    card_ids = {row['id'] for row in cards}
    by_device = {row['id']: row for row in devices}
    units = [dict(row) for row in db.execute('SELECT * FROM phone_units ORDER BY device_id,generation')
             if row['device_id'] in device_ids and row['original_device_id'] in device_ids]
    by_unit = {row['id']: row for row in units}
    for unit in units:
        unit['device_name'] = by_device[unit['device_id']]['name']
        unit['label'] = unit['device_name'] + ' · 第' + str(unit['generation']) + '台' + ('（旧机）' if not unit['active'] else '')
        if unit['original_device_id'] != unit['device_id']:
            import phone_devices
            unit['label'] += '（原' + (phone_devices.number_from_id(unit['original_device_id']) or '待核对') + '号）'
    placements = []
    for row in db.execute('SELECT * FROM phone_card_placements ORDER BY card_id'):
        if row['card_id'] not in card_ids:
            continue
        item = dict(row)
        if item['unit_id'] and item['unit_id'] not in by_unit:
            continue
        item['unit_label'] = by_unit[item['unit_id']]['label'] if item['unit_id'] else ''
        placements.append(item)
    access = []
    for row in db.execute('SELECT * FROM phone_account_access ORDER BY account_id'):
        if row['account_id'] not in account_ids:
            continue
        item = dict(row)
        if any(item[field] and item[field] not in by_unit for field in ('last_unit_id', 'target_unit_id')):
            # Retain account availability, redact inaccessible related identities.
            for field in ('last_unit_id', 'target_unit_id'):
                if item[field] not in by_unit:
                    item[field] = None
            item.update(reason='', owner='', followup_on='', case_id=None)
        item['last_unit_label'] = by_unit[item['last_unit_id']]['label'] if item['last_unit_id'] else ''
        item['target_unit_label'] = by_unit[item['target_unit_id']]['label'] if item['target_unit_id'] else ''
        access.append(item)
    cases = []
    for row in db.execute('SELECT * FROM device_cases ORDER BY updated_at DESC,id'):
        linked = json.loads(row['account_ids'])
        if row['unit_id'] not in by_unit or any(ident not in account_ids for ident in linked):
            continue
        item = dict(row)
        item.update(unit_label=by_unit[item['unit_id']]['label'], account_ids=linked)
        cases.append(item)
    case_ids = {row['id'] for row in cases}
    events = []
    # Filter before limiting, so another office's busy history cannot hide this office's events.
    for row in db.execute('SELECT * FROM device_workflow_events ORDER BY at DESC,id DESC'):
        if ((row['unit_id'] and row['unit_id'] not in by_unit) or (row['card_id'] and row['card_id'] not in card_ids)
                or (row['account_id'] and row['account_id'] not in account_ids) or (row['case_id'] and row['case_id'] not in case_ids)):
            continue
        item = dict(row)
        item['details'] = json.loads(item['detail'])
        detail_accounts = item['details'].get('affected_account_ids', [])
        if any(ident not in account_ids for ident in detail_accounts):
            continue
        # Event details can contain old/new related phone identities.
        referenced = [item['details'].get(field) for field in ('old_unit_id', 'new_unit_id', 'replaces_unit_id', 'from_unit_id', 'target_unit_id')]
        referenced += [item['details'].get(field, {}).get('unit_id') for field in ('before', 'after') if isinstance(item['details'].get(field), dict)]
        if any(ident and ident not in by_unit for ident in referenced):
            continue
        if any(item['details'].get(field) and item['details'][field] not in device_ids
               for field in ('old_device_id', 'device_id', 'original_device_id')):
            continue
        events.append(item)
        if len(events) >= 200:
            break
    for item in access:
        if item['case_id'] and item['case_id'] not in case_ids:
            item.update(case_id=None, reason='', owner='', followup_on='')
    access_map = {row['account_id']: row for row in access}
    placement_map = {row['card_id']: row for row in placements}
    for account in accounts:
        account['device_access'] = access_map.get(account['id'])
        if account['device_access']:
            account['phone_device_name'] = account['device_access']['last_unit_label']
    for card in cards:
        card['placement'] = placement_map.get(card['id'])
    current_by_device,history_by_device,accounts_by_unit = {},{},{}
    for unit in units:
        if unit['active']:current_by_device.setdefault(unit['device_id'],unit)
        history_by_device.setdefault(unit['device_id'],[]).append(unit)
        if unit['original_device_id'] != unit['device_id']:
            history_by_device.setdefault(unit['original_device_id'],[]).append(unit)
    for account in accounts:
        last_unit_id=(account.get('device_access') or {}).get('last_unit_id')
        accounts_by_unit.setdefault(last_unit_id,[]).append(account)
    for device in devices:
        device['current_unit'] = current_by_device.get(device['id'])
        device['unit_history'] = history_by_device.get(device['id'],[])
        current = device['current_unit']
        own = accounts_by_unit.get(current['id'] if current else '',[])
        inactive = sum(row['status'] in {'封禁', '停用'} for row in own)
        device.update(account_ids=[row['id'] for row in own], account_count=len(own), inactive_count=inactive,
                      occupancy='无当前账号' if not own else '仅封禁或停用' if inactive == len(own) else '有在用或待确认账号')
    todos = [dict(kind='device_case', id=row['id'], unit_id=row['unit_id'], title=row['unit_label'] + ' · ' + row['status'],
                  owner=row['owner'], followup_on=row['followup_on'], reason=row['reason']) for row in cases if row['status'] != '已结案']
    todos += [dict(kind='account_recovery', id=row['account_id'], account_id=row['account_id'], case_id=row['case_id'],
                   title='账号登录待恢复', owner=row['owner'], followup_on=row['followup_on'], reason=row['reason'])
              for row in access if row['login_state'] == '登录受阻' and row['recovery_state'] not in RESOLVED_RECOVERY]
    return dict(enabled=True, units=units, placements=placements, access=access, cases=cases, events=events, todos=todos,
                options=dict(unit_statuses=UNIT_STATUSES, case_statuses=CASE_STATUSES,
                             recovery_states=RECOVERY_STATES, login_states=LOGIN_STATES, slots=['1', '2']))
