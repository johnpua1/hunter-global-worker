"""Update only existing monthly and maintenance images; preserve all budgets.

Never start or cancel jobs; active daily executions and schedules are unchanged."""
import copy
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('support_auto_deploy_base',ROOT/'cloudrun/resume-us-phase2-20261007.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
gc=base.recovery.gc
PREVIOUS='1c179c58560e6b936d2b92b0a6651692bb2e435f'
JOBS={'hunter-maintenance': (['python'], ['/app/maintenance.py']),
      'hunter-monthly-v2': ([], ['--mode','monthly'])}


def container(doc,job):
    rows=list(base.recovery.containers(doc))
    command,args=JOBS[job]
    if len(rows)!=1 or rows[0].get('args')!=args or rows[0].get('command',[])!=command:
        raise RuntimeError('SUPPORT_ENTRYPOINT_MISMATCH:'+job)
    return rows[0]


def verify(before,after,market,sha):
    expected=copy.deepcopy(container(before,market))
    expected['image']=base.BASE_IMAGE+sha
    env={v['name']:v for v in expected.get('env',[])}
    env['HUNTER_SOURCE_SHA']={'name':'HUNTER_SOURCE_SHA','value':sha}
    expected['env']=sorted(env.values(),key=lambda v:v['name'])
    actual=copy.deepcopy(container(after,market))
    actual['env']=sorted(actual.get('env',[]),key=lambda v:v['name'])
    if (expected!=actual or base.deploy.daily_runtime(before)!=base.deploy.daily_runtime(after)
            or base.deploy.runtime_capacity(before)!=base.deploy.runtime_capacity(after)):
        raise RuntimeError('SUPPORT_AUTO_READBACK_MISMATCH:'+market)


def checkpoint(doc,market):
    sys.path.insert(0,str(ROOT/'hunter-global'))
    from runner import Drive
    class Reader(Drive):
        def _call(self,op,**kwargs):
            if op not in {'file','list','read','read_chunk'}:raise RuntimeError('PREFLIGHT_READ_ONLY')
            return super()._call(op,**kwargs)
    saved=dict(os.environ)
    try:
        env={v['name']:v for v in container(doc,market).get('env',[])}
        for key in ('APPS_SCRIPT_WEBAPP_URL','APPS_SCRIPT_SHARED_KEY'):
            os.environ[key]=base.deploy.preflight_env_value(doc,env.get(key,{}),key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90',HUNTER_BRIDGE_ATTEMPTS='2')
        drive=Reader()
        try:
            for m in ('US','HK'):
                cp=drive.json(m+'/CONTROL/DAILY_CHECKPOINT.json')
                if cp.get('market')!=m or not cp.get('last_completed_date'):
                    raise RuntimeError('CHECKPOINT_IDENTITY_MISMATCH:'+m)
            print('SUPPORT_BRIDGE_READ_VERIFIED='+market,flush=True)
        finally:drive.http.close()
    finally:
        os.environ.clear();os.environ.update(saved)


def main():
    os.umask(0o077)
    sha=sys.argv[1]
    if not re.fullmatch(r'[0-9a-f]{40}',sha) or subprocess.check_output(
            ['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()!=sha:
        raise RuntimeError('PINNED_CHECKOUT_REQUIRED')
    docs={}
    for market in JOBS:
        job=market
        doc=gc('run','jobs','describe',job);c=container(doc,market)
        if c.get('image') not in {base.BASE_IMAGE+PREVIOUS,base.BASE_IMAGE+sha}:
            raise RuntimeError('SUPPORT_IMAGE_CHANGED:'+market)
        base.deploy.daily_runtime(doc)
        base.deploy.runtime_capacity(doc)
        docs[market]=doc;checkpoint(doc,market)
    if any(container(d,m)['image']!=base.BASE_IMAGE+sha for m,d in docs.items()):base.build(sha)
    for market,before in docs.items():
        current=gc('run','jobs','describe',market)
        if base.configuration(current)!=base.configuration(before):
            raise RuntimeError('SUPPORT_CHANGED_DURING_BUILD:'+market)
    for market,before in docs.items():
        job=market
        current=gc('run','jobs','describe',job)
        if base.configuration(current)!=base.configuration(before):
            raise RuntimeError('SUPPORT_CHANGED_BEFORE_UPDATE:'+job)
        if container(before,market)['image']!=base.BASE_IMAGE+sha:
            gc('run','jobs','update',job,'--image='+base.BASE_IMAGE+sha,
               '--update-env-vars=HUNTER_SOURCE_SHA='+sha)
        after=gc('run','jobs','describe',job);verify(before,after,market,sha)
        print('SUPPORT_AUTO_DEPLOY_VERIFIED='+job+';RUNTIME_AND_CAPACITY_UNCHANGED',flush=True)
    print('NO_EXECUTIONS_STARTED_OR_CANCELLED;EXISTING_RUNS_UNCHANGED;NEXT_RUN_USES_FIX',flush=True)


if __name__=='__main__':main()
