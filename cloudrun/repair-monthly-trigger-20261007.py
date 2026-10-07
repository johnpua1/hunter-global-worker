"""Patch only the two monthly scheduling functions in the existing script HEAD.

Time-driven triggers use saved project source. Keep the versioned Web App and
its URL intact while US/HK workers are running. No trigger/job is launched.
"""
import pathlib
import re
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLASP = ['npx', '-y', '@google/clasp@3.4.1']
FUNCTIONS = {'installMonthlyTriggerAt': 'hunterMonthlyCandidate_',
             'monthlyV2': 'listMonthlyTrigger'}


def section(source, name, following):
    marker = 'function ' + name + '('
    if source.count(marker) != 1:
        raise RuntimeError('MONTHLY_FUNCTION_NOT_UNIQUE:' + name)
    start = source.index(marker)
    end = source.index('\nfunction ' + following + '(', start)
    return start, end, source[start:end]


def patch_source(remote, candidate):
    for name, following in FUNCTIONS.items():
        start, end, old = section(remote, name, following)
        new = section(candidate, name, following)[2]
        baseline = (ROOT / 'tests/fixtures' / (name + '-before-20261007.js')).read_text()
        accepted = {baseline.strip(), new.strip()}
        prior = ROOT / 'tests/fixtures' / (name + '-before-notice-20261007.js')
        if prior.exists():
            accepted.add(prior.read_text().strip())
        if old.strip() not in accepted:
            raise RuntimeError('MONTHLY_SOURCE_CHANGED_REVIEW_REQUIRED:' + name)
        remote = remote[:start] + new + remote[end:]
    return remote


def clasp(*args, cwd=None):
    result = subprocess.run(CLASP + list(args), cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('CLASP_FAILED:' + args[0])
    return result.stdout


def sources(directory):
    return {str(p.relative_to(directory)):p.read_bytes() for p in directory.rglob('*')
            if p.is_file() and p.suffix in ('.js', '.gs', '.ts', '.json')
            and not p.name.startswith('.')}


def clone(script_id, directory):
    directory.mkdir()
    clasp('clone-script', script_id, cwd=directory)
    found = [p for p in directory.iterdir() if p.stem == 'Gateway' and p.suffix in ('.js','.gs')]
    if len(found) != 1:
        raise RuntimeError('EXACTLY_ONE_REMOTE_GATEWAY_REQUIRED')
    return found[0]


def main():
    try:
        clasp('show-authorized-user', '--json')
    except RuntimeError:
        # Existing user-scoped Apps Script credentials; no new cloud identity.
        subprocess.run(CLASP + ['login', '--no-localhost'], check=True)
    rows = clasp('list-scripts')
    ids = []
    for line in rows.splitlines():
        if line.strip().startswith('HUNTER_GLOBAL_BRIDGE'):
            match = re.search(r'[–-]\s*([^\s]+)\s*$', line)
            if match:
                ids.append(match.group(1))
    if len(ids) != 1:
        raise RuntimeError('EXACTLY_ONE_HUNTER_BRIDGE_REQUIRED')
    work = pathlib.Path(tempfile.mkdtemp(prefix='hunter-monthly-patch-'))
    project = work / 'project'
    gateway = clone(ids[0], project)
    before = sources(project)
    candidate = (ROOT / 'bridge/Gateway.gs').read_text()
    patched = patch_source(gateway.read_text(), candidate)
    if patched != gateway.read_text():
        gateway.write_text(patched)
        expected = sources(project)
        # Keep every unrelated remote source and manifest byte-for-byte.
        changed = [k for k in before if before[k] != expected.get(k)]
        if changed != [gateway.name] or before.keys() != expected.keys():
            raise RuntimeError('UNRELATED_SCRIPT_CHANGE_BLOCKED')
        clone(ids[0], work / 'fresh')
        if sources(work / 'fresh') != before:
            raise RuntimeError('REMOTE_SCRIPT_CHANGED_BEFORE_PUSH')
        clasp('push', '--force', cwd=project)
    else:
        expected = before
    clone(ids[0], work / 'readback')
    if sources(work / 'readback') != expected:
        raise RuntimeError('MONTHLY_SOURCE_READBACK_FAILED')
    print('MONTHLY_SOURCE_VERIFIED=pointer_and_notice_before_advance;retry_current_month_on_failure')
    print('MONTHLY_TRIGGER_AND_WEBAPP_URL=unchanged;no_execution_started')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print('MONTHLY_REPAIR_STOPPED=' + str(exc))
        raise SystemExit(1)
