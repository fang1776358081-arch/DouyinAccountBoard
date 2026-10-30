"""Pure JSON-body content coding; callers retain auth, route and cache policy.

Only pass fresh identity bytes, once. The HTTP layer must restrict use to its
reviewed successful read route, skip an existing Content-Encoding, add
Vary: Accept-Encoding, and send content_length after this function returns.
No requests, identifiers, JSON contents, parameters or credentials are logged.
"""
from dataclasses import dataclass
import gzip
import re
from typing import Optional


_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_QVALUE = re.compile(r'^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$')
MINIMUM_BYTES = 1024
COMPRESSION_LEVEL = 4


@dataclass(frozen=True)
class PreparedBody:
    body: bytes
    content_encoding: Optional[str] = None
    acceptable: bool = True

    @property
    def content_length(self):
        return len(self.body)


def _qualities(header):
    """Return explicit weights in thousandths; conflicting duplicates deny.

    An invalid explicit coding weight is zero, so a wildcard cannot resurrect
    that coding. Invalid items with no valid coding token are ignored.
    """
    weights = {}
    for item in header.split(','):
        parts = [part.strip() for part in item.split(';')]
        coding = parts[0].lower()
        if not _TOKEN.fullmatch(coding):
            continue
        weight = 1000
        if len(parts) > 1:
            match = re.fullmatch(r'q\s*=\s*(.*)', parts[1], flags=re.IGNORECASE)
            value = match.group(1) if match else ''
            if len(parts) != 2 or not _QVALUE.fullmatch(value):
                weight = 0
            elif value.startswith('0'):
                fraction = value.partition('.')[2]
                weight = int((fraction + '000')[:3])
        weights[coding] = min(weights.get(coding, 1000), weight)
    return weights


def prepare(payload, accept_encoding, *, minimum_bytes=MINIMUM_BYTES):
    """Choose identity or deterministic gzip without changing the JSON bytes.

    Missing/empty Accept-Encoding keeps legacy identity responses. Gzip wins
    equal weights (normal browser headers), but a higher identity weight wins.
    With identity allowed, small or non-shrinking bodies stay uncompressed.
    With identity explicitly excluded, accepted gzip remains usable even for a
    small/non-shrinking body. No acceptable coding returns empty unacceptable
    bytes; the HTTP layer must send 406, not silently send forbidden identity.
    """
    if not isinstance(payload, bytes):
        raise TypeError('JSON transport requires identity bytes')
    if accept_encoding is not None and not isinstance(accept_encoding, str):
        raise TypeError('JSON transport requires a text encoding header')
    if type(minimum_bytes) is not int or minimum_bytes < 0:
        raise ValueError('Invalid JSON compression threshold')
    if accept_encoding is None or accept_encoding.strip() == '':
        return PreparedBody(payload)
    weights = _qualities(accept_encoding)
    gzip_weight = weights.get('gzip', weights.get('*', 0))
    identity_weight = weights.get('identity', 0 if weights.get('*') == 0 else 1000)
    if gzip_weight > 0 and gzip_weight >= identity_weight:
        if len(payload) >= minimum_bytes or identity_weight == 0:
            compressed = gzip.compress(payload, compresslevel=COMPRESSION_LEVEL, mtime=0)
            if len(compressed) < len(payload) or identity_weight == 0:
                return PreparedBody(compressed, 'gzip')
    if identity_weight > 0:
        return PreparedBody(payload)
    return PreparedBody(b'', acceptable=False)
