"""Pure, conservative employee SIM quota calculation. No persistence or I/O.

One stable unit represents an application slot through activation/replacement.
Existing phone-card status, keeper and owner text are deliberately not inputs.
All rules and verification claims are supplied by the authenticated service.
"""
from datetime import date, timedelta


STATUS_LABELS = {'eligible': '符合已知申请条件', 'ineligible': '暂不符合',
                 'needs_verification': '待核验'}
NETWORKS = {'unicom', 'telecom', 'mobile', 'broadcast'}
PHASES = {'pending', 'active', 'released', 'failed', 'cancelled'}
ISSUE_LABELS = {
    'verification_missing': '尚未登记这个范围与统计期间的名下核验',
    'verification_date_invalid': '核验日期缺失或格式不正确',
    'verification_conflict': '同一时间的核验记录互相矛盾，请核对原始来源',
    'verification_in_future': '核验日期晚于当前日期',
    'verification_claim_invalid': '核验数量或来源不完整',
    'verification_coverage_unknown': '尚未确认核验数量是否已经包含公司卡',
    'verification_validity_invalid': '核验有效日期格式不正确',
    'verification_validity_unknown': '尚未确认核验资料可使用到哪一天',
    'verification_expired': '核验资料已过有效期，需要重新核验',
    'verification_window_mismatch': '核验统计期间与本次日限额或月限额不一致',
    'verification_outside_window': '核验日期不在其声明的统计期间内',
    'verification_snapshot_invalid': '核验所包含的公司卡或在途记录不完整',
    'rule_unconfirmed': '数量规则的依据、生效日期或确认状态不完整',
    'rule_not_effective': '该数量规则尚未生效',
    'rule_scope_unknown': '数量规则的运营商范围或统计期间未明确',
    'rule_scope_id_invalid': '数量规则的范围标识不正确',
    'rule_limit_unknown': '尚未确认该范围的数量上限',
    'snapshot_overlap': '同一名额同时被列入公司卡和在途核验，需核对',
    'snapshot_unit_missing': '核验关联的历史名额资料缺失，无法确定是否重复计算',
    'external_snapshot_contains_company': '仅统计其他渠道的核验不能同时声明已包含公司卡',
    'unit_scope_unknown': '部分名额的实际网络或渠道未确认，无法确定统计归属',
    'activation_date_unknown': '部分已办理名额缺少实际激活日期',
    'activation_date_invalid': '实际激活日期格式不正确',
    'activation_in_future': '实际激活日期晚于当前日期',
    'snapshot_unit_scope_mismatch': '核验关联的名额不属于声明的运营商、网络或渠道',
    'snapshot_active_date_conflict': '公司卡激活日期与核验所包含的记录相矛盾',
    'company_exceeds_verified_total': '已包含的公司卡与在途数量大于核验总数',
    'snapshot_contains_already_released': '核验仍包含在核验前已确认释放的名额，请重核来源',
    'uncovered_existing_company_unit': '补录的历史公司卡是否已包含在核验总数中尚未明确',
    'covered_pending_outcome_unverified': '已包含在核验中的在途记录已变更结果，需重核名额',
    'negative_derived_total': '核验与释放记录推导出矛盾数量，请核对来源',
    'pending_policy_unknown': '尚未确认运营商如何计算在途申请的占额',
    'pending_coverage_policy_conflict': '在途核验口径与已确认的占额规则不一致',
    'release_unconfirmed': '名额释放日期或运营商确认依据不足，仍保留占用',
    'release_policy_unconfirmed': '历史释放规则的来源、生效日期或运营商归属不完整',
    'release_policy_incomplete': '注销释放的起算日期或明确等待天数不完整',
    'release_before_allowed_date': '确认释放日期早于已明确的注销释放日期',
    'release_policy_unknown': '运营商名额释放规则尚未确认',
    'release_before_activation': '名额释放日期早于实际激活日期',
    'unit_phase_unknown': '实名名额所处办理阶段不明确',
    'unit_phase_conflict': '办理阶段与实际激活记录矛盾',
    'unit_network_invalid': '部分名额的底层网络资料格式不正确',
    'unit_network_conflict': '名额记录的实际网络与运营商配置相矛盾',
    'network_policy_invalid': '历史网络或渠道依据不完整，不能用当前配置改写历史归属',
    'cross_scope_total_conflict': '单家运营商核验数量大于全部运营商合计数量，请核对来源',
    'company_inventory_incomplete': '尚未确认公司实名台账完整，不能把已登记数量当作公司全部数量',
    'company_inventory_confirmation_invalid': '公司实名台账完整核对项格式不正确',
}


