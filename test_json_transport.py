"""Pure content-coding tests; explicitly selected in the isolated runner only."""
import gzip
import json
import random
import unittest

from test_isolation import checked_root

checked_root()  # Fail closed before any application-related imports.

import json_transport


class JsonTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.dumps({'kind': 'synthetic-only', 'accounts': [
            {'id': 'synthetic-' + str(index), 'name': '合成账号', 'note': '仅合成资料' * 8}
            for index in range(120)]}, ensure_ascii=False).encode('utf-8')

    def assert_identity(self, header, payload=None):
        body = self.payload if payload is None else payload
        result = json_transport.prepare(body, header)
        self.assertTrue(result.acceptable)
        self.assertIsNone(result.content_encoding)
        self.assertEqual(result.body, body)
        self.assertEqual(result.content_length, len(body))

    def assert_gzip(self, header, payload=None):
        body = self.payload if payload is None else payload
        result = json_transport.prepare(body, header)
        self.assertTrue(result.acceptable)
        self.assertEqual(result.content_encoding, 'gzip')
        self.assertEqual(gzip.decompress(result.body), body)
        self.assertEqual(result.content_length, len(result.body))
        return result

    def assert_unacceptable(self, header):
        result = json_transport.prepare(self.payload, header)
        self.assertFalse(result.acceptable)
        self.assertEqual(result.body, b'')
        self.assertIsNone(result.content_encoding)
        self.assertEqual(result.content_length, 0)

    def test_absent_empty_and_whitespace_keep_legacy_identity(self):
        for header in (None, '', '   '):
            with self.subTest(header=header):
                self.assert_identity(header)

    def test_browser_gzip_deflate_unicode_roundtrip_and_encoded_length(self):
        result = self.assert_gzip('gzip, deflate')
        self.assertLess(result.content_length, len(self.payload))
        self.assertEqual(json.loads(gzip.decompress(result.body)), json.loads(self.payload))

    def test_coding_case_whitespace_and_quality_name_are_case_insensitive(self):
        for header in ('GZip, Deflate', '  gzip ; Q = 1.000 , deflate;q=0.5 '):
            with self.subTest(header=header):
                self.assert_gzip(header)

    def test_explicit_gzip_zero_is_never_revived_by_wildcard(self):
        for header in ('gzip;q=0', 'gzip;q=0, *;q=1', '*;q=1, GZIP;q=0.000'):
            with self.subTest(header=header):
                self.assert_identity(header)

    def test_higher_identity_preference_wins(self):
        for header in ('gzip;q=0.5', 'gzip;q=0.8, identity;q=1', '*;q=0.1'):
            with self.subTest(header=header):
                self.assert_identity(header)

    def test_gzip_accepts_valid_fractional_weights_and_equal_preference(self):
        for header in ('gzip;q=0.001, identity;q=0', 'gzip;q=0.8, identity;q=0.5',
                       'gzip;q=0.500, identity;q=0.5', 'gzip;q=1.'):
            with self.subTest(header=header):
                self.assert_gzip(header)

    def test_wildcard_can_select_gzip_when_identity_is_excluded(self):
        for header in ('*', '*;q=0.5, identity;q=0', '*;q=1, br;q=0'):
            with self.subTest(header=header):
                self.assert_gzip(header)

    def test_wildcard_zero_excludes_implicit_identity(self):
        for header in ('*;q=0', 'br, *;q=0', 'gzip;q=0, identity;q=0'):
            with self.subTest(header=header):
                self.assert_unacceptable(header)

    def test_explicit_coding_or_identity_can_override_wildcard_zero(self):
        self.assert_gzip('gzip, *;q=0')
        self.assert_identity('identity;q=1, *;q=0')
        self.assert_identity('gzip;q=0, identity;q=0.1, *;q=0')

    def test_unsupported_codings_fall_back_only_when_identity_is_allowed(self):
        self.assert_identity('br, deflate')
        self.assert_unacceptable('br, deflate, identity;q=0')

    def test_malformed_explicit_weights_fail_closed_for_that_coding(self):
        for value in ('abc', '.5', '1.001', '2', '-1', '0.1234', 'nan', '0.5;q=1', '0.١', '0.５'):
            with self.subTest(value=value):
                self.assert_identity('gzip;q=' + value + ', *;q=1')
                self.assert_unacceptable('gzip;q=' + value + ', identity;q=0')
        self.assert_identity('gzip;level=4, *;q=1')
        self.assert_identity('gzip;q=1;level=4, *;q=1')

    def test_conflicting_duplicate_weights_conservatively_keep_lowest(self):
        self.assert_identity('gzip;q=0, gzip;q=1')
        self.assert_identity('gzip;q=1, GZIP;q=0')
        self.assert_unacceptable('gzip;q=0, gzip;q=1, identity;q=0')
        self.assert_gzip('gzip;q=1, identity;q=0, identity;q=1')

    def test_small_body_avoids_compression_with_identity_available(self):
        for body in (b'', b'{}', '合成'.encode('utf-8') * 10):
            with self.subTest(length=len(body)):
                self.assert_identity('gzip, deflate', body)

    def test_incompressible_body_avoids_expansion_with_identity_available(self):
        body = random.Random(20261030).randbytes(4096)
        self.assert_identity('gzip', body)

    def test_identity_exclusion_never_silently_returns_identity(self):
        self.assert_gzip('gzip, identity;q=0', b'{}')
        self.assert_gzip('gzip, identity;q=0', random.Random(20261030).randbytes(4096))

    def test_gzip_is_repeatable_and_does_not_expose_wall_clock(self):
        first = self.assert_gzip('gzip')
        second = self.assert_gzip('gzip')
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.body[4:8], b'\0\0\0\0')

    def test_input_contract_rejects_nonbytes_and_invalid_threshold(self):
        with self.assertRaises(TypeError):
            json_transport.prepare('not-encoded', 'gzip')
        for header in (1, b'gzip'):
            with self.subTest(header_type=type(header).__name__), self.assertRaises(TypeError):
                json_transport.prepare(self.payload, header)
        for threshold in (-1, True, '1024'):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                json_transport.prepare(self.payload, 'gzip', minimum_bytes=threshold)


if __name__ == '__main__':
    unittest.main()
