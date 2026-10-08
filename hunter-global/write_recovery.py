"""Recover transport responses without replaying mutable or append writes."""
import logging
import time
from urllib.parse import urlsplit

import requests

LOG = logging.getLogger('hunter')
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def transient(exc):
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is None:
            return False
        host = urlsplit(response.url or '').hostname
        return (host in {'script.google.com', 'script.googleusercontent.com'} and
                (response.status_code in TRANSIENT_HTTP or
                 response.status_code == 404 and host == 'script.googleusercontent.com'))
    if isinstance(exc, ValueError):
        return (isinstance(exc, requests.exceptions.JSONDecodeError) or
                str(exc).startswith(('BRIDGE_RESPONSE_REDIRECT_UNRESOLVED:',
                                     'BRIDGE_POST_RETURNED_HEALTH:',
                                     'BRIDGE_RESPONSE_NOT_OBJECT:',
                                     'BRIDGE_RESPONSE_SHAPE:')))
    return False


def response_get(http, location, timeout):
    """GET only the trusted response resource; never POST or fetch a Drive file."""
    from execution_budget import timeout as budget_timeout
    target = urlsplit(location)
    if target.scheme != 'https' or target.hostname != 'script.googleusercontent.com':
        raise ValueError('BRIDGE_UNEXPECTED_RESPONSE_HOST')
    for attempt in range(3):
        try:
            response = http.get(location, timeout=budget_timeout(min(timeout, 30)),
                                allow_redirects=False)
            if response.status_code in TRANSIENT_HTTP | {404}:
                response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if not transient(exc) or attempt == 2:
                raise
            LOG.warning('BRIDGE_WRITE_RESPONSE_GET_RETRY attempt=%d type=%s',
                        attempt + 1, type(exc).__name__)
            time.sleep(attempt + 1)


def immutable_put(op, fields):
    # Gateway.bridgePut_ holds ScriptLock and returns an existing identical
    # file BEFORE createFile. A different existing hash raises a conflict.
    # CAS checks precede that branch, so CAS and append are never replayed.
    return (op == 'put' and fields.get('immutable') is True and
            'expected_sha256' not in fields)