def _issue_label(code):
    return ISSUE_LABELS.get(code, '资料存在缺失或冲突，请核对相关记录')


def _day(value):
    if isinstance(value, date):
        return value if type(value) is date else None
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        result = date.fromisoformat(value)
        return result if result.isoformat() == value else None
    except ValueError:
        return None


def _number(value):
    return type(value) is int and value >= 0


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _known_network(value):
    return isinstance(value, str) and value in NETWORKS


def age_on(birth_date, today):
    """Completed years; a Feb 29 birthday advances on Mar 1 in other years."""
    birth, current = _day(birth_date), _day(today)
    if not birth or not current or birth > current:
        return None
    return current.year - birth.year - ((current.month, current.day) < (birth.month, birth.day))


def threshold_on(birth_date, minimum_age):
    birth = _day(birth_date)
    if not birth or not _number(minimum_age) or minimum_age > 150:
        return None
    try:
        return birth.replace(year=birth.year + minimum_age).isoformat()
    except ValueError:
        if birth.month == 2 and birth.day == 29 and birth.year + minimum_age <= 9999:
            return date(birth.year + minimum_age, 3, 1).isoformat()
        return None


def _unique(rows, label):
    result, issues = {}, []
    if not isinstance(rows, list):
        return {}, [label + '_invalid']
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            issues.append(label + '_invalid_id')
            continue
        if row['id'] in result and result[row['id']] != row:
            issues.append(label + '_duplicate_conflict')
        else:
            result[row['id']] = row
    return result, list(dict.fromkeys(issues))


def _reason(code, text, kind='unknown'):
    return {'code': code, 'text': text, 'kind': kind}


def _window(period, today):
    if period == 'stock':
        return None, None
    if period == 'day':
        return today, today
    if period == 'month':
        start = today.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1)) - timedelta(days=1)
        return start, end
    return None, None


def _frozen_network(unit):
    policy = unit.get('network_policy')
    if not isinstance(policy, dict):
        return None
    network = policy.get('network')
    event = _day(unit.get('activated_on')) or _day(unit.get('requested_on'))
    effective = _day(policy.get('effective_from'))
    recorded = _day(policy.get('recorded_on')) if policy.get('recorded_on') else None
    channel = policy.get('channel_id', '')
    if (network != 'unknown' and not _known_network(network) or
            not isinstance(channel, str) or not _text(policy.get('source')) or
            policy.get('operator_id') != unit.get('operator_id') or
            policy.get('recorded_on') and not recorded):
        return None
    # A correctly identified lack of historical network evidence is not a false
    # assertion about the date or network, and does not invalidate all-scope totals.
    if network == 'unknown' and not channel and not policy.get('effective_from'):
        return 'unknown', ''
    if not effective or not event or effective > event:
        return None
    return network, channel


def _network(unit, operator):
    if 'network_policy' in unit:
        frozen = _frozen_network(unit)
        return frozen[0] if frozen else 'invalid'
    own, configured = unit.get('network', 'unknown'), operator.get('network', 'unknown')
    if _known_network(own) and _known_network(configured) and own != configured:
        return 'conflict'
    return own if _known_network(own) else configured if _known_network(configured) else 'unknown'


