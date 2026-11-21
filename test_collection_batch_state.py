"""Synthetic-only checkpoint regressions: no network, profile, session, or live accounts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collection_batch_state import BatchState


URL = 'https://www.douyin.com/user/MS4wSyntheticBatch'
OTHER = URL + 'Other'
EARLY = '2026-11-19T09:00:00+08:00'
NOW = '2026-11-20T09:00:00+08:00'
LATER = '2026-11-20T10:00:00+08:00'


def success(account='00123', count=0, observed_at=NOW, **changes):
    return dict(account=account, status='success', url=URL, count=count, observed_at=observed_at, **changes)


class BatchStateTest(unittest.TestCase):
    def test_ordered_exact_handles_deduplicate_without_losing_zeroes(self):
        state = BatchState(['00123', 'other', '00123', '123'])
        self.assertEqual(state.pending_handles(), ['00123', 'other', '123'])
        state.merge([success()])
        self.assertEqual(state.success('00123')['count'], 0)
        self.assertEqual(state.pending_handles(), ['other', '123'])
        self.assertIsNone(state.success('123'))
        self.assertEqual(state.profile('00123'), URL)
        self.assertEqual(state.rows()[0]['count'], 0)
        for handles in ('00123', [123], [' 00123'], ['00123 '], ['']):
            with self.subTest(handles=handles), self.assertRaises(ValueError):
                BatchState(handles)

    def test_success_duplicate_is_idempotent_and_same_time_conflict_is_atomic(self):
        state = BatchState(['00123', 'other'])
        state.merge([success()])
        before = state.to_dict()
        self.assertEqual(state.merge([success()])['changed'], 0)
        with self.assertRaises(ValueError):
            state.merge([dict(account='other', status='resolved', url=URL), success(count=1)])
        self.assertEqual(state.to_dict(), before)

    def test_unknown_account_and_conflicting_profile_never_partially_merge(self):
        state = BatchState(['00123', 'other'])
        state.merge([dict(account='00123', status='resolved', url=URL)])
        before = state.to_dict()
        for bad in (dict(account='outside', status='failed'),
                    dict(account='00123', status='resolved', url=OTHER),
                    {**success(), 'url': OTHER}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                state.merge([dict(account='other', status='failed', error='SearchUnavailable'), bad])
            self.assertEqual(state.to_dict(), before)

    def test_profile_seed_is_pending_and_failure_url_is_not_a_verified_mapping(self):
        state = BatchState(['00123', 'other'])
        state.merge([dict(account='00123', status='resolved', url=URL),
                     dict(account='other', status='failed', url=OTHER, error='ProfileUnavailable')])
        self.assertEqual(state.profile('00123'), URL)
        self.assertEqual(state.profile('other'), '')
        self.assertEqual(state.pending_handles(), ['00123', 'other'])
        self.assertEqual([r['count'] for r in state.rows()], [None, None])

    def test_partial_checkpoint_resume_keeps_success_when_remaining_are_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'checkpoint.json'
            state = BatchState.open(path, ['00123', 'other', 'third'])
            state.merge([success(), dict(account='other', status='failed', error='SearchNeedsVerification'),
                         dict(account='third', status='skipped', count=None, error='SearchNeedsVerification')])
            state.save(path)
            resumed = BatchState.open(path, ['00123', 'other', 'third'])
            self.assertEqual(resumed.to_dict(), state.to_dict())
            self.assertEqual(resumed.pending_handles(), ['other', 'third'])
            resumed.merge([success(account='other', count=17, observed_at=LATER)])
            resumed.save(path)
            self.assertEqual(BatchState.open(path, ['00123', 'other', 'third']).pending_handles(), ['third'])
            with self.assertRaises(ValueError):
                BatchState.open(path, ['other', '00123', 'third'])

    def test_later_failure_keeps_previous_success_and_older_reports_cannot_roll_back(self):
        state = BatchState(['00123'])
        state.merge([success(count=18)])
        state.merge([dict(account='00123', status='failed', observed_at=LATER, error='LoginRequired')])
        self.assertEqual(state.merge([success(count=3, observed_at=EARLY)])['ignored_older'], 1)
        row = state.rows()[0]
        self.assertEqual((row['status'], row['count'], row['observed_at']), ('success', 18, NOW))
        self.assertEqual((row['last_attempt_status'], row['last_error']), ('failed', 'LoginRequired'))
        self.assertEqual(state.pending_handles(), [])

    def test_count_url_and_sample_time_validation(self):
        state = BatchState(['00123'])
        bad_rows = [{**success(), 'count': value} for value in (True, -1, 1.5, '0', None, 10000001)]
        bad_rows += [{**success(), 'observed_at': value} for value in ('', None, '2026-11-20T09:00:00', 'invalid')]
        bad_rows += [{**success(), 'url': value} for value in ('', URL + '/', URL + '?share=synthetic',
                    'http://www.douyin.com/user/MS4wSynthetic', 'https://evil.invalid/user/MS4wSynthetic')]
        bad_rows += [dict(account='00123', status=status, count=0) for status in ('failed', 'skipped', 'resolved')]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                state.merge([row])
        self.assertEqual(state.pending_handles(), ['00123'])

    def test_untimed_conflict_failure_remains_retryable_without_fake_timestamp_or_count(self):
        state = BatchState(['00123'])
        state.merge([dict(account='00123', status='failed', error='ProfileConflict')])
        row = state.rows()[0]
        self.assertEqual((row['status'], row['count'], row['observed_at']), ('failed', None, ''))
        self.assertEqual(state.pending_handles(), ['00123'])

    def test_atomic_save_failure_retains_old_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'checkpoint.json'
            state = BatchState(['00123']); state.save(path)
            before = path.read_bytes()
            state.merge([success()])
            with patch('collection_batch_state.os.replace', side_effect=OSError('synthetic interrupted write')):
                with self.assertRaises(OSError):
                    state.save(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_checkpoint_rejects_tampered_identity_mapping_and_quantities(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'checkpoint.json'
            state = BatchState(['00123']); state.merge([success()])
            original = state.to_dict()
            for change in ('identity', 'profile', 'count', 'version'):
                value = copy.deepcopy(original)
                if change == 'identity': value['records'][0]['account'] = 'other'
                elif change == 'profile': value['records'][0]['profile'] = OTHER
                elif change == 'count': value['records'][0]['success']['count'] = True
                else: value['version'] = True
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.subTest(change=change), self.assertRaises(ValueError):
                    BatchState.open(path, ['00123'])

    def test_read_apis_return_copies(self):
        state = BatchState(['00123']); state.merge([success()])
        state.success('00123')['count'] = 999
        state.to_dict()['records'][0]['success']['count'] = 999
        state.rows()[0]['count'] = 999
        self.assertEqual(state.success('00123')['count'], 0)


if __name__ == '__main__':
    unittest.main()
