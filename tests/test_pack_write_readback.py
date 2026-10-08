import base64
import copy
import gzip
import unittest
from unittest.mock import Mock

import runner


class PackWriteReadbackTests(unittest.TestCase):
    path = 'US/CONTROL/INCREMENTAL_CACHE/pack-00011-1.zip'
    payload = bytes(range(256)) * 11000

    def drive(self):
        d = runner.Drive.__new__(runner.Drive)
        info = {'id': self.path, 'revision': 'persisted:123', 'size': len(self.payload)}
        sample = self.payload[:4096]
        zipped = gzip.compress(sample)
        proof = {'revision': info['revision'], 'size': len(self.payload), 'offset': 0,
                 'length': len(sample), 'encoding': 'gzip', 'eof': False,
                 'sha256': runner.digest(sample), 'compressed_sha256': runner.digest(zipped),
                 'file_sha256': runner.digest(self.payload),
                 'data_base64': base64.b64encode(zipped).decode()}
        d.file = Mock(return_value=info)
        d.read = Mock(side_effect=AssertionError('full pack must not be downloaded'))
        d._call = Mock(side_effect=[{'file': info, 'sha256': runner.digest(self.payload)}, proof])
        return d, info, proof

    def test_large_pack_uses_independent_stored_full_hash_without_full_download(self):
        d, info, proof = self.drive()
        self.assertEqual(d.put(self.path, self.payload), info)
        self.assertEqual(d._call.call_count, 2)
        d._call.assert_called_with('read_verified_chunk', path=self.path,
                                  revision=info['revision'], offset=0, length=4096, _attempts=2)
        self.assertEqual(d.file.call_count, 2)
        d.read.assert_not_called()

    def test_every_proof_integrity_field_is_checked(self):
        for key, bad in [('revision', 'changed'), ('size', 1), ('offset', 1),
                         ('length', 1), ('encoding', 'raw'), ('eof', True),
                         ('sha256', '0'*64), ('compressed_sha256', '0'*64),
                         ('file_sha256', '0'*64)]:
            with self.subTest(key=key):
                d, info, proof = self.drive()
                proof[key] = bad
                with self.assertRaisesRegex(RuntimeError, 'PACK_WRITE_READBACK_MISMATCH'):
                    d.put(self.path, self.payload)
                d.read.assert_not_called()

    def test_same_sample_different_tail_cannot_pass(self):
        d, info, proof = self.drive()
        proof['file_sha256'] = runner.digest(self.payload[:-1] + b'x')
        with self.assertRaisesRegex(RuntimeError, 'PACK_WRITE_READBACK_MISMATCH'):
            d.put(self.path, self.payload)

    def test_revision_change_or_deleted_file_after_proof_cannot_pass(self):
        for current in (None, {'id': self.path, 'revision': 'changed', 'size': len(self.payload)}):
            d, info, proof = self.drive()
            d.file.side_effect = [info, current]
            with self.assertRaisesRegex(RuntimeError, 'PACK_WRITE_REVISION_CHANGED'):
                d.put(self.path, self.payload)

    def test_missing_revision_size_or_identity_fails_before_proof(self):
        for key in ('revision', 'size', 'id'):
            d, info, proof = self.drive()
            del info[key]
            with self.assertRaisesRegex(RuntimeError, 'PACK_WRITE_IDENTITY_MISMATCH'):
                d.put(self.path, self.payload)
            self.assertEqual(d._call.call_count, 1)

    def test_transport_failure_remains_resumable_without_second_write(self):
        d, info, proof = self.drive()
        d._call.side_effect = [{'file': info, 'sha256': runner.digest(self.payload)},
                               runner.requests.ReadTimeout()]
        with self.assertRaisesRegex(RuntimeError, 'LARGE_READ_TRANSPORT_RETRY_REQUIRED'):
            d.put(self.path, self.payload)
        self.assertEqual(d._call.call_count, 2)
        d.read.assert_not_called()

    def test_scope_keeps_sources_phase2_and_small_pack_full_readback(self):
        for path, content in [('US/PHASE2/EARNINGS_HISTORY.json', self.payload),
                              ('US/BASE/batch-0011.ndjson.gz', self.payload),
                              ('US/CONTROL/INCREMENTAL_CACHE/MANIFEST.json', self.payload),
                              (self.path, b'small')]:
            d = runner.Drive.__new__(runner.Drive)
            d._call = Mock(return_value={'sha256': runner.digest(content), 'file': {}})
            d.read = Mock(return_value=content)
            d.file = Mock()
            d.put(path, content)
            d.read.assert_called_once_with(path)
            d.file.assert_not_called()

    def test_wrong_write_ack_is_never_accepted(self):
        d, info, proof = self.drive()
        d._call.side_effect = [{'file': info, 'sha256': '0'*64}]
        with self.assertRaisesRegex(RuntimeError, 'BRIDGE_WRITE_SHA_MISMATCH'):
            d.put(self.path, self.payload)
        d.file.assert_not_called()