def _unit_info(unit, operator, today):
    phase = unit.get('phase')
    activated = _day(unit.get('activated_on'))
    released = _day(unit.get('release_confirmed_on'))
    issues = []
    if not isinstance(phase, str) or phase not in PHASES:
        issues.append('unit_phase_unknown')
    if unit.get('activated_on') and not activated:
        issues.append('activation_date_invalid')
    if activated and activated > today:
        issues.append('activation_in_future')
    if unit.get('network') not in (None, '', 'unknown') and not _known_network(unit.get('network')):
        issues.append('unit_network_invalid')
    actual_network = _network(unit, operator)
    if actual_network == 'conflict':
        issues.append('unit_network_conflict')
    if actual_network == 'invalid':
        issues.append('network_policy_invalid')
    frozen = _frozen_network(unit) if 'network_policy' in unit else None
    channel = frozen[1] if frozen else '' if 'network_policy' in unit else unit.get('channel_id', '')
    issued = phase in ('active', 'released') or bool(activated and activated <= today)
    valid_release = False
    if phase == 'released' or unit.get('release_confirmed_on'):
        frozen = unit.get('release_policy')
        policy = frozen if isinstance(frozen, dict) else operator
        policy_date = _day(policy.get('effective_from'))
        policy_valid = (_text(policy.get('source')) and policy_date and released
                        and policy_date <= released)
        if frozen is not None and (not isinstance(frozen, dict) or
                (frozen.get('operator_id') is not None and frozen['operator_id'] != unit.get('operator_id'))):
            policy_valid = False
        if not released or released > today or not _text(unit.get('release_source')):
            issues.append('release_unconfirmed')
        elif not policy_valid:
            issues.append('release_policy_unconfirmed')
        elif policy.get('release_mode') == 'confirmed':
            valid_release = True
        elif policy.get('release_mode') == 'after_days':
            cancelled = _day(unit.get('cancelled_on'))
            delay = policy.get('release_delay_days')
            if not cancelled or not _number(delay):
                issues.append('release_policy_incomplete')
            elif released < cancelled + timedelta(days=delay):
                issues.append('release_before_allowed_date')
            else:
                valid_release = True
        else:
            issues.append('release_policy_unknown')
        if activated and released and released < activated:
            valid_release = False
            issues.append('release_before_activation')
    if phase in ('failed', 'cancelled', 'pending') and activated:
        issues.append('unit_phase_conflict')
    return {**unit, 'channel_id': channel, '_issued': issued, '_current': issued and not valid_release,
            '_pending': phase == 'pending' and not activated,
            '_activated': activated, '_released': released if valid_release else None,
            '_network': actual_network, '_issues': issues}


def _scope_match(unit, scope, ident):
    if scope == 'all':
        return True
    if scope == 'operator':
        return unit.get('operator_id') == ident
    if scope == 'network':
        return None if not _known_network(unit['_network']) else unit['_network'] == ident
    if scope == 'channel':
        channel = unit.get('channel_id', '')
        return None if not _text(channel) else channel == ident
    return None


def _snapshot_ids(check, key):
    value = check.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return set(value)


def _select_check(checks, scope, scope_id, period, today, operator):
    rows = [row for row in checks.values() if row.get('scope') == scope
            and row.get('scope_id', '') == scope_id and row.get('period') == period]
    if not rows:
        return None, ['verification_missing']
    if any(not _day(row.get('verified_on')) for row in rows):
        return None, ['verification_date_invalid']
    latest_date = max(_day(row['verified_on']) for row in rows)
    rows = [row for row in rows if _day(row['verified_on']) == latest_date]
    latest_recorded = max(row.get('recorded_at', 0) if _number(row.get('recorded_at', 0)) else 0 for row in rows)
    rows = [row for row in rows if (row.get('recorded_at', 0) if _number(row.get('recorded_at', 0)) else 0) == latest_recorded]
    claims = {(repr(row.get('count')), repr(row.get('coverage')), repr(row.get('unit_ids')),
               repr(row.get('pending_unit_ids')), row.get('valid_until', '')) for row in rows}
    if len(claims) != 1:
        return None, ['verification_conflict']
    check = rows[0]
    issues = []
    if latest_date > today:
        issues.append('verification_in_future')
    if not _text(check.get('source')) or not _number(check.get('count')):
        issues.append('verification_claim_invalid')
    if check.get('coverage') not in ('total', 'external'):
        issues.append('verification_coverage_unknown')
    until = _day(check.get('valid_until'))
    validity = operator.get('verification_valid_days')
    if check.get('valid_until') and not until:
        issues.append('verification_validity_invalid')
    if _number(validity):
        policy_until = latest_date + timedelta(days=validity)
        until = min(until, policy_until) if until else policy_until
    if not until:
        issues.append('verification_validity_unknown')
    elif until < latest_date or until < today:
        issues.append('verification_expired')
    start, end = _window(period, today)
    if period != 'stock' and (_day(check.get('period_start')) != start or _day(check.get('period_end')) != end):
        issues.append('verification_window_mismatch')
    if period != 'stock' and not start <= latest_date <= end:
        issues.append('verification_outside_window')
    if _snapshot_ids(check, 'unit_ids') is None or _snapshot_ids(check, 'pending_unit_ids') is None:
        issues.append('verification_snapshot_invalid')
    return {**check, '_valid_through': until.isoformat() if until else None}, issues


