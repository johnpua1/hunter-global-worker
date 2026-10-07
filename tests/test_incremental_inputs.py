import copy
import hashlib
import json
import sys
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
from incremental_inputs import InputCache, CachedDrive, FOLDER, fingerprint
from runner import compact, digest, lines_gz
from foundation import read_existing
import derived
import phase2_runtime as phase2
import test_patch_snapshot_reuse as patch_tests
from test_patch_snapshot_reuse import CountingDrive
from test_foundation import row


def inventory(drive, market):
    entries = {}
    for path, data in drive.data.items():
        parts = path.split('/')
        if parts[0] != market or parts[1] not in ('BASE', 'DAILY', 'REPAIR_PATCH', 'CORPORATE_ACTIONS'):
            continue
        entries[path] = {'id': path, 'name': parts[-1], 'mimeType': 'application/octet-stream', **fingerprint(data)}
        for length in range(2, len(parts)):
            parent = '/'.join(parts[:length])
            entries[parent] = {'id': parent, 'name': parts[length-1], 'mimeType': FOLDER}
    return {'schema': 1, 'market': market, 'entries': list(entries.values())}


class Store(CountingDrive):
    def __init__(self):
        super().__init__()
        self.http = Mock()

    def source_inventory(self, market):
        return inventory(self, market)

    def fork_reader(self):
        return self

    def put(self, path, data, **kwargs):
        if 'expected_sha' in kwargs and digest(self.data.get(path, b'')) != kwargs['expected_sha']:
            raise RuntimeError('BRIDGE_STALE_WRITE')
        if kwargs.get('immutable') and path in self.data and self.data[path] != data:
            raise RuntimeError('IMMUTABLE_CONFLICT')
        super().put(path, data, **kwargs)


def open_cache(store, market='US'):
    cache = InputCache(store, market)
    return cache, CachedDrive(store, cache)


