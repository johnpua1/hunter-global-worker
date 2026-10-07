"""Verified input packs on existing Drive; source files remain authoritative.

Cloud Run containers are ephemeral. A small CAS manifest points to compressed
packs, so the next execution restores history without fetching every original
file. Live metadata fingerprints detect late appends, equal-size corrections,
deletions and corporate actions. No trading/indicator formula changes here.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
import requests
from contextlib import contextmanager

from runner import compact, digest
from execution_budget import WorkBudgetExceeded

LOG = logging.getLogger('hunter.incremental')
FOLDER = 'application/vnd.google-apps.folder'
AREAS = {'BASE', 'DAILY', 'REPAIR_PATCH', 'CORPORATE_ACTIONS'}
PACK_TARGET = 3_000_000
PACK_LIMIT = 9_000_000  # Below the existing Bridge's 10 MB write cap.


def fingerprint(data):
    return {'md5': hashlib.md5(data).hexdigest(), 'size': len(data)}


class InputCache:
    def __init__(self, raw, market):
        self.raw, self.market = raw, market
        self.prefix = market + '/CONTROL/INCREMENTAL_CACHE/'
        self.pointer = self.prefix + 'MANIFEST.json'
        self.records = {}
        inventory = raw.source_inventory(market)
        if inventory.get('schema') != 1 or inventory.get('market') != market:
            raise RuntimeError('INPUT_INVENTORY_IDENTITY_MISMATCH')
        for entry in inventory['entries']:
            path = entry['id']
            if not self.source(path) or path in self.records:
                raise RuntimeError('INPUT_INVENTORY_PATH_INVALID')
            if entry.get('mimeType') != FOLDER:
                if not re.fullmatch('[a-f0-9]{32}', entry.get('md5', '')) or int(entry['size']) < 0:
                    raise RuntimeError('INPUT_INVENTORY_FINGERPRINT_INVALID')
            self.records[path] = dict(entry)
        self.original = raw.read(self.pointer) if raw.file(self.pointer) else None
        self.manifest = {'schema': 1, 'market': market, 'entries': {}, 'packs': {}}
        if self.original is not None:
            try:
                doc = json.loads(self.original)
                if doc.get('schema') != 1 or doc.get('market') != market:
                    raise ValueError('identity')
                for key, pack in doc['packs'].items():
                    if not key.isdigit() or pack['slot'] not in (0, 1) or not re.fullmatch('[a-f0-9]{64}', pack['sha256']):
                        raise ValueError('pack')
                for path, entry in doc['entries'].items():
                    if not self.source(path) or str(entry['pack']) not in doc['packs']:
                        raise ValueError('source')
                    if not re.fullmatch('[a-f0-9]{64}', entry['sha256']):
                        raise ValueError('hash')
                self.manifest = doc
            except (ValueError, KeyError, TypeError, AttributeError):
                LOG.warning('INPUT_CACHE_REBUILD market=%s reason=MANIFEST_INVALID', market)
        self.memory, self.loaded_packs, self.dirty = {}, {}, set()
        self.guard = threading.RLock()
        self.checkpoint_at = time.monotonic()
        self.locks = {}
        self.hits = self.misses = 0
        LOG.info('INPUT_CACHE_OPEN market=%s indexed_files=%d cached_files=%d', market,
                 sum(e.get('mimeType') != FOLDER for e in self.records.values()),
                 len(self.manifest['entries']))
        LOG.info('INPUT_CACHE_RESUME market=%s unchanged_saved_files=%d', market,
                 sum(self.matches(p, e) for p, e in self.manifest['entries'].items()))

    def source(self, path):
        parts = path.split('/')
        return (len(parts) >= 2 and parts[0] == self.market and parts[1] in AREAS
                and all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in ('.', '..') for p in parts))

    def lock(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def matches(self, path, entry):
        info = self.records.get(path, {})
        return (info.get('mimeType') != FOLDER and info.get('md5') == entry.get('md5')
                and int(info.get('size', -1)) == entry.get('size'))

    def pack_path(self, number, slot):
        return self.prefix + 'pack-%05d-%d.zip' % (int(number), slot)

    def load_pack(self, reader, number):
        key = str(number)
        with self.lock('pack:' + key):
            if key not in self.loaded_packs:
                try:
                    spec = self.manifest['packs'][key]
                    data = reader.read(self.pack_path(key, spec['slot']))
                    if digest(data) != spec['sha256']:
                        raise ValueError('hash')
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        if sum(e.file_size for e in archive.infolist()) > 30_000_000:
                            raise ValueError('expanded size')
                        self.loaded_packs[key] = {p: archive.read(p) for p, e in self.manifest['entries'].items()
                                                 if str(e['pack']) == key}
                    LOG.info('INPUT_PACK_RESTORED market=%s pack=%s files=%d',
                             self.market, key, len(self.loaded_packs[key]))
                except WorkBudgetExceeded:
                    raise
                except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, OSError, requests.RequestException) as exc:
                    if str(exc) == 'LARGE_READ_TRANSPORT_RETRY_REQUIRED':
                        raise
                    # Corruption is a cache miss, never an accepted source.
                    self.loaded_packs[key] = {}
                    LOG.warning('INPUT_CACHE_PACK_REBUILD market=%s pack=%s', self.market, key)
            return self.loaded_packs[key]

    def read(self, reader, path):
        with self.lock('source:' + path):
            with self.guard:
                if path in self.memory:
                    self.hits += 1
                    return self.memory[path]
                entry = self.manifest['entries'].get(path)
                if entry and self.matches(path, entry):
                    data = self.load_pack(reader, entry['pack']).get(path)
                    if data is not None and digest(data) == entry['sha256'] and fingerprint(data) == {
                            'md5': entry['md5'], 'size': entry['size']}:
                        self.memory[path] = data
                        self.hits += 1
                        return data
            LOG.info('INPUT_SOURCE_READ market=%s path=%s', self.market, path)
            info = self.records.get(path)
            data = (reader.read_known(path, info) if info is not None and hasattr(reader, 'read_known')
                    else reader.read(path))
            if not info or fingerprint(data) != {'md5': info.get('md5'), 'size': int(info.get('size', -1))}:
                raise RuntimeError('INPUT_SOURCE_CHANGED_DURING_READ:' + path)
            with self.guard:
                self.memory[path] = data
                self.dirty.add(path)
                self.misses += 1
            return data

    def written(self, path, data, mime):
        with self.guard:
            self._written(path, data, mime)

    def _written(self, path, data, mime):
        # Only called after the existing append/put SHA verification succeeds.
        self.records[path] = {'id': path, 'name': path.rsplit('/', 1)[-1],
                              'mimeType': mime, **fingerprint(data)}
        parts = path.split('/')
        for length in range(2, len(parts)):
            parent = '/'.join(parts[:length])
            self.records.setdefault(parent, {'id': parent, 'name': parts[length-1], 'mimeType': FOLDER})
        self.memory[path] = data
        self.dirty.add(path)

    def checkpoint(self):
        # The lock also bounds uncommitted work while another reader saves.
        with self.guard:
            if self.dirty and (len(self.dirty) >= 25 or time.monotonic() - self.checkpoint_at >= 120
                               or sum(len(self.memory[p]) for p in self.dirty) >= 6_000_000):
                self.flush()

    def flush(self):
        from execution_budget import saving
        with self.guard, saving():
            # Never share a requests.Session with a reader still in flight.
            writer = self.raw.fork_reader() if hasattr(self.raw, 'fork_reader') else self.raw
            try:
                self._flush(writer)
                self.checkpoint_at = time.monotonic()
            finally:
                if writer is not self.raw:
                    writer.http.close()

    def _flush(self, writer):
        entries = {p: dict(e) for p, e in self.manifest['entries'].items() if self.matches(p, e)}
        pending = {p: self.memory[p] for p in self.dirty}
        if not pending and entries == self.manifest['entries']:
            return
        # Fill the previous small tail pack; daily append growth doesn't create
        # one more remote cache read per trading day forever.
        packs = {k: dict(v) for k, v in self.manifest['packs'].items()}
        written_groups = {}
        tail = next((k for k in sorted(packs, key=int, reverse=True)
                     if packs[k]['size'] < PACK_TARGET // 2), None)
        if pending and tail is not None:
            tail_paths = [p for p, e in entries.items() if str(e['pack']) == tail]
            previous = ({p: self.memory[p] for p in tail_paths}
                        if all(p in self.memory for p in tail_paths) else self.load_pack(writer, tail))
            for path, entry in list(entries.items()):
                if str(entry['pack']) == tail:
                    if (path not in pending and path in previous and digest(previous[path]) == entry['sha256']
                            and fingerprint(previous[path]) == {'md5': entry['md5'], 'size': entry['size']}):
                        pending[path] = previous[path]
                    del entries[path]
        for path in pending:
            entries.pop(path, None)
        groups, group, size = [], {}, 0
        for path, data in sorted(pending.items()):
            if group and size + len(data) > PACK_TARGET:
                groups.append(group); group, size = {}, 0
            group[path] = data; size += len(data)
        if group:
            groups.append(group)
        number = max(map(int, packs), default=-1) + 1
        for index, group in enumerate(groups):
            key = tail if index == 0 and tail is not None else str(number)
            if key != tail:
                number += 1
            slot = 1 - packs[key]['slot'] if key in packs else 0
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for path, data in group.items():
                    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data)
            data = buffer.getvalue()
            if len(data) > PACK_LIMIT:
                raise RuntimeError('INPUT_CACHE_PACK_TOO_LARGE')
            writer.put(self.pack_path(key, slot), data, mime='application/octet-stream')
            packs[key] = {'slot': slot, 'sha256': digest(data), 'size': len(data)}
            written_groups[key] = dict(group)
            for path, data in group.items():
                entries[path] = {**fingerprint(data), 'sha256': digest(data), 'pack': int(key)}
        used = {str(e['pack']) for e in entries.values()}
        doc = {'schema': 1, 'market': self.market, 'entries': entries,
               'packs': {k: v for k, v in packs.items() if k in used}}
        data = compact(doc)
        kwargs = {'expected_sha': digest(self.original)} if self.original is not None else {'immutable': True}
        writer.put(self.pointer, data, **kwargs)
        self.original, self.manifest = data, doc
        self.loaded_packs = {k: v for k, v in self.loaded_packs.items() if k in used}
        self.loaded_packs.update(written_groups)
        self.dirty.clear()
        LOG.info('INPUT_CACHE_COMMITTED market=%s files=%d packs=%d source_reads=%d cache_hits=%d',
                 self.market, len(entries), len(doc['packs']), self.misses, self.hits)


class CachedDrive:
    def __init__(self, raw, cache):
        self.raw, self.cache = raw, cache

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def fork_reader(self):
        return CachedDrive(self.raw.fork_reader(), self.cache)

    def read(self, path):
        path = path.strip('/')
        if not self.cache.source(path):
            return self.raw.read(path)
        data = self.cache.read(self.raw, path)
        self.cache.checkpoint()
        return data

    def json(self, path):
        return json.loads(self.read(path))

    def file(self, path):
        return self.cache.records.get(path) if self.cache.source(path) else self.raw.file(path)

    def list(self, path, name=None):
        if self.cache.source(path):
            prefix = path + '/'
            return [dict(e) for p, e in sorted(self.cache.records.items())
                    if p.startswith(prefix) and '/' not in p[len(prefix):] and (name is None or e['name'] == name)]
        return self.raw.list(path, name) if name is not None else self.raw.list(path)

    def put(self, path, content, mime='application/json', **kwargs):
        result = self.raw.put(path, content, mime=mime, **kwargs)
        if self.cache.source(path):
            self.cache.written(path, content, mime)
            self.cache.checkpoint()
        return result

    def append(self, path, content, mime='application/json'):
        result = self.raw.append(path, content, mime)
        if self.cache.source(path):
            self.cache.written(path, content, mime)
            self.cache.checkpoint()
        return result

    def put_fast(self, path, content, mime='application/json', **kwargs):
        result = self.raw.put_fast(path, content, mime=mime, **kwargs)
        if self.cache.source(path):
            self.cache.written(path, content, mime)
            self.cache.checkpoint()
        return result


def save_inputs(drive, strict=True):
    if isinstance(drive, CachedDrive):
        try:
            drive.cache.flush()
        except Exception as exc:
            LOG.error('INPUT_CACHE_SAVE_FAILED market=%s type=%s', drive.cache.market, type(exc).__name__)
            if strict:
                raise


@contextmanager
def incremental_inputs(raw, market):
    if os.getenv('HUNTER_INCREMENTAL_INPUTS') != '1':
        yield raw
        return
    cache = InputCache(raw, market)
    failed = False
    try:
        yield CachedDrive(raw, cache)
    except BaseException:
        failed = True
        raise
    finally:
        # A performance cache never fabricates or erases completion. On a cache
        # failure the next run can still reconstruct verified source inputs.
        save_inputs(CachedDrive(raw, cache), strict=not failed)
        LOG.info('INPUT_CACHE_SUMMARY market=%s source_reads=%d cache_hits=%d',
                 market, cache.misses, cache.hits)