def _limit(rule, operator, units, checks, today):
    scope, ident, period = rule.get('scope'), rule.get('scope_id', ''), rule.get('period')
    result = {'rule_id': rule.get('id'), 'scope': scope, 'scope_id': ident,
              'period': period, 'limit': rule.get('limit'), 'used': None,
              'reserved': None, 'available': None, 'verification_id': None,
              'coverage': None, 'issues': [], '_other': None, '_verified': None,
              '_verification_on': None, '_followup': None, '_company_complete': False}
    issues = result['issues']
    effective = _day(rule.get('effective_from'))
    if rule.get('confirmed') is not True or not _text(rule.get('source')) or not effective:
        issues.append('rule_unconfirmed')
    elif effective > today:
        issues.append('rule_not_effective')
    if scope not in ('all', 'operator', 'network', 'channel') or period not in ('stock', 'day', 'month'):
        issues.append('rule_scope_unknown')
        return result
    if not isinstance(ident, str) or (scope != 'all' and not ident) or (scope == 'all' and ident):
        issues.append('rule_scope_id_invalid')
        return result
    if not _number(rule.get('limit')):
        issues.append('rule_limit_unknown')
    check, check_issues = _select_check(checks, scope, ident, period, today, operator)
    issues.extend(check_issues)
    if not check:
        return result
    result['verification_id'] = check['id']
    result['coverage'] = check.get('coverage')
    result['_company_complete'] = check.get('company_inventory_complete') is True
    if 'company_inventory_complete' in check and type(check['company_inventory_complete']) is not bool:
        issues.append('company_inventory_confirmation_invalid')
    result['_verification_on'] = check.get('verified_on')
    until = _day(check.get('_valid_through'))
    if until:
        result['_followup'] = until.isoformat()
    covered = _snapshot_ids(check, 'unit_ids')
    covered_pending = _snapshot_ids(check, 'pending_unit_ids')
    if covered is None or covered_pending is None:
        return result
    if covered & covered_pending:
        issues.append('snapshot_overlap')
    if (covered | covered_pending) - set(units):
        issues.append('snapshot_unit_missing')
    if check.get('coverage') == 'external' and (covered or covered_pending):
        issues.append('external_snapshot_contains_company')
    selected, pending = [], []
    start, end = _window(period, today)
    for unit in units.values():
        match = _scope_match(unit, scope, ident)
        if match is None:
            relevant_issued = (unit['_current'] if period == 'stock' else unit['_issued']
                               and (not unit['_activated'] or start <= unit['_activated'] <= end))
            if relevant_issued or unit['_pending']:
                issues.append('unit_scope_unknown')
            continue
        if not match:
            continue
        if period == 'stock':
            if unit['_issued']:
                selected.append(unit)
                if unit['_current'] or unit['id'] in covered or unit['id'] in covered_pending:
                    issues.extend(unit['_issues'])
        elif unit['_issued']:
            activated = unit['_activated']
            if not activated:
                issues.append('activation_date_unknown')
            elif start <= activated <= end:
                selected.append(unit)
        if unit['_pending']:
            pending.append(unit)
    for ident_ in covered | covered_pending:
        unit = units.get(ident_)
        if unit and _scope_match(unit, scope, ident) is not True:
            issues.append('snapshot_unit_scope_mismatch')
        if unit and ident_ in covered and (not unit['_issued'] or
                (unit['_activated'] and unit['_activated'] > _day(check['verified_on']))):
            issues.append('snapshot_active_date_conflict')
    if not _number(check.get('count')):
        return result
    source_count = check['count']
    if check.get('coverage') == 'total':
        if len(covered) + len(covered_pending) > source_count:
            issues.append('company_exceeds_verified_total')
        used = source_count
        for unit in selected:
            if unit['id'] in covered or unit['id'] in covered_pending:
                if period == 'stock' and unit['_released']:
                    if unit['_released'] > _day(check['verified_on']):
                        used -= 1
                    else:
                        issues.append('snapshot_contains_already_released')
                continue
            if period == 'stock' and not unit['_current']:
                continue
            if unit['_activated'] and unit['_activated'] > _day(check['verified_on']):
                used += 1
            else:
                issues.append('uncovered_existing_company_unit')
        for ident_ in covered_pending:
            unit = units.get(ident_)
            if unit and not unit['_pending'] and not unit['_issued']:
                issues.append('covered_pending_outcome_unverified')
        if result['_company_complete']:
            result['_other'] = source_count - len(covered) - len(covered_pending)
        result['_verified'] = source_count
    elif check.get('coverage') == 'external':
        company_count = sum(unit['_current'] if period == 'stock' else True for unit in selected)
        used = source_count + company_count
        result['_other'] = source_count
        if result['_company_complete']:
            result['_verified'] = used
        else:
            issues.append('company_inventory_incomplete')
    else:
        return result
    if used < 0:
        issues.append('negative_derived_total')
    uncovered_pending = [unit for unit in pending if unit['id'] not in covered_pending and unit['id'] not in covered]
    if pending and type(rule.get('pending_counts')) is not bool:
        issues.append('pending_policy_unknown')
    if covered_pending and rule.get('pending_counts') is False:
        issues.append('pending_coverage_policy_conflict')
    # Even when a carrier does not count pending activations yet, the company
    # reserves them for planning so the same remaining capacity is not assigned twice.
    reserved = len(uncovered_pending)
    issues[:] = list(dict.fromkeys(issues))
    evidence_issues = [issue for issue in issues if issue not in {
        'rule_unconfirmed', 'rule_not_effective', 'rule_limit_unknown', 'pending_policy_unknown'}]
    if not evidence_issues:
        result['used'], result['reserved'] = used, reserved
    if not issues:
        result['available'] = max(0, rule['limit'] - used - reserved)
    elif evidence_issues and not (check.get('coverage') == 'external'
                                  and set(evidence_issues) <= {'company_inventory_incomplete'}):
        result['_other'] = result['_verified'] = None
    return result


