"""Read-only physical-card ledger from explicit links and exact owner names.

Quota release is never a card cancellation status. Candidate checks deliberately
match static/phone-cards.js cardReady, with scope and unfinished-task guards.
"""
import office_scope

COUNTS = ('registered', 'unclosed', 'closed', 'candidates', 'pending_cancel', 'needs_review')
UNCLOSED = {'正常', '欠费', '停机', '停用', '待注销'}
CARD_COLUMNS = 'id,phone,carrier,status,keeper,location,code_state,return_state'


def _chunks(values, size=200):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def name_associations(db, actor, employees, allowed_cards=None):
    """Current exact-name view, never a stored quota or historical ownership row.

    The user explicitly adopted legacy ``owner`` as the real-name matching
    field. Global ambiguity and historical manual claims still win, including
    claims outside this actor's scope. Hidden cards contribute only a boolean.
    """
    names = {row['name'] for row in employees if row['name']}
    unique, cards, hidden, conflicts = {}, [], set(), set()
    if allowed_cards is None:
        visible, scope_parameters = '1=1', ()
    elif getattr(db, 'scope_enabled', False):
        visible = ('EXISTS(SELECT 1 FROM phone_card_offices s JOIN user_office_grants g ON g.office_id=s.office_id '
                   'JOIN offices o ON o.id=g.office_id WHERE s.card_id=c.id AND g.user_id=? AND o.active=1)')
        scope_parameters = (actor['id'],)
    else:
        visible, scope_parameters = '1=0', ()
    unclaimed = ('NOT EXISTS(SELECT 1 FROM employee_card_links l WHERE l.card_id=c.id OR l.phone_snapshot=c.phone) '
                 'AND NOT EXISTS(SELECT 1 FROM employee_card_units u WHERE u.current_card_id=c.id)')
    for chunk in _chunks(names):
        placeholders = ','.join('?' for _ in chunk)
        unique.update((row['name'], row['n'] == 1) for row in db.execute(
            'SELECT name,COUNT(*) AS n FROM employee_card_employees WHERE name IN (' + placeholders + ') GROUP BY name', tuple(chunk)))
        selected = [name for name in chunk if unique.get(name)]
        if not selected:
            continue
        # Return only already-known employee names as conflict flags, never a
        # claimant ID, hidden card, phone or relationship from another scope.
        conflict = ('EXISTS(SELECT 1 FROM employee_card_links l LEFT JOIN employee_card_units u ON u.id=l.unit_id '
                    'WHERE (l.card_id=c.id OR l.phone_snapshot=c.phone) AND (u.employee_id IS NULL OR u.employee_id<>e.id)) '
                    'OR EXISTS(SELECT 1 FROM employee_card_units u WHERE u.current_card_id=c.id AND u.employee_id<>e.id)')
        conflicts.update(row[0] for row in db.execute(
            'SELECT DISTINCT c.owner FROM phone_cards c JOIN employee_card_employees e ON e.name=c.owner '
            'WHERE c.owner IN (' + ','.join('?' for _ in selected) + ') AND (' + conflict + ')', tuple(selected)))
        # These names were checked against all employees, including hidden
        # ones; no per-card employee lookup is needed.
        predicate = ('c.owner IN (' + ','.join('?' for _ in selected) + ') AND c.owner<>\'\' AND ' + unclaimed)
        columns = ','.join('c.' + field for field in CARD_COLUMNS.split(',')) + ',c.owner'
        cards.extend(dict(row) for row in db.execute('SELECT ' + columns + ' FROM phone_cards c WHERE ' + predicate +
                                                     ' AND ' + visible + ' ORDER BY c.id', tuple(selected) + scope_parameters))
        if allowed_cards is not None:
            hidden.update(row[0] for row in db.execute('SELECT DISTINCT c.owner FROM phone_cards c WHERE ' + predicate +
                                                       ' AND NOT (' + visible + ')', tuple(selected) + scope_parameters))
    by_name = {row['name']: row['id'] for row in employees if unique.get(row['name'])}
    associations = [dict(card, employee_id=by_name[card['owner']]) for card in cards]
    # Office assignment is part of the dependency, even if both old and new
    # offices remain authorized. This metadata is internal to version hashing.
    scopes = {}
    if getattr(db, 'scope_enabled', False):
        for chunk in _chunks({card['id'] for card in associations}):
            for row in db.execute('SELECT card_id,office_id FROM phone_card_offices WHERE card_id IN (' + ','.join('?' for _ in chunk) + ') ORDER BY card_id,office_id', tuple(chunk)):
                scopes.setdefault(row[0], []).append(row[1])
    for card in associations:
        card['_office_ids'] = scopes.get(card['id'], [])
    metadata = [dict(employee_id=row['id'], name=row['name'], unique=unique.get(row['name'], False),
                     association_complete=unique.get(row['name'], False) and row['name'] not in conflicts,
                     scope_incomplete=row['name'] in hidden,
                     issues=(['姓名无法唯一确认，未自动关联手机卡'] if not unique.get(row['name']) else
                             ['卡主文字与既有实名关系不一致，待核对'] if row['name'] in conflicts else []))
                for row in employees]
    return associations, metadata


