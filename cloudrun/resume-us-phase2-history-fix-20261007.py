"""Verify the real history blocker, deploy US only, resume its Oct 6 Phase 2.

Only the specifically observed old US execution may be replaced, and only
after live data proves its history assertion cannot pass. HK is untouched.
"""
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('us_history_recovery_base',
    ROOT / 'cloudrun/resume-us-phase2-20261007.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
recovery = base.recovery
JOB = base.JOB
OLD = 'hunter-us-daily-w4m6s'
PREVIOUS = '0d662bba78b9c4860e929abce418da68c594c86b'
DEPLOYED = '741b5a85b168d94d4839b2ef41588da06d56f82e'


def verify_old_execution(exact):
    """Match an immutable execution image to the registry, including digests."""
    containers=list(recovery.containers(exact))
    if recovery.name(exact)!=OLD or len(containers)!=1:
        raise RuntimeError('OLD_US_EXECUTION_IDENTITY_MISMATCH_NO_CANCEL')
    image=containers[0].get('image','')
    tagged=base.BASE_IMAGE+PREVIOUS
    if image==tagged:return
    repository=base.BASE_IMAGE.rstrip(':')
    if not re.fullmatch(re.escape(repository)+r'@sha256:[0-9a-f]{64}',image):
        raise RuntimeError('OLD_US_EXECUTION_IMAGE_MISMATCH_NO_CANCEL')
    # Tags refer to version resources whose final component is sha256:<digest>.
    # Query the specific package and require the exact SHA tag, never a prefix.
    rows=recovery.gc('artifacts','tags','list','--package=runner',
                     '--repository=hunter-worker','--location='+recovery.REGION,
                     '--filter=name~"/tags/'+PREVIOUS+'$"')
    matches=[r for r in rows if r.get('name','').endswith('/tags/'+PREVIOUS)] if isinstance(rows,list) else []
    digest=matches[0].get('version','').rsplit('/',1)[-1] if len(matches)==1 else ''
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or image!=repository+'@'+digest:
        print('OLD_US_EXECUTION_OBSERVED_IMAGE='+image,flush=True)
        raise RuntimeError('OLD_US_REGISTRY_DIGEST_MISMATCH_NO_CANCEL')
    print('OLD_US_EXECUTION_DIGEST_VERIFIED='+digest,flush=True)


def eligible():
    rows = recovery.gc('run','jobs','executions','list','--job='+JOB,
                       '--sort-by=~metadata.creationTimestamp')
    if not isinstance(rows,list):
        raise RuntimeError('US_EXECUTION_LIST_INVALID')
    if (not rows or recovery.name(rows[0]) != OLD or
            any(recovery.name(r) != OLD and recovery.state(r) == 'ACTIVE' for r in rows)):
        print('US_EXECUTION_CHANGED_OR_CONCURRENT_NO_START',flush=True)
        return None
    return rows[0]


def verify_history(doc):
    """No mutations: validate current Bridge bytes, not an offline snapshot."""
    sys.path.insert(0,str(ROOT / 'hunter-global'))
    from runner import Drive
    from foundation import current_universe
    from phase2_runtime import active_index, validate_history_transition
    from phase2_pack import unpack
    class Reader(Drive):
        def _call(self,op,**kwargs):
            if op not in {'file','list','read','read_chunk'}:
                raise RuntimeError('HISTORY_PREFLIGHT_READ_ONLY')
            return super()._call(op,**kwargs)
    saved=dict(os.environ)
    try:
        values={v['name']:v for v in base.container(doc).get('env',[])}
        for key in ('APPS_SCRIPT_WEBAPP_URL','APPS_SCRIPT_SHARED_KEY'):
            os.environ[key]=base.deploy.preflight_env_value(doc,values.get(key,{}),key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90',HUNTER_BRIDGE_ATTEMPTS='2')
        drive=Reader()
        try:
            active,_=active_index('US',current_universe(drive,'US'))
            history=unpack(drive.json('US/PHASE2/EARNINGS_HISTORY.json'),active)
        finally:
            drive.http.close()
        ids={e['event_id'] for e in history['events']}
        validate_history_transition('US',ids,history,active)
        inactive=sum(e['security_id'] not in active for e in history['events'])
        missing=sum(e.get('reaction_status')=='PRICE_MISSING' for e in history['events'])
        print(f'US_REAL_HISTORY_VERIFIED=events:{len(ids)};retained_inactive:{inactive};price_missing:{missing}',flush=True)
        return inactive
    finally:
        os.environ.clear();os.environ.update(saved)


def main():
    os.umask(0o077)
    sha=sys.argv[1]
    head=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}',sha) or head != sha:
        raise RuntimeError('PINNED_CHECKOUT_REQUIRED')
    if sys.argv[2:] not in ([],['--resume-deployed']):
        raise RuntimeError('UNKNOWN_RECOVERY_ARGUMENT')
    resume=sys.argv[2:]==['--resume-deployed']
    target=DEPLOYED if resume else sha
    old=eligible()
    if old is None:return
    before=recovery.gc('run','jobs','describe',JOB)
    c=base.container(before)
    allowed={base.BASE_IMAGE+DEPLOYED} if resume else {base.BASE_IMAGE+PREVIOUS,base.BASE_IMAGE+sha}
    if (c.get('image') not in allowed
            or base.deploy.daily_runtime(before)!=(7200,0)
            or {v['name']:v.get('value') for v in c.get('env',[])}.get('DERIVED_READ_WORKERS')!='2'):
        raise RuntimeError('EXPECTED_US_CONFIGURATION_REQUIRED')
    if not base.preflight(before):return
    inactive=verify_history(before)
    if recovery.state(old)=='ACTIVE' and not inactive:
        print('ACTIVE_US_WITHOUT_PROVEN_BLOCKER_PRESERVED',flush=True);return
    if recovery.state(old)=='ACTIVE':
        verify_old_execution(recovery.gc('run','jobs','executions','describe',OLD))
    if not resume and c['image']!=base.BASE_IMAGE+target:base.build(target)
    if eligible() is None:return
    current=recovery.gc('run','jobs','describe',JOB)
    if base.configuration(current)!=base.configuration(before):
        raise RuntimeError('US_CONFIGURATION_CHANGED_DURING_BUILD')
    if not resume:
        recovery.gc('run','jobs','update',JOB,'--image='+base.BASE_IMAGE+target,
                    '--update-env-vars=HUNTER_SOURCE_SHA='+target)
    after=recovery.gc('run','jobs','describe',JOB)
    base.verify_update(before,after,target)
    print('US_HISTORY_FIX_DEPLOY_VERIFIED;RUNTIME_CAPACITY_AND_AUTO_ENTRY_UNCHANGED',flush=True)
    if not base.preflight(after):return
    old=eligible()
    if old is None:return
    if recovery.state(old)=='ACTIVE':
        # Recheck actual execution identity/image before any cancellation.
        exact=recovery.gc('run','jobs','executions','describe',OLD)
        verify_old_execution(exact)
        if not inactive:raise RuntimeError('NO_PROVEN_BLOCKER_NO_CANCEL')
        recovery.JOB,recovery.OLD=JOB,OLD
        recovery.cancel_old()  # Confirms terminal state before replacement.
    old=eligible()
    if old is None or recovery.state(old)=='ACTIVE':return
    # A receipt committed during build/cancellation always wins.
    if not base.preflight(after):return
    if eligible() is None:return
    launch_config=recovery.gc('run','jobs','describe',JOB)
    if base.configuration(launch_config)!=base.configuration(after):
        raise RuntimeError('US_CONFIGURATION_CHANGED_BEFORE_LAUNCH')
    override=base.ARGS+['--phase2-only','--as-of',base.DATE]
    own=recovery.name(recovery.gc('run','jobs','execute',JOB,'--args='+','.join(override),'--async'))
    if not re.fullmatch(re.escape(JOB)+r'-[a-z0-9]+',own):
        raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY')
    print('STARTED_US_PHASE2_ONLY='+own,flush=True)
    rows=recovery.gc('run','jobs','executions','list','--job='+JOB,
                     '--sort-by=~metadata.creationTimestamp')
    if any(recovery.name(r)!=own and recovery.state(r)=='ACTIVE' for r in rows):
        recovery.gc('run','jobs','executions','cancel',own,'--async')
        raise RuntimeError('CONCURRENT_US_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED')
    print('US_DAILY_NOT_REPLAYED;HK_AND_SUPPORT_UNCHANGED;PHASE2_COMPLETION_NOT_YET_VERIFIED',flush=True)


if __name__=='__main__':main()
