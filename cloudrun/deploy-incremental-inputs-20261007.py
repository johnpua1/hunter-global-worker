"""Update existing daily images only; never launch, cancel, or replay a job."""
import copy
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('phase2_commit_deploy_base',ROOT/'cloudrun/resume-us-phase2-20261007.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
gc=base.recovery.gc
PREVIOUS={'US':{'f49620bd89f38cbe9d5257ff83a7c7318045e45f','a1cb9dac1bb8885e7b303786814bba3574fd10df'},
          'HK':{'7cac2554fe0bc9b25a3ae649266c662bee12939b','a1cb9dac1bb8885e7b303786814bba3574fd10df'}}


def container(doc,market):
    rows=list(base.recovery.containers(doc))
    if len(rows)!=1 or rows[0].get('args')!=['--mode','auto','--market',market] or rows[0].get('command'):
        raise RuntimeError('DAILY_ENTRYPOINT_MISMATCH:'+market)
    return rows[0]


def verify(before,after,market,sha):
    expected=copy.deepcopy(container(before,market))
    expected['image']=base.BASE_IMAGE+sha
    env={v['name']:v for v in expected.get('env',[])}
    env['HUNTER_SOURCE_SHA']={'name':'HUNTER_SOURCE_SHA','value':sha}
    env['HUNTER_INCREMENTAL_INPUTS']={'name':'HUNTER_INCREMENTAL_INPUTS','value':'1'}
    expected['env']=sorted(env.values(),key=lambda v:v['name'])
    actual=copy.deepcopy(container(after,market))
    actual['env']=sorted(actual.get('env',[]),key=lambda v:v['name'])
    if (expected!=actual or base.deploy.daily_runtime(before)!=base.deploy.daily_runtime(after)
            or base.deploy.runtime_capacity(before)!=base.deploy.runtime_capacity(after)):
        raise RuntimeError('DAILY_INCREMENTAL_READBACK_MISMATCH:'+market)


def checkpoint(doc,market):
    sys.path.insert(0,str(ROOT/'hunter-global'))
    from runner import Drive
    class Reader(Drive):
        def _call(self,op,**kwargs):
            if op not in {'file','list','read','read_chunk','source_inventory'}:raise RuntimeError('PREFLIGHT_READ_ONLY')
            return super()._call(op,**kwargs)
    saved=dict(os.environ)
    try:
        env={v['name']:v for v in container(doc,market).get('env',[])}
        for key in ('APPS_SCRIPT_WEBAPP_URL','APPS_SCRIPT_SHARED_KEY'):
            os.environ[key]=base.deploy.preflight_env_value(doc,env.get(key,{}),key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90',HUNTER_BRIDGE_ATTEMPTS='2')
        drive=Reader()
        try:
            cp=drive.json(market+'/CONTROL/DAILY_CHECKPOINT.json')
            inventory=drive.source_inventory(market)
            if (inventory.get('schema')!=1 or inventory.get('market')!=market or
                    not any(e['id'].startswith(market+'/BASE/') and e.get('md5') for e in inventory.get('entries',[]))):
                raise RuntimeError('INCREMENTAL_BRIDGE_PREFLIGHT_FAILED:'+market)
            print('INCREMENTAL_BRIDGE_VERIFIED='+market,flush=True)
        finally:drive.http.close()
        if cp.get('market')!=market or not cp.get('last_completed_date'):
            raise RuntimeError('CHECKPOINT_IDENTITY_MISMATCH:'+market)
        print(market+'_CHECKPOINT_READBACK='+str({k:cp.get(k) for k in
              ('last_completed_date','phase2_completed_date','phase2_completed_at_myt')}),flush=True)
    finally:
        os.environ.clear();os.environ.update(saved)


def main():
    os.umask(0o077)
    sha=sys.argv[1]
    if not re.fullmatch(r'[0-9a-f]{40}',sha) or subprocess.check_output(
            ['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()!=sha:
        raise RuntimeError('PINNED_CHECKOUT_REQUIRED')
    docs={}
    for market in PREVIOUS:
        job='hunter-'+market.lower()+'-daily'
        doc=gc('run','jobs','describe',job);c=container(doc,market)
        if c.get('image') not in {base.BASE_IMAGE+s for s in PREVIOUS[market] | {sha}}:
            raise RuntimeError('DAILY_IMAGE_CHANGED:'+market)
        if base.deploy.daily_runtime(doc)!=(7200,0):raise RuntimeError('DAILY_BUDGET_CHANGED:'+market)
        docs[market]=doc;checkpoint(doc,market)
    if any(container(d,m)['image']!=base.BASE_IMAGE+sha for m,d in docs.items()):base.build(sha)
    for market,before in docs.items():
        current=gc('run','jobs','describe','hunter-'+market.lower()+'-daily')
        if base.configuration(current)!=base.configuration(before):
            raise RuntimeError('DAILY_CHANGED_DURING_BUILD:'+market)
    for market,before in docs.items():
        job='hunter-'+market.lower()+'-daily'
        if (container(before,market)['image']!=base.BASE_IMAGE+sha or
                {e['name']:e.get('value') for e in container(before,market).get('env',[])}.get('HUNTER_INCREMENTAL_INPUTS')!='1'):
            gc('run','jobs','update',job,'--image='+base.BASE_IMAGE+sha,
               '--update-env-vars=HUNTER_SOURCE_SHA='+sha+',HUNTER_INCREMENTAL_INPUTS=1')
        after=gc('run','jobs','describe',job);verify(before,after,market,sha)
        print('INCREMENTAL_DEPLOY_VERIFIED='+job+';RUNTIME_AND_CAPACITY_UNCHANGED',flush=True)
    print('NO_EXECUTIONS_STARTED_OR_CANCELLED;EXISTING_RUNS_UNCHANGED;NEXT_RUN_USES_FIX',flush=True)


if __name__=='__main__':main()
