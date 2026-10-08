"""Bounded read retries with credential-free Cloud Shell failure diagnostics."""
import json
import subprocess
import time


class CloudCommandError(RuntimeError):
    pass


def classify(stderr):
    value = stderr.lower()
    for category, terms in (
        ('AUTH_REQUIRED', ('reauth', 'invalid_grant', 'login required', 'gcloud auth login', 'expired', 'refresh your current auth', 'no active account', 'not currently have an active account')),
        ('PERMISSION_DENIED', ('permission_denied', 'permission denied', 'forbidden', '403')),
        ('NOT_FOUND', ('not_found', 'not found', '404')),
        ('RATE_LIMIT', ('429', 'resource_exhausted', 'rate limit', 'quota exceeded')),
        ('TRANSIENT_TRANSPORT', ('timed out', 'timeout', 'connection', 'unavailable', '503', '502', '504', 'internal error', 'name resolution')),
    ):
        if any(term in value for term in terms):
            return category
    return 'UNCLASSIFIED'


def gc(*args):
    # Explicit allowlist: execute/update/cancel and ambiguous writes NEVER retry.
    read = (args[:2] == ('logging', 'read') or args[:3] == ('run', 'jobs', 'describe')
            or args[:4] in (('run', 'jobs', 'executions', 'list'),
                           ('run', 'jobs', 'executions', 'describe')))
    operation = '_'.join(args[:4] if args[:3] == ('run', 'jobs', 'executions') else args[:2] if args[0] == 'logging' else args[:3]).upper()
    command = ['gcloud', *args, '--project=rgs-hunter-global', '--quiet', '--format=json']
    if args[0] == 'run':
        command.append('--region=us-central1')
    for attempt in range(3 if read else 1):
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=120)
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout or '{}')
                except ValueError:
                    category = 'INVALID_JSON'
            else:
                category = classify(result.stderr)
        except subprocess.TimeoutExpired:
            category = 'TRANSIENT_TRANSPORT'
        except OSError:
            category = 'COMMAND_UNAVAILABLE'
        if read and category in {'TRANSIENT_TRANSPORT', 'RATE_LIMIT', 'INVALID_JSON'} and attempt < 2:
            print('MONITOR_READ_RETRY=' + operation + ':' + category + ':ATTEMPT_' + str(attempt + 1), flush=True)
            time.sleep(5 * (attempt + 1))
            continue
        suffix = '' if read else ':WRITE_RESULT_UNCERTAIN_DO_NOT_REPEAT'
        raise CloudCommandError('GCLOUD_FAILED:' + operation + ':' + category + suffix)
