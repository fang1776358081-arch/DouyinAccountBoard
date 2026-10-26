"""Cached board version so unchanged listeners wait without a database connection.

Durable changes already land in operations_history inside the save transaction.
This module can read them back after the process forgets its last in-memory notice.
"""
import json
import threading
import time
import operations

_condition = threading.Condition()
_version = None
_pending = None


def recipients(actor, update):
    if not actor or not isinstance(update, dict):
        return False
    if actor.get('role') in ('owner', 'admin'):
        return True
    account = update.get('account') if isinstance(update.get('account'), dict) else {}
    offices = account.get('office_ids') or []
    if not offices:
        return True
    return bool(set(offices) & set(actor.get('office_ids') or []))


def current():
    with _condition:
        return _version


def publish(version):
    global _version
    with _condition:
        if version != _version:
            _version = version
            _condition.notify_all()


def notify_office_move(db, account_id, previous_offices=None, next_offices=None):
    global _pending, _version
    row = db.execute('SELECT * FROM accounts WHERE id=?', (str(account_id),)).fetchone()
    versions = operations.versions(db)
    account = dict(row)
    update = {'account': account, 'version': operations.token(versions), 'record_version': versions.get('account:'+str(account_id))}
    if previous_offices is not None and next_offices is not None:
        previous = [str(item) for item in previous_offices]
        nxt = [str(item) for item in next_offices]
        if set(previous) != set(nxt):
            account['office_ids'] = nxt
            removed = [office for office in previous if office not in set(nxt)]
            added = [office for office in nxt if office not in set(previous)]
            if removed:
                update['removed_office_ids'] = removed
            if added:
                update['added_office_ids'] = added
            import uuid
            db.execute("INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, int(time.time()), None, '同步', 'move', 'sync:office', None,
                        operations.encoded(dict(id=str(account_id), previous=previous, next=nxt, account=account))))
    with _condition:
        _pending = update
        _version = update['version']
        _condition.notify_all()


def notify_account(db, account_id):
    notify_office_move(db, account_id)


def note_grant_change(db, before, after):
    """An office-grant change invalidates cached boards. An unchanged grant does not."""
    if {str(item) for item in before or []} == {str(item) for item in after or []}:
        return
    notify_reload(db)


def notify_reload(db):
    global _pending, _version
    import uuid
    db.execute("INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
               (uuid.uuid4().hex, int(time.time()), None, '同步', 'reload', 'sync:reload', None, None))
    update = {'reload': True, 'version': operations.token(operations.versions(db))}
    with _condition:
        _pending = update
        _version = update['version']
        _condition.notify_all()


def _visible(actor, update):
    removed = list(update.get('removed_office_ids') or [])
    added = list(update.get('added_office_ids') or [])
    if not removed and not added:
        return update if recipients(actor, update) else None
    account = dict(update.get('account') or {})
    delivered = {'version': update.get('version'), 'record_version': update.get('record_version')}
    offices = set(account.get('office_ids') or [])
    granted = set(actor.get('office_ids') or [])
    if actor.get('role') in ('owner', 'admin') or granted & offices:
        delivered['account'] = account
        return delivered
    if granted & set(removed):
        delivered['removed'] = account.get('id')
        return delivered
    return None


def _office_move_change(cursor, move, actor):
    if not isinstance(move, dict) or not isinstance(actor, dict):
        return None
    previous = {str(item) for item in move.get('previous') or [] if isinstance(item, str)}
    nxt = {str(item) for item in move.get('next') or [] if isinstance(item, str)}
    account = move.get('account') if isinstance(move.get('account'), dict) else None
    ident = str(move.get('id') or (account or {}).get('id') or '')
    if not ident:
        return None
    granted = {str(item) for item in actor.get('office_ids') or []}
    entity = 'account:' + ident
    if actor.get('role') in ('owner', 'admin'):
        if not account:
            return None
        return dict(cursor=cursor, entity=entity, kind='change', after=account,
                    record_version=operations.token(account), offices=sorted(nxt))
    if granted & previous and not (granted & nxt):
        return dict(cursor=cursor, entity=entity, kind='remove', after=None, record_version=None, offices=sorted(previous))
    if granted & nxt and account:
        return dict(cursor=cursor, entity=entity, kind='change', after=account,
                    record_version=operations.token(account), offices=sorted(nxt))
    return None


def change_visible(actor, offices):
    if not isinstance(actor, dict) or not isinstance(offices, list):
        return False
    if actor.get('role') in ('owner', 'admin'):
        return True
    granted = {str(item) for item in actor.get('office_ids') or []}
    return bool(granted & {str(item) for item in offices})


def since(db, cursor, actor):
    """Return committed changes after cursor. A missing cursor or a reload marker is a full reload."""
    if type(cursor) is not int or cursor < 0:
        raise ValueError('同步记号不正确')
    if cursor != 0 and not db.execute('SELECT 1 FROM operations_history WHERE id=?', (cursor,)).fetchone():
        latest = db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
        return {'reload': True, 'cursor': latest}
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        query = 'SELECT id,entity,after_json,scope_offices FROM operations_history WHERE id>? ORDER BY id'
    else:
        query = 'SELECT id,entity,after_json,NULL FROM operations_history WHERE id>? ORDER BY id'
    changes = []
    latest = cursor
    for row in db.execute(query, (cursor,)):
        latest = row[0]
        if row[1] == 'sync:reload':
            return {'reload': True, 'cursor': latest}
        if row[1] == 'sync:office':
            move = json.loads(row[2]) if row[2] else None
            delivered = _office_move_change(row[0], move, actor)
            if delivered:
                changes.append(delivered)
            continue
        after = json.loads(row[2]) if row[2] else None
        try:
            offices = json.loads(row[3]) if row[3] else []
        except (json.JSONDecodeError, TypeError, ValueError):
            offices = []
        if not isinstance(offices, list) or not all(isinstance(item, str) for item in offices):
            offices = []
        item = dict(cursor=row[0], entity=row[1], kind='remove' if after is None else 'change',
                    after=after if isinstance(after, dict) else None,
                    record_version=operations.token(after) if isinstance(after, dict) else None,
                    offices=[str(office) for office in offices])
        if change_visible(actor, item['offices']):
            changes.append(item)
    return {'changes': changes, 'cursor': latest}


def wait(known, actor, seconds=1):
    with _condition:
        deadline = time.monotonic() + seconds
        while _version == known:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'version': _version}
            _condition.wait(remaining)
        update = _pending
    if update and update.get('version') != known:
        visible = _visible(actor, update)
        if visible:
            return visible
    return {'version': current()}
