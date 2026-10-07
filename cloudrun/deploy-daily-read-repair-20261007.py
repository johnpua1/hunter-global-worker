"""Deploy the read/resume fix, then replace only the known old US execution.

Uses existing jobs, capacity, and the approved 7200s/zero-retry budget. Does not
launch HK or support jobs. Repeated invocation cannot start another execution.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('daily_deploy', ROOT / 'deploy-sync-fix-20261007.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
recovery = deploy.recovery
JOB = 'hunter-us-daily'
OLD = 'hunter-us-daily-fmxrz'
recovery.JOB, recovery.OLD = JOB, OLD


def expected_latest(rows):
    if not rows or recovery.name(rows[0]) != OLD:
        print('LATEST_US_EXECUTION_CHANGED_NO_START', flush=True)
        return False
    if any(recovery.name(r) != OLD and recovery.state(r) == 'ACTIVE' for r in rows):
        print('OTHER_US_EXECUTION_ACTIVE_NO_START', flush=True)
        return False
    return True


def main():
    if not expected_latest(recovery.executions()):
        return
    # Finish build, both image readbacks, and resource/budget checks before
    # touching the running US execution. Any deploy failure leaves it running.
    original = sys.argv[:]
    try:
        sys.argv = [original[0], original[1], '--daily-only', '--repair-timeout', '--no-start']
        deploy.main()
    finally:
        sys.argv = original
    rows = recovery.executions()
    if not expected_latest(rows):
        return
    if recovery.state(rows[0]) == 'SUCCEEDED':
        print('US_COMPLETED_DURING_BUILD_NO_RESTART', flush=True)
        return
    if recovery.state(rows[0]) == 'ACTIVE':
        recovery.cancel_old()  # Only OLD; waits for confirmed termination.
    rows = recovery.executions()
    if not expected_latest(rows) or any(recovery.state(r) == 'ACTIVE' for r in rows):
        print('US_NOT_IDLE_NO_START', flush=True)
        return
    if recovery.state(rows[0]) == 'SUCCEEDED':
        print('US_COMPLETED_NO_RESTART', flush=True)
        return
    # Re-read immediately before dispatch; preserve another launcher's work.
    rows = recovery.executions()
    if not expected_latest(rows) or any(recovery.state(r) == 'ACTIVE' for r in rows):
        return
    own = recovery.name(recovery.gc('run', 'jobs', 'execute', JOB, '--async'))
    if not own.startswith(JOB + '-'):
        raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY')
    print('STARTED_REPAIRED_US=' + own, flush=True)
    if any(recovery.name(r) != own and recovery.state(r) == 'ACTIVE' for r in recovery.executions()):
        recovery.gc('run', 'jobs', 'executions', 'cancel', own, '--async')
        print('CONCURRENT_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED', flush=True)
    print('HK_NOT_STARTED;SUPPORT_UNCHANGED;DATA_COMPLETION_NOT_YET_VERIFIED', flush=True)


if __name__ == '__main__':
    main()
