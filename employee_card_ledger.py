"""Read-only physical-card ledger derived from explicit employee card links.

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
                    scope_label='当前办公室权限范围不完整，卡数量及明细已隐藏'), []
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
                           keeper=card.get('keeper', '') if card else '', location=card.get('location', '') if card else ''))
    counts = dict(registered=len(result), unclosed=sum(row['ledger_state'] == 'unclosed' for row in result),
                  closed=sum(row['ledger_state'] == 'closed' for row in result),
                  candidates=sum(row['candidate'] for row in result), pending_cancel=sum(row['pending_cancel'] for row in result),
                  needs_review=sum(row['needs_review'] for row in result), scope_complete=True, scope_label=scope_label)
    return counts, result


def aggregate(ledgers, scope_label):
    complete = all(row['scope_complete'] for row in ledgers)
    return dict({key: sum(row[key] for row in ledgers) if complete else None for key in COUNTS},
                scope_complete=complete, scope_label=scope_label)


def matches(ledger, state):
    key = {'unclosed': 'unclosed', 'closed': 'closed', 'candidate': 'candidates',
           'pending_cancel': 'pending_cancel', 'review': 'needs_review'}.get(state)
    if not key:
        return True
    if state == 'review' and not ledger['scope_complete']:
        return True
    return ledger[key] is not None and ledger[key] > 0
