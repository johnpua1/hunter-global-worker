"""Durable pre-ranking results bound to source contents and calculation code."""
import hashlib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from runner import compact, digest

LOG = logging.getLogger('hunter')


@lru_cache(maxsize=1)
def calculation_version():
    root = Path(__file__).parent
    return digest(b''.join((root / name).read_bytes() for name in
                          ('derived.py', 'analytics.py', 'derived_resume.py')))


def context_hash(market, date, universe, base_count, daily, patches, events, anchors):
    value = [calculation_version(), market, date, universe, base_count,
             dict(daily), dict(patches), events, dict(anchors)]
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode())


def source_identity(info):
    # A name or size alone cannot prove that an earlier calculation is valid.
    if not isinstance(info, dict) or not re.fullmatch('[a-f0-9]{32}', info.get('md5', '')):
        return None
    return {'md5': info['md5'], 'size': int(info['size'])}


def verify_source(raw, identity):
    if identity and (len(raw) != identity['size'] or
                     hashlib.md5(raw).hexdigest() != identity['md5']):
        raise RuntimeError('DERIVED_RESUME_SOURCE_CHANGED')


class BatchResults:
    def __init__(self, drive, market, date, context):
        self.drive, self.market, self.date, self.context = drive, market, date, context

    def key(self, batch, source):
        return digest(compact([self.context, batch, source]))

    def path(self, batch, source):
        return (f'{self.market}/CONTROL/PHASE2_RESUME/DERIVED_BATCHES/{self.date}/'
                f'batch-{batch:04d}/{self.key(batch, source)}.json')

    def restore(self, reader, batch, source):
        if source is None:
            return None
        path = self.path(batch, source)
        if not reader.file(path):
            return None
        doc = reader.json(path)
        rows = doc.get('rows')
        if (doc.get('schema') != 1 or doc.get('input_sha256') != self.key(batch, source)
                or not isinstance(rows, list) or doc.get('rows_sha256') != digest(compact(rows))):
            raise RuntimeError('DERIVED_RESUME_RESULT_INVALID')
        # JSON round trips integer map keys as strings; ranking expects periods.
        for row in rows:
            for field in ('ma', 'slope_5d', 'price_vs_ma', 'low_distance'):
                row[field] = {int(k): v for k, v in row[field].items()}
        return rows

    def save(self, batch, source, rows):
        if source is None:
            return
        payload = compact({'schema': 1, 'input_sha256': self.key(batch, source),
                           'rows_sha256': digest(compact(rows)), 'rows': rows})
        # No completion marker is published here. Drive.put verifies the write;
        # immutable keys prevent mixed-input or partial results replacing it.
        from execution_budget import saving
        with saving():
            self.drive.put(self.path(batch, source), payload, immutable=True)
        LOG.info('DERIVED_BATCH_SAVED market=%s date=%s batch=%d rows=%d',
                 self.market, self.date, batch, len(rows))
