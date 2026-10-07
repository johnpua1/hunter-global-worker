"""Verify the failed US 378-byte file through real split reads, then resume Phase 2."""
import importlib.util
import os
import pathlib
import sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('us_small_chunk_resume',ROOT/'cloudrun/resume-us-phase2-20261007.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
base.OLD='hunter-us-daily-nbmrf'
base.PREVIOUS='7cac2554fe0bc9b25a3ae649266c662bee12939b'
PATH='US/DAILY/2025-03-17/part-0001.ndjson.gz'
SHA='a05151fd75d2aec413a48e5af0da500e9c689c485de09a0526cae63bc021fb67'
daily_preflight=base.preflight
verified=False

def verify_file(doc):
    sys.path.insert(0,str(ROOT/'hunter-global'))
    from runner import Drive,digest,parse_lines_gz
    class SplitReader(Drive):
        def _call(self,op,**fields):
            if op not in {'file','read','read_chunk'}:raise RuntimeError('PREFLIGHT_READ_ONLY')
            if fields.get('path')==PATH and (op=='read' or (op=='read_chunk' and fields['length']>189)):
                # Exercise actual smaller-range recovery even when the original
                # whole-response endpoint happens to work at preflight time.
                raise ValueError('PREFLIGHT_WHOLE_RESPONSE_FAILURE_INJECTED')
            return super()._call(op,**fields)
    saved=dict(os.environ)
    try:
        values={v['name']:v for v in base.container(doc).get('env',[])}
        for key in ('APPS_SCRIPT_WEBAPP_URL','APPS_SCRIPT_SHARED_KEY'):
            os.environ[key]=base.deploy.preflight_env_value(doc,values.get(key,{}),key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90',HUNTER_BRIDGE_ATTEMPTS='2')
        drive=SplitReader()
        try:raw=drive.read(PATH)
        finally:drive.http.close()
        if len(raw)!=378 or digest(raw)!=SHA:raise RuntimeError('US_FAILED_FILE_SHA_MISMATCH_NO_START')
        rows=parse_lines_gz(raw)
        if len(rows)!=6 or any(r.get('date')!='2025-03-17' or not r.get('security_id','').startswith('US-') for r in rows):
            raise RuntimeError('US_FAILED_FILE_IDENTITY_MISMATCH_NO_START')
        print('US_FAILED_FILE_SPLIT_RECOVERY_VERIFIED;BYTES=378;ROWS=6;SHA256_MATCH;READ_ONLY',flush=True)
    finally:
        os.environ.clear();os.environ.update(saved)

def preflight(doc):
    global verified
    if not daily_preflight(doc):return False
    if not verified:
        verify_file(doc);verified=True
    return True

base.preflight=preflight
if __name__=='__main__':base.main()
