"""Version-bound, compressed large-file reads with durable chunk checkpoints."""
import base64
import gzip
import json
import logging
import os
import re
import threading

from execution_budget import saving
from runner import compact, digest

LOG = logging.getLogger(__name__)
CHUNK = 131072


def _decode(raw, spec):
    if digest(raw) != spec['compressed_sha256']:
        raise RuntimeError('LARGE_READ_COMPRESSED_HASH_MISMATCH')
    data = gzip.decompress(raw)
    if len(data) != spec['length'] or digest(data) != spec['sha256']:
        raise RuntimeError('LARGE_READ_CHUNK_HASH_MISMATCH')
    return data


def read_large(drive, path, info):
    if not info.get('revision'):
        info = drive.file(path)
    if not info or not info.get('revision'):
        raise RuntimeError('LARGE_READ_BRIDGE_UPGRADE_REQUIRED')
    identity = {'path': path, 'revision': info['revision'], 'size': int(info['size'])}
    if identity['size'] > 30_000_000:
        raise RuntimeError('LARGE_READ_SIZE_LIMIT')
    state = getattr(drive, '_read_state', None)
    if state is None:
        state = drive._read_state = {'guard': threading.Lock(), 'locks': {}, 'memory': {}}
    with state['guard']:
        lock = state['locks'].setdefault(path, threading.Lock())
    with lock:
        key = (path, identity['revision'], identity['size'])
        with state['guard']:
            if key in state['memory']:
                LOG.info('LARGE_READ_MEMORY_REUSED path=%s bytes=%d', path, identity['size'])
                return state['memory'][key]
        market = path.split('/')[0]
        root = market + '/CONTROL/READ_CACHE/' + digest(path.encode()) + '/'
        pointer = root + 'MANIFEST.json'
        prior = drive.read(pointer) if drive.file(pointer) else None
        doc = json.loads(prior) if prior is not None else {}
        if doc.get('schema') != 1 or any(doc.get(k) != v for k, v in identity.items()):
            doc = {'schema': 1, **identity, 'file_sha256': None, 'next_offset': 0, 'pieces': []}
        # Validate the entire saved layout *before* any remote reads.
        # A prior run may have checkpointed hundreds of small parts; loading
        # them serially adds minutes of needless Bridge latency on recovery.
        # Only saved immutable parts are read concurrently; the checkpoint
        # writer below remains strictly serial and SHA-verified.
        chunks, offset = [], 0
        for piece in doc['pieces']:
            if (piece['offset'] != offset or not 0 < piece['length'] <= CHUNK
                    or not re.fullmatch('[a-f0-9]{64}', piece['compressed_sha256'])
                    or not re.fullmatch('[a-f0-9]{64}', piece.get('sha256', ''))):
                raise RuntimeError('LARGE_READ_MANIFEST_INVALID')
            offset += piece['length']
        if offset != doc['next_offset'] or offset > identity['size']:
            raise RuntimeError('LARGE_READ_MANIFEST_OFFSET_INVALID')

        def restore_part(reader, piece):
            raw = reader.read(root + 'part-' + piece['compressed_sha256'] + '.gz')
            return _decode(raw, piece)

        saved_parts = doc['pieces']
        if len(saved_parts) > 1 and hasattr(drive, 'fork_reader'):
            from runner import map_drive_reads
            # Cap nested readers to 2: outer DERIVED reads may already be
            # parallel, and Apps Script has finite request capacity.
            workers = max(1, min(2, int(os.getenv('HUNTER_CACHE_RESTORE_WORKERS', '2'))))
            chunks.extend(map_drive_reads(drive, restore_part, saved_parts, workers))
        else:
            chunks.extend(restore_part(drive, piece) for piece in saved_parts)

        if sum(map(len, chunks)) != offset:
            raise RuntimeError('LARGE_READ_MANIFEST_OFFSET_INVALID')
        LOG.info('LARGE_READ_RESUME path=%s bytes_saved=%d total=%d', path, offset, identity['size'])
        while offset < identity['size']:
            length = min(CHUNK, identity['size'] - offset)
            result = drive._call('read_verified_chunk', path=path, revision=identity['revision'],
                                 offset=offset, length=length, _attempts=2)
            if (result['revision'] != identity['revision'] or result['size'] != identity['size']
                    or result['offset'] != offset or result['length'] != length
                    or result['encoding'] != 'gzip' or result['eof'] != (offset + length == identity['size'])
                    or not re.fullmatch('[a-f0-9]{64}', result['file_sha256'])):
                raise RuntimeError('LARGE_READ_SOURCE_CHANGED')
            if doc['file_sha256'] is not None and doc['file_sha256'] != result['file_sha256']:
                raise RuntimeError('LARGE_READ_SOURCE_HASH_CHANGED')
            data = _decode(base64.b64decode(result['data_base64'], validate=True), result)
            compressed = gzip.compress(data, compresslevel=1, mtime=0)
            piece = {'offset': offset, 'length': length, 'sha256': digest(data),
                     'compressed_sha256': digest(compressed)}
            desired = {**doc, 'file_sha256': result['file_sha256'], 'next_offset': offset + length,
                       'pieces': doc['pieces'] + [piece]}
            with saving():
                drive.put(root + 'part-' + piece['compressed_sha256'] + '.gz', compressed,
                          mime='application/octet-stream', immutable=True)
                kwargs = {'expected_sha': digest(prior)} if prior is not None else {'immutable': True}
                saved = compact(desired)
                drive.put(pointer, saved, **kwargs)
            prior, doc = saved, desired
            chunks.append(data); offset += length
            LOG.info('LARGE_READ_CHECKPOINT path=%s bytes_saved=%d total=%d', path, offset, identity['size'])
        data = b''.join(chunks)
        current = drive.file(path)
        if (not current or current.get('revision') != identity['revision']
                or int(current['size']) != identity['size'] or digest(data) != doc['file_sha256']):
            raise RuntimeError('LARGE_READ_FINAL_IDENTITY_MISMATCH')
        with state['guard']:
            # Bound memory, especially when derived reads traverse many BASE batches.
            while state['memory'] and sum(map(len, state['memory'].values())) + len(data) > 32_000_000:
                del state['memory'][next(iter(state['memory']))]
            state['memory'][key] = data
        LOG.info('LARGE_READ_VERIFIED path=%s bytes=%d', path, len(data))
        return data
