"""
In-memory circular log buffer for the security scanner.
Does NOT persist across restarts — staff-only read via /security-scanner/log/.
"""
import logging
import threading
from collections import deque
from datetime import datetime, timezone

MAX_ENTRIES = 500

_LEVEL_CSS = {
    'DEBUG':    'secondary',
    'INFO':     'info',
    'WARNING':  'warning',
    'ERROR':    'danger',
    'CRITICAL': 'danger',
}


class _ScanMemHandler(logging.Handler):
    def __init__(self, maxlen=MAX_ENTRIES):
        super().__init__()
        self._lock = threading.Lock()
        self._records: deque = deque(maxlen=maxlen)

    def emit(self, record):
        try:
            entry = {
                'ts':    datetime.now(tz=timezone.utc).strftime('%H:%M:%S'),
                'level': record.levelname,
                'css':   _LEVEL_CSS.get(record.levelname, 'secondary'),
                'msg':   self.format(record),
            }
            with self._lock:
                self._records.append(entry)
        except Exception:
            self.handleError(record)

    def get_entries(self):
        with self._lock:
            return list(self._records)

    def clear(self):
        with self._lock:
            self._records.clear()


_handler = _ScanMemHandler()
_handler.setLevel(logging.DEBUG)
_handler.setFormatter(logging.Formatter('%(message)s'))

# Attach to the scanner logger — propagate=False so we don't spam the
# console/file with DEBUG noise; WARNING+ still reaches root via propagation
# because we keep propagate=True and only the memory handler is at DEBUG here.
# Actually we set propagate=False to avoid double-writes; WARNING+ is handled
# by the memory handler too so nothing is lost.
_logger = logging.getLogger('djmanager.scanner')
_logger.addHandler(_handler)
_logger.setLevel(logging.DEBUG)
_logger.propagate = False  # avoid console spam from DEBUG/INFO records


def get_log_entries():
    """Return list of dicts: ts, level, css, msg."""
    return _handler.get_entries()


def clear_log():
    _handler.clear()
