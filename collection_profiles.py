"""Private, exact-handle index of previously confirmed public Douyin profiles."""
import json
import os
from pathlib import Path
import re
import stat

MAX_INDEX_BYTES=8*1024*1024
MAX_INDEX_ENTRIES=20000
INDEX_ERROR='无法读取有效的私有主页索引，请检查配置'


def canonical_profile(value):
    return value if isinstance(value,str) and re.fullmatch(
        r'https://www\.douyin\.com/user/MS4w[A-Za-z0-9_-]{1,196}',value) else ''


class ProfileRequired(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__('需要已确认主页')


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError()
        result[key]=value
    return result


def _metadata(item):
    return (item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_mode,item.st_uid,item.st_gid,item.st_nlink)


def _private_file(item):
    return (stat.S_ISREG(item.st_mode) and item.st_nlink==1 and 0<=item.st_size<=MAX_INDEX_BYTES and
            (os.name=='nt' or not item.st_mode&0o077))


class ProfileIndex:
    def __init__(self,profiles=None):
        self._profiles={key:frozenset(values) for key,values in (profiles or {}).items()}

    @classmethod
    def from_file(cls,value=None):
        if not value:return cls()
        try:
            path=Path(value)
            if not path.is_absolute():raise ValueError()
            before=path.lstat()
            if not _private_file(before):raise ValueError()
            flags=os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
            with os.fdopen(os.open(path,flags),'rb') as stream:
                opened=os.fstat(stream.fileno())
                if not _private_file(opened) or _metadata(opened)!=_metadata(before):raise ValueError()
                data=stream.read(MAX_INDEX_BYTES+1)
                if len(data)>MAX_INDEX_BYTES or _metadata(os.fstat(stream.fileno()))!=_metadata(opened):raise ValueError()
            payload=json.loads(data.decode('utf-8-sig'),object_pairs_hook=_unique_object)
            if (not isinstance(payload,dict) or set(payload)!={'version','profiles'} or
                    type(payload['version']) is not int or payload['version']!=1 or
                    not isinstance(payload['profiles'],list) or len(payload['profiles'])>MAX_INDEX_ENTRIES):raise ValueError()
            profiles={}
            for row in payload['profiles']:
                if not isinstance(row,dict) or set(row)!={'account','url'}:raise ValueError()
                account=row['account'];url=canonical_profile(row['url'])
                if (not isinstance(account,str) or not 1<=len(account)<=100 or
                        any(char.isspace() or ord(char)<33 for char in account) or not url):raise ValueError()
                profiles.setdefault(account,set()).add(url)
            return cls(profiles)
        except (OSError,ValueError,TypeError,UnicodeError,RecursionError):
            raise ValueError(INDEX_ERROR) from None

    def resolve(self,account,homepage=''):
        if explicit:=canonical_profile(homepage):return explicit
        values=self._profiles.get(account,()) if isinstance(account,str) else ()
        if len(values)==1:return next(iter(values))
        raise ProfileRequired('ProfileConflict' if values else 'ProfileMissing')
