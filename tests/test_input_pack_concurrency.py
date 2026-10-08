"""Exercise concurrent network reads and changes during an in-flight restore."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_incremental_inputs import Store, open_cache
from runner import digest


class InputPackConcurrencyTests(unittest.TestCase):
    def fixture(self):
        store = Store()
        paths = ['US/BASE/batch-0001.ndjson.gz', 'US/BASE/batch-0002.ndjson.gz']
        store.data.update({paths[0]: b'a' * 40, paths[1]: b'b' * 40})
        cache, drive = open_cache(store)
        with patch('incremental_inputs.PACK_TARGET', 50):
            for path in paths:
                drive.read(path)
            cache.flush()
        cache, drive = open_cache(store)
        self.assertEqual(len(cache.manifest['packs']), 2)
        return store, cache, drive, paths

    def test_distinct_pack_network_reads_overlap(self):
        store, cache, drive, paths = self.fixture()
        barrier = threading.Barrier(2, timeout=3)
        original = store.read
        def read(path):
            if path.endswith('.zip'):
                barrier.wait()
            return original(path)
        with patch.object(store, 'read', side_effect=read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(drive.read, paths))
        self.assertEqual(results, [b'a' * 40, b'b' * 40])
        self.assertEqual(cache.hits, 2)
        self.assertEqual(cache.misses, 0)

    def test_writer_can_publish_while_pack_read_waits_and_new_bytes_win(self):
        store, cache, drive, paths = self.fixture()
        started, release = threading.Event(), threading.Event()
        original = store.read
        def read(path):
            if path.endswith('.zip'):
                started.set()
                if not release.wait(3):
                    raise AssertionError('read never released')
            return original(path)
        with patch.object(store, 'read', side_effect=read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(drive.read, paths[0])
                try:
                    self.assertTrue(started.wait(3))
                    # This would block on the old global network read lock.
                    update = pool.submit(cache.written, paths[0], b'new', 'application/octet-stream')
                    update.result(timeout=2)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=3), b'new')

    def test_pack_version_change_cannot_reuse_old_loaded_bytes(self):
        store, cache, drive, paths = self.fixture()
        self.assertEqual(drive.read(paths[0]), b'a' * 40)
        # Corrupting a changed cache spec must fall back to verified source.
        entry = cache.manifest['entries'][paths[0]]
        spec = cache.manifest['packs'][str(entry['pack'])]
        spec['sha256'] = digest(b'corrupt')
        store.data[cache.pack_path(entry['pack'], spec['slot'])] = b'corrupt'
        cache.memory.clear()
        self.assertEqual(drive.read(paths[0]), b'a' * 40)
        self.assertEqual(cache.misses, 1)


if __name__ == '__main__':
    unittest.main()
