"""One shared slot for heavy board work. Callers wait until the current holder finishes."""
import threading

_slot = threading.Lock()
_state = threading.Lock()
_waiting = 0


def acquire():
    global _waiting
    with _state:
        _waiting += 1
    try:
        _slot.acquire()
    except BaseException:
        with _state:
            _waiting -= 1
        raise
    with _state:
        _waiting -= 1


def release():
    _slot.release()


def waiting():
    with _state:
        return _waiting