class IncrementalInputsTests(unittest.TestCase):
    def test_next_day_and_late_append_read_only_new_originals(self):
        import datetime as dt
        for market in ('US', 'HK'):
            with self.subTest(market=market):
                store = Store(); sid = market + '-000001'
                for i in range(500):
                    day = (dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat()
                    store.data[f'{market}/DAILY/{day}/part-0001.ndjson.gz'] = lines_gz([row(day, 100, sid)])
                # MemoryDrive's flat list isn't the real Bridge; provide the
                # market root's DAILY folder while cached lists handle depth.
                original_list = store.list
                store.list = lambda path: [{'name': 'DAILY', 'mimeType': FOLDER}] if path == market else original_list(path)
                securities = [{'security_id': sid}]
                cache, drive = open_cache(store, market)
                first_rows = {}
                read_existing(drive, market, securities, '2023-12-31', first_rows)
                cache.flush()
                originals = {p: data for p, data in store.data.items() if '/DAILY/' in p}
                new = f'{market}/DAILY/2026-10-07/part-0001.ndjson.gz'
                late = f'{market}/DAILY/2024-01-01/part-0002.ndjson.gz'
                store.data[new] = lines_gz([row('2026-10-07', 120, sid)])
                store.data[late] = lines_gz([row('2024-01-01', 50, market + '-000002')])
                store.reads.clear(); store.lists.clear()
                cache, drive = open_cache(store, market)
                rows = {}
                last, keys = read_existing(drive, market, securities, '2023-12-31', rows)
                self.assertEqual(last[sid], '2026-10-07')
                self.assertEqual(len(keys), 502)
                self.assertEqual({p for p in store.reads if '/DAILY/' in p}, {new, late})
                self.assertFalse(any('/DAILY/' in p for p in store.lists))
                self.assertTrue(all(store.data[p] == data for p, data in originals.items()))

    def test_equal_size_correction_deletion_and_new_action_are_detected(self):
        store = Store()
        a, b = 'US/REPAIR_PATCH/a.json', 'US/REPAIR_PATCH/b.json'
        store.data.update({a: b'{"value":1}', b: b'{"value":2}'})
        cache, drive = open_cache(store)
        drive.read(a); drive.read(b); cache.flush()
        store.data[a] = b'{"value":9}'; del store.data[b]
        action = 'US/CORPORATE_ACTIONS/split.json'; store.data[action] = b'[]'
        store.reads.clear()
        cache, drive = open_cache(store)
        self.assertEqual(drive.json(a), {'value': 9})
        self.assertIsNone(drive.file(b))
        self.assertEqual([e['name'] for e in drive.list('US/REPAIR_PATCH')], ['a.json'])
        self.assertEqual(drive.json(action), [])
        cache.flush()
        self.assertNotIn(b, cache.manifest['entries'])
        self.assertEqual(store.reads[a], 1)

    def test_pack_corruption_recovers_verified_source_and_race_is_rejected(self):
        store = Store(); path = 'US/BASE/batch-0001.ndjson.gz'; store.data[path] = b'original'
        cache, drive = open_cache(store); drive.read(path); cache.flush()
        spec = cache.manifest['packs']['0']
        store.data[cache.pack_path('0', spec['slot'])] = b'corrupt'
        cache, drive = open_cache(store)
        self.assertEqual(drive.read(path), b'original')
        cache.flush()
        cache, drive = open_cache(store)
        # Simulate metadata/read race for a not-yet-cached new source.
        new = 'US/REPAIR_PATCH/new.json'; store.data[new] = b'old'
        cache, drive = open_cache(store); store.data[new] = b'NEW'
        with self.assertRaisesRegex(RuntimeError, 'SOURCE_CHANGED_DURING_READ'):
            drive.read(new)
        self.assertNotIn(new, cache.memory)

    def test_failed_pack_write_keeps_previous_manifest_and_current_data(self):
        store = Store(); path = 'US/REPAIR_PATCH/a.json'; store.data[path] = b'old'
        cache, drive = open_cache(store); drive.read(path); cache.flush()
        before = store.data[cache.pointer]
        store.data[path] = b'new'
        cache, drive = open_cache(store); drive.read(path)
        with patch.object(store, 'put', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                cache.flush()
        self.assertEqual(store.data[cache.pointer], before)
        cache, drive = open_cache(store)
        self.assertEqual(drive.read(path), b'new')

    def test_warm_concurrent_read_loads_one_pack_and_append_updates_inventory(self):
        store = Store()
        paths = ['US/REPAIR_PATCH/%03d.json' % i for i in range(50)]
        store.data.update({p: compact({'x': i}) for i, p in enumerate(paths)})
        cache, drive = open_cache(store)
        for p in paths: drive.read(p)
        cache.flush(); store.reads.clear()
        cache, drive = open_cache(store)
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(drive.read, paths))
        self.assertEqual(actual, [store.data[p] for p in paths])
        self.assertEqual(sum(n for p, n in store.reads.items() if p.endswith('.zip')), 1)
        self.assertEqual(sum(store.reads[p] for p in paths), 0)
        new = 'US/DAILY/2026-10-07/part-0001.ndjson.gz'
        drive.append(new, b'new', 'application/x-gzip')
        self.assertEqual(drive.read(new), b'new')
        self.assertEqual(drive.list('US/DAILY/2026-10-07')[0]['name'], 'part-0001.ndjson.gz')
        self.assertEqual(store.reads[new], 0)

    def test_cache_storage_and_read_count_do_not_grow_one_pack_per_day(self):
        store = Store()
        for i in range(30):
            path = 'US/REPAIR_PATCH/%03d.json' % i
            store.data[path] = compact({'x': i})
            cache, drive = open_cache(store); drive.read(path); cache.flush()
        self.assertEqual(len(cache.manifest['packs']), 1)
        self.assertEqual(len([p for p in store.data if p.endswith('.zip')]), 2)

    def test_manifest_conflict_cannot_publish_stale_inputs(self):
        store = Store(); path = 'US/REPAIR_PATCH/a.json'; store.data[path] = b'one'
        cache, drive = open_cache(store); drive.read(path); cache.flush()
        store.data[path] = b'two'
        earlier, reader = open_cache(store); reader.read(path)
        store.data[path] = b'new'
        later, reader = open_cache(store); reader.read(path); later.flush()
        committed = store.data[later.pointer]
        with self.assertRaisesRegex(RuntimeError, 'STALE_WRITE'):
            earlier.flush()
        self.assertEqual(store.data[later.pointer], committed)
        restored, reader = open_cache(store)
        # A conflicting pack write may invalidate a cache pack, but never
        # overrides source fingerprints or returns stale market data.
        self.assertEqual(reader.read(path), b'new')

    def test_rank_and_phase2_exactly_match_cold_sources_on_second_execution(self):
        for market in ('US', 'HK'):
            with self.subTest(market=market), ExitStack() as stack:
                raw, sid, date, daily, paths = patch_tests.PatchSnapshotTests().fixture(stack, market)
                store = Store(); store.data = raw.data
                history = {'events': [{'security_id': sid, 'report_date': '2026-01-26',
                                       'session': 'BMO', 'reaction_status': 'PARTIAL'}]}
                derived.build(store, market, date, daily_rows=daily)
                expected_rank = {p: v for p, v in store.data.items() if '/DERIVED/' in p}
                expected_phase2 = phase2.calculate_reactions(store, market, copy.deepcopy(history), daily_rows=daily)
                for _ in range(2):
                    store.reads.clear()
                    cache, drive = open_cache(store, market)
                    snapshot = {}
                    derived.build(drive, market, date, daily_rows=daily, patch_snapshot=snapshot)
                    actual = phase2.calculate_reactions(drive, market, copy.deepcopy(history),
                                                         daily_rows=daily, patch_snapshot=snapshot)
                    cache.flush()
                    self.assertEqual(expected_phase2, actual)
                    self.assertEqual(expected_rank, {p: v for p, v in store.data.items() if '/DERIVED/' in p})
                self.assertEqual(sum(n for p, n in store.reads.items() if cache.source(p)), 0)


if __name__ == '__main__':
    unittest.main()
