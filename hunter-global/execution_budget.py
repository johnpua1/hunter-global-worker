"""Leave time for durable checkpoints before the existing 7200s task limit."""
import contextlib
import contextvars
import logging
import os
import time

LOG = logging.getLogger(__name__)
_started = None
_saving = contextvars.ContextVar('hunter_checkpoint_io', default=False)
WORK_SECONDS = 6000
SAVE_SECONDS = 6900


class WorkBudgetExceeded(RuntimeError):
    pass


def start():
    global _started
    _started = time.monotonic() if os.getenv('HUNTER_RESUMABLE_RUN') == '1' else None


def timeout(requested):
    if _started is None:
        return requested
    remaining = (_started + (SAVE_SECONDS if _saving.get() else WORK_SECONDS)
                 - time.monotonic())
    if remaining <= 1:
        raise WorkBudgetExceeded('CHECKPOINT_BUDGET_EXHAUSTED' if _saving.get()
                                 else 'WORK_BUDGET_EXHAUSTED_RESUME_REQUIRED')
    return min(requested, remaining)


@contextlib.contextmanager
def saving():
    token = _saving.set(True)
    try:
        yield
    finally:
        _saving.reset(token)
