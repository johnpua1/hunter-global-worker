"""Durable, verified result publication; completion stays in DAILY_CHECKPOINT."""
import datetime as dt
import gzip
import json
import logging
from zoneinfo import ZoneInfo

from runner import compact, digest

LOG = logging.getLogger(__name__)
MAX_BLOB = 1_000_000


def enabled(drive):
    from incremental_inputs import CachedDrive
    return isinstance(drive, CachedDrive)


def prefix(market):
    return market + '/CONTROL/PHASE2_RESUME/'


def context(drive, market):
    from incremental_inputs import FOLDER
    records = drive.cache.records
    sources = [[p, r.get('md5'), int(r.get('size', 0))]
               for p, r in sorted(records.items()) if r.get('mimeType') != FOLDER]
    cp = drive.json(market + '/CONTROL/DAILY_CHECKPOINT.json')
    if cp.get('market') != market:
        raise RuntimeError('PHASE2_RESUME_MARKET_MISMATCH')
    return {'market': market, 'as_of': cp['last_completed_date'],
            'run_day': dt.datetime.now(ZoneInfo('Asia/Kuala_Lumpur')).date().isoformat(),
            'sources_sha256': digest(compact(sources)),
            'universe_sha256': digest(drive.read(market + '/CURRENT_UNIVERSE.json')),
            'sector_sha256': digest(drive.read(market + '/PHASE2/SECTOR_MAP.json'))}


def publish(drive, market, doc):
    expected_paths = [market + '/PHASE2/EARNINGS_HISTORY.json', market + '/PHASE2/EARNINGS_CALENDAR.json']
    if (doc.get('schema') != 1 or doc.get('result', {}).get('market') != market
            or [o['path'] for o in doc['outputs']] != expected_paths):
        raise RuntimeError('PHASE2_RESUME_RESULTS_INVALID')
    # Check every immutable staged blob before publishing any business output.
    # The output CAS itself protects against a concurrent writer. On the normal
    # path no download of the (potentially multi-MB) current output is needed.
    prepared = []
    for output in doc['outputs']:
        pieces = []
        for sha in output['blobs']:
            if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                raise RuntimeError('PHASE2_RESUME_BLOB_ID_INVALID')
            raw = drive.read(prefix(market) + 'blob-' + sha + '.gz')
            if digest(raw) != sha:
                raise RuntimeError('PHASE2_RESUME_BLOB_CORRUPT')
            pieces.append(raw)
        data = gzip.decompress(b''.join(pieces))
        if digest(data) != output['sha256']:
            raise RuntimeError('PHASE2_RESUME_RESULT_CORRUPT')
        prepared.append((output, data))
    from execution_budget import saving
    with saving():
        # The Bridge verifies both staged and finalized file SHA-256. Reusing
        # its verified write response avoids a second full-file readback through
        # READ_CACHE, which can exhaust the Cloud Run budget after committing.
        writer = getattr(drive, 'put_fast', drive.put)
        for output, data in prepared:
            try:
                writer(output['path'], data, expected_sha=output['expected_sha256'])
            except RuntimeError as exc:
                if str(exc) != 'BRIDGE_STALE_WRITE':
                    raise
                # A previous attempt may have committed before its response
                # was lost. Only an exact byte match is accepted on recovery.
                # Otherwise a concurrent correction must fail closed.
                if digest(drive.read(output['path'])) != output['sha256']:
                    raise RuntimeError('PHASE2_RESUME_OUTPUT_CHANGED') from exc
                LOG.info('PHASE2_RESULT_ALREADY_VERIFIED market=%s path=%s',
                         market, output['path'])
            else:
                LOG.info('PHASE2_RESULT_COMMITTED market=%s path=%s',
                         market, output['path'])
    return doc['result']


def resume(drive, market):
    if not enabled(drive):
        return None
    pointer = prefix(market) + 'RESULTS.json'
    if not drive.file(pointer):
        return None
    doc = drive.json(pointer)
    if doc.get('context') != context(drive, market):
        LOG.info('PHASE2_RESUME_INPUTS_CHANGED market=%s', market)
        return None
    LOG.info('PHASE2_RESUMING_RESULT_COMMIT market=%s', market)
    return publish(drive, market, doc)


def stage_and_publish(drive, market, outputs, result):
    if not enabled(drive):
        for path, data, expected in outputs:
            drive.put(path, data, expected_sha=expected)
        return result
    from incremental_inputs import save_inputs
    from execution_budget import saving
    # Phase 2 calculations are complete; reserve the existing checkpoint
    # window for immutable staging, verified CAS publication and its receipt.
    with saving():
        save_inputs(drive)
        pointer = prefix(market) + 'RESULTS.json'
        prior = drive.read(pointer) if drive.file(pointer) else None
        doc = {'schema': 1, 'context': context(drive, market), 'outputs': [], 'result': result}
        writer = getattr(drive, 'put_fast', drive.put)
        for path, data, expected in outputs:
            compressed = gzip.compress(data, compresslevel=1, mtime=0)
            blobs = []
            for offset in range(0, len(compressed), MAX_BLOB):
                blob = compressed[offset:offset + MAX_BLOB]
                sha = digest(blob)
                writer(prefix(market) + 'blob-' + sha + '.gz', blob,
                       mime='application/octet-stream', immutable=True)
                blobs.append(sha)
            doc['outputs'].append({'path': path, 'sha256': digest(data),
                                   'expected_sha256': expected, 'blobs': blobs})
        kwargs = {'expected_sha': digest(prior)} if prior is not None else {'immutable': True}
        drive.put(pointer, compact(doc), **kwargs)
        LOG.info('PHASE2_RESULTS_STAGED market=%s date=%s', market, doc['context']['as_of'])
        return publish(drive, market, doc)
