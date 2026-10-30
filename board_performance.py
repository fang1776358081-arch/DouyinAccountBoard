"""Opt-in fixed-phase durations only; never inspect requests or database rows."""
from contextlib import contextmanager
import json
import logging
import os
import time


BUILD_ID = 'nickname-fill-20261030-r1'


PHASES = frozenset(('connect', 'transaction_close', 'read_begin', 'auth', 'auth_meta',
    'entities', 'scope', 'collection', 'lifecycle', 'cards', 'devices', 'workflow',
    'layout', 'versions', 'nurture', 'offices', 'cursor', 'backup', 'json_encode',
    'response_write', 'compression', 'wire_compact'))


class BoardTimings:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.milliseconds = {}
        self.started = time.perf_counter() if enabled else None
        self.emitted = False

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get('DOUYIN_BOARD_TIMING') == '1')

    def _record(self, name, started):
        if name not in PHASES:
            raise ValueError('Unrecognized performance phase')
        self.milliseconds[name] = self.milliseconds.get(name, 0.0) + (time.perf_counter() - started) * 1000

    @contextmanager
    def phase(self, name):
        if name not in PHASES:
            raise ValueError('Unrecognized performance phase')
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            self._record(name, started)

    @contextmanager
    def connection(self, opener):
        if not self.enabled:
            with opener() as db:
                yield db
            return
        started = time.perf_counter()
        closing = None
        connected = False
        try:
            with opener() as db:
                self._record('connect', started)
                connected = True
                try:
                    yield db
                finally:
                    closing = time.perf_counter()
        finally:
            if connected and closing is not None:
                self._record('transaction_close', closing)
            elif not connected:
                self._record('connect', started)

    def values(self):
        if not self.enabled:
            return {}
        return {**{name: round(value, 3) for name, value in self.milliseconds.items()},
                'total': round((time.perf_counter() - self.started) * 1000, 3)}

    def headers(self):
        if not self.enabled:
            return []
        # No paths, identifiers, counts, SQL, parameters, or exception messages.
        return [('Server-Timing', ', '.join(name + ';dur=' + format(value, '.3f')
                                           for name, value in self.values().items()))]

    def emit(self):
        if not self.enabled or self.emitted:
            return
        self.emitted = True
        logging.getLogger('douyin_board.performance').info(
            json.dumps(self.values(), separators=(',', ':')))
