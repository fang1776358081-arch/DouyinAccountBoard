"""Names-only repair contract using self-made isolated SQLite fixtures only."""
import copy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root

checked_root()  # Fail closed before any application-related import.

import app
import auth
import nurture
import office_three_nickname as fill
import operations
import test_office_three_import as support
from sql_dialect import begin_write

DAY = support.DAY


class NicknameFillTest(unittest.TestCase):
    insert_account = staticmethod(support.OfficeThreeFixture.insert_account)
    snapshot = support.OfficeThreeFixture.snapshot

    def setUp(self):
        support.OfficeThreeFixture.setUp(self)
        clock = patch.object(app, 'today', return_value=DAY)
        clock.start()
        self.addCleanup(clock.stop)
        self.set_name('昵称待补充（手机1）')
        with app.connect() as db:
            db.execute("INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices) "
                       "VALUES('synthetic-old-name-history',1,?,'合成记录人','old','account:old-three',NULL,'{}','[\"three\"]')",
                       (self.actor['id'],))

    def set_name(self, name):
        with app.connect() as db:
            db.execute("UPDATE accounts SET name=? WHERE id='old-three'", (name,))

    def body(self, rows=None):
        return dict(mode=fill.MODE, office_id='three', accounts_file=support.csv_file(
            support.ACCOUNT_HEADERS, [support.account_row('synthetic_reuse', name='合成补全昵称')]
            if rows is None else rows))

    def preview(self, body=None, actor=None):
        with app.connect() as db:
            begin_write(db)
            return fill.preview(db, actor or self.actor, self.body() if body is None else body, DAY)

    def apply_body(self, preview, **extra):
        defaults = dict(mode=fill.MODE, office_id='three', preview_id=preview['preview_id'],
                        confirmation=fill.CONFIRMATION, request_id=str(uuid4()), _expected_versions={})
        return {**defaults, **extra}

    def apply(self, preview, *, body=None, day=DAY):
        with app.connect() as db:
            begin_write(db)
            return fill.apply(db, self.actor, self.apply_body(preview) if body is None else body, day)

    def rows(self, table):
        with app.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]

    def assert_only_names(self, before, changes):
        expected = copy.deepcopy(before)
        for row in expected['accounts']:
            if row['id'] in changes:
                row['name'] = changes[row['id']]
        self.assertEqual(self.snapshot(), expected)

    def test_preview_needs_only_accounts_and_does_not_write_business_records(self):
        before = self.snapshot()
        result = self.preview()
        self.assertEqual(result['mode'], fill.MODE)
        self.assertEqual(result['office_id'], 'three')
        self.assertRegex(result['preview_id'], r'^[a-f0-9]{32}$')
        self.assertEqual(result['confirmation'], fill.CONFIRMATION)
        self.assertEqual(result['summary'], dict(source_rows=1, matched_accounts=1,
            updated_accounts=1, conflicts=0, unmatched=0, unchanged=0, skipped=0))
        self.assertEqual(result['rows'][0]['action'], 'update')
        self.assertEqual(result['rows'][0]['current_name'], '昵称待补充（手机1）')
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            payload = operations.take_preview(db, self.actor, result['preview_id'], fill.KIND)
        self.assertEqual(payload['plan'][0]['account_id'], 'old-three')
        self.assertEqual(payload['plan'][0]['douyin_id'], 'synthetic_reuse')
        self.assertEqual(payload['plan'][0]['source_row'], 11)
        self.assertEqual(payload['plan'][0]['source_sheet'], fill.SOURCE_SHEET)

    def test_apply_changes_only_name_and_records_source_before_after_and_version(self):
        before = self.snapshot()
        old_history = self.rows('operations_history')
        preview = self.preview()
        result = self.apply(preview)
        self.assertEqual(result['updated_accounts'], 1)
        self.assertNotIn('_record_versions', result)
        self.assertTrue(result['verification']['only_name_fields_changed'])
        self.assertTrue(result['verification']['history_preserved'])
        self.assert_only_names(before, {'old-three': '合成补全昵称'})
        changed = result['changes'][0]
        self.assertEqual(changed['before_name'], '昵称待补充（手机1）')
        self.assertEqual(changed['after_name'], '合成补全昵称')
        self.assertEqual(changed['source_row'], 11)
        self.assertEqual(changed['douyin_id'], 'synthetic_reuse')
        history = self.rows('operations_history')
        self.assertEqual(history[:len(old_history)], old_history)
        saved = next(row for row in history if row['id'] == changed['history_id'])
        self.assertEqual(json.loads(saved['before_json'])['name'], changed['before_name'])
        self.assertEqual(json.loads(saved['after_json'])['name'], changed['after_name'])
        audit = next(row for row in self.rows('auth_audit') if row['action'] == fill.AUDIT_ACTION)
        provenance = json.loads(audit['target'])
        self.assertEqual(provenance['source'], fill.SOURCE_URL)
        self.assertEqual(provenance['row'], 11)
        self.assertEqual(provenance['history_id'], changed['history_id'])
        self.assertEqual(provenance['preview'], preview['preview_id'])
        self.assertLessEqual(len(audit['target']), 200)
        with app.connect() as db:
            current = operations.snapshot(db)['account:old-three']
            self.assertEqual(changed['after_version'], operations.token(current))

    def test_repeat_preview_is_same_and_never_creates_or_erases_accounts(self):
        first = self.preview()
        self.apply(first)
        before = self.snapshot()
        history = self.rows('operations_history')
        second = self.preview()
        self.assertEqual(second['summary']['updated_accounts'], 0)
        self.assertEqual(second['summary']['unchanged'], 1)
        self.assertEqual(self.apply(second)['updated_accounts'], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rows('operations_history'), history)
        with self.assertRaises(ValueError):
            self.apply(first)  # HTTP replay happens before this service call.

    def test_blank_source_preserves_real_name_and_different_real_names_conflict(self):
        self.set_name('合成已确认真实昵称')
        before = self.snapshot()
        blank = self.preview(self.body([support.account_row('synthetic_reuse', name='')]))
        self.assertEqual(blank['summary']['skipped'], 1)
        self.assertEqual(blank['summary']['updated_accounts'], 0)
        self.apply(blank)
        disagreement = self.preview()
        self.assertEqual(disagreement['summary']['conflicts'], 1)
        self.assertEqual(disagreement['summary']['updated_accounts'], 0)
        self.assertEqual(self.snapshot(), before)

    def test_only_exact_generated_placeholders_and_empty_values_can_be_filled(self):
        for old in (None, '', '昵称待补充', '昵称待补充（手机1）', '昵称待补充（手机999999）'):
            with self.subTest(old=old):
                self.set_name(old)
                result = self.preview()
                self.assertEqual(result['summary']['updated_accounts'], 1)
                self.assertEqual(self.apply(result)['changes'][0]['before_name'], old)
        for old in ('昵称待补充旗舰店', '昵称待补充(手机1)', ' 昵称待补充', '昵称待补充（手机0）'):
            with self.subTest(real_or_ambiguous=old):
                self.set_name(old)
                result = self.preview()
                self.assertEqual(result['summary']['updated_accounts'], 0)
                self.assertEqual(result['summary']['conflicts'], 1)

    def test_empty_invalid_or_placeholder_source_names_never_overwrite(self):
        for name, action in (('', 'skip'), ('  ', 'skip'), ('合' * 101, 'conflict'),
                             ('昵称待补充（手机2）', 'conflict'), ('昵称待补充：待确认', 'conflict')):
            with self.subTest(name=name):
                before = self.snapshot()
                result = self.preview(self.body([support.account_row('synthetic_reuse', name=name)]))
                self.assertEqual(result['rows'][0]['action'], action)
                self.assertEqual(result['summary']['updated_accounts'], 0)
                self.assertEqual(self.snapshot(), before)

    def test_identifiers_retain_leading_zero_and_case_without_phone_matching(self):
        handles = ('000012345', 'Synthetic_Case_A', 'synthetic_case_a')
        with app.connect() as db:
            for index, handle in enumerate(handles, 3):
                self.insert_account(db, 'synthetic-name-' + str(index), handle,
                                    '昵称待补充', 'office3:1', index, 'three')
        accounts = [support.account_row(handle, row=index + 10, name='合成精确昵称' + str(index),
                    device='不是可匹配编号', computer='') for index, handle in enumerate(handles)]
        result = self.preview(self.body(accounts))
        self.assertEqual(result['summary']['updated_accounts'], 3)
        saved = self.apply(result)
        self.assertEqual([row['douyin_id'] for row in saved['changes']], list(handles))
        self.assertEqual(len(self.rows('accounts')), 3 + 3)

    def test_missing_invalid_unmatched_and_source_duplicate_identifiers_are_explicit(self):
        rows = [support.account_row('', row=11), support.account_row('invalid/handle', row=12),
                support.account_row('synthetic_missing_target', row=13),
                support.account_row('synthetic_reuse', row=14),
                support.account_row('synthetic_reuse', row=15, name='合成冲突二')]
        result = self.preview(self.body(rows))
        self.assertEqual(result['summary']['source_rows'], 5)
        self.assertEqual(result['summary']['updated_accounts'], 0)
        self.assertEqual(result['summary']['conflicts'], 4)
        self.assertEqual(result['summary']['unmatched'], 1)
        self.assertEqual([row['action'] for row in result['rows']],
                         ['conflict', 'conflict', 'unmatched', 'conflict', 'conflict'])

    def test_global_duplicate_and_nonexclusive_office_do_not_expose_or_update_old_name(self):
        with app.connect() as db:
            self.insert_account(db, 'synthetic-duplicate', 'synthetic_reuse',
                                '合成另一办姓名', 'synth-pc-1', 8, 'synthetic-other')
        before = self.snapshot()
        duplicate = self.preview()
        self.assertEqual(duplicate['summary']['conflicts'], 1)
        self.assertEqual(duplicate['rows'][0]['account_id'], '')
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            db.execute("DELETE FROM account_offices WHERE account_id='synthetic-duplicate'")
            db.execute("DELETE FROM accounts WHERE id='synthetic-duplicate'")
        for office in ('synthetic-other', None):
            with app.connect() as db:
                db.execute("DELETE FROM account_offices WHERE account_id='old-three'")
                if office:
                    db.execute("INSERT INTO account_offices VALUES('old-three',?)", (office,))
            result = self.preview()
            self.assertEqual(result['summary']['updated_accounts'], 0)
            self.assertEqual(result['summary']['conflicts'], 1)
            self.assertEqual(result['rows'][0]['current_name'], '')
        with patch.object(fill.office_scope, 'entity_offices', return_value={'three', 'synthetic-other'}):
            self.assertEqual(self.preview()['summary']['updated_accounts'], 0)

    def test_owner_target_mode_and_current_permission_checks_precede_writes(self):
        with app.connect() as db:
            employee = auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?',
                                                   (self.employee_id,)).fetchone())
        with self.assertRaises(auth.AccessError):
            self.preview(actor=employee)
        for field, value in (('office_id', 'synthetic-other'), ('mode', 'wrong-mode')):
            body = self.body()
            body[field] = value
            with self.assertRaises(ValueError):
                self.preview(body)
        result = self.preview()
        before = self.snapshot()
        with app.connect() as db:
            db.execute('UPDATE auth_users SET active=0 WHERE id=?', (self.actor['id'],))
        with self.assertRaises(auth.AccessError):
            self.apply(result)
        self.assertEqual(self.snapshot(), {**before, 'auth_users': self.rows('auth_users')})

    def test_preview_expiry_actor_binding_and_day_changes_reject_without_writes(self):
        for change in ('expire', 'actor', 'day'):
            with self.subTest(change=change):
                result = self.preview()
                before = self.snapshot()
                if change in ('expire', 'actor'):
                    with app.connect() as db:
                        column, value = ('expires_at', 0) if change == 'expire' else ('actor_id', self.employee_id)
                        db.execute('UPDATE operations_previews SET ' + column + '=? WHERE id=?',
                                   (value, result['preview_id']))
                with self.assertRaises(ValueError):
                    self.apply(result, day='2026-10-29' if change == 'day' else DAY)
                self.assertEqual(self.snapshot(), before)

    def test_concurrent_name_or_scope_change_invalidates_preview(self):
        result = self.preview()
        self.set_name('合成别人已保存')
        before = self.snapshot()
        with self.assertRaises(operations.ConflictError):
            self.apply(result)
        self.assertEqual(self.snapshot(), before)
        self.set_name('昵称待补充')
        result = self.preview()
        with app.connect() as db:
            db.execute("UPDATE account_offices SET office_id='synthetic-other' WHERE account_id='old-three'")
        before = self.snapshot()
        with self.assertRaises(operations.ConflictError):
            self.apply(result)
        self.assertEqual(self.snapshot(), before)

    def test_client_patch_injection_confirmation_and_request_guards_are_rejected(self):
        result = self.preview()
        for extra in ({'changes': {'name': '合成客户端任意值'}}, {'confirmation': 'wrong'},
                      {'request_id': 'invalid'}, {'_expected_versions': {'account:old-three': 'fake'}},
                      {'mode': 'wrong-mode'}):
            with self.subTest(extra=extra):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.apply(result, body=self.apply_body(result, **extra))
                self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            begin_write(db)
            db.restore_generation = None
            with self.assertRaises(ValueError):
                fill.apply(db, self.actor, self.apply_body(result), DAY)

    def test_private_raw_columns_are_discarded_before_validation_and_persistence(self):
        headers = [''] * 22
        for index, title in ((2, '注册抖音用的手机号'), (5, '抖音名称'),
                             (6, '抖音在哪个手机上'), (7, '抖音号'), (11, '对应电脑')):
            headers[index] = title
        headers[13] = 'SYNTHETIC_PRIVATE_HEADER'
        row = [''] * 22
        row[5], row[7], row[13] = '合成安全姓名', 'synthetic_reuse', '=SYNTHETIC_PRIVATE_FORMULA'
        body = self.body()
        body['accounts_file'] = support.csv_file(headers, [row])
        body['progress_file'] = {'filename': 'ignored.csv', 'content': 'SYNTHETIC_PRIVATE_PROGRESS'}
        result = self.preview(body)
        self.assertEqual(result['summary']['updated_accounts'], 1)
        with app.connect() as db:
            stored = operations.take_preview(db, self.actor, result['preview_id'], fill.KIND)
        self.assertNotIn('SYNTHETIC_PRIVATE_', json.dumps([result, stored], ensure_ascii=False))
        self.assertEqual(stored['plan'][0]['source_row'], 2)

    def test_preview_detects_nurture_side_effect_and_rolls_it_back(self):
        with app.connect() as db:
            db.execute("UPDATE accounts SET registered_on=? WHERE id='old-three'", (DAY,))
        before = self.snapshot()
        result = self.preview()
        self.assertEqual(result['summary']['updated_accounts'], 0)
        self.assertEqual(result['summary']['conflicts'], 1)
        self.assertEqual(self.snapshot(), before)

    def test_apply_rolls_back_late_nurture_business_or_audit_history_mutation(self):
        real_sync, real_audit = nurture.sync, auth.audit
        for problem in ('nurture', 'audit-business', 'audit-history'):
            with self.subTest(problem=problem):
                result = self.preview()
                before = self.snapshot()
                history, audits = self.rows('operations_history'), self.rows('auth_audit')

                def bad_sync(db, day, account_ids=None):
                    real_sync(db, day, account_ids)
                    db.execute("UPDATE accounts SET note='合成计划外变化' WHERE id='synth-account-1'")

                def bad_audit(db, actor, action, target=''):
                    real_audit(db, actor, action, target)
                    if problem == 'audit-business':
                        db.execute("UPDATE accounts SET note='合成审计后变化' WHERE id='synth-account-1'")
                    else:
                        db.execute("UPDATE operations_history SET actor_name='合成历史污染' WHERE group_id='synthetic-old-name-history'")

                target = patch.object(nurture, 'sync', side_effect=bad_sync) if problem == 'nurture' else patch.object(auth, 'audit', side_effect=bad_audit)
                with target, self.assertRaises(operations.ConflictError):
                    self.apply(result)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.rows('operations_history'), history)
                self.assertEqual(self.rows('auth_audit'), audits)


if __name__ == '__main__':
    unittest.main()
