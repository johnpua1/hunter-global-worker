"""Resume HK October 8 ranking using the deployed acceleration worker.

October 7 ranking is already committed. Do not rebuild it, change images,
start US, delete data or turn a DAILY receipt into a Phase 2 completion flag.
"""
import ast
import types

WORKER = '705a8977a102c2a35f62f5e214962618ab9ada33'
TARGET = '2026-10-08'


def controller(root):
    path = root / 'cloudrun/phase2-restore-acceleration-20261008.sh'
    body = path.read_text().split("<<'PHASE2_PY'", 1)[1].split('\n', 1)[1].rsplit('\nPHASE2_PY', 1)[0]
    tree = ast.parse(body)
    if not isinstance(tree.body[-1], ast.Try):
        raise RuntimeError('CONTROLLER_ENTRYPOINT_CHANGED')
    tree.body.pop()
    m = types.ModuleType('hk_rank_resume_controller')
    exec(compile(tree, str(path), 'exec'), m.__dict__)
    m.WORKER_SHA = WORKER
    m.EXPECTED_SHA = {'HK': WORKER}
    m.GOAL = {'HK': TARGET}
    m.peer_active = lambda market: m.active(m.r.executions('US'))
    return m


def prepare(m):
    doc = m.r.gc('run', 'jobs', 'describe', 'hunter-hk-daily')
    m.r.verify(doc, doc, 'HK', WORKER)
    if m.pack_deployment.configuration(m.r, doc) != m.pack_deployment.configuration(
            m.r, m.pack_deployment.desired(doc, m.r, 'HK', WORKER)):
        raise RuntimeError('HK_ACCELERATION_TEMPLATE_REQUIRED')
    cp = m.r.checkpoint(doc, 'HK')
    if cp.get('market') != 'HK' or cp.get('last_completed_date') not in ('2026-10-07', TARGET):
        raise RuntimeError('HK_OCTOBER7_RANK_COMMIT_REQUIRED')
    # The foundation checkpoint advances after RANK and all detail writes.
    # Reuse that receipt without rereading completed October 7 output files.
    m.emit('HK_OCTOBER7_RANK_PRESERVED;ONLY_UNFINISHED_OCTOBER8_WORK')
    m.preflight_write_recovery()
    return doc


def run(m):
    doc = prepare(m)
    attempts = {}
    deadline = m.time.monotonic() + 7 * 3600
    m.emit('HK_ACCELERATION_ATTACHED=' + WORKER + ';NO_BUILD_NO_TEMPLATE_UPDATE')
    while m.time.monotonic() < deadline:
        date = m.step('HK', doc, attempts)
        if date:
            m.full_proof(doc, 'HK', date)
            cp = m.r.checkpoint(doc, 'HK')
            if m.choose(cp, 'HK') == ('COMPLETE', TARGET) and not m.active(m.r.executions('HK')):
                m.emit('HK_RANK_2026_10_07_PRESERVED_AND_2026_10_08_COMPLETE;HK_PHASE2_ACCEPTED_2026_10_08')
                return
        m.time.sleep(45)
    raise RuntimeError('HK_MONITOR_TIME_LIMIT_CLOUD_EXECUTION_PRESERVED')


def main(root):
    m = controller(root)
    try:
        run(m)
    except Exception as exc:
        m.emit('HK_NOT_ACCEPTED=' + m.error_reason(exc))
        raise SystemExit(1) from None
    finally:
        m.LOCK.close()
