"""Exact-owner derived links on disposable synthetic SQLite only."""
import json
import unittest

import auth
import employee_cards as service
import employee_card_ledger as ledger
import operations
import test_employee_cards as fixture


class EmployeeCardNameMatchTest(unittest.TestCase):
    setUp = fixture.EmployeeCardsTest.setUp
    tearDown = fixture.EmployeeCardsTest.tearDown
    actor = fixture.EmployeeCardsTest.actor
    meta = fixture.EmployeeCardsTest.meta
    legacy_snapshot = fixture.EmployeeCardsTest.legacy_snapshot
    prepare = fixture.EmployeeCardsTest.prepare
    commit_preview = fixture.EmployeeCardsTest.commit_preview
    save = fixture.EmployeeCardsTest.save
    person = fixture.EmployeeCardsTest.person

    def name(self, employee):
        return self.db.execute('SELECT name FROM employee_card_employees WHERE id=?', (employee,)).fetchone()[0]

    def card(self, employee, ident=1, **changes):
        values = dict(owner=self.name(employee), carrier='待确认', status='正常',
                      code_state='无待办', return_state='不需归还')
        values.update(changes)
        self.db.execute('UPDATE phone_cards SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', (*values.values(), ident))
        self.db.commit()

    def link(self, employee, ident=1):
        return self.save('card_link', dict(card_id=ident, operator_id='minsheng',
                         activated_on='2026-10-01', source='合成人工实名确认'), employee)['id']

    def snapshot(self):
        tables = ('phone_cards', 'accounts', 'auth_users', 'user_office_grants', 'phone_card_offices',
                  'account_offices', 'offices', *service.TABLES, 'operations_previews')
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')] for name in tables}

    def make_eligible(self, employee):
        for row in self.db.execute('SELECT id,payload FROM employee_card_operators').fetchall():
            value = json.loads(row['payload'])
            value.update(network='mobile', age_confirmed=True, min_age=18,
                         waiting_confirmed=True, waiting_days=0)
            self.db.execute('UPDATE employee_card_operators SET payload=? WHERE id=?', (json.dumps(value), row['id']))
        self.db.commit()
        data = dict(scope='all', scope_id='', period='stock', period_start=self.today, period_end=self.today,
                    count=0, coverage='total', verified_on=self.today, valid_until=self.today,
                    source='合成完整零占用核验', company_inventory_complete=True)
        self.save('verification', data, employee)
        for operator in ('minsheng', 'zhongxing', 'changcheng', 'liantong', 'dianxin', 'yidong'):
            self.save('verification', dict(data, scope='operator', scope_id=operator, period='day'), employee)

    def test_exact_unique_name_rows_preserve_real_status_and_never_create_quota(self):
        employee = self.person()
        self.card(employee)
        self.card(employee, 2, status='已注销')
        before = self.snapshot()
        for _ in range(2):
            detail = self.meta(employee_id=employee)
            self.assertEqual(detail['card_ledger']['registered'], 2)
            self.assertEqual(detail['card_ledger']['name_matched_count'], 2)
            self.assertEqual((detail['card_ledger']['unclosed'], detail['card_ledger']['closed']), (1, 1))
            self.assertEqual(detail['units'], [])
            self.assertEqual(detail['card_links'], [])
            for row in detail['linked_cards']:
                self.assertEqual(row['association_source'], 'name')
                self.assertEqual((row['operator_id'], row['operator_name']), ('', '待确认'))
                self.assertEqual((row['activated_on'], row['linked_on']), ('', ''))
        self.assertEqual(self.snapshot(), before)

    def test_only_exact_owner_matches_no_keeper_label_substring_or_alias(self):
        employee = self.person()
        self.card(employee, owner='其他卡主', keeper=self.name(employee), label=self.name(employee))
        self.card(employee, 2, owner=self.name(employee) + '备注')
        self.card(employee, 3, owner=' ' + self.name(employee))
        self.assertEqual(self.meta(employee_id=employee)['card_ledger']['registered'], 0)

    def test_new_card_owner_and_employee_name_changes_are_current_and_versioned(self):
        employee, other = self.person(), self.person('002')
        initial = self.meta(employee_id=employee)['version']
        self.card(employee)
        current = self.meta(employee_id=employee)
        self.assertNotEqual(initial, current['version'])
        self.assertEqual(current['card_ledger']['registered'], 1)
        self.card(other)
        self.assertEqual(self.meta(employee_id=employee)['card_ledger']['registered'], 0)
        self.assertEqual(self.meta(employee_id=other)['card_ledger']['registered'], 1)
        before = self.meta(employee_id=other)['version']
        self.save('employee', dict(name='合成更正姓名'), other)
        current = self.meta(employee_id=other)
        self.assertNotEqual(before, current['version'])
        self.assertEqual(current['card_ledger']['registered'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_units').fetchone()[0], 0)

    def test_manual_link_wins_even_when_owner_name_changes(self):
        employee, other = self.person(), self.person('002')
        self.link(employee)
        self.card(other)
        detail = self.meta(employee_id=employee)
        self.assertEqual(detail['card_ledger']['registered'], 1)
        self.assertEqual(detail['linked_cards'][0]['association_source'], 'explicit')
        self.assertEqual(detail['linked_cards'][0]['operator_id'], 'minsheng')
        self.assertIsNone(self.meta(employee_id=other)['card_ledger']['registered'])
        self.save('employee', dict(name='合成人工关系改名仍保留'), employee)
        self.assertEqual(self.meta(employee_id=employee)['card_ledger']['registered'], 1)

    def test_historical_phone_and_current_unit_claim_prevent_automatic_reassignment(self):
        employee, other = self.person(), self.person('002')
        unit = self.link(employee)
        self.db.execute("UPDATE phone_cards SET phone='13800000999' WHERE id=1")
        self.db.execute("UPDATE phone_cards SET phone='13800000001',owner=? WHERE id=2", (self.name(other),))
        self.db.commit()
        self.assertIsNone(self.meta(employee_id=other)['card_ledger']['registered'])
        self.assertEqual(self.meta(employee_id=employee)['linked_cards'][0]['ledger_state'], 'review')
        self.db.execute('DELETE FROM employee_card_links WHERE unit_id=?', (unit,))
        self.db.commit()
        self.card(other, 1)
        detail = self.meta(employee_id=other)
        self.assertNotIn(1, {row['card_id'] for row in detail['linked_cards']})

    def test_hidden_manual_relationship_is_not_reassigned_or_disclosed(self):
        visible, hidden = self.person(), self.person('SECRET-HIDDEN', 'two')
        self.link(hidden)
        self.card(visible)
        result = self.meta(2, employee_id=visible)
        self.assertIsNone(result['card_ledger']['registered'])
        self.assertFalse(result['card_ledger']['association_complete'])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(hidden, serialized)
        self.assertNotIn('SECRET-HIDDEN', serialized)

    def test_global_duplicate_including_hidden_employee_is_not_unique(self):
        employee, hidden = self.person(), self.person('SECRET-HIDDEN', 'two')
        self.card(employee)
        before = self.meta(2, employee_id=employee)['version']
        self.save('employee', dict(name=self.name(employee)), hidden)
        result = self.meta(2, employee_id=employee)
        self.assertIsNone(result['card_ledger']['registered'])
        self.assertTrue(result['card_ledger']['scope_complete'])
        self.assertFalse(result['card_ledger']['association_complete'])
        self.assertTrue(result['card_ledger']['association_issues'])
        self.assertNotEqual(before, result['version'])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(hidden, serialized)
        self.assertNotIn('SECRET-HIDDEN', serialized)

    def test_ambiguous_names_preserve_manual_detail_without_claiming_complete_totals(self):
        employee, other = self.person(), self.person('002')
        self.link(employee)
        self.save('employee', dict(name=self.name(employee)), other)
        detail = self.meta(employee_id=employee)
        self.assertIsNone(detail['card_ledger']['registered'])
        self.assertTrue(detail['card_ledger']['scope_complete'])
        self.assertFalse(detail['card_ledger']['association_complete'])
        self.assertEqual(detail['linked_cards'][0]['card_id'], 1)
        self.assertEqual(detail['linked_cards'][0]['association_source'], 'explicit')
        for filters in (dict(operator_id='zhongxing'), dict(card_state='closed'), dict(card_state='candidate')):
            result = self.meta(view='cards', **filters)
            self.assertTrue(result['card_ledger_filter_incomplete'])
            self.assertTrue(result['card_ledger_totals']['scope_complete'])
            self.assertFalse(result['card_ledger_totals']['association_complete'])
            self.assertIsNone(result['card_ledger_totals']['registered'])
        self.assertEqual(self.meta(view='cards', card_state='review')['total'], 2)

    def test_hidden_duplicate_cannot_leave_an_otherwise_fully_eligible_quota(self):
        employee = self.person()
        self.make_eligible(employee)
        before = self.meta(2, employee_id=employee)
        self.assertTrue(all(op['status'] == 'eligible' for op in before['summary']['operators']))
        hidden = self.person('SECRET-HIDDEN', 'two')
        self.save('employee', dict(name=self.name(employee)), hidden)
        after = self.meta(2, employee_id=employee)
        self.assertTrue(all(op['status'] == 'needs_verification' for op in after['summary']['operators']))
        self.assertTrue(all(op['estimated_available'] is None for op in after['summary']['operators']))
        self.assertEqual(after['summary']['verified_total'], 0, 'original exact verification remains a source value')
        self.assertEqual(len(after['verifications']), 7)
        self.assertIsNone(after['card_ledger']['registered'])
        self.assertNotIn('SECRET-HIDDEN', json.dumps(after))

    def test_other_manual_claims_block_false_zero_and_eligibility_without_leaking_claimant(self):
        employee = self.person()
        self.make_eligible(employee)
        other = self.person('SECRET-HIDDEN', 'two')
        self.link(other, 3)
        self.assertTrue(all(op['status'] == 'eligible' for op in self.meta(2, employee_id=employee)['summary']['operators']))
        self.card(employee, 3)
        for actor in (1, 2):
            result = self.meta(actor, employee_id=employee)
            self.assertTrue(result['card_ledger']['scope_complete'])
            self.assertFalse(result['card_ledger']['association_complete'])
            self.assertIsNone(result['card_ledger']['registered'])
            self.assertEqual(result['linked_cards'], [])
            self.assertTrue(all(op['status'] == 'needs_verification' for op in result['summary']['operators']))
            self.assertTrue(all(op['estimated_available'] is None for op in result['summary']['operators']))
            text = json.dumps(result, ensure_ascii=False)
            for secret in (other, 'SECRET-HIDDEN'):
                self.assertNotIn(secret, text)
            if actor == 2:
                self.assertNotIn('13800000003', text)
        filtered = self.meta(2, view='cards', operator_id='zhongxing')
        self.assertTrue(filtered['card_ledger_filter_incomplete'])
        self.assertIsNone(filtered['card_ledger_totals']['registered'])

    def test_existing_same_employee_manual_claim_does_not_make_association_incomplete(self):
        employee = self.person()
        self.link(employee)
        self.card(employee)
        result = self.meta(employee_id=employee)
        self.assertTrue(result['card_ledger']['association_complete'])
        self.assertEqual(result['card_ledger']['registered'], 1)
        self.assertEqual(result['card_ledger']['name_matched_count'], 0)
        self.assertEqual(result['linked_cards'][0]['association_source'], 'explicit')

    def test_personal_employee_view_does_not_expand_employee_or_card_scope(self):
        employee = self.person(user_id=3)
        other = self.person('SECRET-OTHER', 'two')
        self.card(employee)
        self.card(other, 3)
        listed = self.meta(3, view='cards')
        self.assertEqual([row['id'] for row in listed['employees']], [employee])
        self.assertEqual(listed['card_ledger_totals']['registered'], 1)
        detail = self.meta(3, employee_id=employee)
        self.assertEqual([row['card_id'] for row in detail['linked_cards']], [1])
        self.assertNotIn('13800000003', json.dumps(detail))
        with self.assertRaises(auth.AccessError):
            self.meta(3, employee_id=other)

    def test_historical_date_read_does_not_backfill_dates_or_modify_records(self):
        employee = self.person()
        self.card(employee)
        before = self.snapshot()
        result = service.get(self.db, self.actor(), {'employee_id': [employee]}, '2026-10-01')
        self.assertEqual(result['linked_cards'][0]['activated_on'], '')
        self.assertEqual(result['linked_cards'][0]['linked_on'], '')
        self.assertEqual(result['units'], [])
        self.assertEqual(self.snapshot(), before)

    def test_hidden_same_name_card_redacts_entire_scope_for_manager_and_self(self):
        employee = self.person(user_id=3)
        self.card(employee)
        self.card(employee, 3)
        for actor in (2, 3):
            result = self.meta(actor, view='cards')
            row = result['employees'][0]
            self.assertFalse(row['card_ledger']['scope_complete'])
            self.assertTrue(all(row['card_ledger'][key] is None for key in ledger.COUNTS))
            self.assertIsNone(row['summary']['verified_total'])
            self.assertNotIn('13800000003', json.dumps(result))
            with self.assertRaises(auth.AccessError):
                self.meta(actor, employee_id=employee)
            filtered = self.meta(actor, view='cards', operator_id='minsheng')
            self.assertTrue(filtered['card_ledger_filter_incomplete'])
            self.assertIsNone(filtered['card_ledger_totals']['registered'])
        self.assertEqual(self.meta(4)['employees'], [])

    def test_unknown_issuer_keeps_quota_unknown_without_losing_verification_source(self):
        employee = self.person()
        self.save('verification', dict(scope='all', scope_id='', period='stock',
                  period_start=self.today, period_end=self.today, count=6, coverage='total',
                  verified_on=self.today, valid_until=self.today, source='合成已确认名下六张',
                  company_inventory_complete=False), employee)
        self.card(employee, carrier='中国联通')
        result = self.meta(employee_id=employee)
        summary = result['summary']
        self.assertEqual(summary['verified_total'], 6)
        self.assertIsNone(summary['company_current'])
        self.assertIsNone(summary['estimated_available'])
        self.assertEqual(result['verifications'][0]['count'], 6)
        self.assertEqual(result['verifications'][0]['source'], '合成已确认名下六张')
        for op in summary['operators']:
            self.assertIsNone(op['estimated_available'])
            self.assertIsNone(op['counts']['company_current'])
            self.assertIn('name_association_quota_unverified', [row['code'] for row in op['reasons']])
        self.assertEqual(result['units'], [])
        self.assertEqual(result['linked_cards'][0]['operator_id'], '')

    def test_manual_operator_filters_and_all_view_do_not_guess_automatic_issuer(self):
        employee = self.person()
        self.link(employee)
        self.card(employee, 2, carrier='民生电信', status='已注销')
        all_rows = self.meta(employee_id=employee)
        self.assertEqual(all_rows['card_ledger']['registered'], 2)
        known = self.meta(employee_id=employee, operator_id='minsheng')
        self.assertEqual(known['card_ledger']['registered'], 1)
        self.assertEqual(known['card_ledger']['name_matched_count'], 0)
        self.assertEqual(known['linked_cards'][0]['association_source'], 'explicit')
        self.assertEqual(self.meta(view='cards', query='13800000002')['total'], 1)
        self.assertEqual(self.meta(view='cards', card_state='closed')['card_ledger_totals']['registered'], 2)

    def test_stale_preview_rejects_owner_changes_and_stable_preview_still_applies(self):
        employee = self.person()
        self.card(employee)
        prepared = self.prepare('employee', dict(employment_status='unknown'), employee)
        self.card(employee, owner='合成其他人')
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(prepared)
        self.card(employee)
        prepared = self.prepare('employee', dict(employment_status='unknown'), employee)
        self.commit_preview(prepared)
        self.assertEqual(self.meta(employee_id=employee)['employee']['employment_status'], 'unknown')
        self.assertEqual(self.meta(employee_id=employee)['card_ledger']['registered'], 1)

    def test_scope_changes_and_global_duplicates_invalidate_existing_preview(self):
        employee = self.person()
        self.card(employee)
        prepared = self.prepare('employee', dict(employment_status='unknown'), employee)
        self.db.execute("UPDATE phone_card_offices SET office_id='two' WHERE card_id=1")
        self.db.commit()
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(prepared)
        other = self.person('002')
        prepared = self.prepare('employee', dict(employment_status='unknown'), employee)
        self.save('employee', dict(name=self.name(employee)), other)
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(prepared)

    def test_twenty_people_seventy_cards_reads_are_batched_paginated_and_readonly(self):
        people = [self.person(str(index).zfill(3)) for index in range(1, 21)]
        for index in range(4, 74):
            self.db.execute('INSERT INTO phone_cards(id,phone,owner,status,carrier) VALUES(?,?,?,?,?)',
                            (index, '139' + str(index).zfill(8), self.name(people[(index - 4) % 20]), '正常', '待确认'))
            self.db.execute('INSERT INTO phone_card_offices VALUES(?,?)', (index, 'one'))
        self.db.commit()
        before = self.snapshot()
        trace = []
        self.db.set_trace_callback(trace.append)
        try:
            first = self.meta(view='cards', page_size=20)
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(first['total'], 20)
        self.assertEqual(first['card_ledger_totals']['registered'], 70)
        self.assertEqual(first['card_ledger_totals']['name_matched_count'], 70)
        self.assertEqual(sum(row['card_ledger']['registered'] for row in first['employees']), 70)
        self.assertEqual(len([sql for sql in trace if sql.startswith('SELECT name,COUNT(*)')]), 1)
        self.assertEqual(len([sql for sql in trace if sql.startswith('SELECT c.id,c.phone')]), 1)
        self.assertEqual(self.snapshot(), before)
        second = self.meta(view='cards', page_size=20, page=2)
        self.assertEqual(second['card_ledger_totals'], first['card_ledger_totals'])


if __name__ == '__main__':
    unittest.main()
