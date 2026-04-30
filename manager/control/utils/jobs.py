"""
Lightweight in-process background job queue.

Usage:
    from .jobs import start_job, get_job

    job_id = start_job(my_slow_function, arg1, kwarg=val)
    # returns immediately with a short hex job_id

    state = get_job(job_id)
    # {'status': 'running'} | {'status': 'done', 'result': {...}} | {'status': 'error', 'result': {...}}
"""
import threading
import time
import uuid
import logging

logger = logging.getLogger('djmanager.jobs')

_jobs: dict = {}
_lock = threading.Lock()
_KEEP_SECONDS = 3600   # clean up jobs older than 1 hour
_CLEANUP_INTERVAL = 300  # run cleanup every 5 minutes
_last_cleanup = 0


def _cleanup():
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - _KEEP_SECONDS
    with _lock:
        stale = [jid for jid, v in _jobs.items() if v.get('ts', 0) < cutoff and v['status'] != 'running']
        for jid in stale:
            del _jobs[jid]


def start_job(fn, *args, on_done=None, **kwargs):
    """
    Run fn(*args, **kwargs) in a daemon thread.
    on_done(job_id, result) is called from the worker thread when finished.
    Returns job_id (8-char hex string).
    """
    job_id = uuid.uuid4().hex[:8]
    with _lock:
        _jobs[job_id] = {'status': 'running', 'result': None, 'ts': time.monotonic()}

    def _worker():
        try:
            result = fn(*args, **kwargs)
            status = 'done'
        except Exception as exc:
            logger.exception('Background job %s raised', job_id)
            result = {'ok': False, 'error': str(exc)}
            status = 'error'
        with _lock:
            _jobs[job_id] = {'status': status, 'result': result, 'ts': time.monotonic()}
        if on_done:
            try:
                on_done(job_id, result)
            except Exception:
                logger.exception('on_done callback for job %s raised', job_id)
        _cleanup()

    t = threading.Thread(target=_worker, daemon=True, name=f'job-{job_id}')
    t.start()
    return job_id


def get_job(job_id: str):
    """Return current state dict or None if unknown."""
    with _lock:
        return dict(_jobs.get(job_id, {})) or None
