"""Private index behavior using synthetic public identifiers and local fixtures."""
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

import collection_profiles as profiles

URL='https://www.douyin.com/user/MS4wSyntheticConfirmed'
OTHER=URL+'Other'


class ProfileIndexTest(unittest.TestCase):
    def load(self,rows):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'private-index.json'
            path.write_text(json.dumps({'version':1,'profiles':rows}),encoding='utf-8');path.chmod(0o600)
            index=profiles.ProfileIndex.from_file(str(path))
        return index

    def assert_required(self,index,account,code):
        with self.assertRaises(profiles.ProfileRequired) as result:index.resolve(account)
        self.assertEqual(result.exception.code,code)

    def test_exact_original_handle_leading_zero_and_same_url_deduplication(self):
        index=self.load([{'account':'0012345678','url':URL},{'account':'0012345678','url':URL}])
        self.assertEqual(index.resolve('0012345678'),URL)
        for account in ('12345678','001234567','00123456789','0012345678 ',12345678,None):
            self.assert_required(index,account,'ProfileMissing')

    def test_multiple_confirmed_urls_are_conflict_but_explicit_canonical_job_wins(self):
        index=self.load([{'account':'0012345678','url':URL},{'account':'0012345678','url':OTHER}])
        self.assert_required(index,'0012345678','ProfileConflict')
        self.assertEqual(index.resolve('0012345678',URL),URL)
        self.assertEqual(index.resolve('other-account',URL),URL)
        self.assert_required(index,'not-present','ProfileMissing')

    def test_unconfigured_empty_index_and_in_memory_cache_need_no_file_for_explicit_profile(self):
        with patch.object(Path,'lstat') as inspect:
            index=profiles.ProfileIndex.from_file()
        inspect.assert_not_called();self.assertEqual(index.resolve('0012345678',URL),URL)
        self.assert_required(index,'0012345678','ProfileMissing')
        empty=self.load([]);self.assert_required(empty,'0012345678','ProfileMissing')
        cached=self.load([{'account':'0012345678','url':URL}])
        with patch.object(profiles.os,'open',side_effect=AssertionError('cache lookup must not reread disk')):
            self.assertEqual(cached.resolve('0012345678'),URL)

    def test_only_exact_canonical_ms4w_profiles_are_accepted(self):
        self.assertEqual(profiles.canonical_profile(URL),URL)
        for value in (URL+'/',URL+'?token=synthetic-secret',URL+'#fragment','http://www.douyin.com/user/MS4wSynthetic',
                      'https://www.douyin.com.evil.invalid/user/MS4wSynthetic','https://user@www.douyin.com/user/MS4wSynthetic',
                      'https://www.douyin.com/user/0012345678','MS4wSynthetic',URL+'\n',None):
            with self.subTest(value=value):self.assertEqual(profiles.canonical_profile(value),'')

    def test_invalid_schema_identity_url_or_unknown_private_fields_fail_with_fixed_error(self):
        payloads=[{'version':True,'profiles':[]},{'version':1,'profiles':{},'token':'synthetic-secret'},
            {'version':1,'profiles':[{'account':123,'url':URL}]},
            {'version':1,'profiles':[{'account':'0012345678 ','url':URL}]},
            {'version':1,'profiles':[{'account':'0012345678','url':URL+'?cookie=synthetic-secret'}]},
            {'version':1,'profiles':[{'account':'0012345678','url':URL,'cookie':'synthetic-secret'}]}]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic-secret-path.json'
            for payload in payloads:
                path.write_text(json.dumps(payload),encoding='utf-8');path.chmod(0o600)
                with self.subTest(payload=payload),self.assertRaises(ValueError) as error:profiles.ProfileIndex.from_file(str(path))
                self.assertEqual(str(error.exception),profiles.INDEX_ERROR);self.assertNotIn(str(path),str(error.exception))
            path.write_text('{"version":1,"profiles":[],"profiles":[]}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'^'+profiles.INDEX_ERROR+'$'):profiles.ProfileIndex.from_file(str(path))
        with self.assertRaisesRegex(ValueError,'^'+profiles.INDEX_ERROR+'$'):profiles.ProfileIndex.from_file('relative.json')

    def test_bytes_and_entry_counts_are_bounded_before_untrusted_input_is_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'index.json';path.write_text(json.dumps({'version':1,'profiles':[]}),encoding='utf-8');path.chmod(0o600)
            with patch.object(profiles,'MAX_INDEX_BYTES',4),patch.object(profiles.os,'open') as opened:
                with self.assertRaisesRegex(ValueError,'^'+profiles.INDEX_ERROR+'$'):profiles.ProfileIndex.from_file(str(path))
            opened.assert_not_called()
        with patch.object(profiles,'MAX_INDEX_ENTRIES',1),self.assertRaises(ValueError):
            self.load([{'account':'a','url':URL},{'account':'b','url':URL}])

    def test_private_regular_single_link_and_posix_mode_are_required(self):
        good=types.SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_nlink=1,st_size=20)
        with patch.object(profiles,'os',types.SimpleNamespace(name='posix')):
            self.assertTrue(profiles._private_file(good))
            for key,value in (('st_mode',stat.S_IFLNK|0o600),('st_mode',stat.S_IFIFO|0o600),
                              ('st_mode',stat.S_IFREG|0o644),('st_nlink',2)):
                bad=types.SimpleNamespace(**vars(good));setattr(bad,key,value)
                self.assertFalse(profiles._private_file(bad))

    def test_index_swapped_during_open_is_rejected_without_revealing_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'index.json';path.write_text(json.dumps({'version':1,'profiles':[]}),encoding='utf-8');path.chmod(0o600)
            item=path.stat();changed=types.SimpleNamespace(**{key:getattr(item,key) for key in
                ('st_dev','st_ino','st_size','st_mtime_ns','st_mode','st_uid','st_gid','st_nlink')});changed.st_ino+=1
            with patch.object(profiles.os,'fstat',return_value=changed),self.assertRaisesRegex(ValueError,'^'+profiles.INDEX_ERROR+'$'):
                profiles.ProfileIndex.from_file(str(path))


if __name__=='__main__':unittest.main()
