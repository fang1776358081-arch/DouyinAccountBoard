"""Batch real-name association against fresh, synthetic SQLite only."""
import copy
import json
import sqlite3
import unittest
import uuid

import auth
import employee_cards as service
import idempotency
import operations
import test_employee_cards as fixture


class EmployeeCardBatchTest(unittest.TestCase):
    setUp = fixture.EmployeeCardsTest.setUp
    tearDown = fixture.EmployeeCardsTest.tearDown
    actor = fixture.EmployeeCardsTest.actor
    meta = fixture.EmployeeCardsTest.meta
    legacy_snapshot = fixture.EmployeeCardsTest.legacy_snapshot
    prepare = fixture.EmployeeCardsTest.prepare
    commit_preview = fixture.EmployeeCardsTest.commit_preview
    save = fixture.EmployeeCardsTest.save
    person = fixture.EmployeeCardsTest.person
    operator = fixture.EmployeeCardsTest.operator

    def data(self, *cards):
        return dict(cards=[dict(card_id=ident, operator_id=operator, activated_on=day)
                           for ident, operator, day in (cards or [(1, 'minsheng', ''), (2, 'zhongxing', '2026-10-01')])],
                    source='合成员工授权的明确实名核对依据')

    def business(self):
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                for name in ('employee_card_units', 'employee_card_links', 'employee_card_events')}

    def original(self):
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                for name in ('phone_cards', 'accounts', 'auth_users', 'user_office_grants',
                             'phone_card_offices', 'account_offices', 'offices')}

    def link(self, employee, card=1, operator='minsheng', day='2026-10-01'):
        return self.save('card_link', dict(card_id=card, operator_id=operator,
                                          activated_on=day, source='合成原实名依据'), employee)['id']

    def test_mixed_operators_preview_is_readonly_and_apply_preserves_legacy_fields(self):
        employee = self.person()
        before, original = self.business(), self.original()
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.assertEqual(self.business(), before)
        self.assertTrue(preview['can_apply'])
        self.assertFalse(preview['has_conflicts'])
        self.assertEqual(preview['counts'], dict(total=2, new=2, existing=0, conflict=0))
        self.assertEqual([row['operator_name'] for row in preview['rows']], ['民生', '中兴'])
        result, _ = self.commit_preview(preview)
        self.assertEqual(result['counts'], preview['counts'])
        self.assertNotIn('rows', result)
        detail = self.meta(employee_id=employee)
        self.assertEqual(detail['card_ledger']['registered'], 2)
        self.assertEqual({unit['operator_id'] for unit in detail['units']}, {'minsheng', 'zhongxing'})
        unknown = next(unit for unit in detail['units'] if unit['operator_id'] == 'minsheng')
        self.assertEqual((unknown['activated_on'], unknown['requested_on']), ('', ''))
        self.assertEqual(unknown['network_policy']['network'], 'unknown')
        self.assertEqual(unknown['phase'], 'active')
        self.assertEqual(self.original(), original)

    def test_existing_same_employee_is_unchanged_even_with_empty_or_different_input_date(self):
        employee = self.person()
        unit = self.link(employee)
        old = tuple(self.db.execute('SELECT * FROM employee_card_units WHERE id=?', (unit,)).fetchone())
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.assertEqual(preview['counts'], dict(total=2, new=1, existing=1, conflict=0))
        self.assertEqual(preview['rows'][0]['status'], 'existing')
        self.assertEqual(preview['rows'][0]['activated_on'], '2026-10-01')
        self.commit_preview(preview)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM employee_card_units WHERE id=?', (unit,)).fetchone()), old)
        before = self.business()
        repeat = self.prepare('card_link_batch', self.data((1, 'minsheng', '2026-11-01'), (2, 'zhongxing', '')), employee)
        self.assertEqual(repeat['counts'], dict(total=2, new=0, existing=2, conflict=0))
        self.commit_preview(repeat)
        self.assertEqual(self.business(), before)

    def test_existing_pending_application_is_not_guessed_or_completed(self):
        employee = self.person()
        application = self.save('application', dict(operator_id='minsheng', requested_on=self.today,
                                quantity=1, result='pending', source='合成在途申请'), employee)
        pending = [tuple(row) for row in self.db.execute('SELECT * FROM employee_card_units WHERE application_id=?', (application['id'],))]
        before = tuple(self.db.execute('SELECT * FROM employee_card_applications WHERE id=?', (application['id'],)).fetchone())
        preview = self.prepare('card_link_batch', self.data((1, 'minsheng', '')), employee)
        self.assertTrue(any('不自动完成在途申请' in message for message in preview['summary']))
        self.commit_preview(preview)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM employee_card_units WHERE application_id=?', (application['id'],))], pending)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM employee_card_applications WHERE id=?', (application['id'],)).fetchone()), before)
        units = [dict(row) for row in self.db.execute('SELECT * FROM employee_card_units WHERE employee_id=?', (employee,))]
        self.assertEqual(sorted(unit['phase'] for unit in units), ['active', 'pending'])
        self.assertEqual(next(unit['activated_on'] for unit in units if unit['phase'] == 'active'), '')

    def test_other_employee_conflict_blocks_entire_preview_and_does_not_leak_identity(self):
        employee, other = self.person(), self.person('SECRET-OTHER', 'two')
        self.link(other)
        before = self.business()
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.assertTrue(preview['has_conflicts'])
        self.assertFalse(preview['can_apply'])
        self.assertEqual(preview['counts'], dict(total=2, new=1, existing=0, conflict=1))
        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn(other, serialized)
        self.assertNotIn('SECRET-OTHER', serialized)
        self.assertEqual(self.business(), before)
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)

    def test_same_employee_wrong_operator_is_a_blocked_conflict(self):
        employee = self.person()
        self.link(employee)
        before = self.business()
        preview = self.prepare('card_link_batch', self.data((1, 'zhongxing', '')), employee)
        self.assertEqual(preview['rows'][0]['status'], 'conflict')
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)

    def test_selected_card_changed_phone_is_not_silently_treated_as_existing(self):
        employee = self.person()
        self.link(employee)
        self.db.execute("UPDATE phone_cards SET phone='13800000999' WHERE id=1")
        self.db.commit()
        before = self.business()
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.assertEqual(preview['rows'][0]['phone'], '13800000999')
        self.assertEqual(preview['rows'][0]['status'], 'conflict')
        self.assertEqual(self.business(), before)

    def test_historical_phone_on_a_different_card_blocks_rebinding(self):
        employee, other = self.person(), self.person('002')
        self.link(other)
        self.db.execute("UPDATE phone_cards SET phone='13800000999' WHERE id=1")
        self.db.execute("UPDATE phone_cards SET phone='13800000001' WHERE id=2")
        self.db.commit()
        before = self.business()
        preview = self.prepare('card_link_batch', self.data((2, 'minsheng', '')), employee)
        self.assertEqual(preview['rows'][0]['status'], 'conflict')
        with self.assertRaises(ValueError):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)

    def test_conflicted_preview_stays_blocked_after_conflict_disappears(self):
        employee, other = self.person(), self.person('002')
        unit = self.link(other)
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.db.execute('DELETE FROM employee_card_links WHERE unit_id=?', (unit,))
        self.db.execute('DELETE FROM employee_card_units WHERE id=?', (unit,))
        self.db.commit()
        before = self.business()
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)

    def test_new_cross_employee_link_after_preview_is_409_and_never_partially_applies(self):
        employee, other = self.person(), self.person('002')
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.link(other)
        before = self.business()
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_units WHERE employee_id=?', (employee,)).fetchone()[0], 0)

    def test_selected_card_snapshot_change_is_409_without_writes(self):
        employee = self.person()
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.db.execute("UPDATE phone_cards SET note='合成并发修改' WHERE id=2")
        self.db.commit()
        before = self.business()
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)

    def test_invalid_id_duplicate_limit_fields_and_dates_are_rejected(self):
        employee = self.person()
        cases = [dict(cards=[], source='依据'), dict(cards=[self.data()['cards'][0]] * 201, source='依据'),
                 self.data((1, 'minsheng', ''), (1, 'minsheng', '')),
                 self.data((True, 'minsheng', '')), self.data(('1', 'minsheng', '')),
                 self.data((1, '', '')), self.data((1, 'guessed-brand', '')),
                 self.data((1, 'minsheng', '2026-11-03')), self.data((1, 'minsheng', 'invalid')),
                 dict(self.data(), source=''), dict(self.data(), owner='不能据文字实名')]
        nested = copy.deepcopy(self.data())
        nested['cards'][0]['phone'] = '13800000001'
        cases.append(nested)
        before = self.business()
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.prepare('card_link_batch', data, employee)
                self.assertEqual(self.business(), before)

    def test_unauthorized_or_multioffice_card_aborts_without_hidden_details(self):
        employee = self.person()
        for card in (3, 999):
            with self.assertRaises(auth.AccessError) as raised:
                self.prepare('card_link_batch', self.data((1, 'minsheng', ''), (card, 'minsheng', '')), employee, actor=2)
            self.assertNotIn('13800000003', str(raised.exception))
        with self.assertRaises(ValueError):
            self.prepare('card_link_batch', self.data((999, 'minsheng', '')), employee)
        self.db.execute("INSERT INTO phone_card_offices VALUES(2,'two')")
        self.db.commit()
        before = self.business()
        with self.assertRaises(auth.AccessError):
            self.prepare('card_link_batch', self.data(), employee, actor=2)
        self.assertEqual(self.business(), before)

    def test_card_office_reauthorization_occurs_for_every_selected_card_at_apply(self):
        employee = self.person()
        preview = self.prepare('card_link_batch', self.data(), employee, actor=2)
        self.db.execute("UPDATE phone_card_offices SET office_id='two' WHERE card_id=2")
        self.db.commit()
        before = self.business()
        with self.assertRaises(auth.AccessError):
            self.commit_preview(preview, actor=2)
        self.assertEqual(self.business(), before)

    def test_office_change_invalidates_preview_even_when_manager_still_has_both_grants(self):
        employee = self.person()
        self.db.execute("INSERT INTO user_office_grants VALUES(2,'two')")
        self.db.commit()
        preview = self.prepare('card_link_batch', self.data(), employee, actor=2)
        self.db.execute("UPDATE phone_card_offices SET office_id='two' WHERE card_id=2")
        self.db.commit()
        before = self.business()
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(preview, actor=2)
        self.assertEqual(self.business(), before)

    def test_manager_and_employee_scope_and_ordinary_employee_are_rechecked(self):
        employee = self.person(user_id=3)
        with self.assertRaises(auth.AccessError):
            self.prepare('card_link_batch', self.data(), employee, actor=3)
        outside = self.person('002', 'two')
        with self.assertRaises(auth.AccessError):
            self.prepare('card_link_batch', self.data(), outside, actor=2)
        preview = self.prepare('card_link_batch', self.data(), employee, actor=2)
        self.db.execute("UPDATE auth_users SET permissions='[]' WHERE id=2")
        self.db.commit()
        before = self.business()
        with self.assertRaises(auth.AccessError):
            self.commit_preview(preview, actor=2)
        self.assertEqual(self.business(), before)

    def test_uuid_replay_and_lookup_are_phone_free_after_card_access_is_removed(self):
        employee = self.person()
        result, body = self.commit_preview(self.prepare('card_link_batch', self.data(), employee, actor=2), actor=2)
        before = self.business()
        self.db.execute("UPDATE phone_card_offices SET office_id='two' WHERE card_id IN (1,2)")
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        replay = service.apply(self.db, self.actor(2), body, self.today)
        self.db.commit()
        self.assertEqual(replay, result)
        self.assertEqual(self.meta(2, request_id=body['request_id'])['result'], result)
        receipt = self.db.execute('SELECT result FROM employee_card_requests WHERE request_id=?', (body['request_id'],)).fetchone()[0]
        self.assertEqual(set(json.loads(receipt)), {'ok', 'id', 'employee_id', 'action', 'version', 'counts'})
        for secret in ('13800000001', '13800000002', 'phone', 'card_id', 'source', 'birth_date', 'csrf'):
            self.assertNotIn(secret, receipt)
        self.assertEqual(self.business(), before)
        changed = dict(body, preview_id='different-preview')
        with self.assertRaisesRegex(ValueError, '同一请求'):
            service.apply(self.db, self.actor(2), changed, self.today)

    def test_actor_expiry_date_version_and_restore_generation_are_not_bypassed(self):
        employee = self.person()
        preview = self.prepare('card_link_batch', self.data(), employee)
        with self.assertRaisesRegex(ValueError, '预览'):
            self.commit_preview(preview, actor=2)
        self.db.execute('UPDATE operations_previews SET expires_at=0 WHERE id=?', (preview['preview_id'],))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, '过期'):
            self.commit_preview(preview)
        preview = self.prepare('card_link_batch', self.data(), employee)
        with self.assertRaises(operations.ConflictError):
            service.apply(self.db, self.actor(), dict(preview_id=preview['preview_id'], confirmation='确认保存', request_id=str(uuid.uuid4())), '2026-11-03')
        self.save('employee', dict(name='合成并发更新'), employee)
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(preview)
        self.db.restore_generation = 'synthetic-batch-one'
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.db.restore_generation = 'synthetic-batch-two'
        before = self.business()
        with self.assertRaises(idempotency.IdempotencyError):
            self.commit_preview(preview)
        self.assertEqual(self.business(), before)
        saved, body = self.commit_preview(self.prepare('card_link_batch', self.data(), employee))
        self.db.restore_generation = 'synthetic-batch-three'
        with self.assertRaises(idempotency.IdempotencyError):
            service.apply(self.db, self.actor(), body, self.today)
        with self.assertRaises(idempotency.IdempotencyError):
            self.meta(request_id=body['request_id'])

    def test_late_database_error_rolls_back_batch_even_if_caller_commits(self):
        employee = self.person()
        preview = self.prepare('card_link_batch', self.data(), employee)
        self.db.execute("""CREATE TRIGGER synthetic_batch_failure BEFORE INSERT ON employee_card_events
          WHEN NEW.action='card_link_batch' AND (SELECT COUNT(*) FROM employee_card_events WHERE action='card_link_batch')>0
          BEGIN SELECT RAISE(ABORT,'synthetic failure after first event'); END""")
        self.db.commit()
        before = self.business()
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic failure'):
            service.apply(self.db, self.actor(), dict(preview_id=preview['preview_id'], confirmation='确认保存', request_id=str(uuid.uuid4())), self.today)
        self.db.commit()
        self.assertEqual(self.business(), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_requests WHERE action=?', ('card_link_batch',)).fetchone()[0], 0)

    def test_two_hundred_cards_use_bounded_history_read_and_deterministic_snapshots(self):
        employee = self.person()
        for ident in range(4, 202):
            self.db.execute('INSERT INTO phone_cards(id,phone) VALUES(?,?)', (ident, '139' + str(ident).zfill(8)))
            self.db.execute('INSERT INTO phone_card_offices VALUES(?,?)', (ident, 'one'))
        self.db.commit()
        values = self.data(*[(ident, 'minsheng', '') for ident in [2, 1] + list(range(4, 202))])
        trace = []
        self.db.set_trace_callback(trace.append)
        try:
            preview = self.prepare('card_link_batch', values, employee, actor=2)
            self.assertEqual(preview['counts']['new'], 200)
            self.assertEqual([row['card_id'] for row in preview['rows']], sorted(row['card_id'] for row in preview['rows']))
            result, _ = self.commit_preview(preview, actor=2)
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(result['counts']['new'], 200)
        classification_queries = [sql for sql in trace if sql.startswith('SELECT l.*,u.employee_id,u.operator_id,u.activated_on FROM employee_card_links')]
        self.assertEqual(len(classification_queries), 2, 'one bounded history classification per preview/apply, no per-card history queries')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_units WHERE employee_id=?', (employee,)).fetchone()[0], 200)


if __name__ == '__main__':
    unittest.main()
