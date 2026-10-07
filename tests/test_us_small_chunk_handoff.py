import base64
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hunter-global'))
import runner
spec=importlib.util.spec_from_file_location('us_split_handoff',ROOT/'cloudrun/resume-us-phase2-small-chunk-20261007.py')
handoff=importlib.util.module_from_spec(spec);spec.loader.exec_module(handoff)
RAW=base64.b64decode('H4sIAAAAAAACA7WTwU7DMAyG7zxGzm1kJ3Zs7zUQ52liBYY2irYOhBDvTtK1G6JiO6DlVv9J5U+f/el2zf1+u+o+5qulm7m72xogBUiucstF1+RSgMA1xBol19rX5sXNogcKkA9zxIgIVLmn1eNTSQJBf4xjEoqVW7fv/Quw/gClGFgqd79ud02f/AyS5n+9tev9pmQKULnF8nm/6zbNSzdf7Obtw6GnVCPUwLmnbrtYNvNpt183n3/Q6Vk6Yw8jj6lPpblkpIEC2sBjVO4MCPmDOSNHYBQWxRNBStcA4PN6zKsWBYhEBIZ6pJkGBxqvPaUqMxtoOKF5FT7hIIlcQwkJnyUSL0qlQRLCgGAjkfo8fP24IXOQSAOReAIs9RBUFctQDUSaJ7EkBBRZ7IcrTIZXYGOJF2xZ6jcmKlIyiSdbk2CwNakfbU2SI9w1vAnxBW902O7BG45oIt76QeTAKjpulQSPv7ZtIJs+GMHE7D9c3yaFytoBBQAA')

class USSplitHandoffTests(unittest.TestCase):
    def run_preflight(self,raw):
        calls=[]
        def call(reader,op,**f):
            calls.append((op,f))
            if op=='file':return {'file':{'size':len(raw)}}
            self.assertEqual(op,'read_chunk')
            part=raw[f['offset']:f['offset']+f['length']]
            return dict(offset=f['offset'],length=len(part),size=len(raw),eof=f['offset']+len(part)==len(raw),
                        data_base64=base64.b64encode(part).decode(),sha256=runner.digest(part))
        with patch.object(handoff.base,'container',return_value={'env':[]}), \
             patch.object(handoff.base.deploy,'preflight_env_value',side_effect=lambda d,v,k:'https://script.google.com/macros/s/test/exec' if k.endswith('URL') else 'key'), \
             patch.object(runner.Drive,'_call',call):
            handoff.verify_file({})
        return calls

    def test_verified_real_file_uses_two_slices_and_preserves_digest(self):
        calls=self.run_preflight(RAW)
        self.assertEqual([(f['offset'],f['length']) for op,f in calls if op=='read_chunk'],[(0,189),(189,189)])

    def test_changed_file_blocks_launch_preflight_even_with_valid_piece_hashes(self):
        with self.assertRaisesRegex(RuntimeError,'SHA_MISMATCH_NO_START'):
            self.run_preflight(RAW[:-1]+bytes([RAW[-1]^1]))
