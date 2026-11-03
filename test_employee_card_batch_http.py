"""Real loopback batch-link requests against fresh synthetic databases only."""
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

import app
import test_employee_cards_http as fixture


class EmployeeCardBatchHTTPTest(unittest.TestCase):
    setUp = fixture.EmployeeCardsHTTPTest.setUp
    tearDown = fixture.EmployeeCardsHTTPTest.tearDown
    call = fixture.EmployeeCardsHTTPTest.call
    login = fixture.EmployeeCardsHTTPTest.login
    new_user = fixture.EmployeeCardsHTTPTest.new_user
    preview_employee = fixture.EmployeeCardsHTTPTest.preview_employee
    apply_preview = fixture.EmployeeCardsHTTPTest.apply_preview

    def person(self, number):
        status, receipt, _ = self.apply_preview(self.preview_employee(number))
        self.assertEqual(status, 200, receipt)
        return receipt['employee_id']

    def cards(self, quantity=2):
        with app.connect() as db:
            first = db.execute('SELECT COALESCE(MAX(id),0)+100 FROM phone_cards').fetchone()[0]
            for index in range(quantity):
                db.execute('INSERT INTO phone_cards(id,phone,label,carrier,status,owner,keeper,note) VALUES(?,?,?,?,?,?,?,?)',
                           (first + index, '1380000' + str(first + index).zfill(4), '合成原卡标签',
                            '其他', '停用', '提供人文字不代表实名', '合成保管人', '原卡备注必须保留'))
        return list(range(first, first + quantity))

    def snapshot(self):
        with app.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {name: ([tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                           if name in tables else None)
                    for name in ('phone_cards', 'accounts', 'auth_users', 'phone_card_offices', 'user_office_grants')}

    def counts(self):
        with app.connect() as db:
            return {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                    for name in ('employee_card_units', 'employee_card_links', 'employee_card_events')}

    def preview_group(self, employee_id, cards, client=None):
        _, meta, _ = self.call('/api/employee-cards?employee_id=' + employee_id, client=client or self.owner)
        rows = [dict(card_id=card, operator_id='minsheng' if index == 0 else 'changcheng', activated_on='')
                for index, card in enumerate(cards)]
        return self.call('/api/employee-cards/preview',
                         dict(action='card_link_batch', employee_id=employee_id,
                              expected_version=meta['version'],
                              data=dict(cards=rows, source='仅合成测试：本人确认实名关系，未知办理日期留空')),
                         client=client or self.owner)

    def test_preview_apply_replay_and_legacy_data_stay_unchanged(self):
        employee = self.person('BATCH-001')
        cards = self.cards()
        before, inventory = self.snapshot(), self.counts()
        with patch.object(app.operations, 'snapshot', side_effect=AssertionError('full board forbidden')):
            status, preview, headers = self.preview_group(employee, cards)
        self.assertEqual(status, 200, preview)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(preview['counts'], dict(total=2, new=2, existing=0, conflict=0))
        self.assertTrue(preview['can_apply'])
        self.assertFalse(preview['has_conflicts'])
        self.assertEqual({row['card_id'] for row in preview['rows']}, set(cards))
        self.assertTrue(all(row['status'] == 'new' and isinstance(row['phone'], str) for row in preview['rows']))
        self.assertEqual(self.counts(), inventory)
        self.assertEqual(self.snapshot(), before)
        request_id = str(uuid4())
        status, receipt, _ = self.apply_preview(preview, request_id)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt['counts']['new'], 2)
        self.assertNotIn('rows', receipt)
        self.assertNotIn('phone', json.dumps(receipt))
        status, replay, _ = self.apply_preview(preview, request_id)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay, receipt)
        self.assertEqual(self.counts()['employee_card_links'], inventory['employee_card_links'] + 2)
        self.assertEqual(self.snapshot(), before)
        _, detail, _ = self.call('/api/employee-cards?employee_id=' + employee, client=self.owner)
        self.assertEqual(detail['card_ledger']['registered'], 2)
        self.assertTrue(all(unit['activated_on'] == '' for unit in detail['units']))

    def test_existing_other_employee_is_a_blocked_preview_not_a_partial_write(self):
        target, other = self.person('BATCH-001'), self.person('BATCH-002')
        cards = self.cards()
        _, first, _ = self.preview_group(other, [cards[1]])
        self.assertEqual(self.apply_preview(first)[0], 200)
        before, inventory = self.snapshot(), self.counts()
        status, preview, _ = self.preview_group(target, cards)
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview['has_conflicts'])
        self.assertFalse(preview['can_apply'])
        self.assertEqual(preview['counts']['conflict'], 1)
        self.assertEqual(self.apply_preview(preview)[0], 409)
        self.assertEqual(self.counts(), inventory)
        self.assertEqual(self.snapshot(), before)

    def test_any_card_changed_after_preview_rejects_the_entire_batch(self):
        target, cards = self.person('BATCH-001'), self.cards()
        _, preview, _ = self.preview_group(target, cards)
        inventory = self.counts()
        with app.connect() as db:
            db.execute("UPDATE phone_cards SET note='合成并发修改原备注' WHERE id=?", (cards[-1],))
        after_external_change = self.snapshot()
        self.assertEqual(self.apply_preview(preview)[0], 409)
        self.assertEqual(self.counts(), inventory)
        self.assertEqual(self.snapshot(), after_external_change)

    def test_batch_requires_authentication_csrf_and_card_manage(self):
        target, cards = self.person('BATCH-001'), self.cards()
        inventory = self.counts()
        self.new_user('synthetic-task-only', ['card_tasks'])
        task_user = self.login('synthetic-task-only')
        body = dict(action='card_link_batch', employee_id=target, expected_version='not-authorized',
                    data=dict(cards=[dict(card_id=cards[0], operator_id='minsheng', activated_on='')], source='合成依据'))
        self.assertEqual(self.call('/api/employee-cards/preview', body)[0], 401)
        self.assertEqual(self.call('/api/employee-cards/preview', body, client=task_user)[0], 403)
        self.assertEqual(self.call('/api/employee-cards/preview', body, client=self.owner,
                                  headers={'X-CSRF-Token': 'synthetic-invalid'})[0], 403)
        self.assertEqual(self.counts(), inventory)


if __name__ == '__main__':
    unittest.main()
