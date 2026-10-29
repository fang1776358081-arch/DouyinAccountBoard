"""Office-local phone-number imports; isolated synthetic records only."""
import unittest

from test_isolation import checked_root

checked_root()

import app
import auth
import data_exchange
import office_scope
import office_three_import
import operations
import phone_devices
import test_office_three_import as support
from sql_dialect import begin_write


DAY = '2026-10-29'


class DeviceOfficeImportsTest(unittest.TestCase):
    # Reuse only the reviewed fixture helpers, not its test subclasses/suite.
    setUp = support.OfficeThreeFixture.setUp
    insert_account = staticmethod(support.OfficeThreeFixture.insert_account)
    snapshot = support.OfficeThreeFixture.snapshot
    assert_old_rows_preserved = support.OfficeThreeFixture.assert_old_rows_preserved
    body = support.OfficeThreeFixture.body
    preview = support.OfficeThreeFixture.preview
    apply = support.OfficeThreeFixture.apply

    def add_device(self, office, number, name='合成原设备'):
        with app.connect() as db:
            begin_write(db)
            return phone_devices.save(db, {'number': number, 'office_ids': [office],
                                          'name': name, 'state': '待确认', 'note': '合成旧资料保留'},
                                      create=True, actor=self.actor)

    def device_preview(self, rows, office='three', actor=None, scope=None):
        if scope is None:
            scope = {'office_ids': [office]} if office else {}
        with app.connect() as db:
            begin_write(db)
            return data_exchange.plan_import(db, actor or self.actor, 'devices', rows, DAY, scope)

    def device_apply(self, preview, rows=None):
        chosen = rows if rows is not None else [row['row'] for row in preview['rows'] if row['selected']]
        with app.connect() as db:
            begin_write(db)
            return data_exchange.apply_import(db, self.actor,
                {'preview_id': preview['preview_id'], 'rows': chosen}, DAY)

    def test_number_import_is_office_local_and_existing_not_selected(self):
        first = self.add_device('three', '29', '三办原有同号')
        other = self.add_device('synthetic-other', '29', '其他办公室同号')
        self.assertNotEqual(first, other)
        before = self.snapshot()
        result = self.device_preview([['手机序号', '手机名称'], ['29', '不应默认覆盖'], ['30', '三办新设备']])
        self.assertEqual([row['action'] for row in result['rows']], ['update', 'create'])
        self.assertEqual([row['selected'] for row in result['rows']], [False, True])
        self.assertEqual(result['rows'][0]['entity'], 'device:' + first)
        self.device_apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            self.assertEqual(len(phone_devices.office_number_devices(db, 'three', '29')), 1)
            self.assertEqual(len(phone_devices.office_number_devices(db, 'synthetic-other', '29')), 1)
            self.assertEqual(len(phone_devices.office_number_devices(db, 'three', '30')), 1)

    def test_new_same_number_in_other_office_does_not_reuse_or_modify(self):
        original = self.add_device('three', '29')
        before = self.snapshot()
        result = self.device_preview([['手机序号'], ['29']], office='synthetic-other')
        self.assertEqual(result['rows'][0]['action'], 'create')
        self.device_apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            created = phone_devices.office_number_devices(db, 'synthetic-other', '29')
            self.assertEqual(len(created), 1)
            self.assertNotEqual(created[0]['id'], original)

    def test_internal_id_and_display_number_are_separate_and_must_agree(self):
        ident = self.add_device('three', '29')
        before = self.snapshot()
        valid = self.device_preview([['内部编号', '手机序号', '手机名称'], [ident, '29', '明确编辑']], office=None)
        self.assertEqual(valid['rows'][0]['action'], 'update')
        self.assertFalse(valid['rows'][0]['selected'])
        invalid = self.device_preview([['内部编号', '手机序号'], [ident, '30'], ['phone:' + 'a' * 32 + ':29', '29']])
        self.assertTrue(all(row['action'] == 'error' for row in invalid['rows']))
        self.assertIn('不一致', invalid['rows'][0]['error'])
        self.assertIn('不存在', invalid['rows'][1]['error'])
        self.assertEqual(before, self.snapshot())

    def test_number_only_requires_explicit_single_office_and_plain_number(self):
        for scope in ({}, {'office_ids': []}, {'office_ids': ['three', 'synthetic-other']}):
            with self.subTest(scope=scope):
                result = self.device_preview([['手机序号'], ['29']], scope=scope)
                self.assertEqual(result['rows'][0]['action'], 'error')
        result = self.device_preview([['手机序号'], ['office3:29']])
        self.assertEqual(result['rows'][0]['action'], 'error')
        self.assertIn('内部编号', result['rows'][0]['error'])

    def test_duplicate_rows_use_office_number_including_explicit_ids(self):
        ident = self.add_device('three', '29')
        for rows in ([['内部编号', '手机序号', '手机名称'], [ident, '29', '合成甲'], ['', '029', '合成乙']],
                     [['手机序号'], ['30'], ['030']]):
            with self.subTest(rows=rows):
                result = self.device_preview(rows)
                self.assertTrue(all(row['action'] == 'error' for row in result['rows']))
                self.assertTrue(all('重复' in row['error'] for row in result['rows']))

    def test_ambiguous_or_multi_office_device_is_not_reused(self):
        ident = self.add_device('three', '29')
        with app.connect() as db:
            db.execute('INSERT INTO phone_device_offices VALUES(?,?)', (ident, 'synthetic-other'))
        before = self.snapshot()
        result = self.device_preview([['手机序号'], ['29']])
        self.assertEqual(result['rows'][0]['action'], 'error')
        self.assertIn('多个办公室', result['rows'][0]['error'])
        with app.connect() as db:
            duplicate = phone_devices.new_identity('29')
            db.execute('INSERT INTO phone_devices(id,name,state) VALUES(?,?,?)', (duplicate, '合成重复旧设备', '待确认'))
            db.execute('INSERT INTO phone_device_offices VALUES(?,?)', (duplicate, 'three'))
        result = self.device_preview([['手机序号'], ['29']])
        self.assertEqual(result['rows'][0]['action'], 'error')
        self.assertIn('多条', result['rows'][0]['error'])
        self.assert_old_rows_preserved(before)

    def test_permission_errors_do_not_return_existing_device_details(self):
        ident = self.add_device('synthetic-other', '29', '合成不可见名称')
        with app.connect() as db:
            db.execute('INSERT INTO user_office_grants VALUES(?,?)', (self.employee_id, 'three'))
            actor = auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?', (self.employee_id,)).fetchone())
        for rows, office in (([['内部编号', '手机序号'], [ident, '29']], 'three'),
                            ([['手机序号'], ['29']], 'synthetic-other')):
            with self.subTest(rows=rows):
                result = self.device_preview(rows, office=office, actor=actor)
                row = result['rows'][0]
                self.assertEqual(row['action'], 'error')
                self.assertEqual(row['error'], '无权导入此资料或关联对象')
                self.assertNotIn('identity', row)
                self.assertNotIn('合成不可见名称', str(result))

    def test_apply_rejects_number_claimed_after_preview(self):
        preview = self.device_preview([['手机序号', '手机名称'], ['29', '合成计划设备']])
        existing = self.add_device('three', '29', '合成抢先登记')
        before = self.snapshot()
        with self.assertRaises((ValueError, operations.ConflictError)):
            self.device_apply(preview)
        self.assertEqual(before, self.snapshot())
        with app.connect() as db:
            self.assertEqual([row['id'] for row in phone_devices.office_number_devices(db, 'three', '29')], [existing])

    def test_export_roundtrip_preserves_internal_ids_and_separate_numbers(self):
        ident = self.add_device('three', '29')
        with app.connect() as db:
            template = data_exchange.table_rows(db, 'devices', template=True, actor=self.actor)
            exported = data_exchange.table_rows(db, 'devices', actor=self.actor)
        self.assertEqual(template[0][:2], ['内部编号', '手机序号'])
        row = next(row for row in exported[1:] if row[0] == ident)
        self.assertEqual(row[1], '29')
        result = self.device_preview(exported, office=None)
        self.assertTrue(all(row['action'] == 'same' for row in result['rows']))
        self.assertTrue(all(not row['selected'] for row in result['rows']))

    def test_unscoped_legacy_template_retains_office3_identifier(self):
        with app.connect() as db:
            begin_write(db)
            db.scope_enabled = False
            preview = data_exchange.plan_import(db, self.actor, 'devices',
                [['手机序号', '手机名称'], ['office3:78', '合成旧格式']], DAY)
            self.assertEqual(preview['rows'][0]['action'], 'create')
            data_exchange.apply_import(db, self.actor, {'preview_id': preview['preview_id'], 'rows': [2]}, DAY)
            self.assertIsNotNone(db.execute("SELECT 1 FROM phone_devices WHERE id='office3:78'").fetchone())

    def test_dual_import_reuses_new_identity_without_creating_legacy_duplicate(self):
        ident = self.add_device('three', '2', '三办合成新格式设备')
        before = self.snapshot()
        preview = self.preview()
        self.assertEqual(preview['summary']['new_devices'], 0)
        self.assertEqual(preview['summary']['new_accounts'], 1)
        self.assertEqual(preview['summary']['new_layouts'], 1)
        self.apply(preview)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            account = db.execute("SELECT phone_device_id FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
            self.assertEqual(account[0], ident)
            self.assertIsNone(db.execute("SELECT 1 FROM phone_devices WHERE id='office3:2'").fetchone())

    def test_dual_layout_reuse_compares_real_identity_not_legacy_prefix(self):
        ident = self.add_device('three', '2')
        with app.connect() as db:
            self.insert_account(db, 'synthetic-new-format-old-account', 'synthetic_uuid_reuse',
                                '合成复用新格式', 'office3:1', 8, 'three')
            db.execute('UPDATE accounts SET phone_device_id=? WHERE id=?', (ident, 'synthetic-new-format-old-account'))
        before = self.snapshot()
        body = self.body(accounts=[support.account_row('synthetic_uuid_reuse', device='2', name='合成复用新格式')],
                         progress=[support.progress_row(labels=('', '2 合成复用新格式'))])
        preview = self.preview(body)
        self.assertEqual(preview['summary']['reused_accounts'], 1)
        self.assertEqual(preview['summary']['new_devices'], 0)
        self.assertEqual(preview['summary']['new_layouts'], 1)
        self.apply(preview)
        self.assert_old_rows_preserved(before)

    def test_dual_import_conflicts_on_multi_scope_and_occupied_legacy_identity(self):
        ident = self.add_device('three', '2')
        with app.connect() as db:
            db.execute('INSERT INTO phone_device_offices VALUES(?,?)', (ident, 'synthetic-other'))
        before = self.snapshot()
        preview = self.preview()
        self.assertEqual(preview['summary']['new_accounts'], 0)
        self.assertEqual(preview['summary']['new_devices'], 0)
        self.assertGreater(preview['summary']['conflicts'], 0)
        self.assertEqual(before, self.snapshot())
        with app.connect() as db:
            db.execute('DELETE FROM phone_device_offices WHERE device_id=?', (ident,))
            db.execute('INSERT INTO phone_devices(id,name,state) VALUES(?,?,?)', ('office3:2', '合成原来源待分配', '待确认'))
        before = self.snapshot()
        preview = self.preview()
        self.assertEqual(preview['summary']['new_accounts'], 0)
        self.assertEqual(preview['summary']['new_devices'], 0)
        self.assertEqual(before, self.snapshot())