def read_context(db, actor, bundle, allowed_cards=None, employee_ids=None):
    """Read only explicitly linked, authorized cards and their related accounts.

    Hidden account rows contribute only a boolean incompleteness flag per
    already-authorized phone; their IDs, names and other values never leave SQL.
    """
    employees = ({row['id'] for row in bundle['employees']} if employee_ids is None else set(employee_ids))
    ids = {link['card_id'] for link in bundle['links']
           if link['employee_id'] in employees and link['card_id'] is not None}
    ids.update(unit['current_card_id'] for unit in bundle['units']
               if unit['employee_id'] in employees and unit['current_card_id'] is not None)
    ids.update(card['id'] for card in bundle.get('name_cards', []) if card['employee_id'] in employees)
    if allowed_cards is not None:
        ids &= set(allowed_cards)
    cards = {}
    for chunk in _chunks(ids):
        rows = db.execute('SELECT ' + CARD_COLUMNS + ' FROM phone_cards WHERE id IN (' + ','.join('?' for _ in chunk) + ')', tuple(chunk))
        cards.update((row['id'], dict(row)) for row in rows)
    phones = {card['phone'] for card in cards.values() if card['phone']}
    accounts, incomplete = {}, set()
    scoped = getattr(db, 'scope_enabled', False) and not office_scope.global_scope(actor)
    visible = ("EXISTS(SELECT 1 FROM account_offices s JOIN user_office_grants g ON g.office_id=s.office_id "
               "JOIN offices o ON o.id=g.office_id WHERE s.account_id=a.id AND g.user_id=? AND o.active=1)")
    for chunk in _chunks(phones):
        predicate = 'a.registration_phone IN (' + ','.join('?' for _ in chunk) + ')'
        parameters = tuple(chunk)
        if scoped:
            parameters += (actor['id'],)
            hidden = db.execute('SELECT DISTINCT a.registration_phone FROM accounts a WHERE ' + predicate + ' AND NOT ' + visible, parameters)
            incomplete.update(row[0] for row in hidden)
            predicate += ' AND ' + visible
        rows = db.execute('SELECT a.id,a.registration_phone,a.status,a.account_type,a.card_stability,a.password_state FROM accounts a WHERE ' + predicate, parameters)
        for row in rows:
            value = dict(row)
            accounts.setdefault(value['registration_phone'], []).append(value)
    return dict(cards=cards, accounts_by_phone=accounts, incomplete_account_phones=incomplete)


def _candidate(card, accounts, account_scope_complete):
    reasons = []
    if not account_scope_complete:
        return False, ['关联账号的授权范围不完整，需有完整权限的人员核对']
    if not accounts:
        reasons.append('未关联同注册手机号的账号，无法核对条件')
    else:
        # Exact cardReady predicates: only literal 封禁 is excluded; a blocked
        # small account may satisfy related.some(小号), as in the existing UI.
        active = [account for account in accounts if account.get('status') != '封禁']
        if not active:
            reasons.append('没有未封禁的关联账号')
        if not any(account.get('account_type') == '小号' for account in accounts):
            reasons.append('未登记关联小号')
        if sum(account.get('account_type') == '大号' for account in accounts) > 1:
            reasons.append('关联多个大号，需核对')
        if any(not account.get('status') for account in accounts):
            reasons.append('关联账号状态资料未核对')
        if any(account.get('account_type') not in ('大号', '小号') for account in active):
            reasons.append('未封禁账号的类型尚未明确')
        if any(account.get('card_stability') != '已稳定' for account in active):
            reasons.append('未封禁账号尚未全部登记为已稳定')
        if any(account.get('password_state') != '已修改' for account in active):
            reasons.append('未封禁账号尚未全部登记为已改密')
    code = card.get('code_state')
    returning = card.get('return_state')
    if code == '待接码':
        reasons.append('仍有待接码事项，先处理并人工核对')
    elif code == '暂无法接码':
        reasons.append('当前暂无法接码，处理事项尚未完成')
    elif code not in ('无待办', '已完成'):
        reasons.append('接码事项状态未核对，不能当作已完成')
    if returning == '待归还':
        reasons.append('仍有待归还事项，先处理并人工核对')
    elif returning not in ('不需归还', '已归还'):
        reasons.append('归还事项状态未核对，不能当作已完成')
    if reasons:
        return False, reasons
    return True, ['符合现有稳定、改密等业务核对条件；仍需人工确认注销']


