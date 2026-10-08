"""Continue from acknowledged work; never re-download outputs to verify writes."""
import json
import os

from runner import compact, digest


def enabled():
    return os.getenv('HUNTER_CONTINUE_ONLY') == '1'


class OutputProgress:
    """A small control record, not a scan of completed data files.

    An interrupted write intent is deliberately not replayed: its remote
    outcome is unknown. Only an acknowledged output plus acknowledged receipt
    can be skipped on the next execution.
    """
    def __init__(self, drive, market, date, stage):
        self.drive = drive
        self.path = f'{market}/CONTROL/PHASE2_RESUME/{stage}_{date}.json'
        self.raw = drive.read(self.path) if drive.file(self.path) else None
        self.doc = json.loads(self.raw) if self.raw else {
            'schema': 1, 'market': market, 'as_of': date, 'outputs': {}}
        if (self.doc.get('schema') != 1 or self.doc.get('market') != market
                or self.doc.get('as_of') != date):
            raise RuntimeError('CONTINUATION_RECEIPT_IDENTITY_MISMATCH')

    def save(self):
        raw = compact(self.doc)
        kwargs = {'expected_sha': digest(self.raw)} if self.raw is not None else {'immutable': True}
        self.drive.put(self.path, raw, **kwargs)
        self.raw = raw

    def done(self, path, sha):
        entry = self.doc['outputs'].get(path)
        if entry is None:
            return False
        if entry.get('sha256') != sha:
            raise RuntimeError('CONTINUATION_OUTPUT_INPUTS_CHANGED')
        if entry.get('status') != 'COMMITTED':
            raise RuntimeError('CONTINUATION_WRITE_OUTCOME_UNKNOWN')
        return True

    def put(self, path, data, **kwargs):
        sha = digest(data)
        if self.done(path, sha):
            return
        from execution_budget import saving
        with saving():
            self.doc['outputs'][path] = {'sha256': sha, 'status': 'PENDING'}
            self.save()
            self.drive.put(path, data, **kwargs)
            self.doc['outputs'][path]['status'] = 'COMMITTED'
            self.save()

    def complete(self, rows):
        if self.doc.get('status') == 'COMPLETE' and self.doc.get('rows') == rows:
            return
        if any(e.get('status') != 'COMMITTED' for e in self.doc['outputs'].values()):
            raise RuntimeError('CONTINUATION_OUTPUTS_INCOMPLETE')
        self.doc.update(status='COMPLETE', rows=rows)
        self.save()
