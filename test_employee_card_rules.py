"""Pure synthetic domain regressions. Run through run_isolated_tests.py."""
from copy import deepcopy
import unittest

import employee_card_rules as domain


TODAY = '2026-11-02'


def fixture():
    employee = {'id': 'employee-001', 'name': '合成员工', 'employee_no': '0001',
                'birth_date': '2000-11-02', 'employment_status': 'active',
                'consent_confirmed': True}
    operators = [{'id': 'op-a', 'name': '合成运营商', 'network': 'telecom',
                  'channel_id': '', 'age_confirmed': True, 'min_age': 21,
                  'source': '合成规则依据', 'effective_from': '2026-01-01',
                  'verification_valid_days': None, 'release_mode': 'confirmed',
                  'release_delay_days': None, 'waiting_days': 0,
                  'waiting_confirmed': True}]
    units = [{'id': 'unit-1', 'operator_id': 'op-a', 'network': 'unknown',
              'channel_id': '', 'requested_on': '2026-01-01',
              'activated_on': '2026-01-01', 'release_confirmed_on': '',
              'release_source': '', 'phase': 'active'}]
    rules = [dict(id='global-stock', scope='all', scope_id='', period='stock', limit=10),
             dict(id='op-day', scope='operator', scope_id='op-a', period='day', limit=2),
             dict(id='unicom-month', scope='network', scope_id='unicom', period='month', limit=3)]
    for rule in rules:
        rule.update(pending_counts=True, source='合成规则依据', effective_from='2026-01-01', confirmed=True)
    checks = [dict(id='check-stock', scope='all', scope_id='', period='stock',
                   period_start='', period_end='', count=5, coverage='total',
                   verified_on=TODAY, valid_until='2026-11-30', source='合成全口径核验',
                   unit_ids=['unit-1'], pending_unit_ids=[], company_inventory_complete=True),
              dict(id='check-day', scope='operator', scope_id='op-a', period='day',
                   period_start=TODAY, period_end=TODAY, count=0, coverage='total',
                   verified_on=TODAY, valid_until='2026-11-30', source='合成当日核验',
                   unit_ids=[], pending_unit_ids=[], company_inventory_complete=True)]
    return dict(employee=employee, operators=operators, units=units,
                verifications=checks, applications=[], today=TODAY, rules=rules)


def run(data):
    return domain.evaluate_employee(**data)


def result(data):
    return run(data)['operators'][0]


def limit(data, rule_id='global-stock'):
    return next(row for row in result(data)['limits'] if row['rule_id'] == rule_id)


def pending_unit(ident='unit-2'):
    return {'id': ident, 'operator_id': 'op-a', 'network': 'unknown', 'channel_id': '',
            'requested_on': TODAY, 'activated_on': '', 'release_confirmed_on': '',
            'release_source': '', 'phase': 'pending'}


