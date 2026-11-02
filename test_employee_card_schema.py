"""Reviewed additive extension contracts; no network or production database."""
import hashlib
from pathlib import Path
import re
import unittest

import pg_schema


class EmployeeCardSchemaTest(unittest.TestCase):
    def test_previously_released_migrations_are_immutable(self):
        expected = {
            '0001_legacy_baseline.sql': 'cb9467f8c1813b8ad7cb4f79e13ec2254bbc6551681f434d7bba68d4da83adbd',
            '0002_office_scope.sql': 'ce2cdbab215f622ca142781934492561f2899a9b91863de6291946a4d6161132',
            '0003_idempotency.sql': '3f90e954e9f7aa2d3457ab8d128c1ec04c2379e00d5d52bc2522490a4a04bfa6',
            '0004_device_workflow.sql': 'c8de2761c288e6e05851ce827b9fe8376a376d8864ecb93d800471a13f4e217e',
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((pg_schema.MIGRATIONS / name).read_bytes()).hexdigest(), digest)

    def test_only_complete_reviewed_v4_or_v5_can_start(self):
        expected = pg_schema.expected_versions()
        self.assertEqual(len(expected), 5)
        self.assertTrue(pg_schema.runtime_versions_supported(expected[:4]))
        self.assertTrue(pg_schema.runtime_versions_supported(expected))
        for invalid in ([], expected[:3], expected[:4] + [(5, 'unreviewed')],
                        expected + [(6, 'unknown')], list(reversed(expected)),
                        expected[:2] + expected[3:], [(1, 'drifted')] + expected[1:4]):
            with self.subTest(invalid=invalid):
                self.assertFalse(pg_schema.runtime_versions_supported(invalid))

    def test_new_migration_has_no_legacy_data_or_permission_mutation(self):
        sql = (pg_schema.MIGRATIONS / pg_schema.FILES[-1]).read_text(encoding='utf-8')
        sql = re.sub(r'--[^\n]*', '', sql)
        self.assertNotRegex(sql, r'(?i)\b(?:DROP|TRUNCATE|GRANT|REVOKE|ALTER)\s')
        touched = re.findall(r'(?i)\b(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)', sql)
        self.assertTrue(touched)
        self.assertTrue(all(name.startswith('employee_card_') for name in touched))

    def test_runtime_startup_never_applies_the_extension(self):
        root = Path(__file__).resolve().parent
        for name in ('cloudbase_runtime.py', 'pg_runtime.py'):
            source = (root / name).read_text(encoding='utf-8')
            self.assertIn('runtime_versions_supported(rows)', source)
            self.assertNotIn('apply_migrations(', source)


if __name__ == '__main__':
    unittest.main()
