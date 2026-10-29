"""Read-only missing-nickname evidence, using fresh isolated synthetic data."""
import json
import unittest
from unittest.mock import patch

from test_isolation import checked_root

checked_root()  # Refuse to import the application outside an isolated root.

import app
import auth
import office_three_import
import operations
import test_office_three_import as support


class OfficeThreeNicknamePreviewTest(unittest.TestCase):
    # Reuse reviewed fixture helpers without inheriting its behavioral suite.
    setUp = support.OfficeThreeFixture.setUp
    insert_account = staticmethod(support.OfficeThreeFixture.insert_account)
    snapshot = support.OfficeThreeFixture.snapshot
    body = support.OfficeThreeFixture.body
    preview = support.OfficeThreeFixture.preview
    apply = support.OfficeThreeFixture.apply

    def old_name(self, name):
        with app.connect() as db:
            db.execute("UPDATE accounts SET name=? WHERE id='old-three'", (name,))

    def nickname_body(self, name='合成已核对昵称', **fields):
        return self.body(accounts=[support.account_row('synthetic_reuse', name=name, **fields)], progress=[])

    def test_exact_missing_names_are_candidates_by_unique_handle_only(self):
        for old in (None, '', '昵称待补充', '昵称待补充（手机1）', '昵称待补充（手机999999）'):
            with self.subTest(old=old):
                self.old_name(old)
                before = self.snapshot()
                # Phone, registration number, shopfront and computer do not
                # establish identity. Only the unique real handle is used.
                body = self.nickname_body(device='不是可匹配手机', computer='')
                result = self.preview(body)
                self.assertEqual(result['missing_nickname_suggestions'], [{
                    'row': 11, 'account_id': 'old-three', 'douyin_id': 'synthetic_reuse',
                    'current_name': old if old is not None else '', 'source_name': '合成已核对昵称'}])
                self.assertEqual(result['missing_nickname_suggestion_count'], 1)
                self.assertEqual(result['summary']['new_accounts'], 0)
                self.assertEqual(before, self.snapshot())

    def test_real_or_near_placeholder_old_names_are_never_candidates(self):
        names = ('原有真实昵称', '昵称待补充旗舰店', '昵称待补充（手机0）',
                 '昵称待补充（手机01）', '昵称待补充（手机1000000）',
                 '昵称待补充(手机1)', ' 昵称待补充', '昵称待补充（手机1）备注')
        for name in names:
            with self.subTest(name=name):
                self.old_name(name)
                result = self.preview(self.nickname_body())
                self.assertEqual(result['missing_nickname_suggestions'], [])
                self.assertEqual(result['missing_nickname_suggestion_count'], 0)

    def test_empty_long_or_suspected_placeholder_source_names_are_excluded(self):
        self.old_name('昵称待补充（手机1）')
        for name in ('', '   ', '合' * 101, '昵称待补充', '昵称待补充（手机2）',
                     '昵称待补充（手机0）', '昵称待补充(手机2)', '昵称待补充：稍后确认'):
            with self.subTest(name=name):
                result = self.preview(self.nickname_body(name))
                self.assertEqual(result['missing_nickname_suggestions'], [])
        result = self.preview(self.nickname_body('合' * 100))
        self.assertEqual(result['missing_nickname_suggestion_count'], 1)

    def test_missing_invalid_or_duplicate_source_handles_are_excluded(self):
        self.old_name('昵称待补充')
        for handle in ('', 'x', '含中文', 'invalid/handle'):
            with self.subTest(handle=handle):
                result = self.preview(self.body(accounts=[support.account_row(handle, name='合成新昵称')], progress=[]))
                self.assertEqual(result['missing_nickname_suggestions'], [])
        duplicated = self.body(accounts=[
            support.account_row('synthetic_reuse', name='合成新昵称甲'),
            support.account_row('synthetic_reuse', row=12, name='合成新昵称乙')], progress=[])
        result = self.preview(duplicated)
        self.assertEqual(result['missing_nickname_suggestions'], [])

    def test_global_duplicate_existing_handles_are_excluded(self):
        self.old_name('昵称待补充')
        with app.connect() as db:
            self.insert_account(db, 'synthetic-duplicate-handle', 'synthetic_reuse',
                                '昵称待补充', 'synth-pc-1', 8, 'synthetic-other')
        before = self.snapshot()
        result = self.preview(self.nickname_body())
        self.assertEqual(result['missing_nickname_suggestions'], [])
        self.assertEqual(before, self.snapshot())

    def test_foreign_or_pending_accounts_are_excluded(self):
        self.old_name('昵称待补充')
        for offices in (['synthetic-other'], []):
            with self.subTest(offices=offices):
                with app.connect() as db:
                    db.execute("DELETE FROM account_offices WHERE account_id='old-three'")
                    for office in offices:
                        db.execute("INSERT INTO account_offices VALUES('old-three',?)", (office,))
                before = self.snapshot()
                result = self.preview(self.nickname_body())
                self.assertEqual(result['missing_nickname_suggestions'], [])
                self.assertEqual(before, self.snapshot())

    def test_unexpected_multi_office_mapping_is_defensively_excluded(self):
        self.old_name('昵称待补充')
        before = self.snapshot()
        # account_offices has a single-account PK. Simulate an unexpected scope
        # result for this defensive check; do not weaken or replace the schema.
        with patch.object(office_three_import.office_scope, 'entity_offices',
                          return_value={'three', 'synthetic-other'}):
            result = self.preview(self.nickname_body())
        self.assertEqual(result['missing_nickname_suggestions'], [])
        self.assertEqual(before, self.snapshot())

    def test_read_only_evidence_is_not_a_persisted_or_executable_update_plan(self):
        self.old_name('昵称待补充（手机1）')
        before = self.snapshot()
        result = self.preview(self.nickname_body())
        self.assertEqual(result['missing_nickname_suggestion_count'], 1)
        with app.connect() as db:
            saved = operations.take_preview(db, self.actor, result['preview_id'], office_three_import.KIND)
        self.assertNotIn('missing_nickname_suggestions', saved)
        self.assertNotIn('missing_nickname_suggestion_count', saved)
        self.assertNotIn('missing_nickname_suggestions', saved['summary'])
        self.assertEqual(saved['plan']['accounts'], [])
        self.assertEqual(result['summary']['new_accounts'], 0)
        # Even attempted client additions cannot turn evidence into a write.
        applied = self.apply(result, missing_nickname_suggestions=result['missing_nickname_suggestions'])
        self.assertEqual(applied['imported'], 0)
        self.assertTrue(applied['verification']['original_records_preserved'])
        self.assertEqual(before, self.snapshot())
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM accounts WHERE id='old-three'").fetchone()[0],
                             '昵称待补充（手机1）')

    def test_null_nickname_is_displayed_as_empty_without_rewriting_the_record(self):
        self.old_name(None)
        before = self.snapshot()
        result = self.preview(self.nickname_body())
        self.assertEqual(result['missing_nickname_suggestion_count'], 1)
        self.assertEqual(result['missing_nickname_suggestions'][0]['current_name'], '')
        self.apply(result)
        self.assertEqual(before, self.snapshot())
        with app.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM accounts WHERE id='old-three'").fetchone()[0])

    def test_private_raw_columns_cannot_enter_suggestions_or_preview_storage(self):
        self.old_name('昵称待补充')
        headers = [''] * 22
        for index, title in ((2, '注册抖音用的手机号'), (5, '抖音名称'), (6, '抖音在哪个手机上'),
                             (7, '抖音号'), (11, '对应电脑')):
            headers[index] = title
        headers[13] = '密码'
        account = [''] * 22
        for index, value in zip(office_three_import.ACCOUNT_COLUMNS,
                                support.account_row('synthetic_reuse', name='合成已核对昵称')[1:]):
            account[index] = value
        account[13] = 'SYNTHETIC_EXCLUDED_NICKNAME_ACCOUNT_SECRET'
        matrix_headers = [''] * 24
        for slot, index in enumerate((7, 8, 9, 10, 11, 12, 13, 14, 15, 17), 1):
            matrix_headers[index] = '发布设备' + str(slot)
        matrix_headers[1] = 'SYNTHETIC_EXCLUDED_NICKNAME_HEADER_SECRET'
        matrix = [''] * 24
        matrix[1:4] = ['SYNTHETIC_EXCLUDED_NICKNAME_REMOTE', 'SYNTHETIC_EXCLUDED_NICKNAME_AUTOMATION',
                       'SYNTHETIC_EXCLUDED_NICKNAME_IP']
        body = {'office_id': 'three', 'accounts_file': support.csv_file(headers, [account]),
                'progress_file': support.csv_file(matrix_headers, [matrix])}
        before = self.snapshot()
        result = self.preview(body)
        self.assertEqual(result['missing_nickname_suggestion_count'], 1)
        self.assertEqual(set(result['missing_nickname_suggestions'][0]),
                         {'row', 'account_id', 'douyin_id', 'current_name', 'source_name'})
        with app.connect() as db:
            stored = '\n'.join(row[0] for row in db.execute('SELECT payload FROM operations_previews'))
        self.assertNotIn('SYNTHETIC_EXCLUDED_NICKNAME_', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('SYNTHETIC_EXCLUDED_NICKNAME_', stored)
        self.assertEqual(before, self.snapshot())

    def test_owner_and_target_office_checks_still_precede_evidence(self):
        self.old_name('昵称待补充')
        with app.connect() as db:
            actor = auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?', (self.employee_id,)).fetchone())
        with self.assertRaises(auth.AccessError):
            self.preview(self.nickname_body(), actor=actor)
        body = self.nickname_body()
        body['office_id'] = 'synthetic-other'
        with self.assertRaises(ValueError):
            self.preview(body)