def derive(employee_id, bundle, context, *, operator_id='', scope_complete=True):
    operators = {row['id']: row for row in bundle['operators']}
    name = operators.get(operator_id, {}).get('name', '所选登记运营商') if operator_id else '全部登记运营商'
    scope_label = name + ' · 已明确关联的公司卡（含历史补换卡），不等于名下核验总数；待核对可与未销重叠'
    if not scope_complete:
        return dict({key: None for key in COUNTS}, scope_complete=False,
                    scope_label='当前办公室权限范围不完整，卡数量及明细已隐藏',
                    association_complete=False, name_matched_count=None,
                    association_issues=['部分手机卡超出当前授权范围']), []
    units = {row['id']: row for row in bundle['units'] if row['employee_id'] == employee_id}
    grouped = {}
    for index, link in enumerate(bundle['links']):
        if link['employee_id'] != employee_id:
            continue
        unit = units.get(link['unit_id'])
        if operator_id and (not unit or unit['operator_id'] != operator_id):
            continue
        key = ('card', link['card_id']) if link['card_id'] is not None else ('missing', link.get('id', index))
        grouped.setdefault(key, []).append(link)
    result = []
    for links in grouped.values():
        links = sorted(links, key=lambda row: (row.get('linked_on', ''), row.get('id', '')))
        link = links[0]
        unit = units.get(link['unit_id'])
        card = context['cards'].get(link['card_id'])
        operator = operators.get(unit['operator_id']) if unit else None
        reasons, candidate, state = [], False, 'review'
        status = card.get('status', '') if card else ''
        duplicate_conflict = len({row['unit_id'] for row in links}) > 1 or len({row.get('phone_snapshot', '') for row in links}) > 1
        if duplicate_conflict:
            reasons.append('同一卡存在不一致的历史实名关联，需人工核对')
        if not unit or not operator:
            reasons.append('实名关联的登记运营商资料待核对')
        if not card:
            reasons.append('原手机卡记录已缺失，不能推断已销或未销')
        elif not card.get('phone') or not link.get('phone_snapshot') or card['phone'] != link['phone_snapshot']:
            reasons.append('当前号码与实名关联原记录不一致或未登记，需核对')
        elif status != '已注销' and status not in UNCLOSED:
            reasons.append('手机卡状态未核对或为历史文字，不能推断已销或未销')
        elif not reasons:
            state = 'closed' if status == '已注销' else 'unclosed'
            if state == 'unclosed':
                candidate, reasons = _candidate(card, context['accounts_by_phone'].get(card['phone'], []),
                                                card['phone'] not in context['incomplete_account_phones'])
            else:
                reasons = ['手机卡已明确登记为已注销，不再列注销候选']
        result.append(dict(card_id=link['card_id'], phone_snapshot=link.get('phone_snapshot', ''),
                           phone=card['phone'] if card else '', carrier=card.get('carrier', '') if card else '',
                           operator_id=unit['operator_id'] if unit else '', operator_name=operator['name'] if operator else '待核对',
                           card_status=status, ledger_state=state, candidate=candidate, candidate_reasons=reasons,
                           pending_cancel=state == 'unclosed' and status == '待注销',
                           needs_review=state == 'review' or state == 'unclosed' and not candidate,
                           linked_on=link.get('linked_on', ''), activated_on=unit.get('activated_on', '') if unit else '',
                           keeper=card.get('keeper', '') if card else '', location=card.get('location', '') if card else '',
                           association_source='explicit', association_label='人工实名关联'))
    # Auto rows have no known issuer, activation date, unit or linked-on date.
    # They belong only to the physical ledger, never to a guessed issuer group.
    if not operator_id:
        existing_ids = {row['card_id'] for row in result if row['card_id'] is not None}
        existing_phones = {row['phone_snapshot'] for row in result if row['phone_snapshot']}
        for auto in bundle.get('name_cards', []):
            if auto['employee_id'] != employee_id or auto['id'] in existing_ids or auto['phone'] in existing_phones:
                continue
            card = context['cards'].get(auto['id'])
            reasons, candidate, state = [], False, 'review'
            status = card.get('status', '') if card else ''
            if not card or not card.get('phone') or card['phone'] != auto['phone']:
                reasons = ['当前手机卡号码缺失或已变化，需核对']
            elif status != '已注销' and status not in UNCLOSED:
                reasons = ['手机卡状态未核对或为历史文字，不能推断已销或未销']
            else:
                state = 'closed' if status == '已注销' else 'unclosed'
                if state == 'unclosed':
                    candidate, reasons = _candidate(card, context['accounts_by_phone'].get(card['phone'], []),
                                                    card['phone'] not in context['incomplete_account_phones'])
                else:
                    reasons = ['手机卡已明确登记为已注销，不再列注销候选']
            result.append(dict(card_id=auto['id'], phone_snapshot=auto['phone'], phone=card['phone'] if card else '',
                               carrier=card.get('carrier', '') if card else '', operator_id='', operator_name='待确认',
                               card_status=status, ledger_state=state, candidate=candidate, candidate_reasons=reasons,
                               pending_cancel=state == 'unclosed' and status == '待注销',
                               needs_review=state == 'review' or state == 'unclosed' and not candidate,
                               linked_on='', activated_on='', keeper=card.get('keeper', '') if card else '',
                               location=card.get('location', '') if card else '',
                               association_source='name', association_label='卡主姓名自动关联'))
            existing_ids.add(auto['id'])
            if auto['phone']:
                existing_phones.add(auto['phone'])
    association_issues = [issue for item in bundle.get('name_match_metadata', []) if item['employee_id'] == employee_id
                          for issue in item['issues']]
    association_complete = all(item.get('association_complete', True) for item in bundle.get('name_match_metadata', [])
                               if item['employee_id'] == employee_id)
    counts = dict(registered=len(result), unclosed=sum(row['ledger_state'] == 'unclosed' for row in result),
                  closed=sum(row['ledger_state'] == 'closed' for row in result),
                  candidates=sum(row['candidate'] for row in result), pending_cancel=sum(row['pending_cancel'] for row in result),
                  needs_review=sum(row['needs_review'] for row in result), scope_complete=True, scope_label=scope_label,
                  association_complete=association_complete,
                  name_matched_count=sum(row['association_source'] == 'name' for row in result),
                  association_issues=association_issues)
    if not association_complete:
        # Authorized, explicit rows remain inspectable. Ambiguity is not a
        # permission error, but the total cannot be presented as a real zero.
        counts.update({key: None for key in COUNTS})
        counts.update(name_matched_count=None, scope_label='卡主姓名对应实名关系尚未完整核对，完整卡数量待核对；已确认人工关联仍可查看')
    return counts, result


def aggregate(ledgers, scope_label):
    scoped = all(row['scope_complete'] for row in ledgers)
    associated = all(row.get('association_complete', True) for row in ledgers)
    complete = scoped and associated
    return dict({key: sum(row[key] for row in ledgers) if complete else None for key in COUNTS},
                scope_complete=scoped, association_complete=associated, scope_label=scope_label,
                name_matched_count=sum(row.get('name_matched_count', 0) for row in ledgers) if complete else None,
                association_issues=list(dict.fromkeys(issue for row in ledgers for issue in row.get('association_issues', []))))


def matches(ledger, state):
    key = {'unclosed': 'unclosed', 'closed': 'closed', 'candidate': 'candidates',
           'pending_cancel': 'pending_cancel', 'review': 'needs_review'}.get(state)
    if not key:
        return True
    if state == 'review' and (not ledger['scope_complete'] or not ledger.get('association_complete', True)):
        return True
    return ledger[key] is not None and ledger[key] > 0
