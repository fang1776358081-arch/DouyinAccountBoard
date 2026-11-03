"""Employee real-name card ledger. Legacy owner/keeper strings are never identities.

Read independently of /api/board; callers own authenticated read/write transactions.
The module never writes phone_cards, auth_users or office grants.
"""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import time
import uuid

import auth
import idempotency
import office_scope
import operations
import employee_card_ledger

TABLES = ('employee_card_employees', 'employee_card_operators', 'employee_card_rules',
          'employee_card_applications', 'employee_card_units', 'employee_card_links',
          'employee_card_verifications', 'employee_card_events', 'employee_card_requests')
SOURCE = '用户提供的当前业务规则（2026-11-02），未独立核验运营商正式条款'
EFFECTIVE = '2026-11-02'
OPERATOR_NAMES = (('minsheng', '民生'), ('zhongxing', '中兴'), ('changcheng', '长城'),
                  ('liantong', '联通'), ('dianxin', '电信'), ('yidong', '移动'))
ACTIONS = {'employee', 'operator', 'rules', 'verification', 'application', 'card_link', 'card_link_batch',
           'replacement', 'cancel_request', 'release'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _id():
    return str(uuid.uuid4())


def _generation(db):
    generation = getattr(db, 'restore_generation', None)
    if generation is None and getattr(db, 'dialect', 'sqlite') == 'sqlite':
        return 'sqlite-local'
    if not isinstance(generation, str) or not generation or len(generation) > 200:
        raise ValueError('恢复代次未配置，员工办卡写入与请求回执不可用')
    return generation


def _require_generation(db, generation):
    if generation != _generation(db):
        raise idempotency.IdempotencyError('RESTORE_GENERATION_EXPIRED', '该预览或请求属于已关闭的恢复代次，请重新读取核对，不能再次执行旧请求')


def _text(value, label, *, maximum=500, required=False):
    if not isinstance(value, str):
        raise ValueError(label + '格式不正确')
    value = value.strip()
    if len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(label + '过长或包含无效字符')
    if required and not value:
        raise ValueError('请填写' + label)
    return value


def _day(value, label, *, optional=False, latest=None):
    value = _text(value, label, maximum=10, required=not optional)
    if not value:
        return ''
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(label + '不正确') from None
    if parsed.isoformat() != value or latest and value > latest:
        raise ValueError(label + '不正确或晚于允许日期')
    return value


def _number(value, label, *, minimum=0, maximum=100000, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(label + '需为范围内的整数')
    return value


def _choice(value, allowed, label):
    if value not in allowed:
        raise ValueError(label + '不正确')
    return value


def _fields(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError('存在未开放的员工办卡字段')


def _table(db, name):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        return db.execute('SELECT to_regclass(?)', (name,)).fetchone()[0] is not None
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def available(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        import pg_schema
        expected = next((item for item in pg_schema.expected_versions() if item[0] == 5), None)
        if expected is None:
            return False
        row = db.execute('SELECT version,sha256 FROM schema_migrations WHERE version=5').fetchone()
        if not row or (row[0], row[1]) != expected:
            return False
        row = db.execute('SELECT ' + ','.join('to_regclass(?)' for _ in TABLES), TABLES).fetchone()
        return bool(row) and all(row[i] is not None for i in range(len(TABLES)))
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return set(TABLES) <= names


def migrate(db):
    if getattr(db, 'dialect', 'sqlite') != 'sqlite':
        raise ValueError('员工办卡PostgreSQL结构只能通过受控迁移创建')
    ddl = Path(__file__).with_name('migrations').joinpath('postgresql/0005_employee_cards.sql').read_text(encoding='utf-8')
    db.executescript(ddl.replace('BIGINT', 'INTEGER'))
    seed(db)


def seed(db):
    """Configuration only; never create people, cards or ownership links."""
    for ident, name in OPERATOR_NAMES:
        operator = dict(id=ident, name=name, network='unknown', channel_id='',
                        age_confirmed=ident == 'minsheng', min_age=21 if ident == 'minsheng' else None,
                        source=SOURCE, effective_from=EFFECTIVE, verification_valid_days=None,
                        release_mode='unknown', release_delay_days=None,
                        waiting_confirmed=False, waiting_days=None, waiting_basis='',
                        occupied_phases=['active'])
        db.execute('INSERT INTO employee_card_operators(id,name,payload) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING',
                   (ident, name, _json(operator)))
    rules = [dict(id='global-stock', scope='all', scope_id='', period='stock', limit=10),
             dict(id='unicom-month', scope='network', scope_id='unicom', period='month', limit=3)]
    rules += [dict(id=ident + '-day', scope='operator', scope_id=ident, period='day', limit=2)
              for ident, _ in OPERATOR_NAMES]
    for rule in rules:
        rule.update(pending_counts=None, source=SOURCE, effective_from=EFFECTIVE, confirmed=True)
        db.execute('INSERT INTO employee_card_rules(id,payload) VALUES(?,?) ON CONFLICT(id) DO NOTHING',
                   (rule['id'], _json(rule)))


def _actor(db, actor):
    auth.require(actor)
    row = db.execute('SELECT * FROM auth_users WHERE id=? AND active=1', (actor['id'],)).fetchone()
    if not row:
        raise auth.AccessError('请先登录工作台', 401)
    return {**auth.public_user(row), 'csrf': actor.get('csrf', '')}


def _manager(actor):
    return actor['role'] == 'owner' or 'card_manage' in actor['permissions']


def _grants(db, actor):
    if getattr(db, 'scope_enabled', False):
        return office_scope._granted_offices(db, actor)
    if _table(db, 'user_office_grants'):
        return {r[0] for r in db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?', (actor['id'],))}
    return set()


def _visible(db, actor, employee, grants=None):
    if _manager(actor):
        return office_scope.global_scope(actor) or bool(employee['office_id'] and employee['office_id'] in (_grants(db, actor) if grants is None else grants))
    return employee['auth_user_id'] is not None and employee['auth_user_id'] == actor['id']


def _require_employee(db, actor, ident):
    row = db.execute('SELECT * FROM employee_card_employees WHERE id=?', (ident,)).fetchone()
    if not row or not _visible(db, actor, row):
        raise auth.AccessError('无权访问此员工资料', 403)
    return dict(row)


def _offices(db, actor):
    if not _table(db, 'offices'):
        return []
    if getattr(db, 'scope_enabled', False):
        return office_scope.list_offices(db, actor)
    return [dict(r) for r in db.execute('SELECT id,name FROM offices WHERE active=1 ORDER BY id')
            if office_scope.global_scope(actor) or r['id'] in _grants(db, actor)]


def _allowed_cards(db, actor):
    if getattr(db, 'scope_enabled', False):
        return office_scope._visible_ids(db, actor, 'cards')
    if not office_scope.global_scope(actor):
        return set()
    return {r[0] for r in db.execute('SELECT id FROM phone_cards')}


def _bundle(db, actor):
    grants = _grants(db, actor)
    if _manager(actor) and office_scope.global_scope(actor):
        employees = [dict(r) for r in db.execute('SELECT * FROM employee_card_employees ORDER BY employee_no,id')]
    elif _manager(actor):
        employees = ([dict(r) for r in db.execute('SELECT * FROM employee_card_employees WHERE office_id IN (' + ','.join('?' for _ in grants) + ') ORDER BY employee_no,id', tuple(sorted(grants)))] if grants else [])
    else:
        employees = [dict(r) for r in db.execute('SELECT * FROM employee_card_employees WHERE auth_user_id=? ORDER BY employee_no,id', (actor['id'],))]
    ids = {r['id'] for r in employees}
    operators = [dict(json.loads(r['payload']), revision=r['revision']) for r in db.execute('SELECT * FROM employee_card_operators ORDER BY id')]
    rules = [dict(json.loads(r['payload']), revision=r['revision']) for r in db.execute('SELECT * FROM employee_card_rules ORDER BY id')]
    def rows(table, order):
        if not ids:
            return []
        return db.execute('SELECT * FROM ' + table + ' WHERE employee_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY ' + order, tuple(sorted(ids)))
    # Scope predicates execute in the database; no legacy account/card payloads.
    units = [dict(r) for r in rows('employee_card_units', 'id')]
    for unit in units:
        for field in ('release_policy', 'network_policy'):
            if unit[field]:
                unit[field] = json.loads(unit[field])
            else:
                unit.pop(field)
    applications = [dict(json.loads(r['payload']), id=r['id'], employee_id=r['employee_id'],
                         operator_id=r['operator_id'], applied_on=r['applied_on'], result=r['result'],
                         quantity=r['quantity'], revision=r['revision'])
                    for r in rows('employee_card_applications', 'id')]
    verifications = [dict(json.loads(r['payload']), id=r['id'], employee_id=r['employee_id'],
                          revision=r['revision'], created_at=r['created_at'])
                     for r in rows('employee_card_verifications', 'created_at,id')]
    links = ([dict(r) for r in db.execute('SELECT l.*,u.employee_id FROM employee_card_links l JOIN employee_card_units u ON u.id=l.unit_id WHERE u.employee_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY l.id', tuple(sorted(ids)))] if ids else [])
    return dict(employees=employees, operators=operators, rules=rules, units=units,
                applications=applications, verifications=verifications, links=links)


def _version(db, actor, ident='', *, bundle=None, scope=None):
    records = dict(_bundle(db, actor) if bundle is None else bundle)
    if ident:
        for key in ('employees', 'units', 'applications', 'verifications', 'links'):
            records[key] = [row for row in records[key] if row.get('employee_id', row.get('id')) == ident]
    if scope is None:
        scope = dict(actor_id=actor['id'], role=actor['role'], permissions=sorted(actor['permissions']),
                     offices=sorted(_grants(db, actor)))
    scope = dict(scope, restore_generation=_generation(db))
    return hashlib.sha256(_json(dict(scope=scope, records=records)).encode()).hexdigest()


def _scope_incomplete(ident, bundle, allowed_cards):
    if allowed_cards is None:
        return False
    return (any(u['employee_id'] == ident and u['current_card_id'] is not None and u['current_card_id'] not in allowed_cards for u in bundle['units'])
            or any(link['employee_id'] == ident and link['card_id'] not in allowed_cards for link in bundle['links']))


def _summary(employee, bundle, today, allowed_cards=None):
    from employee_card_rules import evaluate_employee
    ident = employee['id']
    units = [dict(row) for row in bundle['units'] if row['employee_id'] == ident]
    operators = {row['id']: row for row in bundle['operators']}
    for unit in units:
        op = operators[unit['operator_id']]
        policy = unit.get('network_policy')
        if policy is None and unit['phase'] != 'pending':
            policy = _unknown_network(unit['operator_id'], today)
            unit['network_policy'] = policy
        unit.update(network=policy.get('network', 'unknown') if policy else op.get('network', 'unknown'),
                    channel_id=policy.get('channel_id', '') if policy else op.get('channel_id', ''))
    profile = dict(employee, consent_confirmed=bool(employee['consent_source'] and employee['consent_on']))
    result = evaluate_employee(profile, bundle['operators'], units,
                               [r for r in bundle['verifications'] if r['employee_id'] == ident],
                               [r for r in bundle['applications'] if r['employee_id'] == ident],
                               today, rules=bundle['rules'])
    if _scope_incomplete(ident, bundle, allowed_cards):
        result = dict(result, scope_incomplete=True, issues=['部分关联卡超出当前办公室范围，名额判断待核验'],
                      next_followup_on=None, followup_action='verify')
        basis = dict(company='scope_incomplete', company_inventory_complete=None, verified_total='unknown',
                     other_current='unknown', verification_id=None,
                     notes=['当前范围不完整，已隐藏关联卡统计及其核验依据'])
        result.update(company_inventory_complete=None, count_basis=dict(basis))
        for key in ('company_lifetime', 'company_current', 'other_current', 'verified_total', 'pending_count', 'estimated_available'):
            result[key] = None
        result['counts'] = {key: None for key in result.get('counts', {})}
        for op in result.get('operators', []):
            op.update(status='needs_verification', status_label='待核验', estimated_available=None,
                      counts={key: None for key in op['counts']}, limits=[],
                      verification_on=None, next_followup_on=None, followup_action='verify',
                      company_inventory_complete=None, count_basis=dict(basis),
                      reasons=[dict(code='scope_incomplete', text='关联卡范围不完整，不能确定可申请数量', kind='unknown')])
            op.pop('count_evidence', None)
    return result


def _public_employee(row, *, birth=False):
    public = dict(row)
    if not birth:
        public.pop('birth_date', None)
        public.pop('consent_source', None)
        public.pop('consent_on', None)
    return public


def _query(query, key, default=''):
    value = query.get(key, [default])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        raise ValueError('查询条件不正确')
    return value[0]


def get(db, actor, query, today):
    actor = _actor(db, actor)
    _day(today, '当前日期')
    meta = dict(available=available(db), today=today, user={k: actor[k] for k in ('id', 'name', 'role', 'csrf')},
                can_manage=_manager(actor), can_configure_rules=actor['role'] == 'owner',
                can_view_birth_date=_manager(actor), operators=[], employees=[], summaries=[],
                total=0, page=1, page_size=50, offices=[] , staff_candidates=[],
                card_ledger_filter_incomplete=False,
                card_ledger_totals=dict({key: None for key in employee_card_ledger.COUNTS},
                                        scope_complete=False, scope_label='员工卡台账尚未读取'))
    if not meta['available']:
        return dict(meta, message='员工办卡结构升级尚未完成，原手机卡功能可继续使用')
    if 'request_id' in query:
        auth.require(actor, 'card_manage')
        request_id = idempotency.request_uuid(_query(query, 'request_id'))
        row = db.execute('SELECT * FROM employee_card_requests WHERE actor_id=? AND request_id=?', (actor['id'], request_id)).fetchone()
        if not row:
            raise auth.AccessError('请求未完成或不存在，请使用原请求核对', 404)
        _require_generation(db, row['restore_generation'])
        _authorize(db, actor, row['action'], row['employee_id'])
        return dict(request_found=True, result=json.loads(row['result']))
    bundle = _bundle(db, actor)
    cards_allowed = None if office_scope.global_scope(actor) else _allowed_cards(db, actor)
    scope = dict(actor_id=actor['id'], role=actor['role'], permissions=sorted(actor['permissions']), offices=sorted(_grants(db, actor)))
    meta.update(operators=bundle['operators'], rules=bundle['rules'], offices=_offices(db, actor), version=_version(db, actor, bundle=bundle, scope=scope))
    if actor['role'] == 'owner':
        meta['staff_candidates'] = [dict(r) for r in db.execute('SELECT id,name FROM auth_users WHERE active=1 ORDER BY id')]
    ident = _query(query, 'employee_id')
    operator = _query(query, 'operator_id')
    card_state = _query(query, 'card_state')
    if card_state not in ('', 'unclosed', 'closed', 'candidate', 'pending_cancel', 'review'):
        raise ValueError('手机卡台账状态筛选不正确')
    complete = {row['id']: not _scope_incomplete(row['id'], bundle, cards_allowed) for row in bundle['employees']}
    if ident:
        employee = _require_employee(db, actor, ident)
        if not complete[ident]:
            raise auth.AccessError('该员工有关联卡超出当前办公室范围，请全局管理人员核对；未返回卡明细或核验来源', 403)
        ledger_context = employee_card_ledger.read_context(db, actor, bundle, cards_allowed, {ident})
        card_ledger, linked_cards = employee_card_ledger.derive(ident, bundle, ledger_context, operator_id=operator)
        version = _version(db, actor, ident, bundle=bundle, scope=scope)
        summary = _summary(employee, bundle, today, cards_allowed)
        units = [dict(r) for r in bundle['units'] if r['employee_id'] == ident]
        for unit in units:
            unit['card_phone'] = ''
            card_id = unit['current_card_id']
            if card_id is not None and (cards_allowed is None or card_id in cards_allowed):
                row = ledger_context['cards'].get(card_id)
                if row:
                    unit['card_phone'], unit['card_status'] = row['phone'], row['status']
            elif card_id is not None:
                unit['current_card_id'] = None
                unit['card_scope_hidden'] = True
        card_query = _text(_query(query, 'card_query'), '卡查询', maximum=40)
        candidates = []
        if _manager(actor) and (cards_allowed is None or cards_allowed):
            restriction = '' if cards_allowed is None else 'id IN (' + ','.join('?' for _ in cards_allowed) + ') AND '
            parameters = (() if cards_allowed is None else tuple(sorted(cards_allowed))) + (card_query, '%' + card_query + '%', card_query)
            candidates = [dict(r) for r in db.execute('SELECT id,phone,carrier,status,owner,keeper FROM phone_cards WHERE ' + restriction + '(?=\'\' OR phone LIKE ? OR CAST(id AS TEXT)=?) ORDER BY id DESC LIMIT 100', parameters)]
        history = [dict(id=r['id'], at=r['at'], action=r['action'], detail=json.loads(r['detail'])) for r in db.execute('SELECT * FROM employee_card_events WHERE employee_id=? ORDER BY at DESC,id DESC LIMIT 100', (ident,))]
        return dict(meta, employee=dict(_public_employee(employee, birth=True), version=version, card_ledger=card_ledger),
                    summary=summary, units=units, verifications=[r for r in bundle['verifications'] if r['employee_id'] == ident],
                    applications=[r for r in bundle['applications'] if r['employee_id'] == ident],
                    history=history, card_candidates=candidates,
                    card_links=[r for r in bundle['links'] if r['employee_id'] == ident],
                    linked_cards=linked_cards, card_ledger=card_ledger, card_ledger_totals=card_ledger,
                    version=version, can_view_birth_date=True)
    ledger_context = employee_card_ledger.read_context(db, actor, bundle, cards_allowed,
                                                       {employee_id for employee_id, value in complete.items() if value})
    rows = []
    linked_by_employee = {}
    for employee in bundle['employees']:
        summary = _summary(employee, bundle, today, cards_allowed)
        ledger, linked = employee_card_ledger.derive(employee['id'], bundle, ledger_context,
                                                    operator_id=operator, scope_complete=complete[employee['id']])
        linked_by_employee[employee['id']] = linked
        rows.append(dict(_public_employee(employee), summary=summary, card_ledger=ledger,
                         version=_version(db, actor, employee['id'], bundle=bundle, scope=scope)))
    text = _query(query, 'query').strip().casefold()
    status = _query(query, 'status')
    office = _query(query, 'office_id')
    employment = _query(query, 'employment')
    followup = _query(query, 'followup')
    cards_view = _query(query, 'view') == 'cards'
    rows = [r for r in rows if (not text or text in (r['name'] + ' ' + r['employee_no']).casefold()
                               or any(text in (card['phone_snapshot'] + ' ' + card['phone']).casefold() for card in linked_by_employee[r['id']]))
            and (not office or r['office_id'] == office) and (not employment or r['employment_status'] == employment)
            and any((not operator or op['operator_id'] == operator) and (not status or op['status'] == status)
                    for op in r['summary']['operators'])
            and (followup not in ('due', 'scheduled', 'unscheduled')
                 or followup == 'due' and any(op.get('next_followup_on') and op['next_followup_on'] <= today for op in r['summary']['operators'])
                 or followup == 'scheduled' and bool(r['summary'].get('next_followup_on'))
                 or followup == 'unscheduled' and not r['summary'].get('next_followup_on'))]
    # Do not turn an unresolved card match into a confirmed zero when a card
    # predicate excludes the row. Non-card employee filters still apply first.
    filter_incomplete = any(not row['card_ledger']['scope_complete'] for row in rows)
    rows = [row for row in rows
            if (not cards_view or status or not operator or bool(row['card_ledger']['registered']))
            and employee_card_ledger.matches(row['card_ledger'], card_state)]
    try:
        page, size = int(_query(query, 'page', '1')), int(_query(query, 'page_size', '50'))
    except ValueError:
        raise ValueError('页码不正确') from None
    if page < 1 or size not in (20, 50, 100):
        raise ValueError('页码或每页条数不正确')
    page = min(page, max(1, (len(rows) + size - 1) // size))
    summaries = []
    for op in bundle['operators']:
        values = [v for r in rows for v in r['summary']['operators'] if v['operator_id'] == op['id']]
        def known_sum(key):
            return None if any(v['counts'][key] is None for v in values) else sum(v['counts'][key] for v in values)
        summaries.append(dict(operator_id=op['id'], name=op['name'], employees=len(values),
                              company_current=known_sum('company_current'),
                              company_lifetime=known_sum('company_lifetime'),
                              pending_count=known_sum('pending'),
                              eligible=sum(v['status'] == 'eligible' for v in values),
                              ineligible=sum(v['status'] == 'ineligible' for v in values),
                              needs_verification=sum(v['status'] == 'needs_verification' for v in values)))
    scope_label = (rows[0]['card_ledger']['scope_label'] if rows else '当前登记运营商范围内的已关联卡（含历史补换卡）')
    ledger_totals = employee_card_ledger.aggregate([row['card_ledger'] for row in rows],
                                                 '当前员工筛选结果 · ' + scope_label)
    if filter_incomplete:
        ledger_totals = dict({key: None for key in employee_card_ledger.COUNTS}, scope_complete=False,
                             scope_label='部分关联卡范围不完整，当前卡筛选匹配及合计待核对')
    return dict(meta, employees=rows[(page - 1) * size:page * size], summaries=summaries,
                card_ledger_totals=ledger_totals,
                card_ledger_filter_incomplete=filter_incomplete,
                total=len(rows), page=page, page_size=size)


def _authorize(db, actor, action, ident):
    auth.require(actor, 'card_manage')
    if action not in ACTIONS:
        raise ValueError('员工办卡操作不正确')
    if action in ('operator', 'rules'):
        auth.require(actor, 'owner')
    elif ident:
        _require_employee(db, actor, ident)


def _ensure_available(db):
    if not available(db):
        raise auth.AccessError('员工办卡结构尚未升级，未保存任何资料', 503)


def _office(db, actor, office_id):
    office_id = _text(office_id, '办公室', maximum=80)
    if not office_id:
        if not office_scope.global_scope(actor):
            raise auth.AccessError('只有全局管理人员可登记待分配员工', 403)
        return ''
    if not _table(db, 'offices') or not db.execute('SELECT 1 FROM offices WHERE id=? AND active=1', (office_id,)).fetchone():
        raise ValueError('办公室不存在或已停用')
    if not office_scope.global_scope(actor) and office_id not in _grants(db, actor):
        raise auth.AccessError('无权在该办公室登记员工', 403)
    return office_id


def _card(db, actor, value):
    ident = _number(value, '手机卡编号', minimum=1, maximum=2**63 - 1)
    if getattr(db, 'scope_enabled', False):
        office_scope.require_entity(db, actor, 'cards', ident, write=True)
    elif not office_scope.global_scope(actor):
        raise auth.AccessError('当前模式不支持该人员管理手机卡归属', 403)
    row = db.execute('SELECT * FROM phone_cards WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('手机卡不存在')
    return dict(row)


def _operator(db, ident):
    row = db.execute('SELECT * FROM employee_card_operators WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('请选择已配置的运营商')
    return dict(json.loads(row['payload']), revision=row['revision'])


def _unit(db, employee_id, ident):
    row = db.execute('SELECT * FROM employee_card_units WHERE id=? AND employee_id=?', (ident, employee_id)).fetchone()
    if not row:
        raise ValueError('该员工的实名占额记录不存在')
    return dict(row)


def _unknown_network(operator_id, today):
    return dict(network='unknown', channel_id='', operator_id=operator_id,
                source='发卡时实际网络尚未核验；不根据当前品牌配置倒推',
                effective_from='', recorded_on=today)


def _network_policy(operator, activated_on, today):
    """A server-owned historical observation, never accepted from a form."""
    if not operator.get('source') or not operator.get('effective_from') or operator['effective_from'] > activated_on:
        return _unknown_network(operator['id'], today)
    return {**{key: operator.get(key, '') for key in ('network', 'channel_id', 'source', 'effective_from')},
            'operator_id': operator['id'], 'recorded_on': today}


def _frozen_network(unit, operators):
    policy = unit.get('network_policy')
    if isinstance(policy, str) and policy:
        policy = json.loads(policy)
    if isinstance(policy, dict):
        return policy.get('network', 'unknown'), policy.get('channel_id', '')
    if unit['phase'] == 'pending':
        operator = operators[unit['operator_id']]
        return operator.get('network', 'unknown'), operator.get('channel_id', '')
    return 'unknown', ''


def _event(db, actor, employee_id, action, detail):
    # Never copy birth dates or a session into history/idempotency receipts.
    db.execute('INSERT INTO employee_card_events(id,employee_id,at,actor_id,action,detail) VALUES(?,?,?,?,?,?)',
               (_id(), employee_id, int(time.time()), actor['id'], action, _json(detail)))


def _new_card_links(db, actor, employee_id, records, today, *, action='card_link', batch_id=''):
    """Shared new-link storage; inputs have already passed real-name and scope checks."""
    units, links, events = [], [], []
    for item in records:
        ident, card, operator = item['id'], item['card'], item['operator']
        activated, source = item['activated_on'], item['source']
        units.append((ident, employee_id, operator['id'], 'active', activated, activated,
                      card['id'], source, _json(_network_policy(operator, activated, today))))
        links.append((_id(), ident, card['id'], card['phone'], source, today))
        detail = dict(unit_id=ident, card_id=card['id'], source=source)
        if batch_id:
            detail['batch_id'] = batch_id
        events.append((_id(), employee_id, int(time.time()), actor['id'], action, _json(detail)))
    if units:
        db.executemany('INSERT INTO employee_card_units(id,employee_id,operator_id,phase,requested_on,activated_on,current_card_id,source,network_policy) VALUES(?,?,?,?,?,?,?,?,?)', units)
        db.executemany('INSERT INTO employee_card_links(id,unit_id,card_id,phone_snapshot,source,linked_on) VALUES(?,?,?,?,?,?)', links)
        db.executemany('INSERT INTO employee_card_events(id,employee_id,at,actor_id,action,detail) VALUES(?,?,?,?,?,?)', events)


def _batch_data(data, today):
    _fields(data, ('cards', 'source'))
    source = _text(data.get('source', ''), '实名关联依据', required=True)
    values = data.get('cards')
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise ValueError('每批请选择1至200张手机卡')
    cards, seen = [], set()
    for item in values:
        _fields(item, ('card_id', 'operator_id', 'activated_on'))
        ident = _number(item.get('card_id'), '手机卡编号', minimum=1, maximum=2**63 - 1)
        if ident in seen:
            raise ValueError('同一批手机卡编号不能重复')
        seen.add(ident)
        cards.append(dict(card_id=ident,
                          operator_id=_text(item.get('operator_id', ''), '每张卡的发卡运营商', maximum=80, required=True),
                          activated_on=_day(item.get('activated_on', ''), '办理日期', optional=True, latest=today)))
    return dict(cards=sorted(cards, key=lambda item: item['card_id']), source=source)


def _batch_classification(db, employee_id, data, snapshots):
    """Only selected, authorized phones are returned; unrelated identities stay private."""
    cards = {card['id']: card for card in snapshots}
    operators = {ident: _operator(db, ident) for ident in sorted({item['operator_id'] for item in data['cards']})}
    ids, phones = sorted(cards), sorted({card['phone'] for card in snapshots})
    query = ('SELECT l.*,u.employee_id,u.operator_id,u.activated_on FROM employee_card_links l '
             'LEFT JOIN employee_card_units u ON u.id=l.unit_id WHERE l.card_id IN (' +
             ','.join('?' for _ in ids) + ') OR l.phone_snapshot IN (' + ','.join('?' for _ in phones) + ') ORDER BY l.id')
    links = [dict(row) for row in db.execute(query, tuple(ids + phones))]
    digest = hashlib.sha256(_json(links).encode()).hexdigest()
    rows, records, counts = [], [], dict(total=len(data['cards']), new=0, existing=0, conflict=0)
    for item in data['cards']:
        card, operator = cards[item['card_id']], operators[item['operator_id']]
        prior = [link for link in links if link['card_id'] == card['id'] or link['phone_snapshot'] == card['phone']]
        status, reason, activated = 'new', '新建明确实名关联；原手机卡资料保持不变', item['activated_on']
        if not card['phone']:
            status, reason = 'conflict', '当前卡号码缺失，请先核对原手机卡资料'
        elif prior:
            if len(prior) == 1 and prior[0]['card_id'] == card['id'] and prior[0]['phone_snapshot'] == card['phone'] and prior[0]['employee_id'] == employee_id and prior[0]['operator_id'] == operator['id']:
                status, reason, activated = 'existing', '已关联同一员工和运营商，保留原办理日期，不重复新增', prior[0]['activated_on']
            else:
                status, reason = 'conflict', '已有其他实名关联、运营商或号码历史冲突，请核对；本批不会重绑或覆盖'
        counts[status] += 1
        rows.append(dict(card_id=card['id'], phone=card['phone'], operator_id=operator['id'],
                         operator_name=operator['name'], status=status,
                         status_label={'new': '新关联', 'existing': '已有关联', 'conflict': '冲突待核对'}[status],
                         reason=reason, activated_on=activated))
        if status == 'new':
            records.append(dict(card=card, operator=operator, activated_on=activated, source=data['source']))
    return dict(rows=rows, counts=counts, has_conflicts=bool(counts['conflict']),
                can_apply=not counts['conflict'], digest=digest, records=records)


def _execute_card_batch(db, actor, plan, today, *, previewing=False):
    data, eid = plan['data'], plan['employee_id']
    classified = _batch_classification(db, eid, data, plan['card_snapshots'])
    if previewing:
        # Frozen even if the other association is removed before a later confirmation.
        plan['batch_blocked'] = classified['has_conflicts']
        plan['batch_link_digest'] = classified['digest']
    elif plan.get('batch_blocked', True):
        raise operations.ConflictError('该批预览存在冲突，不能确认；请核对后重新预览；整批未保存')
    elif classified['has_conflicts'] or classified['digest'] != plan.get('batch_link_digest'):
        raise operations.ConflictError('手机卡实名关联已变化，请重新预览；整批未保存')
    if not classified['has_conflicts']:
        for item in classified['records']:
            item['id'] = plan['batch_unit_ids'][str(item['card']['id'])]
        db.execute('SAVEPOINT employee_card_batch_write')
        try:
            _new_card_links(db, actor, eid, classified['records'], today,
                            action='card_link_batch', batch_id=plan['new_id'])
        except BaseException:
            db.execute('ROLLBACK TO SAVEPOINT employee_card_batch_write')
            raise
        finally:
            db.execute('RELEASE SAVEPOINT employee_card_batch_write')
    counts = classified['counts']
    return dict(id=plan['new_id'], employee_id=eid, counts=counts, rows=classified['rows'],
                has_conflicts=classified['has_conflicts'], can_apply=classified['can_apply'],
                summary=[f"本批{counts['total']}张：新关联{counts['new']}张，已有关联{counts['existing']}张，冲突{counts['conflict']}张；" +
                         ('存在冲突，整批禁止确认' if classified['has_conflicts'] else '仅保存实名关联，原卡资料不变'),
                         '本次登记存量实名关系，不自动完成在途申请；已有申请请在高级办卡管理逐项核对。'])


def _execute(db, actor, plan, today, *, previewing=False):
    action, eid, data = plan['action'], plan['employee_id'], plan['data']
    ident = data.get('id') or plan['new_id']
    if action == 'employee':
        _fields(data, ('name', 'employee_no', 'birth_date', 'employment_status', 'office_id', 'auth_user_id', 'consent_source', 'consent_on'))
        old = _require_employee(db, actor, eid) if eid else None
        name = _text(data.get('name', old['name'] if old else ''), '员工姓名', maximum=100, required=True)
        number = _text(data.get('employee_no', old['employee_no'] if old else ''), '员工编号', maximum=80, required=True)
        birthday = _day(data.get('birth_date', old['birth_date'] if old else ''), '出生日期', optional=True, latest=today)
        source = _text(data.get('consent_source', old['consent_source'] if old else ''), '员工授权来源')
        consent_on = _day(data.get('consent_on', old['consent_on'] if old else ''), '员工授权日期', optional=True, latest=today)
        if birthday and not (source and consent_on):
            raise ValueError('保存出生日期前，请登记员工授权来源和日期；不要填写身份证号')
        employment = _choice(data.get('employment_status', old['employment_status'] if old else 'unknown'), ('active', 'left', 'unknown'), '在职状态')
        office = _office(db, actor, data.get('office_id', old['office_id'] if old else ''))
        user_id = data.get('auth_user_id', old['auth_user_id'] if old else None)
        if user_id == '':
            user_id = None
        if not old and user_id is not None or old and user_id != old['auth_user_id']:
            auth.require(actor, 'owner')
        if user_id is not None:
            _number(user_id, '已有员工登录身份', minimum=1, maximum=2**63 - 1)
            if not db.execute('SELECT 1 FROM auth_users WHERE id=?', (user_id,)).fetchone():
                raise ValueError('所绑定的登录身份不存在')
        duplicate = db.execute('SELECT id FROM employee_card_employees WHERE employee_no=? OR (auth_user_id=? AND auth_user_id IS NOT NULL)', (number, user_id)).fetchall()
        if any(row[0] != eid for row in duplicate):
            raise ValueError('员工编号或登录身份已有关联；请核对原员工记录，不按姓名合并')
        if old:
            ident = eid
            db.execute('UPDATE employee_card_employees SET name=?,employee_no=?,birth_date=?,employment_status=?,office_id=?,auth_user_id=?,consent_source=?,consent_on=?,revision=revision+1 WHERE id=?',
                       (name, number, birthday, employment, office, user_id, source, consent_on, ident))
        else:
            db.execute('INSERT INTO employee_card_employees(id,employee_no,name,office_id,auth_user_id,birth_date,employment_status,consent_source,consent_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (ident, number, name, office, user_id, birthday, employment, source, consent_on, int(time.time())))
        _event(db, actor, ident, action, dict(fields=sorted(data), birth_authorized=bool(birthday and source and consent_on)))
        return dict(id=ident, employee_id=ident, summary=['保存员工资料；实名关联不会从卡主或负责人文字推断'])
    if action == 'operator':
        allowed = ('id', 'name', 'network', 'channel_id', 'age_confirmed', 'min_age', 'source', 'effective_from',
                   'verification_valid_days', 'release_mode', 'release_delay_days', 'waiting_confirmed', 'waiting_days', 'waiting_basis', 'occupied_phases')
        _fields(data, allowed)
        old = _operator(db, ident) if data.get('id') else {}
        value = {key: data.get(key, old.get(key)) for key in allowed if key != 'id'}
        value.update(id=ident, name=_text(value['name'], '运营商名称', maximum=40, required=True),
                     network=_choice(value.get('network') or 'unknown', ('unknown', 'unicom', 'telecom', 'mobile', 'broadcast'), '底层网络'),
                     channel_id=_text(value.get('channel_id') or '', '渠道', maximum=80),
                     source=_text(value.get('source') or '', '规则依据', required=True),
                     effective_from=_day(value.get('effective_from') or '', '生效日期'))
        for field in ('age_confirmed', 'waiting_confirmed'):
            if type(value[field]) is not bool:
                raise ValueError('规则确认项必须明确选择')
        for field in ('min_age', 'verification_valid_days', 'release_delay_days', 'waiting_days'):
            value[field] = _number(value[field], field, nullable=True, maximum=36500 if field != 'min_age' else 120)
        if value['age_confirmed'] and value['min_age'] is None:
            raise ValueError('确认年龄规则时请明确门槛；无额外门槛应有依据后填0')
        value['release_mode'] = _choice(value.get('release_mode') or 'unknown', ('unknown', 'confirmed', 'after_days'), '释放规则')
        value['waiting_basis'] = _choice(value.get('waiting_basis') or '', ('', 'application', 'activation', 'release'), '等待期起算依据')
        value['occupied_phases'] = data.get('occupied_phases', old.get('occupied_phases', ['active']))
        if value['occupied_phases'] != ['active']:
            raise ValueError('已实名、停用、注销处理中或移出列表的卡均继续占额；只有确认释放才结束active占额')
        if value['release_mode'] == 'after_days' and value['release_delay_days'] is None:
            raise ValueError('释放延迟天数尚未明确')
        if value['waiting_confirmed'] and value['waiting_days'] is None:
            raise ValueError('请填写有依据的等待天数')
        if db.execute('SELECT 1 FROM employee_card_operators WHERE name=? AND id<>?', (value['name'], ident)).fetchone():
            raise ValueError('这个运营商名称已存在，请编辑原配置')
        db.execute('INSERT INTO employee_card_operators(id,name,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,payload=excluded.payload,revision=employee_card_operators.revision+1',
                   (ident, value['name'], _json(value)))
        _event(db, actor, '', action, dict(operator_id=ident, fields=sorted(data)))
        return dict(id=ident, employee_id='', summary=['保存运营商规则；未知项不会作为0或无限制'])
    if action == 'rules':
        _fields(data, ('rules',))
        rules = data.get('rules')
        if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
            raise ValueError('请提交1至100条规则')
        seen = set()
        for rule in rules:
            _fields(rule, ('id', 'scope', 'scope_id', 'period', 'limit', 'pending_counts', 'source', 'effective_from', 'confirmed', 'revision'))
            key = _text(rule.get('id', ''), '规则编号', maximum=80, required=True)
            if key in seen:
                raise ValueError('规则编号重复')
            seen.add(key)
            value = dict(id=key, scope=_choice(rule.get('scope'), ('all', 'operator', 'network', 'channel'), '规则范围'),
                         scope_id=_text(rule.get('scope_id', ''), '规则范围编号', maximum=80),
                         period=_choice(rule.get('period'), ('stock', 'day', 'month'), '统计窗口'),
                         limit=_number(rule.get('limit'), '数量上限', nullable=True),
                         source=_text(rule.get('source', ''), '规则依据', required=True),
                         effective_from=_day(rule.get('effective_from', ''), '规则生效日期'))
            if type(rule.get('confirmed')) is not bool or rule.get('pending_counts') is not None and type(rule['pending_counts']) is not bool:
                raise ValueError('规则确认或在途占额选项不正确')
            value.update(confirmed=rule['confirmed'], pending_counts=rule.get('pending_counts'))
            if value['scope'] == 'all' and value['scope_id'] or value['scope'] != 'all' and not value['scope_id']:
                raise ValueError('请明确规则适用范围')
            if value['scope'] == 'operator':
                _operator(db, value['scope_id'])
            db.execute('INSERT INTO employee_card_rules(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=employee_card_rules.revision+1', (key, _json(value)))
        _event(db, actor, '', action, dict(rule_ids=sorted(seen)))
        return dict(id='rules', employee_id='', summary=['保存明确提交的规则；未提交的既有规则保留'])
    _require_employee(db, actor, eid)
    if action == 'card_link_batch':
        return _execute_card_batch(db, actor, plan, today, previewing=previewing)
    if action == 'application':
        _fields(data, ('id', 'operator_id', 'requested_on', 'applied_on', 'quantity', 'result', 'failure_reason', 'retry_on', 'retry_source', 'failure_resolved', 'source'))
        old = db.execute('SELECT * FROM employee_card_applications WHERE id=? AND employee_id=?', (ident, eid)).fetchone() if data.get('id') else None
        if data.get('id') and not old:
            raise ValueError('申请记录不存在')
        op = _operator(db, data.get('operator_id', old['operator_id'] if old else ''))
        day = _day(data.get('requested_on', data.get('applied_on', old['applied_on'] if old else '')), '申请日期', latest=today)
        quantity = _number(data.get('quantity', old['quantity'] if old else None), '申请数量', minimum=1, maximum=100)
        result = _choice(data.get('result', old['result'] if old else 'pending'), ('pending', 'succeeded', 'partial', 'failed', 'cancelled'), '申请结果')
        failure = _text(data.get('failure_reason', ''), '失败原因', maximum=1000)
        retry = _day(data.get('retry_on', ''), '再次申请日期', optional=True)
        retry_source = _text(data.get('retry_source', ''), '再次申请日期依据')
        if retry and not retry_source:
            raise ValueError('请填写运营商给出再次申请日期的依据')
        if old and (quantity != old['quantity'] or op['id'] != old['operator_id'] or day != old['applied_on']):
            raise ValueError('申请数量、运营商和原日期需保留；请新增一次申请记录')
        if old and old['result'] in ('failed', 'cancelled') and result not in ('failed', 'cancelled'):
            raise ValueError('已结束的申请不能重新占用旧名额，请登记新申请')
        unit_ids = [r[0] for r in db.execute('SELECT id FROM employee_card_units WHERE application_id=? ORDER BY id', (ident,))] if old else plan['unit_ids']
        payload = dict(unit_ids=unit_ids, failure_reason=failure, retry_on=retry, retry_source=retry_source,
                       failure_resolved=data.get('failure_resolved') is True, source=_text(data.get('source', ''), '申请来源'))
        if old:
            db.execute('UPDATE employee_card_applications SET result=?,payload=?,revision=revision+1 WHERE id=?', (result, _json(payload), ident))
            if result in ('failed', 'cancelled'):
                db.execute('UPDATE employee_card_units SET phase=?,revision=revision+1 WHERE application_id=? AND phase=?', (result, ident, 'pending'))
        else:
            db.execute('INSERT INTO employee_card_applications(id,employee_id,operator_id,applied_on,result,quantity,payload) VALUES(?,?,?,?,?,?,?)', (ident, eid, op['id'], day, result, quantity, _json(payload)))
            phase = result if result in ('failed', 'cancelled') else 'pending'
            for unit_id in unit_ids:
                db.execute('INSERT INTO employee_card_units(id,employee_id,operator_id,application_id,phase,requested_on,source) VALUES(?,?,?,?,?,?,?)', (unit_id, eid, op['id'], ident, phase, day, payload['source']))
        _event(db, actor, eid, action, dict(application_id=ident, quantity=quantity, result=result, failure_reason=failure, retry_on=retry, retry_source=retry_source))
        return dict(id=ident, employee_id=eid, summary=['记录申请和稳定在途单元；成功后关联已有卡，不重复新增名额'])
    if action in ('card_link', 'replacement'):
        _fields(data, ('unit_id', 'card_id', 'operator_id', 'activated_on', 'source'))
        card = _card(db, actor, data.get('card_id'))
        source = _text(data.get('source', ''), '实名关联依据', required=True)
        unit = _unit(db, eid, data['unit_id']) if data.get('unit_id') else None
        prior = db.execute('SELECT * FROM employee_card_links WHERE card_id=? OR phone_snapshot=?', (card['id'], card['phone'])).fetchall()
        if prior:
            if len(prior) != 1 or prior[0]['card_id'] != card['id']:
                raise ValueError('此号码已有历史实名关联，需核对原占额单元，不能猜测合并')
            existing = _unit(db, eid, prior[0]['unit_id'])
            if unit and unit['id'] != existing['id'] or data.get('operator_id') and data['operator_id'] != existing['operator_id']:
                raise ValueError('手机卡已关联其他占额单元或运营商')
            return dict(id=existing['id'], employee_id=eid, summary=['该卡已关联，保持原占额单元；不重复计算'])
        if action == 'replacement':
            if not unit or unit['phase'] not in ('active',):
                raise ValueError('补换卡请选择尚未释放的在用占额单元')
            ident = unit['id']
            db.execute('UPDATE employee_card_units SET current_card_id=?,revision=revision+1 WHERE id=?', (card['id'], ident))
        else:
            op = _operator(db, data.get('operator_id', unit['operator_id'] if unit else ''))
            activated = _day(data.get('activated_on', ''), '办理日期', optional=True, latest=today)
            network_policy = _json(_network_policy(op, activated, today))
            if unit:
                if unit['phase'] != 'pending' or unit['operator_id'] != op['id'] or activated and activated < unit['requested_on']:
                    raise ValueError('请将同一运营商的未完成在途单元关联到实际办卡结果')
                ident = unit['id']
                db.execute('UPDATE employee_card_units SET phase=?,activated_on=?,current_card_id=?,source=?,network_policy=?,revision=revision+1 WHERE id=?', ('active', activated, card['id'], source, network_policy, ident))
            else:
                _new_card_links(db, actor, eid, [dict(id=ident, card=card, operator=op,
                                activated_on=activated, source=source)], today)
                return dict(id=ident, employee_id=eid, summary=['保存明确实名关系；不修改原卡主、使用人、保管人或卡状态'])
        db.execute('INSERT INTO employee_card_links(id,unit_id,card_id,phone_snapshot,source,linked_on) VALUES(?,?,?,?,?,?)', (_id(), ident, card['id'], card['phone'], source, today))
        _event(db, actor, eid, action, dict(unit_id=ident, card_id=card['id'], source=source))
        return dict(id=ident, employee_id=eid, summary=['保存明确实名关系；不修改原卡主、使用人、保管人或卡状态'])
    if action in ('cancel_request', 'release'):
        _fields(data, ('unit_id', 'requested_on', 'released_on', 'source'))
        unit = _unit(db, eid, data.get('unit_id', ''))
        source = _text(data.get('source', ''), '注销或释放依据', required=True)
        ident = unit['id']
        if action == 'cancel_request':
            requested = _day(data.get('requested_on', ''), '注销申请日期', latest=today)
            if unit['phase'] != 'active' or requested < unit['activated_on']:
                raise ValueError('只能为已办理且尚未释放的实名卡登记注销申请')
            if unit['cancelled_on'] and (unit['cancelled_on'] != requested or unit['cancel_source'] != source):
                raise ValueError('已记录注销申请，请核对历史后登记确认释放')
            if not unit['cancelled_on']:
                db.execute('UPDATE employee_card_units SET cancelled_on=?,cancel_source=?,revision=revision+1 WHERE id=?', (requested, source, ident))
        else:
            released = _day(data.get('released_on', ''), '确认释放日期', latest=today)
            if unit['phase'] == 'released':
                if released != unit['release_confirmed_on'] or source != unit['release_source']:
                    raise ValueError('该名额已释放，不能再次释放或覆盖原依据')
                return dict(id=ident, employee_id=eid, summary=['此前已确认释放，本次不重复释放'])
            op = _operator(db, unit['operator_id'])
            if unit['phase'] != 'active' or not unit['cancelled_on'] or released < unit['cancelled_on']:
                raise ValueError('请先记录注销申请，再依据运营商确认名额释放')
            if op['effective_from'] > released or op['release_mode'] == 'unknown':
                raise ValueError('该运营商名额释放规则未确认，注销处理中仍占名额')
            if op['release_mode'] == 'after_days':
                earliest = (date.fromisoformat(unit['cancelled_on']) + timedelta(days=op['release_delay_days'])).isoformat()
                if released < earliest:
                    raise ValueError('尚未达到已确认的注销释放日期')
            policy = {key: op[key] for key in ('release_mode', 'release_delay_days', 'source', 'effective_from')}
            policy['operator_id'] = unit['operator_id']
            db.execute('UPDATE employee_card_units SET phase=?,release_confirmed_on=?,release_source=?,release_policy=?,revision=revision+1 WHERE id=?', ('released', released, source, _json(policy), ident))
        _event(db, actor, eid, action, dict(unit_id=ident, source=source, date=data.get('released_on', data.get('requested_on'))))
        return dict(id=ident, employee_id=eid, summary=['注销申请仍占名额；只有明确确认释放才减少当前占用'])
    if action == 'verification':
        allowed = ('scope', 'scope_id', 'period', 'period_start', 'period_end', 'count', 'coverage',
                   'verified_on', 'valid_until', 'source', 'pending_unit_ids', 'company_inventory_complete')
        _fields(data, allowed)
        value = dict(scope=_choice(data.get('scope'), ('all', 'operator', 'network', 'channel'), '核验范围'),
                     scope_id=_text(data.get('scope_id', ''), '核验范围编号', maximum=80),
                     period=_choice(data.get('period'), ('stock', 'day', 'month'), '核验统计窗口'),
                     period_start=_day(data.get('period_start', ''), '统计开始日期'),
                     period_end=_day(data.get('period_end', ''), '统计结束日期'),
                     count=_number(data.get('count'), '核验数量'),
                     coverage=_choice(data.get('coverage'), ('total', 'external', 'unknown'), '公司卡是否已包括'),
                     verified_on=_day(data.get('verified_on', ''), '核验日期', latest=today),
                     valid_until=_day(data.get('valid_until', ''), '核验有效期', optional=True),
                     source=_text(data.get('source', ''), '核验来源', required=True))
        complete = data.get('company_inventory_complete', False)
        if type(complete) is not bool:
            raise ValueError('公司实名卡台账完整性必须明确选择，不能以空值或数字替代')
        value['company_inventory_complete'] = complete
        if value['period_start'] > value['period_end'] or value['valid_until'] and value['valid_until'] < value['verified_on']:
            raise ValueError('核验统计范围或有效日期不正确')
        if value['scope'] == 'all' and value['scope_id'] or value['scope'] != 'all' and not value['scope_id']:
            raise ValueError('请明确核验是否涵盖全部基础运营商与虚商')
        if value['scope'] == 'operator':
            _operator(db, value['scope_id'])
        units = [dict(r) for r in db.execute('SELECT * FROM employee_card_units WHERE employee_id=?', (eid,))]
        ops = {r['id']: json.loads(r['payload']) for r in db.execute('SELECT * FROM employee_card_operators')}
        def in_scope(unit):
            if value['scope'] == 'operator' and unit['operator_id'] != value['scope_id']:
                return False
            if value['scope'] in ('network', 'channel'):
                network, channel = _frozen_network(unit, ops)
                if (network if value['scope'] == 'network' else channel) != value['scope_id']:
                    return False
            return True
        def matches(unit):
            if not in_scope(unit):
                return False
            stamp = unit['activated_on']
            return bool(stamp and stamp <= value['verified_on'] and (value['period'] == 'stock' or value['period_start'] <= stamp <= value['period_end']) and (value['period'] != 'stock' or not unit['release_confirmed_on'] or unit['release_confirmed_on'] > value['verified_on']))
        value['company_snapshot_ids'] = sorted(u['id'] for u in units if matches(u))
        value['unit_ids'] = value['company_snapshot_ids'] if value['coverage'] == 'total' else []
        value['recorded_at'] = time.time_ns()
        pending = data.get('pending_unit_ids', [])
        if not isinstance(pending, list) or len(pending) != len(set(pending)) or any(type(i) is not str for i in pending):
            raise ValueError('已计入核验的在途单元需逐条选择')
        valid_pending = {u['id'] for u in units if u['phase'] == 'pending' and in_scope(u)}
        if not set(pending) <= valid_pending or value['coverage'] != 'total' and pending:
            raise ValueError('在途核验关联不正确；仅完整总数可包含公司在途')
        value['pending_unit_ids'] = sorted(pending)
        if value['coverage'] == 'total' and value['count'] < len(value['unit_ids']) + len(pending):
            raise ValueError('核验总数小于已明确包括的公司数量，请核对来源与范围')
        db.execute('INSERT INTO employee_card_verifications(id,employee_id,payload,created_at) VALUES(?,?,?,?)', (ident, eid, _json(value), int(time.time())))
        _event(db, actor, eid, action, dict(verification_id=ident, scope=value['scope'], scope_id=value['scope_id'], period=value['period'], count=value['count'], source=value['source'], company_inventory_complete=complete))
        return dict(id=ident, employee_id=eid, summary=['记录来源、范围与公司卡快照；不会把总数再加一次公司卡'])
    raise ValueError('员工办卡操作不正确')


def _card_snapshots(db, actor, data, *, batch=False):
    if batch:
        # Re-authorize each selected card on every preview/apply; scope cannot be cached.
        snapshots = []
        for item in data['cards']:
            card = _card(db, actor, item['card_id'])
            card['_office_ids'] = (sorted(office_scope.entity_offices(db, 'cards', card['id']))
                                   if getattr(db, 'scope_enabled', False) else [])
            snapshots.append(card)
        return snapshots
    return [_card(db, actor, data['card_id'])] if 'card_id' in data else []


def preview(db, actor, body, today):
    actor = _actor(db, actor)
    _ensure_available(db)
    _fields(body, ('action', 'employee_id', 'data', 'expected_version', 'request_id'))
    action, eid = body.get('action'), _text(body.get('employee_id', ''), '员工标识', maximum=80)
    _authorize(db, actor, action, eid)
    if action not in ('employee', 'operator', 'rules') and not eid:
        raise ValueError('请先选择员工')
    current = _version(db, actor, eid)
    if body.get('expected_version') != current:
        raise operations.ConflictError('员工资料或规则已变化，请刷新核对后重新预览')
    data = body.get('data')
    if not isinstance(data, dict):
        raise ValueError('请提交本次操作资料')
    if action == 'card_link_batch':
        data = _batch_data(data, today)
    quantity = data.get('quantity', 1)
    if action == 'application' and not data.get('id'):
        quantity = _number(quantity, '申请数量', minimum=1, maximum=100)
    plan = dict(action=action, employee_id=eid, data=data, new_id=_id(),
                unit_ids=[_id() for _ in range(quantity if action == 'application' and not data.get('id') else 0)],
                expected_version=current, day=today, restore_generation=_generation(db),
                card_snapshots=_card_snapshots(db, actor, data, batch=action == 'card_link_batch'))
    if action == 'card_link_batch':
        plan['batch_unit_ids'] = {str(item['card_id']): _id() for item in data['cards']}
    db.execute('SAVEPOINT employee_card_preview_check')
    try:
        result = _execute(db, actor, plan, today, previewing=True)
    finally:
        db.execute('ROLLBACK TO SAVEPOINT employee_card_preview_check')
        db.execute('RELEASE SAVEPOINT employee_card_preview_check')
    preview_id = operations.preview(db, actor, 'employee-cards', plan)
    response = dict(preview_id=preview_id, action=action, employee_id=eid, summary=result['summary'],
                    version=current, expires_at=int(time.time()) + 1800)
    if action == 'card_link_batch':
        response.update({key: result[key] for key in ('rows', 'counts', 'has_conflicts', 'can_apply')})
    return response


def apply(db, actor, body, today):
    actor = _actor(db, actor)
    _ensure_available(db)
    _fields(body, ('preview_id', 'request_id', 'confirmation'))
    auth.require(actor, 'card_manage')
    request_id = idempotency.request_uuid(body.get('request_id'))
    fingerprint = hashlib.sha256(_json({k: body[k] for k in sorted(body) if k != 'request_id'}).encode()).hexdigest()
    stored = db.execute('SELECT * FROM employee_card_requests WHERE actor_id=? AND request_id=?', (actor['id'], request_id)).fetchone()
    if stored:
        _require_generation(db, stored['restore_generation'])
        if stored['fingerprint'] != fingerprint:
            raise ValueError('同一请求标识不能用于不同员工办卡内容')
        _authorize(db, actor, stored['action'], stored['employee_id'])
        return json.loads(stored['result'])
    if body.get('confirmation') != '确认保存':
        raise ValueError('请输入“确认保存”后再提交')
    preview_id = _text(body.get('preview_id', ''), '预览标识', maximum=80, required=True)
    row = db.execute('SELECT * FROM operations_previews WHERE id=? AND actor_id=? AND kind=?', (preview_id, actor['id'], 'employee-cards')).fetchone()
    if not row or row['expires_at'] <= int(time.time()):
        raise ValueError('预览不存在或已过期，请重新预览')
    plan = json.loads(row['payload'])
    _require_generation(db, plan.get('restore_generation'))
    _authorize(db, actor, plan['action'], plan['employee_id'])
    if plan['day'] != today or _version(db, actor, plan['employee_id']) != plan['expected_version']:
        raise operations.ConflictError('员工资料、权限、日期或规则已变化，请重新预览；未保存')
    if _card_snapshots(db, actor, plan['data'], batch=plan['action'] == 'card_link_batch') != plan['card_snapshots']:
        raise operations.ConflictError('手机卡资料或范围已变化，请重新预览；未保存')
    saved = _execute(db, actor, plan, today)
    result = dict(ok=True, id=saved['id'], employee_id=saved['employee_id'], action=plan['action'],
                  version=_version(db, actor, saved['employee_id']))
    if plan['action'] == 'card_link_batch':
        # Request lookup rechecks employee scope, so receipts deliberately omit card details.
        result['counts'] = saved['counts']
    db.execute('INSERT INTO employee_card_requests(actor_id,request_id,restore_generation,fingerprint,employee_id,action,result,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (actor['id'], request_id, _generation(db), fingerprint, saved['employee_id'], plan['action'], _json(result), int(time.time())))
    db.execute('DELETE FROM operations_previews WHERE id=?', (body['preview_id'],))
    return result