def _count_basis(value):
    value = value or {}
    complete = value.get('_company_complete', value.get('company_inventory_complete')) is True
    coverage = value.get('coverage')
    verified = value.get('_verified', value.get('verified_total'))
    other = value.get('_other', value.get('other_current'))
    return {'company': 'registered_units', 'company_inventory_complete': complete,
            'verified_total': ('total_including_company' if coverage == 'total' else
                               'external_plus_complete_company') if verified is not None else 'unknown',
            'other_current': ('explicit_external_verification' if coverage == 'external' else
                              'derived_from_complete_company') if other is not None else 'unknown',
            'verification_id': value.get('verification_id'),
            'notes': [] if complete else ['公司实名台账尚未确认完整，公司数量仅代表已登记记录，不推断未登记公司卡为零']}


def _operator_stock_evidence(operator, units, checks, today):
    """Read distribution evidence without inventing a per-issuer stock limit.

    The reconciliation logic is shared, but the absent limit is intentional.
    Its synthetic rule is never exposed as a quota condition or used for capacity.
    """
    display = _limit({'id': '', 'scope': 'operator', 'scope_id': operator['id'],
                      'period': 'stock', 'limit': None, 'pending_counts': True,
                      'confirmed': True, 'source': 'count-evidence-only',
                      'effective_from': today.isoformat()}, operator, units, checks, today)
    codes = [code for code in display['issues'] if code != 'rule_limit_unknown']
    return {'scope': 'operator', 'scope_id': operator['id'], 'period': 'stock',
            'verification_id': display['verification_id'], 'coverage': display['coverage'],
            'verified_on': display['_verification_on'], 'verified_total': display['_verified'],
            'other_current': display['_other'], 'reconciled_used': display['used'],
            'company_inventory_complete': display['_company_complete'], 'count_basis': _count_basis(display),
            'issues': codes, 'issue_labels': [_issue_label(code) for code in codes]}


