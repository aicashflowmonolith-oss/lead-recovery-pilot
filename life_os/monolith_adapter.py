"""Isolated adapter to MONOLITH's existing durable Governor; cash budget is always zero."""
import json
import os
from pathlib import Path
import sys
from .ai_cli import run_bounded, CapabilityUnavailable, AdapterError

BRIDGE = """
import json,sys
from pathlib import Path
from monolith.persistence.sqlite import Database
from monolith.workforce.governor import Governor
p=json.load(sys.stdin)
db=Database(Path(p['database']))
try:
    governor=Governor(database=db,repository=Path(p['artifacts']))
    tid=governor.receive_objective('LIFE OS governed '+p['operation'],operation=p['operation'],
        inputs=p['data'],idempotency_key=p['key'],cash_budget=0,resource_budget=16,max_retries=1)
    record=governor.run(tid)
    if record['task']['state']=='FAILED' and record['task']['metadata']['retry_count']<1:
        record=governor.retry(tid)
    print(json.dumps({'task_id':tid,'state':record['task']['state'],
        'output':record.get('output'),'verification':record.get('verification'),
        'output_hash':record.get('output_hash')}))
finally:
    db.close()
"""


def execute(operation,data,*,request_id,ordinal,home,pulse=None):
    from .request_fabric import validate_business
    validate_business(operation,data)
    # Select the actual mainline explicitly: machine-global monolith imports may target an older tree.
    repo=Path(os.environ.get('LIFE_OS_MONOLITH_ROOT',str(Path.home()/'MONOLITH_WORKTREES/mainline'))).resolve()
    source=repo/'src'
    if not (source/'monolith/workforce/governor.py').is_file():
        raise CapabilityUnavailable('MONOLITH mainline unavailable; configure LIFE_OS_MONOLITH_ROOT')
    target=Path(home)/'execution/monolith'
    target.mkdir(parents=True,exist_ok=True)
    artifacts=target/'artifacts'
    artifacts.mkdir(exist_ok=True)
    payload={'operation':operation.split('.',1)[1],'data':data,'key':f'life-os:{request_id}:{ordinal}',
             'database':str(target/'governor.db'),'artifacts':str(artifacts)}
    env=dict(os.environ)
    env['PYTHONPATH']=str(source)
    code,out,_=run_bounded([sys.executable,'-c',BRIDGE],stdin=json.dumps(payload),cwd=target,
                          env=env,timeout=60,pulse=pulse)
    if code:
        raise AdapterError('MONOLITH governed executor failed')
    receipt=json.loads(out)
    if receipt.get('state')!='SUCCEEDED' or (receipt.get('verification') or {}).get('passed') is not True:
        raise AdapterError('MONOLITH did not independently verify this operation')
    if not receipt.get('output_hash'):
        raise AdapterError('MONOLITH output provenance missing')
    return json.dumps(receipt['output'],indent=2),{
        'executor':'monolith.workforce.Governor','task_id':receipt['task_id'],
        'output_hash':receipt['output_hash'],'verification':receipt['verification'],
        'cash_budget':0,'repository':str(repo)}
