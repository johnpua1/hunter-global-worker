"""Cooperative I/O budgets; scoped so daily/monthly callers keep their defaults."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import time

_deadline = ContextVar("hunter_io_deadline", default=None)


class BudgetExceeded(RuntimeError):
    pass


def propagate_budget(function):
    context = copy_context()
    def wrapped(*args, **kwargs):
        return context.copy().run(function, *args, **kwargs)
    return wrapped


@contextmanager
def budget(deadline):
    parent = _deadline.get()
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        check()
        yield
    finally:
        _deadline.reset(token)


def check():
    deadline = _deadline.get()
    if deadline is not None and deadline - time.monotonic() <= 1:
        raise BudgetExceeded("MAINTENANCE_TIME_BUDGET")


def request_timeout(default):
    check()
    deadline = _deadline.get()
    # Allow separate connection/read phases without spending the entire reserve
    # on each phase. requests timeouts are inactivity bounds, not hard alarms.
    return default if deadline is None else min(default, (deadline - time.monotonic()) / 3)


def retry_sleep(seconds):
    check()
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() + seconds + 1 >= deadline:
        raise BudgetExceeded("MAINTENANCE_TIME_BUDGET")
    time.sleep(seconds)