def _applicable(rule, operator):
    scope, ident = rule.get('scope'), rule.get('scope_id', '')
    if scope == 'all':
        return True
    if scope == 'operator':
        return ident == operator['id']
    if scope == 'network':
        return _known_network(operator.get('network')) and ident == operator['network']
    if scope == 'channel':
        return _text(operator.get('channel_id')) and ident == operator.get('channel_id')
    return True


def evaluate_employee(employee, operators, units, verifications, applications, today, *, rules):
    """Return conservative counts, conditions, nullable capacity and traceable limits.

    Employee/issuer identities are stable strings supplied by the service.
    Unknown carrier mapping, rule or verification coverage never means zero.
    """
    current = _day(today)
    if not current:
        raise ValueError('评估日期不正确')
    if not isinstance(employee, dict):
        raise ValueError('员工资料格式不正确')
    op_map, issues = _unique(operators, 'operator')
    raw_units, unit_issues = _unique(units, 'unit')
    checks, check_issues = _unique(verifications, 'verification')
    apps, app_issues = _unique(applications, 'application')
    rule_map, rule_issues = _unique(rules, 'rule')
    issues += unit_issues + check_issues + app_issues + rule_issues
    if not _text(employee.get('id')):
        issues.append('employee_id_invalid')
    prepared = {}
    for ident, unit in raw_units.items():
        operator = op_map.get(unit.get('operator_id')) if isinstance(unit.get('operator_id'), str) else None
        if not operator:
            issues.append('unit_operator_unknown')
            operator = {}
        prepared[ident] = _unit_info(unit, operator, current)
    issues = list(dict.fromkeys(issues))
    consent = employee.get('consent_confirmed') is True
    age = age_on(employee.get('birth_date'), current) if consent else None
    lifetime = sum(unit['_issued'] for unit in prepared.values())
    occupied = sum(unit['_current'] for unit in prepared.values())
    pending = sum(unit['_pending'] for unit in prepared.values())
    output, global_results = [], []
    for operator in op_map.values():
        ident = operator['id']
        reasons, apply_dates, verify_dates = [], [], []
        for issue in issues:
            reasons.append(_reason(issue, '资料存在缺失或冲突，需核验后计算额度'))
        if employee.get('company_scope_complete', True) is not True:
            reasons.append(_reason('company_scope_incomplete', '公司卡资料范围不完整，不能据局部资料推算名下总额'))
        if employee.get('employment_status') == 'left':
            reasons.append(_reason('employee_left', '员工已离职，公司办卡需人工确认'))
        elif employee.get('employment_status') != 'active':
            reasons.append(_reason('employment_unknown', '在职状态待确认'))
        if not consent:
            reasons.append(_reason('consent_unknown', '尚未记录员工授权提供资料的依据'))
        gate = None
        age_effective = _day(operator.get('effective_from'))
        if (operator.get('age_confirmed') is not True or not _number(operator.get('min_age'))
                or not _text(operator.get('source')) or not age_effective or age_effective > current):
            reasons.append(_reason('age_rule_unknown', '运营商年龄门槛尚未确认'))
        elif age is None:
            reasons.append(_reason('birth_date_unknown', '出生日期缺失、无效或尚未经授权确认'))
        else:
            gate = threshold_on(employee.get('birth_date'), operator['min_age'])
            if not gate:
                reasons.append(_reason('age_rule_invalid', '年龄门槛或出生日期无效'))
            elif current < _day(gate):
                reasons.append(_reason('below_minimum_age', '尚未达到满' + str(operator['min_age']) + '周岁的年龄门槛', 'blocking'))
                apply_dates.append(gate)
        active_rules = [rule for rule in rule_map.values() if _applicable(rule, operator)]
        if not any(rule.get('scope') == 'all' and rule.get('period') == 'stock' for rule in active_rules):
            reasons.append(_reason('global_stock_rule_missing', '缺少覆盖所有运营商及虚商的总占用规则'))
        if not any(rule.get('scope') == 'operator' and rule.get('period') == 'day' for rule in active_rules):
            reasons.append(_reason('operator_day_rule_missing', '单运营商每日实名数量规则待确认'))
        if not _known_network(operator.get('network')) and any(rule.get('scope') == 'network' for rule in rule_map.values()):
            reasons.append(_reason('operator_network_unknown', '实际发卡网络待确认，不能排除网络合并限额'))
        if not _text(operator.get('channel_id')) and any(rule.get('scope') == 'channel' for rule in rule_map.values()):
            reasons.append(_reason('operator_channel_unknown', '办理渠道待确认，不能排除渠道限额'))
        limits = [_limit(rule, operator, prepared, checks, current) for rule in active_rules]
        for limit in limits:
            if limit['scope'] == 'all' and limit['period'] == 'stock':
                global_results.append(limit)
            for issue in limit['issues']:
                reasons.append(_reason(issue, _issue_label(issue)))
            if limit['_followup']:
                verify_dates.append(limit['_followup'])
            if limit['available'] == 0:
                reasons.append(_reason('capacity_exhausted', '当前规则范围内已无未分配名额', 'blocking'))
                _, end = _window(limit['period'], current)
                if end:
                    apply_dates.append((end + timedelta(days=1)).isoformat())
        related_apps = [row for row in apps.values() if row.get('operator_id') == ident]
        for app in related_apps:
            retry = _day(app.get('retry_on'))
            if app.get('retry_on') and (not retry or not app.get('retry_source')):
                reasons.append(_reason('retry_date_unconfirmed', '再次申请日期缺少运营商明确依据'))
            elif retry and app.get('retry_source') and retry > current:
                reasons.append(_reason('explicit_retry_wait', '尚未到运营商明确给出的再次申请日期', 'blocking'))
                apply_dates.append(retry.isoformat())
            if app.get('result') == 'failed' and app.get('failure_resolved') is not True and not (retry and app.get('retry_source')):
                reasons.append(_reason('failure_needs_resolution', '失败原因或再次办理条件待核实，不自动安排每天重试'))
        wait_days = operator.get('waiting_days')
        if operator.get('waiting_confirmed') is not True or not _number(wait_days):
            reasons.append(_reason('waiting_rule_unknown', '是否存在再次申请等待期尚未确认'))
        elif wait_days:
            basis = operator.get('waiting_basis')
            if basis == 'application':
                dates = [_day(row.get('applied_on')) for row in related_apps]
                if any(value is None for value in dates):
                    reasons.append(_reason('waiting_history_date_unknown', '等待期所需的申请日期不完整'))
            elif basis in ('activation', 'release'):
                key = '_activated' if basis == 'activation' else '_released'
                dates = [unit[key] for unit in prepared.values() if unit.get('operator_id') == ident]
            else:
                dates = []
                reasons.append(_reason('waiting_basis_unknown', '等待期起算依据尚未确认'))
            dates = [value for value in dates if value]
            if dates:
                retry = max(dates) + timedelta(days=wait_days)
                if retry > current:
                    reasons.append(_reason('configured_wait', '尚未达到已确认等待期', 'blocking'))
                    apply_dates.append(retry.isoformat())
        stock = next((limit for limit in limits if limit['scope'] == 'operator' and limit['period'] == 'stock'), None)
        count_evidence = _operator_stock_evidence(operator, prepared, checks, current)
        global_limit = next((value for value in limits if value['scope'] == 'all' and value['period'] == 'stock'), None)
        if (count_evidence['reconciled_used'] is not None and global_limit
                and global_limit['used'] is not None and count_evidence['reconciled_used'] > global_limit['used']):
            reasons.append(_reason('cross_scope_total_conflict', _issue_label('cross_scope_total_conflict')))
            count_evidence['issues'].append('cross_scope_total_conflict')
            count_evidence['issue_labels'].append(_issue_label('cross_scope_total_conflict'))
        unknown = any(reason['kind'] == 'unknown' for reason in reasons)
        blocked = any(reason['kind'] == 'blocking' for reason in reasons)
        status = 'ineligible' if blocked else 'needs_verification' if unknown else 'eligible'
        available = None if unknown or not limits else (0 if blocked else min(limit['available'] for limit in limits))
        own = [unit for unit in prepared.values() if unit.get('operator_id') == ident]
        counts = {'company_lifetime': sum(unit['_issued'] for unit in own),
                  'company_current': sum(unit['_current'] for unit in own),
                  'other_current': stock['_other'] if stock else count_evidence['other_current'],
                  'verified_total': stock['_verified'] if stock else count_evidence['verified_total'],
                  'pending': sum(unit['_pending'] for unit in own), 'estimated_available': available}
        stock_basis = _count_basis(stock) if stock else count_evidence['count_basis']
        unresolved = any(reason['code'] == 'failure_needs_resolution' for reason in reasons)
        next_date, action = None, None
        if unresolved:
            action = 'resolve'
        elif unknown:
            review_dates = verify_dates + apply_dates
            next_date = min(review_dates) if review_dates else None
            action = 'verify'
        elif blocked:
            # A stock cap without a confirmed release date has no promised application day.
            indefinite = any(limit['period'] == 'stock' and limit['available'] == 0 for limit in limits)
            next_date = max(apply_dates) if apply_dates and not indefinite else None
            action = 'apply' if next_date else 'verify'
        elif verify_dates:
            next_date, action = min(verify_dates), 'verify'
        verification_on = max((limit['_verification_on'] for limit in limits if limit['_verification_on']), default=None)
        public_limits = [dict({key: value for key, value in limit.items() if not key.startswith('_')},
                              issue_labels=[_issue_label(code) for code in limit['issues']]) for limit in limits]
        output.append({'operator_id': ident, 'status': status, 'status_label': STATUS_LABELS[status],
                       'reasons': reasons, 'estimated_available': available, 'age_threshold_on': gate,
                       'next_followup_on': next_date, 'followup_action': action,
                       'verification_on': verification_on, 'counts': counts,
                       'company_inventory_complete': stock_basis['company_inventory_complete'],
                       'count_basis': stock_basis, 'count_evidence': count_evidence, 'limits': public_limits})
    global_stock = next((row for row in global_results if row['_verified'] is not None), None)
    global_evidence = global_stock or next((row for row in global_results if row['_other'] is not None), None)
    total_available = None
    if output and global_stock and all(row['estimated_available'] is not None for row in output):
        total_available = min(global_stock['available'], sum(row['estimated_available'] for row in output))
    counts = {'company_lifetime': lifetime, 'company_current': occupied,
              'other_current': global_evidence['_other'] if global_evidence else None,
              'verified_total': global_stock['_verified'] if global_stock else None,
              'pending': pending, 'estimated_available': total_available}
    followups = [(row['next_followup_on'], row['followup_action']) for row in output if row['next_followup_on']]
    next_date, action = min(followups) if followups else (None, 'resolve' if any(row['followup_action'] == 'resolve' for row in output) else 'verify' if output else None)
    return {'employee_id': employee.get('id'), 'age_years': age,
            'company_lifetime': lifetime, 'company_current': occupied,
            'other_current': counts['other_current'], 'verified_total': counts['verified_total'],
            'pending_count': pending, 'estimated_available': total_available,
            'company_inventory_complete': _count_basis(global_evidence)['company_inventory_complete'],
            'count_basis': _count_basis(global_evidence),
            'next_followup_on': next_date, 'followup_action': action,
            'counts': counts, 'operators': output, 'issues': issues}
