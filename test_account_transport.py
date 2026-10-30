"""Pure synthetic wire-contract checks; no database, server or real data."""
import copy
import json
import unittest

from test_isolation import checked_root

checked_root()  # Fail closed before importing application-related code.

import account_transport


def fixture():
    units = [dict(id='unit-old', device_id='phone-1', generation=1, active=False, note='合成旧机'),
             dict(id='unit-new', device_id='phone-1', generation=2, active=True, note='合成新机')]
    access = dict(account_id='account-1', last_unit_id='unit-old', target_unit_id='unit-new',
                  login_state='登录受阻', reason='合成恢复记录', case_id='case-1')
    placement = dict(card_id=1, unit_id='unit-old', slot='1', unit_label='合成旧机')
    detail = dict(reason='合成故障', affected_account_ids=['account-1'], before=dict(unit_id='unit-old'))
    return dict(accounts=[dict(id='account-1', name='合成账号', device_access=access, note='保留资料')],
                computers=[dict(id='computer-1', name='合成电脑')],
                phone_cards=dict(cards=[dict(id=1, phone='synthetic-phone', placement=placement)]),
                phone_devices=dict(devices=[dict(id='phone-1', current_unit=units[1], unit_history=units,
                    account_ids=['account-1'], account_count=1, inactive_count=0, occupancy='有在用账号',
                    suggested_state='在用', state='故障', attention='合成提醒')], statuses=['故障']),
                device_workflow=dict(enabled=True, units=units, access=[access], placements=[placement],
                    cases=[dict(id='case-1', account_ids=['account-1'])], todos=[dict(kind='device_case')],
                    options=dict(slots=['1', '2']), events=[dict(id=1, detail=json.dumps(detail), details=detail)]),
                collection=dict(auto_enabled=False), backup=dict(enabled=True),
                publishing_layout=dict(enabled=True, cells=[]), nurture=dict(records=[]),
                record_versions={'account:account-1': 'synthetic-version', 'daily:account-1:2026-10-29': None},
                offices=[dict(id='one', name='合成办公室')], user=dict(id=1, permissions=['publish']),
                date='2026-10-29', today='2026-10-30', cursor=10)


class AccountTransportTests(unittest.TestCase):
    def test_only_proven_duplicate_fields_are_removed_without_mutation(self):
        board = fixture()
        before = copy.deepcopy(board)
        result = account_transport.compact(board)
        expected = copy.deepcopy(board)
        for key in ('unit_history', 'account_ids', 'occupancy', 'suggested_state'):
            del expected['phone_devices']['devices'][0][key]
        for key in ('access', 'placements'):
            del expected['device_workflow'][key]
        del expected['device_workflow']['events'][0]['detail']
        self.assertEqual(result, expected)
        self.assertEqual(board, before)
        self.assertIsNot(result, board)
        self.assertEqual(result['record_versions'], before['record_versions'])

    def test_json_roundtrip_unicode_and_idempotence_preserve_business_fields(self):
        board = fixture()
        result = account_transport.compact(board)
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)
        self.assertEqual(account_transport.compact(result), result)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()),
                        len(json.dumps(board, ensure_ascii=False).encode()))
        for key in ('accounts', 'computers', 'phone_cards', 'record_versions', 'offices', 'user',
                    'publishing_layout', 'nurture', 'collection', 'backup', 'date', 'today', 'cursor'):
            self.assertEqual(result[key], board[key])

    def test_history_requires_complete_unambiguous_equal_units(self):
        mutations = [lambda b: b['device_workflow'].pop('units'),
                     lambda b: b['device_workflow']['units'].pop(),
                     lambda b: b['device_workflow']['units'].__setitem__(0, {**b['device_workflow']['units'][0], 'note': '不同'}),
                     lambda b: b['device_workflow']['units'].append(copy.deepcopy(b['device_workflow']['units'][0]))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                board = copy.deepcopy(fixture())
                # JSON transport normally duplicates shared objects: model that
                # boundary so changing one wire copy cannot change its peer.
                board = json.loads(json.dumps(board))
                mutate(board)
                self.assertEqual(account_transport.compact(board)['phone_devices']['devices'][0]['unit_history'],
                                 board['phone_devices']['devices'][0]['unit_history'])

    def test_missing_redacted_mismatched_or_duplicate_attachments_keep_relations(self):
        for field, owners, attachment in [('access', 'accounts', 'device_access'),
                                           ('placements', 'phone_cards', 'placement')]:
            for change in ('missing', 'none', 'redacted', 'wrong_id', 'duplicate', 'extra_relation'):
                with self.subTest(field=field, change=change):
                    board = json.loads(json.dumps(fixture()))
                    rows = board[owners]['cards'] if owners == 'phone_cards' else board[owners]
                    if change == 'missing':
                        rows[0].pop(attachment)
                    elif change == 'none':
                        rows[0][attachment] = None
                    elif change == 'redacted':
                        rows[0][attachment] = {}
                    elif change == 'wrong_id':
                        rows[0]['id'] = 'not-the-owner'
                    elif change == 'duplicate':
                        rows.append(copy.deepcopy(rows[0]))
                    else:
                        board['device_workflow'][field].append({('account_id' if field == 'access' else 'card_id'): 'unattached'})
                    result = account_transport.compact(board)
                    self.assertEqual(result['device_workflow'][field], board['device_workflow'][field])

    def test_event_raw_detail_is_removed_only_for_equivalent_parsed_dictionary(self):
        samples = [('not json', {}), ('[]', {}), ('null', {}), ('{"reason":"different"}', {}),
                   ('{"reason":"same"}', {'reason': 'same'}), ('{"reason":"same"}', None)]
        for detail, details in samples:
            with self.subTest(detail=detail, details=details):
                board = fixture()
                board['device_workflow']['events'] = [dict(detail=detail, details=details)]
                result = account_transport.compact(board)['device_workflow']['events'][0]
                self.assertEqual('detail' not in result, details == {'reason': 'same'})

    def test_sparse_disabled_and_unknown_shapes_are_retained(self):
        for board in ({}, {'device_workflow': {'enabled': False}},
                      {'phone_devices': {'devices': [None]}},
                      {'device_workflow': {'units': None, 'access': None, 'events': [None]}}):
            self.assertEqual(account_transport.compact(board), board)
        for root in (None, [], 'not a board'):
            with self.assertRaises(TypeError):
                account_transport.compact(root)

    def test_string_and_integer_owner_ids_do_not_cross_match(self):
        board = fixture()
        board['accounts'][0]['id'] = 1
        board['device_workflow']['access'][0]['account_id'] = '1'
        self.assertIn('access', account_transport.compact(board)['device_workflow'])

    def test_boolean_number_and_browser_identity_ambiguity_keep_original_fields(self):
        board = json.loads(json.dumps(fixture()))
        board['accounts'][0]['device_access']['case_id'] = True
        board['device_workflow']['access'][0]['case_id'] = 1
        self.assertIn('access', account_transport.compact(board)['device_workflow'])
        board['device_workflow']['events'] = [dict(detail='{"active":1}', details={'active': True})]
        self.assertIn('detail', account_transport.compact(board)['device_workflow']['events'][0])
        board = fixture()
        board['accounts'] = [dict(id=1, device_access=dict(account_id=1)),
                             dict(id='1', device_access=dict(account_id='1'))]
        board['device_workflow']['access'] = [dict(account_id=1), dict(account_id='1')]
        self.assertIn('access', account_transport.compact(board)['device_workflow'])


if __name__ == '__main__':
    unittest.main()
