"""Isolated derived-ledger regression tests; no application or production import."""
import copy
import json
import unittest

import employee_card_ledger as ledger
import test_employee_cards as fixture


class EmployeeLedgerTest(unittest.TestCase):
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

    def card(self, ident=1, **values):
        current = dict(status='正常', code_state='无待办', return_state='不需归还')
        current.update(values)
        values = current
        self.db.execute('UPDATE phone_cards SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', (*values.values(), ident))
        self.db.commit()

    def account(self, ident='ledger-account-1', phone='13800000001', office='one', **values):
        fields = dict(status='正常', account_type='小号', card_stability='已稳定', password_state='已修改')
        fields.update(values)
        self.db.execute('INSERT INTO accounts(id,registration_phone,status,account_type,card_stability,password_state) VALUES(?,?,?,?,?,?)',
                        (ident, phone, fields['status'], fields['account_type'], fields['card_stability'], fields['password_state']))
        self.db.execute('INSERT INTO account_offices VALUES(?,?)', (ident, office))
        self.db.commit()

    def link(self, person, card=1, operator='minsheng', activated='2026-10-01'):
        return self.save('card_link', dict(card_id=card, operator_id=operator, activated_on=activated, source='合成已授权实名依据'), person)['id']

    def test_empty_profiles_are_preserved_without_card_filters(self):
        self.person()
        result = self.meta(view='cards')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['card_ledger_totals']['registered'], 0)
        self.assertTrue(result['employees'][0]['card_ledger']['scope_complete'])
        self.assertEqual(self.meta(view='cards', card_state='unclosed')['total'], 0)

    def test_all_history_links_count_physical_cards_not_current_unit_only(self):
        person = self.person()
        self.card()
        self.card(2, status='已注销')
        self.account()
        unit = self.link(person)
        self.save('replacement', dict(unit_id=unit, card_id=2, source='合成已核对补换卡'), person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['summary']['company_current'], 1, 'quota count keeps its original one-unit meaning')
        self.assertEqual(detail['card_ledger']['registered'], 2)
        self.assertEqual(detail['card_ledger']['unclosed'], 1)
        self.assertEqual(detail['card_ledger']['closed'], 1)
        self.assertEqual(detail['card_ledger']['candidates'], 1)
        self.assertEqual(len(detail['linked_cards']), 2)
        self.assertEqual({row['card_id'] for row in detail['linked_cards']}, {1, 2})
        snapshot = self.legacy_snapshot()
        self.meta(view='cards', card_state='candidate')
        self.assertEqual(self.legacy_snapshot(), snapshot)

    def test_quota_release_and_cancel_request_never_mean_card_closed_or_pending_cancel(self):
        person = self.person()
        self.card()
        self.account()
        unit = self.link(person)
        self.save('cancel_request', dict(unit_id=unit, requested_on=self.today, source='仅额度注销申请'), person)
        self.operator(release_mode='confirmed')
        self.save('release', dict(unit_id=unit, released_on=self.today, source='仅运营商名额释放确认'), person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['summary']['company_current'], 0)
        self.assertEqual(detail['card_ledger']['unclosed'], 1)
        self.assertEqual(detail['card_ledger']['closed'], 0)
        self.assertEqual(detail['card_ledger']['pending_cancel'], 0)
        self.card(status='待注销')
        self.assertEqual(self.meta(employee_id=person)['card_ledger']['pending_cancel'], 1)

    def test_card_status_partition_is_exact_and_does_not_guess_historical_words(self):
        person = self.person()
        self.link(person)
        for status in ('正常', '欠费', '停机', '停用', '待注销'):
            with self.subTest(status=status):
                self.card(status=status)
                value = self.meta(employee_id=person)['card_ledger']
                self.assertEqual((value['unclosed'], value['closed']), (1, 0))
        for status in ('', '待确认', '已销户', '旧状态'):
            with self.subTest(status=status):
                self.card(status=status)
                detail = self.meta(employee_id=person)
                self.assertEqual((detail['card_ledger']['unclosed'], detail['card_ledger']['closed']), (0, 0))
                self.assertEqual(detail['card_ledger']['needs_review'], 1)
                self.assertEqual(detail['linked_cards'][0]['ledger_state'], 'review')
        self.card(status='已注销')
        self.assertEqual(self.meta(employee_id=person)['card_ledger']['closed'], 1)

    def test_missing_card_and_changed_phone_are_review_not_false_closed_or_unclosed(self):
        person = self.person()
        self.card()
        self.link(person)
        self.card(phone='13800000999', status='待注销')
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['linked_cards'][0]['phone_snapshot'], '13800000001')
        self.assertEqual(detail['linked_cards'][0]['phone'], '13800000999')
        self.assertEqual(detail['linked_cards'][0]['ledger_state'], 'review')
        self.assertFalse(detail['linked_cards'][0]['candidate'])
        self.assertEqual(detail['card_ledger']['pending_cancel'], 0, 'a different current phone cannot establish the historical card cancellation state')
        self.db.execute('DELETE FROM phone_card_offices WHERE card_id=1')
        self.db.execute('DELETE FROM phone_cards WHERE id=1')
        self.db.commit()
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['card_ledger']['registered'], 1)
        self.assertEqual(detail['card_ledger']['needs_review'], 1)
        self.assertEqual(detail['card_ledger']['unclosed'], 0)
        self.assertEqual(detail['card_ledger']['closed'], 0)

    def test_ready_candidate_predicate_preserves_blocked_small_and_single_big_semantics(self):
        person = self.person()
        self.card()
        self.account('blocked-small', account_type='小号', status='封禁', card_stability='待确认', password_state='待确认')
        self.account('active-big', account_type='大号')
        self.link(person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['card_ledger']['candidates'], 1, 'a blocked small still satisfies the existing related.some predicate')
        self.account('second-big', account_type='大号', status='封禁')
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['card_ledger']['candidates'], 0, 'all related large accounts count toward the old <=1 condition')
        self.assertIn('多个大号', '；'.join(detail['linked_cards'][0]['candidate_reasons']))

    def test_candidate_requires_complete_known_readiness_and_no_unfinished_or_unknown_tasks(self):
        person = self.person()
        self.card()
        self.link(person)
        self.assertEqual(self.meta(employee_id=person)['card_ledger']['candidates'], 0)
        self.account()
        self.assertEqual(self.meta(employee_id=person)['card_ledger']['candidates'], 1)
        for field, value, reason in [('code_state', '待接码', '待接码'), ('code_state', '暂无法接码', '暂无法接码'),
                                     ('return_state', '待归还', '待归还'), ('return_state', '待确认', '状态未核对'),
                                     ('code_state', '', '状态未核对')]:
            with self.subTest(field=field, value=value):
                self.card(**{field: value})
                detail = self.meta(employee_id=person)
                self.assertEqual(detail['card_ledger']['candidates'], 0)
                self.assertEqual(detail['card_ledger']['needs_review'], 1)
                self.assertIn(reason, '；'.join(detail['linked_cards'][0]['candidate_reasons']))
        self.card()
        self.db.execute("UPDATE accounts SET password_state='未修改'")
        self.db.commit()
        self.assertEqual(self.meta(employee_id=person)['card_ledger']['candidates'], 0)

    def test_hidden_related_accounts_block_candidate_without_exposing_account_fields(self):
        person = self.person(user_id=3)
        self.card()
        self.account()
        self.account('private-account-marker', office='two', account_type='大号', password_state='未修改')
        self.link(person)
        self.assertEqual(self.meta(employee_id=person)['card_ledger']['candidates'], 0)
        for actor in (2, 3):
            result = self.meta(actor, employee_id=person)
            self.assertEqual(result['card_ledger']['unclosed'], 1)
            self.assertEqual(result['card_ledger']['candidates'], 0)
            self.assertIn('授权范围不完整', '；'.join(result['linked_cards'][0]['candidate_reasons']))
            self.assertNotIn('private-account-marker', json.dumps(result))
            self.assertNotIn('未修改', json.dumps(result['linked_cards'], ensure_ascii=False))

    def test_partial_card_scope_redacts_all_ledger_counts_and_blocks_detail(self):
        person = self.person(user_id=3)
        self.card(3)
        self.link(person, 3)
        for actor in (2, 3):
            result = self.meta(actor, view='cards')
            value = result['employees'][0]['card_ledger']
            self.assertFalse(value['scope_complete'])
            self.assertTrue(all(value[key] is None for key in ledger.COUNTS))
            self.assertTrue(all(result['card_ledger_totals'][key] is None for key in ledger.COUNTS))
            self.assertNotIn('13800000003', json.dumps(result))
            self.assertEqual(self.meta(actor, view='cards', card_state='review')['total'], 1)
            with self.assertRaises(Exception) as error:
                self.meta(actor, employee_id=person)
            self.assertEqual(getattr(error.exception, 'status', None), 403)

    def test_unresolved_card_filter_totals_stay_unknown_even_when_no_row_can_be_confirmed(self):
        person = self.person()
        self.person('002')
        self.link(person, 3)
        for filters in (dict(operator_id='minsheng'), dict(card_state='candidate')):
            result = self.meta(2, view='cards', **filters)
            self.assertEqual(result['total'], 0)
            self.assertTrue(result['card_ledger_filter_incomplete'])
            self.assertFalse(result['card_ledger_totals']['scope_complete'])
            self.assertTrue(all(result['card_ledger_totals'][key] is None for key in ledger.COUNTS))
            self.assertNotIn('13800000003', json.dumps(result))
        for filters in (dict(query='合成实名员工002'), dict(office_id='two')):
            result = self.meta(2, view='cards', operator_id='minsheng', **filters)
            self.assertEqual(result['total'], 0)
            self.assertFalse(result['card_ledger_filter_incomplete'])
            self.assertEqual(result['card_ledger_totals']['registered'], 0)

    def test_operator_scope_state_filters_search_and_totals_are_consistent_across_pages(self):
        person = self.person()
        self.card()
        self.card(2, status='已注销')
        self.account()
        self.link(person)
        self.link(person, 2, 'zhongxing')
        other = self.person('002')
        self.assertEqual(self.meta(view='cards')['total'], 2)
        self.assertEqual(self.meta(view='cards', operator_id='minsheng')['total'], 1)
        self.assertEqual(self.meta(operator_id='minsheng')['total'], 2, 'old operator/status query semantics remain compatible')
        scoped = self.meta(employee_id=person, operator_id='zhongxing')
        self.assertEqual(len(scoped['linked_cards']), 1)
        self.assertEqual(scoped['card_ledger']['closed'], 1)
        self.assertEqual(self.meta(view='cards', card_state='candidate')['total'], 1)
        self.assertEqual(self.meta(view='cards', card_state='closed')['card_ledger_totals']['registered'], 2, 'card-state filters employees, not the matched employee card group')
        self.assertEqual(self.meta(view='cards', query='13800000002')['total'], 1)
        self.assertEqual(self.meta(view='cards', query='002')['total'], 2, 'text search covers both employee number and authorized historical phone')
        self.assertEqual(self.meta(view='cards', card_state='unclosed')['employees'][0]['id'], person)
        for i in range(3, 24):
            self.person(str(i).zfill(3))
        first, second = self.meta(view='cards', page_size=20), self.meta(view='cards', page_size=20, page=2)
        self.assertEqual(first['total'], 23)
        self.assertEqual(len(first['employees']), 20)
        self.assertEqual(len(second['employees']), 3)
        self.assertEqual(first['card_ledger_totals'], second['card_ledger_totals'])
        self.assertEqual(first['card_ledger_totals']['registered'], 2)
        with self.assertRaisesRegex(ValueError, '台账状态'):
            self.meta(card_state='not-a-real-filter')

    def test_reads_are_batched_and_never_modify_cards_accounts_or_ledger_records(self):
        people = [self.person(str(i).zfill(3)) for i in range(1, 5)]
        self.card()
        self.card(2)
        self.account()
        self.link(people[0])
        self.link(people[1], 2)
        before = {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                  for name in ('accounts', 'phone_cards', 'employee_card_units', 'employee_card_links', 'employee_card_events')}
        queries = []
        self.db.set_trace_callback(queries.append)
        self.meta(view='cards')
        self.db.set_trace_callback(None)
        self.assertEqual(sum('FROM phone_cards WHERE id IN (' in statement for statement in queries), 1)
        self.assertEqual(sum('FROM accounts a WHERE a.registration_phone IN (' in statement for statement in queries), 1)
        self.assertFalse(any('SELECT * FROM accounts' in statement for statement in queries))
        after = {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')] for name in before}
        self.assertEqual(before, after)

    def test_unknown_activation_date_is_preserved_and_never_fakes_today_or_clears_known_date(self):
        person = self.person()
        self.card()
        unit = self.link(person, activated='')
        row = self.db.execute('SELECT * FROM employee_card_units WHERE id=?', (unit,)).fetchone()
        self.assertEqual(row['phase'], 'active')
        self.assertEqual(row['activated_on'], '')
        self.assertEqual(row['requested_on'], '')
        self.save('verification', dict(scope='operator', scope_id='minsheng', period='day', period_start=self.today,
                                      period_end=self.today, count=0, coverage='external', verified_on=self.today,
                                      valid_until=self.today, source='合成当日外部实名数量核验', pending_unit_ids=[],
                                      company_inventory_complete=True), person)
        summary = self.meta(employee_id=person)['summary']
        self.assertEqual(summary['company_current'], 1)
        self.assertEqual(summary['pending_count'], 0)
        operator = next(op for op in summary['operators'] if op['operator_id'] == 'minsheng')
        self.assertIsNone(operator['estimated_available'])
        day = next(limit for limit in operator['limits'] if limit['period'] == 'day')
        self.assertIn('activation_date_unknown', day['issues'])
        self.card(2)
        known = self.link(person, 2, activated='2026-10-01')
        before = dict(self.db.execute('SELECT * FROM employee_card_units WHERE id=?', (known,)).fetchone())
        self.link(person, 2, activated='')
        after = dict(self.db.execute('SELECT * FROM employee_card_units WHERE id=?', (known,)).fetchone())
        self.assertEqual(before, after)


class PureLedgerTest(unittest.TestCase):
    def test_duplicate_stable_card_ids_are_counted_once_and_conflicts_are_reviewed(self):
        bundle = dict(operators=[dict(id='one', name='合成运营商')],
                      units=[dict(id='unit', employee_id='person', operator_id='one', activated_on='')],
                      links=[dict(id='link', unit_id='unit', employee_id='person', card_id=1, phone_snapshot='13800000001', linked_on='2026-11-01')])
        context = dict(cards={1: dict(id=1, phone='13800000001', carrier='合成运营商', status='已注销')}, accounts_by_phone={}, incomplete_account_phones=set())
        bundle['links'].append(copy.deepcopy(bundle['links'][0]))
        counts, rows = ledger.derive('person', bundle, context)
        self.assertEqual(counts['registered'], 1)
        self.assertEqual(counts['closed'], 1)
        bundle['links'][1]['phone_snapshot'] = '13800000999'
        counts, rows = ledger.derive('person', bundle, context)
        self.assertEqual(counts['registered'], 1)
        self.assertEqual(counts['needs_review'], 1)
        self.assertEqual(rows[0]['ledger_state'], 'review')


if __name__ == '__main__':
    unittest.main()
