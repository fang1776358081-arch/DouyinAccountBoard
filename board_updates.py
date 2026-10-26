"""Cached board version so unchanged listeners wait without a database connection."""
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
    with _condition:
        _pending = update
        _version = update['version']
        _condition.notify_all()


def notify_account(db, account_id):
    notify_office_move(db, account_id)


def notify_reload(db):
    global _pending, _version
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
