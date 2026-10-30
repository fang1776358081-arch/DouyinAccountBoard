"""Lossless, opt-in wire deduplication for the accounts page.

The browser already reads attached account access/card placement records and
falls back to the workflow arrays.  Its version-lock graph likewise starts
with workflow.units, then fills missing relations from those attachments.
Only proven duplicates are removed here, after authorization and enrichment.
Unchanged branches are shared; callers must treat the returned snapshot as
read-only.  The original board and its nested records are never modified.
"""
import json


def _identity(value):
    # Keep string and integer identities distinct, as the existing wire does.
    if type(value) not in (str, int):
        return None
    return type(value), value


def _same(left, right):
    # Python considers True == 1; JSON clients do not.  Be conservative for
    # malformed/partially hydrated copies, while sharing objects is O(1).
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _index(rows, key):
    if not isinstance(rows, list):
        return None
    result, browser_ids = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        ident = _identity(row.get(key))
        if ident is None or ident in result or str(row[key]) in browser_ids:
            return None
        result[ident] = row
        browser_ids.add(str(row[key]))
    return result


def _attachments_cover(rows, owners, relation_key, attachment):
    if not isinstance(rows, list):
        return False
    index = _index(owners, 'id')
    if index is None:
        return False
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            return False
        ident = _identity(row.get(relation_key))
        if ident is None or ident in seen:
            return False
        seen.add(ident)
        owner = index.get(ident)
        if owner is None or not isinstance(owner.get(attachment), dict) or not _same(owner[attachment], row):
            return False
    return True


def compact(board):
    """Return an equivalent accounts-page snapshot without duplicate records.

    Missing, redacted, mismatched or ambiguous source/attachment relationships
    retain their original fields.  Default API responses do not call this.
    No business fields, permissions, version tokens or sync cursor are changed.
    """
    if not isinstance(board, dict):
        raise TypeError('board must be a dictionary')
    wire = dict(board)
    workflow = board.get('device_workflow')
    units = _index(workflow.get('units'), 'id') if isinstance(workflow, dict) else None
    devices = board.get('phone_devices')
    if isinstance(devices, dict) and isinstance(devices.get('devices'), list):
        device_rows = []
        for device in devices['devices']:
            if not isinstance(device, dict):
                device_rows.append(device)
                continue
            row = dict(device)
            # These derived fields have no client consumers.  Keep state,
            # attention and counts, which the existing resource pages display.
            for key in ('account_ids', 'occupancy', 'suggested_state'):
                row.pop(key, None)
            history = row.get('unit_history')
            if units is not None and isinstance(history, list) and all(
                    isinstance(unit, dict) and _identity(unit.get('id')) in units
                    and _same(units[_identity(unit.get('id'))], unit) for unit in history):
                row.pop('unit_history', None)
            device_rows.append(row)
        wire['phone_devices'] = {**devices, 'devices': device_rows}
    if isinstance(workflow, dict):
        flow = dict(workflow)
        if _attachments_cover(flow.get('access'), board.get('accounts'), 'account_id', 'device_access'):
            flow.pop('access', None)
        cards = board.get('phone_cards')
        card_rows = cards.get('cards') if isinstance(cards, dict) else None
        if _attachments_cover(flow.get('placements'), card_rows, 'card_id', 'placement'):
            flow.pop('placements', None)
        if isinstance(flow.get('events'), list):
            events = []
            for event in flow['events']:
                row = event
                if isinstance(event, dict) and isinstance(event.get('details'), dict):
                    try:
                        detail = json.loads(event.get('detail'))
                    except (ValueError, TypeError):
                        detail = None
                    if isinstance(detail, dict) and _same(detail, event['details']):
                        row = {key: value for key, value in event.items() if key != 'detail'}
                events.append(row)
            flow['events'] = events
        wire['device_workflow'] = flow
    return wire
