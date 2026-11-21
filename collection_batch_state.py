"""Offline checkpoint for exact-handle public collection results; no collector runtime."""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile


MAX_HANDLES = 20000
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
MAX_COUNT = 10000000
PROFILE = re.compile(r'https://www\.douyin\.com/user/MS4w[A-Za-z0-9_-]{1,196}')
HANDLE = re.compile(r'[A-Za-z0-9_.-]{1,100}')
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})')


def _stamp(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError('observation requires a timezone-aware ISO timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('invalid observation timestamp') from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate checkpoint field')
        result[key] = value
    return result


class BatchState:
    def __init__(self, handles):
        if isinstance(handles, (str, bytes)):
            raise ValueError('handles must be an ordered collection of strings')
        self.handles = []
        self._records = {}
        for handle in handles:
            if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
                raise ValueError('invalid complete handle string')
            if handle not in self._records:
                self.handles.append(handle)
                self._records[handle] = dict(account=handle, profile='', success=None, last_attempt=None)
                if len(self.handles) > MAX_HANDLES:
                    raise ValueError('too many handles')

    @classmethod
    def open(cls, path, handles):
        expected = cls(handles)
        path = Path(path)
        if path.is_symlink():
            raise ValueError('checkpoint must not be a symlink')
        if not path.exists():
            return expected
        if not path.is_file() or path.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise ValueError('invalid checkpoint file')
        try:
            value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
            if (not isinstance(value, dict) or set(value) != {'version', 'handles', 'records'} or
                    type(value['version']) is not int or value['version'] != 1 or
                    value['handles'] != expected.handles or not isinstance(value['records'], list) or
                    len(value['records']) != len(expected.handles)):
                raise ValueError('checkpoint input or schema mismatch')
            for handle, record in zip(expected.handles, value['records']):
                if (not isinstance(record, dict) or set(record) != {'account', 'profile', 'success', 'last_attempt'} or
                        record['account'] != handle or not isinstance(record['profile'], str) or
                        (record['profile'] and not PROFILE.fullmatch(record['profile']))):
                    raise ValueError('invalid checkpoint record')
                if record['profile']:
                    expected.merge([dict(account=handle, status='resolved', url=record['profile'])])
                if record['success'] is not None:
                    row = expected._clean(record['success'])
                    if row['account'] != handle or row['status'] != 'success' or row['url'] != record['profile']:
                        raise ValueError('invalid checkpoint success')
                    expected.merge([row])
                if record['last_attempt'] is not None:
                    row = expected._clean(record['last_attempt'])
                    if row['account'] != handle:
                        raise ValueError('invalid checkpoint attempt')
                    expected.merge([row])
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError('invalid checkpoint contents') from None
        return expected

    def _clean(self, raw):
        if not isinstance(raw, dict):
            raise ValueError('observation must be an object')
        account, status = raw.get('account'), raw.get('status')
        if not isinstance(account, str) or account not in self._records:
            raise ValueError('observation account is outside this batch')
        if status not in ('success', 'failed', 'skipped', 'resolved'):
            raise ValueError('invalid observation status')
        row = dict(account=account, status=status)
        url = raw.get('url') or ''
        if not isinstance(url, str) or (url and not PROFILE.fullmatch(url)):
            raise ValueError('observation URL must be an exact canonical profile')
        if status in ('success', 'resolved') and not url:
            raise ValueError('confirmed observation requires a profile URL')
        if url:
            row['url'] = url
        observed = raw.get('observed_at')
        if observed not in (None, ''):
            _stamp(observed)
            row['observed_at'] = observed
        if status == 'success':
            count = raw.get('count')
            if type(count) is not int or not 0 <= count <= MAX_COUNT or 'observed_at' not in row or raw.get('error'):
                raise ValueError('success requires a sampled non-negative integer count')
            row['count'] = count
            if 'name' in raw:
                if not isinstance(raw['name'], str) or len(raw['name']) > 512:
                    raise ValueError('invalid public account name')
                row['name'] = raw['name']
        elif raw.get('count') is not None:
            raise ValueError('non-success observations cannot carry a count')
        if status in ('failed', 'skipped'):
            error = raw.get('error') or ''
            if not isinstance(error, str) or len(error) > 1024:
                raise ValueError('invalid failure code')
            row['error'] = error
        return row

    @staticmethod
    def _newer(candidate, previous):
        if previous is None:
            return True
        before, after = previous.get('observed_at'), candidate.get('observed_at')
        if not after:
            return not before
        return not before or _stamp(after) >= _stamp(before)

    def merge(self, observations):
        """Validate the whole merge before replacing state; never turn failures into counts."""
        if isinstance(observations, (str, bytes, dict)):
            raise ValueError('observations must be a collection of result rows')
        rows = [self._clean(raw) for raw in observations]
        staged = copy.deepcopy(self._records)
        changed, older = 0, 0
        for row in rows:
            record = staged[row['account']]
            before = copy.deepcopy(record)
            url = row.get('url', '')
            if url and record['profile'] and url != record['profile']:
                raise ValueError('conflicting confirmed profile URLs')
            if row['status'] in ('success', 'resolved'):
                record['profile'] = url
            previous = record['success']
            if row['status'] == 'success':
                if (previous and _stamp(row['observed_at']) == _stamp(previous['observed_at']) and
                        row['count'] != previous['count']):
                    raise ValueError('conflicting counts at the same sample time')
                if self._newer(row, previous):
                    record['success'] = row
                else:
                    older += 1
            if self._newer(row, record['last_attempt']):
                record['last_attempt'] = row
            if record != before:
                changed += 1
        self._records = staged
        return dict(received=len(rows), changed=changed, ignored_older=older)

    def profile(self, account):
        return self._records[account]['profile']

    def success(self, account):
        return copy.deepcopy(self._records[account]['success'])

    def pending_handles(self):
        return [account for account in self.handles if self._records[account]['success'] is None]

    def rows(self):
        result = []
        for account in self.handles:
            record = self._records[account]
            success, attempt = record['success'], record['last_attempt']
            best = success or attempt or {}
            result.append(dict(account=account, status=best.get('status', 'pending'),
                url=record['profile'], count=success['count'] if success else None,
                observed_at=best.get('observed_at', ''), name=best.get('name', ''),
                error='' if success else best.get('error', ''),
                last_attempt_status=attempt['status'] if attempt else '',
                last_attempt_at=attempt.get('observed_at', '') if attempt else '',
                last_error=attempt.get('error', '') if attempt else ''))
        return result

    def to_dict(self):
        return dict(version=1, handles=list(self.handles),
                    records=[copy.deepcopy(self._records[account]) for account in self.handles])

    def save(self, path):
        """Replace a checkpoint atomically, retaining the previous file on write failure."""
        path = Path(path)
        if path.is_symlink():
            raise ValueError('checkpoint must not be a symlink')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                    prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(self.to_dict(), stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