class EmployeeCardRulesTest(unittest.TestCase):
    def test_known_rules_positive_and_no_double_company_addition(self):
        data = fixture()
        summary = run(data)
        self.assertEqual(summary['age_years'], 26)
        self.assertEqual(summary['company_lifetime'], 1)
        self.assertEqual(summary['company_current'], 1)
        self.assertEqual(summary['verified_total'], 5)
        self.assertEqual(summary['other_current'], 4)
        self.assertEqual(limit(data)['used'], 5)
        self.assertEqual(result(data)['estimated_available'], 2)
        self.assertEqual(result(data)['status'], 'eligible')

    def test_age_before_on_and_after_twenty_first_birthday(self):
        data = fixture()
        data['employee']['birth_date'] = '2005-11-03'
        self.assertEqual(run(data)['age_years'], 20)
        row = result(data)
        self.assertEqual(row['age_threshold_on'], '2026-11-03')
        self.assertEqual(row['status'], 'ineligible')
        self.assertEqual(row['estimated_available'], 0)
        self.assertEqual(row['next_followup_on'], '2026-11-03')
        data['employee']['birth_date'] = '2005-11-02'
        self.assertEqual(result(data)['status'], 'eligible')
        data['employee']['birth_date'] = '2005-11-01'
        self.assertEqual(run(data)['age_years'], 21)

    def test_leap_birthday_uses_march_first_and_preserves_exact_strings(self):
        self.assertEqual(domain.age_on('2004-02-29', '2025-02-28'), 20)
        self.assertEqual(domain.age_on('2004-02-29', '2025-03-01'), 21)
        self.assertEqual(domain.threshold_on('2004-02-29', 21), '2025-03-01')
        self.assertEqual(domain.age_on('2004-02-29', '2024-02-29'), 20)
        data = fixture()
        run(data)
        self.assertEqual(data['employee']['employee_no'], '0001')

    def test_missing_future_or_unauthorized_birth_date_is_unknown(self):
        for birth in ('', 'not-a-date', '2030-01-01'):
            data = fixture()
            data['employee']['birth_date'] = birth
            self.assertIsNone(run(data)['age_years'])
            self.assertEqual(result(data)['status'], 'needs_verification')
            self.assertIsNone(result(data)['estimated_available'])
        data = fixture()
        data['employee']['consent_confirmed'] = False
        self.assertIsNone(run(data)['age_years'])
        self.assertIsNone(result(data)['age_threshold_on'])

    def test_other_channels_missing_is_never_zero(self):
        data = fixture()
        data['verifications'] = data['verifications'][1:]
        summary = run(data)
        self.assertEqual(summary['company_current'], 1)
        self.assertIsNone(summary['other_current'])
        self.assertIsNone(summary['verified_total'])
        self.assertIsNone(result(data)['estimated_available'])

    def test_including_company_total_does_not_add_three_again(self):
        data = fixture()
        for ident in ('unit-2', 'unit-3'):
            data['units'].append({**data['units'][0], 'id': ident})
        data['verifications'][0]['unit_ids'] = ['unit-1', 'unit-2', 'unit-3']
        self.assertEqual(limit(data)['used'], 5)
        self.assertEqual(run(data)['company_current'], 3)
        self.assertEqual(run(data)['other_current'], 2)

    def test_external_only_adds_current_company_exactly_once(self):
        data = fixture()
        data['verifications'][0].update(count=4, coverage='external', unit_ids=[])
        self.assertEqual(limit(data)['used'], 5)
        self.assertEqual(run(data)['other_current'], 4)
        self.assertEqual(run(data)['verified_total'], 5)

    def test_unknown_coverage_or_company_greater_than_total_is_conflict(self):
        for change in ({'coverage': 'unknown'}, {'count': 0}, {'coverage': 'external'}):
            data = fixture()
            data['verifications'][0].update(change)
            self.assertEqual(result(data)['status'], 'needs_verification')
            self.assertIsNone(result(data)['estimated_available'])
            self.assertIsNone(run(data)['other_current'])

    def test_uncovered_pending_is_reserved_in_every_relevant_scope(self):
        data = fixture()
        data['units'].append(pending_unit())
        self.assertEqual(run(data)['pending_count'], 1)
        self.assertEqual(limit(data)['used'], 5)
        self.assertEqual(limit(data)['reserved'], 1)
        self.assertEqual(limit(data, 'op-day')['reserved'], 1)
        self.assertEqual(result(data)['estimated_available'], 1)

    def test_covered_pending_is_not_counted_twice(self):
        data = fixture()
        data['units'].append(pending_unit())
        data['verifications'][0].update(count=6, pending_unit_ids=['unit-2'])
        data['verifications'][1].update(count=1, pending_unit_ids=['unit-2'])
        self.assertEqual(limit(data)['used'], 6)
        self.assertEqual(limit(data)['reserved'], 0)
        self.assertEqual(result(data)['estimated_available'], 1)

    def test_pending_activation_conversion_keeps_the_same_quota_unit(self):
        data = fixture()
        data['units'].append({**pending_unit(), 'phase': 'active', 'activated_on': TODAY})
        data['verifications'][0].update(count=6, pending_unit_ids=['unit-2'])
        data['verifications'][1].update(count=1, pending_unit_ids=['unit-2'])
        self.assertEqual(run(data)['pending_count'], 0)
        self.assertEqual(run(data)['company_current'], 2)
        self.assertEqual(limit(data)['used'], 6)
        self.assertEqual(limit(data, 'op-day')['used'], 1)
        self.assertEqual(result(data)['estimated_available'], 1)

    def test_unknown_pending_policy_does_not_invent_capacity(self):
        data = fixture()
        data['units'].append(pending_unit())
        data['rules'][0]['pending_counts'] = None
        self.assertIsNone(result(data)['estimated_available'])
        self.assertEqual(run(data)['verified_total'], 5)

    def test_nonoccupying_carrier_pending_still_has_company_reservation(self):
        data = fixture()
        data['units'].append(pending_unit())
        for rule in data['rules']:
            rule['pending_counts'] = False
        self.assertEqual(limit(data)['used'], 5)
        self.assertEqual(limit(data)['reserved'], 1)
        self.assertEqual(result(data)['estimated_available'], 1)

    def test_company_units_added_after_snapshot_adjust_used_once(self):
        data = fixture()
        data['today'] = '2026-11-03'
        data['units'].append({**data['units'][0], 'id': 'unit-2', 'activated_on': '2026-11-03'})
        data['verifications'][1].update(verified_on='2026-11-03', period_start='2026-11-03',
                                        period_end='2026-11-03', count=1, unit_ids=['unit-2'])
        self.assertEqual(limit(data)['used'], 6)
        self.assertEqual(run(data)['verified_total'], 5)
        self.assertEqual(result(data)['estimated_available'], 1)

    def test_late_historical_entry_is_not_guessed_into_old_total(self):
        data = fixture()
        data['units'].append({**data['units'][0], 'id': 'unit-2'})
        self.assertIn('uncovered_existing_company_unit', limit(data)['issues'])
        self.assertIsNone(result(data)['estimated_available'])

    def test_stop_blocked_removed_or_cancellation_request_is_not_release(self):
        for status in ('停用', '已注销', '封禁', '注销申请已提交', '移出公司列表'):
            data = fixture()
            data['units'][0].update(card_status=status, removed=True)
            self.assertEqual(run(data)['company_current'], 1)
            self.assertEqual(limit(data)['used'], 5)

    def test_release_requires_policy_and_evidence(self):
        for mode, source in (('unknown', '合成确认'), ('confirmed', '')):
            data = fixture()
            data['units'][0].update(phase='released', release_confirmed_on=TODAY, release_source=source)
            data['operators'][0]['release_mode'] = mode
            self.assertEqual(run(data)['company_current'], 1)
            self.assertIsNone(result(data)['estimated_available'])

    def test_confirmed_release_preserves_lifetime_and_is_idempotent(self):
        data = fixture()
        data['today'] = '2026-11-03'
        data['units'][0].update(phase='released', release_confirmed_on='2026-11-03', release_source='合成运营商确认')
        data['verifications'][1].update(verified_on='2026-11-03', period_start='2026-11-03', period_end='2026-11-03')
        summary = run(data)
        self.assertEqual(summary['company_lifetime'], 1)
        self.assertEqual(summary['company_current'], 0)
        self.assertEqual(limit(data)['used'], 4)
        data['units'].append(deepcopy(data['units'][0]))
        self.assertEqual(run(data), summary)

    def test_release_does_not_rewind_month_activations(self):
        data = fixture()
        data['today'] = '2026-11-03'
        data['operators'][0]['network'] = 'unicom'
        data['units'][0].update(activated_on=TODAY, phase='released',
                                 release_confirmed_on='2026-11-03', release_source='合成运营商确认')
        data['verifications'][0].update(count=1)
        data['verifications'][1].update(verified_on='2026-11-03', period_start='2026-11-03', period_end='2026-11-03')
        data['verifications'].append({**data['verifications'][0], 'id': 'check-network',
                                      'scope': 'network', 'scope_id': 'unicom', 'period': 'month',
                                      'period_start': '2026-11-01', 'period_end': '2026-11-30'})
        self.assertEqual(run(data)['company_current'], 0)
        self.assertEqual(limit(data, 'unicom-month')['used'], 1)

    def test_release_delay_is_never_invented_or_applied_early(self):
        data = fixture()
        data['operators'][0].update(release_mode='after_days', release_delay_days=7)
        data['units'][0].update(phase='released', cancelled_on='2026-11-01',
                                 release_confirmed_on=TODAY, release_source='合成确认')
        self.assertEqual(run(data)['company_current'], 1)
        self.assertIn('release_before_allowed_date', limit(data)['issues'])
        data['operators'][0]['release_delay_days'] = None
        self.assertIn('release_policy_incomplete', limit(data)['issues'])

    def test_replacement_and_duplicate_import_do_not_create_more_units(self):
        data = fixture()
        data['units'][0]['members'] = [{'card_id': 101}, {'card_id': 102}]
        data['units'].append(deepcopy(data['units'][0]))
        self.assertEqual(run(data)['company_lifetime'], 1)
        self.assertEqual(run(data)['company_current'], 1)
        self.assertEqual(result(data)['status'], 'eligible')
        data['units'][1]['activated_on'] = '2026-02-01'
        self.assertIn('unit_duplicate_conflict', run(data)['issues'])
        self.assertIsNone(result(data)['estimated_available'])

    def test_full_stock_has_no_promised_release_or_application_date(self):
        data = fixture()
        data['verifications'][0]['count'] = 10
        row = result(data)
        self.assertEqual(row['status'], 'ineligible')
        self.assertEqual(row['estimated_available'], 0)
        self.assertIsNone(row['next_followup_on'])
        self.assertEqual(row['followup_action'], 'verify')

    def test_daily_limit_uses_calendar_day_and_new_day_needs_new_check(self):
        data = fixture()
        data['verifications'][1]['count'] = 2
        self.assertEqual(result(data)['next_followup_on'], '2026-11-03')
        self.assertEqual(result(data)['followup_action'], 'apply')
        data['today'] = '2026-11-03'
        self.assertEqual(result(data)['status'], 'needs_verification')
        self.assertIn('verification_window_mismatch', limit(data, 'op-day')['issues'])

    def test_network_month_limit_combines_different_issuers(self):
        data = fixture()
        data['operators'][0]['network'] = 'unicom'
        data['operators'].append({**data['operators'][0], 'id': 'op-b', 'name': '合成另一运营商'})
        data['units'][0]['activated_on'] = '2026-11-01'
        data['units'].append({**data['units'][0], 'id': 'unit-2', 'operator_id': 'op-b'})
        data['rules'].append({**data['rules'][1], 'id': 'op-b-day', 'scope_id': 'op-b'})
        data['verifications'][0].update(count=3, unit_ids=['unit-1', 'unit-2'])
        data['verifications'].append({**data['verifications'][1], 'id': 'check-b-day', 'scope_id': 'op-b'})
        data['verifications'].append({**data['verifications'][0], 'id': 'check-network',
                                      'scope': 'network', 'scope_id': 'unicom', 'period': 'month',
                                      'period_start': '2026-11-01', 'period_end': '2026-11-30'})
        rows = run(data)['operators']
        self.assertEqual([row['status'] for row in rows], ['ineligible', 'ineligible'])
        self.assertEqual([row['next_followup_on'] for row in rows], ['2026-12-01', '2026-12-01'])
        self.assertEqual([row['estimated_available'] for row in rows], [0, 0])

    def test_unknown_network_cannot_avoid_network_limit(self):
        data = fixture()
        data['operators'][0]['network'] = 'unknown'
        self.assertEqual(result(data)['status'], 'needs_verification')
        self.assertIn('operator_network_unknown', [row['code'] for row in result(data)['reasons']])

    def test_expiry_unknown_stale_and_same_day_conflict_are_not_zero(self):
        for update in ({'valid_until': ''}, {'valid_until': '2026-11-01'}):
            data = fixture()
            data['verifications'][0].update(update)
            self.assertIsNone(result(data)['estimated_available'])
            self.assertIsNone(run(data)['verified_total'])
        data = fixture()
        data['verifications'].append({**data['verifications'][0], 'id': 'check-conflict', 'count': 6})
        self.assertIn('verification_conflict', limit(data)['issues'])
        data['verifications'][-1]['recorded_at'] = 2
        self.assertEqual(run(data)['verified_total'], 6)

    def test_configured_verification_validity_has_followup_date(self):
        data = fixture()
        data['operators'][0]['verification_valid_days'] = 3
        for check in data['verifications']:
            check['valid_until'] = ''
        self.assertEqual(result(data)['status'], 'eligible')
        self.assertEqual(result(data)['next_followup_on'], '2026-11-05')
        self.assertEqual(result(data)['followup_action'], 'verify')

    def test_unknown_wait_and_unknown_failure_never_make_daily_retry(self):
        data = fixture()
        data['operators'][0]['waiting_confirmed'] = False
        self.assertIsNone(result(data)['estimated_available'])
        data = fixture()
        data['applications'] = [{'id': 'app-1', 'operator_id': 'op-a', 'unit_ids': [],
                                  'applied_on': TODAY, 'result': 'failed', 'failure_reason': '',
                                  'retry_on': '', 'retry_source': ''}]
        self.assertEqual(result(data)['status'], 'needs_verification')
        self.assertIsNone(result(data)['next_followup_on'])
        self.assertEqual(result(data)['followup_action'], 'resolve')

    def test_explicit_retry_and_age_gate_choose_later_simultaneous_date(self):
        data = fixture()
        data['employee']['birth_date'] = '2005-11-03'
        data['applications'] = [{'id': 'app-1', 'operator_id': 'op-a', 'unit_ids': [],
                                  'applied_on': TODAY, 'result': 'failed', 'failure_reason': '合成明确原因',
                                  'retry_on': '2026-11-05', 'retry_source': '合成运营商告知'}]
        self.assertEqual(result(data)['next_followup_on'], '2026-11-05')
        self.assertEqual(result(data)['followup_action'], 'apply')
        self.assertEqual(result(data)['status'], 'ineligible')

    def test_wait_needs_explicit_basis_and_source_rule(self):
        data = fixture()
        data['operators'][0]['waiting_days'] = 3
        data['applications'] = [{'id': 'app-1', 'operator_id': 'op-a', 'unit_ids': [],
                                  'applied_on': '2026-11-01', 'result': 'submitted'}]
        self.assertEqual(result(data)['status'], 'needs_verification')
        data['operators'][0]['waiting_basis'] = 'application'
        self.assertEqual(result(data)['status'], 'ineligible')
        self.assertEqual(result(data)['next_followup_on'], '2026-11-04')

    def test_rules_not_confirmed_do_not_erase_valid_verified_count(self):
        data = fixture()
        data['rules'][0]['source'] = ''
        self.assertEqual(run(data)['verified_total'], 5)
        self.assertIsNone(result(data)['estimated_available'])

    def test_cancelled_pending_in_old_total_requires_new_verification(self):
        data = fixture()
        data['units'].append({**pending_unit(), 'phase': 'cancelled'})
        data['verifications'][0].update(count=6, pending_unit_ids=['unit-2'])
        self.assertIn('covered_pending_outcome_unverified', limit(data)['issues'])
        self.assertIsNone(result(data)['estimated_available'])

    def test_malformed_claims_are_conservative_and_inputs_are_unchanged(self):
        data = fixture()
        before = deepcopy(data)
        run(data)
        self.assertEqual(data, before)
        variants = []
        for target, key, value in [('check', 'count', []), ('operator', 'network', []),
                                   ('unit', 'operator_id', []), ('rule', 'limit', True),
                                   ('unit', 'phase', {}), ('check', 'source', {})]:
            candidate = fixture()
            group = {'check': 'verifications', 'operator': 'operators', 'unit': 'units', 'rule': 'rules'}[target]
            candidate[group][0][key] = value
            variants.append(candidate)
        for candidate in variants:
            self.assertIsNone(result(candidate)['estimated_available'])
        with self.assertRaises(ValueError):
            domain.evaluate_employee(**{**fixture(), 'today': 'invalid'})

    def test_historical_release_uses_frozen_confirmed_policy(self):
        data = fixture()
        data['today'] = '2026-11-03'
        data['units'][0].update(phase='released', release_confirmed_on='2026-11-03',
                                 release_source='合成释放确认',
                                 release_policy={'operator_id': 'op-a', 'release_mode': 'confirmed',
                                                 'release_delay_days': None, 'source': '合成旧规则',
                                                 'effective_from': '2026-01-01'})
        data['operators'][0]['release_mode'] = 'unknown'
        data['verifications'][1].update(verified_on='2026-11-03', period_start='2026-11-03', period_end='2026-11-03')
        self.assertEqual(run(data)['company_current'], 0)
        self.assertEqual(limit(data)['used'], 4)
        data['operators'][0].update(release_mode='after_days', release_delay_days=30)
        self.assertEqual(run(data)['company_current'], 0)
        self.assertEqual(limit(data)['used'], 4)

    def test_invalid_release_snapshot_never_falls_back_to_current_rule(self):
        for frozen in ({}, 'invalid', {'operator_id': 'op-other', 'release_mode': 'confirmed',
                                       'source': '合成依据', 'effective_from': '2026-01-01'}):
            data = fixture()
            data['units'][0].update(phase='released', release_confirmed_on=TODAY,
                                     release_source='合成确认', release_policy=frozen)
            self.assertEqual(run(data)['company_current'], 1)
            self.assertIn('release_policy_unconfirmed', limit(data)['issues'])

    def test_frozen_waiting_release_policy_is_not_weakened_by_new_config(self):
        data = fixture()
        data['units'][0].update(phase='released', cancelled_on='2026-11-01',
                                 release_confirmed_on=TODAY, release_source='合成确认',
                                 release_policy={'release_mode': 'after_days', 'release_delay_days': 7,
                                                 'source': '合成旧规则', 'effective_from': '2026-01-01'})
        self.assertEqual(data['operators'][0]['release_mode'], 'confirmed')
        self.assertEqual(run(data)['company_current'], 1)
        self.assertIn('release_before_allowed_date', limit(data)['issues'])

    def test_verification_date_must_be_inside_claimed_day_or_month(self):
        data = fixture()
        data['verifications'][1]['verified_on'] = '2026-11-01'
        self.assertIn('verification_outside_window', limit(data, 'op-day')['issues'])
        self.assertIsNone(result(data)['estimated_available'])

    def test_conflicting_or_malformed_unit_network_is_not_silently_ignored(self):
        for network in ('unicom', ['telecom']):
            data = fixture()
            data['units'][0]['network'] = network
            self.assertEqual(result(data)['status'], 'needs_verification')
            self.assertIsNone(result(data)['estimated_available'])

    def test_unknown_network_of_old_activation_does_not_consume_month_capacity(self):
        data = fixture()
        data['operators'][0]['network'] = 'unicom'
        data['operators'].append({**data['operators'][0], 'id': 'op-b', 'network': 'unknown'})
        data['units'][0]['activated_on'] = '2026-11-01'
        data['units'].append({**data['units'][0], 'id': 'old-unit', 'operator_id': 'op-b', 'activated_on': '2026-01-01'})
        data['verifications'][0].update(count=2, unit_ids=['unit-1', 'old-unit'])
        data['verifications'].append({**data['verifications'][0], 'id': 'check-network',
                                      'scope': 'network', 'scope_id': 'unicom', 'period': 'month',
                                      'period_start': '2026-11-01', 'period_end': '2026-11-30',
                                      'count': 1, 'unit_ids': ['unit-1']})
        self.assertEqual(result(data)['status'], 'eligible')
        self.assertEqual(result(data)['estimated_available'], 2)

    def test_default_eight_rules_still_show_independent_operator_stock_check(self):
        data = fixture()
        names = [('minsheng', '民生'), ('zhongxing', '中兴'), ('changcheng', '长城'),
                 ('liantong', '联通'), ('dianxin', '电信'), ('yidong', '移动')]
        data['operators'] = [{**data['operators'][0], 'id': ident, 'name': name,
                              'network': 'unknown', 'age_confirmed': ident == 'minsheng',
                              'min_age': 21 if ident == 'minsheng' else None,
                              'waiting_confirmed': False, 'waiting_days': None} for ident, name in names]
        data['units'][0]['operator_id'] = 'minsheng'
        global_rule, day_rule, network_rule = deepcopy(data['rules'])
        data['rules'] = [global_rule, network_rule] + [{**day_rule, 'id': ident + '-day', 'scope_id': ident} for ident, _ in names]
        data['verifications'][1]['scope_id'] = 'minsheng'
        data['verifications'].append({**data['verifications'][0], 'id': 'minsheng-stock',
                                      'scope': 'operator', 'scope_id': 'minsheng', 'count': 3})
        before_rules = deepcopy(data['rules'])
        row = result(data)
        self.assertEqual(len(data['rules']), 8)
        self.assertEqual(data['rules'], before_rules)
        self.assertEqual(row['counts']['verified_total'], 3)
        self.assertEqual(row['counts']['other_current'], 2)
        self.assertEqual(row['count_evidence']['verification_id'], 'minsheng-stock')
        self.assertEqual(row['count_evidence']['issues'], [])
        self.assertEqual(row['status'], 'needs_verification')
        self.assertIsNone(row['estimated_available'])
        self.assertFalse(any(item['scope'] == 'operator' and item['period'] == 'stock' for item in row['limits']))

    def test_operator_external_stock_evidence_never_creates_a_stock_cap(self):
        data = fixture()
        data['verifications'].append({**data['verifications'][0], 'id': 'operator-stock',
                                      'scope': 'operator', 'scope_id': 'op-a', 'count': 2,
                                      'coverage': 'external', 'unit_ids': []})
        row = result(data)
        self.assertEqual(row['counts']['verified_total'], 3)
        self.assertEqual(row['counts']['other_current'], 2)
        self.assertEqual(row['status'], 'eligible')
        self.assertEqual(row['estimated_available'], 2)
        self.assertEqual(len(row['limits']), 2)
        self.assertNotIn('limit', row['count_evidence'])

    def test_expired_distribution_evidence_has_clear_warning_without_faking_cap(self):
        data = fixture()
        data['verifications'].append({**data['verifications'][0], 'id': 'operator-stock',
                                      'scope': 'operator', 'scope_id': 'op-a', 'count': 3,
                                      'valid_until': '2026-11-01'})
        row = result(data)
        self.assertIsNone(row['counts']['verified_total'])
        self.assertIn('verification_expired', row['count_evidence']['issues'])
        self.assertIn('核验资料已过有效期，需要重新核验', row['count_evidence']['issue_labels'])
        self.assertEqual(row['estimated_available'], 2)

    def test_operator_count_cannot_silently_exceed_all_operator_total(self):
        data = fixture()
        data['verifications'].append({**data['verifications'][0], 'id': 'operator-stock',
                                      'scope': 'operator', 'scope_id': 'op-a', 'count': 6})
        row = result(data)
        self.assertIn('cross_scope_total_conflict', row['count_evidence']['issues'])
        self.assertEqual(row['status'], 'needs_verification')
        self.assertIsNone(row['estimated_available'])

    def test_limit_issues_have_user_facing_chinese_explanations(self):
        data = fixture()
        data['verifications'] = []
        row = result(data)
        self.assertEqual(row['limits'][0]['issue_labels'], ['尚未登记这个范围与统计期间的名下核验'])
        self.assertFalse(any('verification_missing' in reason['text'] for reason in row['reasons']))

    def test_frozen_historical_network_survives_current_config_change(self):
        data = fixture()
        data['operators'][0]['network'] = 'telecom'
        data['operators'].append({**data['operators'][0], 'id': 'op-b', 'network': 'unicom'})
        data['units'][0].update(network='unicom', activated_on='2026-11-01',
                                 network_policy={'network': 'unicom', 'channel_id': 'old-channel',
                                                 'operator_id': 'op-a', 'source': '合成历史网络依据',
                                                 'effective_from': '2026-01-01', 'recorded_on': TODAY})
        data['rules'].append({**data['rules'][1], 'id': 'op-b-day', 'scope_id': 'op-b'})
        data['verifications'][0].update(count=3)
        data['verifications'].append({**data['verifications'][1], 'id': 'check-b-day', 'scope_id': 'op-b'})
        data['verifications'].append({**data['verifications'][0], 'id': 'check-network',
                                      'scope': 'network', 'scope_id': 'unicom', 'period': 'month',
                                      'period_start': '2026-11-01', 'period_end': '2026-11-30'})
        rows = run(data)['operators']
        self.assertEqual(rows[0]['status'], 'eligible')
        self.assertEqual(rows[1]['status'], 'ineligible')
        month = next(item for item in rows[1]['limits'] if item['rule_id'] == 'unicom-month')
        self.assertEqual(month['used'], 3)

    def test_explicit_unknown_historical_network_never_uses_current_mapping(self):
        data = fixture()
        data['operators'][0]['network'] = 'unicom'
        data['units'][0].update(activated_on='2026-11-01', network='unicom',
                                 network_policy={'network': 'unknown', 'channel_id': '',
                                                 'operator_id': 'op-a', 'source': '发卡时实际网络未核验',
                                                 'effective_from': '', 'recorded_on': TODAY})
        data['verifications'].append({**data['verifications'][0], 'id': 'check-network',
                                      'scope': 'network', 'scope_id': 'unicom', 'period': 'month',
                                      'period_start': '2026-11-01', 'period_end': '2026-11-30',
                                      'count': 0, 'unit_ids': []})
        self.assertEqual(run(data)['verified_total'], 5)
        self.assertEqual(run(data)['company_current'], 1)
        self.assertIn('unit_scope_unknown', limit(data, 'unicom-month')['issues'])
        self.assertIsNone(result(data)['estimated_available'])

    def test_invalid_known_historical_network_is_not_trusted(self):
        for effective, issuer in (('2026-11-03', 'op-a'), ('2026-01-01', 'op-other'), ('', 'op-a')):
            data = fixture()
            data['units'][0]['network_policy'] = {'network': 'telecom', 'channel_id': '',
                                                  'operator_id': issuer, 'source': '合成依据',
                                                  'effective_from': effective, 'recorded_on': TODAY}
            self.assertIn('network_policy_invalid', limit(data)['issues'])
            self.assertIsNone(result(data)['estimated_available'])

    def test_total_without_inventory_attestation_keeps_total_not_other_guess(self):
        data = fixture()
        data['verifications'][0].pop('company_inventory_complete')
        summary = run(data)
        self.assertEqual(summary['verified_total'], 5)
        self.assertEqual(limit(data)['used'], 5)
        self.assertIsNone(summary['other_current'])
        self.assertFalse(summary['company_inventory_complete'])
        self.assertEqual(summary['count_basis']['company'], 'registered_units')
        self.assertEqual(summary['count_basis']['other_current'], 'unknown')
        self.assertEqual(result(data)['estimated_available'], 2)

    def test_unlinked_company_inventory_zero_does_not_mean_all_cards_external(self):
        data = fixture()
        data['units'] = []
        data['verifications'][0].update(unit_ids=[], count=10, company_inventory_complete=False)
        summary = run(data)
        self.assertEqual(summary['company_current'], 0)
        self.assertEqual(summary['company_lifetime'], 0)
        self.assertEqual(summary['verified_total'], 10)
        self.assertIsNone(summary['other_current'])
        self.assertFalse(summary['company_inventory_complete'])
        self.assertEqual(result(data)['status'], 'ineligible')
        self.assertEqual(result(data)['estimated_available'], 0)
        self.assertTrue(summary['count_basis']['notes'])

    def test_external_without_inventory_completeness_cannot_understate_total(self):
        data = fixture()
        data['verifications'][0].update(coverage='external', count=3, unit_ids=[],
                                        company_inventory_complete=False)
        summary = run(data)
        self.assertEqual(summary['other_current'], 3)
        self.assertIsNone(summary['verified_total'])
        self.assertIsNone(summary['estimated_available'])
        self.assertIsNone(limit(data)['used'])
        self.assertIsNone(limit(data)['available'])
        self.assertIn('company_inventory_incomplete', limit(data)['issues'])
        self.assertEqual(summary['count_basis']['other_current'], 'explicit_external_verification')
        self.assertEqual(result(data)['status'], 'needs_verification')

    def test_external_complete_company_inventory_can_form_verified_combined_total(self):
        data = fixture()
        data['verifications'][0].update(coverage='external', count=3, unit_ids=[],
                                        company_inventory_complete=True)
        summary = run(data)
        self.assertEqual(summary['other_current'], 3)
        self.assertEqual(summary['verified_total'], 4)
        self.assertTrue(summary['company_inventory_complete'])
        self.assertEqual(summary['count_basis']['verified_total'], 'external_plus_complete_company')
        self.assertEqual(limit(data)['used'], 4)
        self.assertEqual(result(data)['status'], 'eligible')

    def test_operator_count_evidence_also_requires_completeness_for_external_inference(self):
        data = fixture()
        data['verifications'].append({**data['verifications'][0], 'id': 'operator-stock',
                                      'scope': 'operator', 'scope_id': 'op-a', 'count': 3,
                                      'company_inventory_complete': False})
        row = result(data)
        self.assertEqual(row['counts']['verified_total'], 3)
        self.assertIsNone(row['counts']['other_current'])
        self.assertFalse(row['count_evidence']['company_inventory_complete'])
        self.assertEqual(row['count_evidence']['count_basis']['verified_total'], 'total_including_company')

    def test_permission_completeness_does_not_attest_business_inventory(self):
        data = fixture()
        data['employee']['company_scope_complete'] = True
        data['verifications'][0].pop('company_inventory_complete')
        self.assertFalse(run(data)['company_inventory_complete'])
        self.assertIsNone(run(data)['other_current'])
        data['verifications'][0]['company_inventory_complete'] = 1
        self.assertFalse(run(data)['company_inventory_complete'])
        self.assertIn('company_inventory_confirmation_invalid', limit(data)['issues'])
        self.assertIsNone(result(data)['estimated_available'])


if __name__ == '__main__':
    unittest.main()
